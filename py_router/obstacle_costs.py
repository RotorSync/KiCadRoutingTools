"""
Proximity cost functions for PCB routing obstacle maps.

Handles track proximity costs, stub proximity costs, BGA proximity costs,
and cross-layer track alignment tracking.
"""
from __future__ import annotations

from typing import List, Tuple, Dict, Set
import numpy as np

from kicad_parser import PCBData
from routing_config import GridRouteConfig, GridCoord
from bresenham_utils import walk_line

# Module-level cache for pre-computed proximity offset tables
# Key: (radius_grid, cost_grid) -> List of (ex, ey, cost) tuples
_proximity_offset_cache: Dict[Tuple[int, int], List[Tuple[int, int, int]]] = {}


def _get_proximity_offsets(radius_grid: int, cost_grid: int) -> List[Tuple[int, int, int]]:
    """Get pre-computed proximity offsets and costs for a given radius.

    Returns a list of (ex, ey, cost) tuples for all grid cells within the radius.
    Uses caching to avoid recomputing the same table multiple times.
    """
    cache_key = (radius_grid, cost_grid)
    if cache_key in _proximity_offset_cache:
        return _proximity_offset_cache[cache_key]

    offsets = []
    radius_sq = radius_grid * radius_grid

    for ex in range(-radius_grid, radius_grid + 1):
        for ey in range(-radius_grid, radius_grid + 1):
            dist_sq = ex * ex + ey * ey
            if dist_sq <= radius_sq:
                dist = dist_sq ** 0.5
                # Use exact same formula as original to avoid floating-point differences
                proximity = 1.0 - (dist / radius_grid) if radius_grid > 0 else 1.0
                cost = int(proximity * cost_grid)
                offsets.append((ex, ey, cost))

    _proximity_offset_cache[cache_key] = offsets
    return offsets


# Import Rust router
import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'rust_router')))
import rust_alloc  # noqa: E402,F401  # issue #419: set MIMALLOC_PURGE_DELAY before grid_router loads

try:
    from grid_router import GridObstacleMap
except ImportError:
    GridObstacleMap = None

import env_knobs


def add_stub_proximity_costs(obstacles: GridObstacleMap, unrouted_stubs: List[Tuple[float, float]],
                              config: GridRouteConfig):
    """Add stub proximity costs to the obstacle map.

    Uses batch Rust method for performance.
    Also registers zone centers for direction-aware cost calculation.

    via_proximity_cost no longer participates here: 0 used to mean "hard-BLOCK
    vias within the stub radius" (the one knob where 0 was the STRONGEST
    setting, a documented ~200x CPU hazard); 0 now means "no extra via cost
    from proximity" like every other knob's 0-is-off, and the Rust-side ban
    machinery (block_vias + the B2 refcount ledger) is removed.
    """
    if not unrouted_stubs:
        return

    coord = GridCoord(config.grid_step)
    stub_radius_grid = coord.to_grid_dist(config.stub_proximity_radius)
    stub_cost_grid = config.cell_cost(config.stub_proximity_cost)

    # Convert stub positions to grid coordinates
    stub_grid_positions = []
    for stub in unrouted_stubs:
        stub_x, stub_y = stub[0], stub[1]
        gcx, gcy = coord.to_grid(stub_x, stub_y)
        stub_grid_positions.append((gcx, gcy))

    # Use batch Rust method for performance
    obstacles.add_stub_proximity_costs_batch(
        stub_grid_positions, stub_radius_grid, stub_cost_grid
    )


def proximity_max_zone_rects(config: GridRouteConfig, coord: GridCoord):
    """Inclusive grid rectangles of the BGA ESCAPE FIELDS (zone expanded by
    bga_proximity_radius) for 'zoned' sum composition: inside them sources
    compose by MAX (the stacked foreign-stub fields there price a net's
    MANDATORY approach, not an avoidable crowd), outside by SUM. Independent
    of the bga_proximity_cost knob -- this is a composition boundary, not the
    BGA cost. Returns None unless zoned mode is active and zones exist."""
    if env_knobs.PROXIMITY_SUM_MODE != 'zoned' or not config.bga_exclusion_zones:
        return None
    r = coord.to_grid_dist(config.bga_proximity_radius)
    rects = []
    for zone in config.bga_exclusion_zones:
        min_x, min_y, max_x, max_y = zone[:4]
        gx0, gy0 = coord.to_grid(min_x, min_y)
        gx1, gy1 = coord.to_grid(max_x, max_y)
        rects.append((gx0 - r, gy0 - r, gx1 + r, gy1 + r))
    return rects


def apply_stub_proximity(obstacles: GridObstacleMap, pcb_data: PCBData,
                         stub_net_ids: List[int],
                         all_stubs: List[Tuple[float, float]],
                         config: GridRouteConfig,
                         ghost_via_groups: Dict[int, List[Tuple[int, int]]] = None):
    """Single entry point for the STUB proximity map (foreign stubs/chip pads
    + ripped-route via ghosts), composing per the active mode.

    Max mode (default): byte-identical to the historical calls -- the flat
    ``all_stubs`` batch (per-cell max), then the ghost vias as a second flat
    batch at the ripped-route falloff (formerly merge_ripped_route_costs).

    Sum mode (KICAD_PROXIMITY_SUM): each SOURCE -- one net's stubs+chip pads,
    or one ripped net's ghost vias -- is deduped internally by max (a net's
    six connector stubs are one source), then sources ADD per cell. One
    grouped Rust call per stamp sequence; the caller's clear (or fresh clone)
    brackets it exactly like max mode, and stamping the same sequence after a
    clear reproduces identical values (no cross-prepare accumulation).

    ghost_via_groups must already be C1-filtered (re-routed nets dropped) --
    both modes assume each source appears at most once.
    """
    coord = GridCoord(config.grid_step)
    ghost_active = (ghost_via_groups and
                    config.ripped_route_avoidance_cost > 0)

    if not env_knobs.PROXIMITY_SUM:
        if all_stubs:
            add_stub_proximity_costs(obstacles, all_stubs, config)
        if ghost_active:
            all_via_positions = []
            for via_list in ghost_via_groups.values():
                all_via_positions.extend(via_list)
            if all_via_positions:
                obstacles.add_stub_proximity_costs_batch(
                    all_via_positions,
                    coord.to_grid_dist(config.ripped_route_avoidance_radius),
                    config.cell_cost(config.ripped_route_avoidance_cost))
        return

    # Sum mode: (points, radius, cost) per source
    from connectivity import get_stub_endpoints
    from net_queries import get_chip_pad_positions
    groups = []
    stub_radius_grid = coord.to_grid_dist(config.stub_proximity_radius)
    stub_cost_grid = config.cell_cost(config.stub_proximity_cost)
    if stub_cost_grid > 0 and stub_radius_grid > 0:
        for nid in stub_net_ids:
            pts = (get_stub_endpoints(pcb_data, [nid])
                   + get_chip_pad_positions(pcb_data, [nid]))
            if pts:
                groups.append(([coord.to_grid(p[0], p[1]) for p in pts],
                               stub_radius_grid, stub_cost_grid))
    if ghost_active:
        rra_radius = coord.to_grid_dist(config.ripped_route_avoidance_radius)
        rra_cost = config.cell_cost(config.ripped_route_avoidance_cost)
        for nid in sorted(ghost_via_groups):
            vias = ghost_via_groups[nid]
            if vias:
                groups.append(([tuple(v) for v in vias], rra_radius, rra_cost))
    if groups:
        obstacles.add_stub_proximity_costs_grouped(
            groups, proximity_max_zone_rects(config, coord))


# (add_bga_proximity_costs, the old base-map stub_proximity stamp, is deleted:
# it was dead in production after the B1 move to the cache below -- the stamp
# was wiped by prepare_obstacles_inplace's clear_stub_proximity before every
# single-ended net. compute_bga_proximity_cost_cells is the live path.)

# Reserved track_proximity_cache key for the BGA proximity cost cells
# (soft-knobs review B1): real net ids are positive, so -1 can never collide
# or be evicted by the per-net cache lifecycle.
BGA_PROXIMITY_CACHE_KEY = -1


def compute_bga_proximity_cost_cells(config: GridRouteConfig,
                                     num_layers: int) -> np.ndarray:
    """BGA proximity costs as an (N, 4) [layer, gx, gy, cost] array.

    Linear falloff from full cost at the zone edge to zero at
    bga_proximity_radius; zone-INTERIOR cells clamp to dist=0 = full
    edge-tier cost (so allowed-cells windows punched through the hard block
    are not free-via holes). Emitted for the LAYER proximity map via the
    track-proximity cache under
    BGA_PROXIMITY_CACHE_KEY (soft-knobs review B1): the old base-map
    stub_proximity stamp was wiped by prepare_obstacles_inplace's
    clear_stub_proximity() before every single-ended net, silently no-op'ing
    the knob in the most common path (and double-counting was the risk for
    diff pairs, whose obstacle build merges the cache on top of the base).
    Registered once; merge_track_proximity_costs re-applies it on every
    prepare in every path. Rows are replicated per layer because the old
    stub_proximity stamp was all-layer.
    """
    if config.bga_proximity_radius <= 0 or config.bga_proximity_cost <= 0 \
            or not config.bga_exclusion_zones or num_layers <= 0:
        return np.empty((0, 4), dtype=np.int32)

    coord = GridCoord(config.grid_step)
    radius_grid = coord.to_grid_dist(config.bga_proximity_radius)
    cost_grid = config.cell_cost(config.bga_proximity_cost)
    cells: Dict[Tuple[int, int], int] = {}

    for zone in config.bga_exclusion_zones:
        min_x, min_y, max_x, max_y = zone[:4]
        gmin_x, gmin_y = coord.to_grid(min_x, min_y)
        gmax_x, gmax_y = coord.to_grid(max_x, max_y)
        for gx in range(gmin_x - radius_grid, gmax_x + radius_grid + 1):
            for gy in range(gmin_y - radius_grid, gmax_y + radius_grid + 1):
                cx = max(gmin_x, min(gx, gmax_x))
                cy = max(gmin_y, min(gy, gmax_y))
                dist = ((gx - cx) ** 2 + (gy - cy) ** 2) ** 0.5
                if dist <= radius_grid:
                    proximity = 1.0 - (dist / radius_grid) if radius_grid > 0 else 1.0
                    cost = int(proximity * cost_grid)
                    key = (gx, gy)
                    if cost > cells.get(key, 0):
                        cells[key] = cost

    if not cells:
        return np.empty((0, 4), dtype=np.int32)
    base = np.array([[gx, gy, c] for (gx, gy), c in cells.items()], dtype=np.int32)
    rows = []
    for li in range(num_layers):
        layer_col = np.full((base.shape[0], 1), li, dtype=np.int32)
        rows.append(np.hstack([layer_col, base]))
    return np.vstack(rows)


def compute_track_proximity_for_net(pcb_data: PCBData, net_id: int, config: GridRouteConfig,
                                     layer_map: Dict[str, int]) -> np.ndarray:
    """Compute track proximity costs for a single net's segments.

    Returns a numpy array with columns [layer, gx, gy, cost] for efficient storage and batch merge.
    This allows incremental updates: compute once when route succeeds, remove when ripped up.

    Args:
        pcb_data: PCB data containing routed segments
        net_id: Net ID to compute proximity for
        config: Routing configuration with track_proximity_distance and track_proximity_cost
        layer_map: Mapping of layer names to layer indices

    Returns:
        numpy array of shape (N, 4) with columns [layer, gx, gy, cost], dtype int32
    """
    # Use dict internally for efficient max tracking, convert to numpy at end
    result: Dict[Tuple[int, int, int], int] = {}  # (layer, gx, gy) -> cost

    if config.track_proximity_distance <= 0 or config.track_proximity_cost <= 0:
        return np.empty((0, 4), dtype=np.int32)  # Feature disabled

    coord = GridCoord(config.grid_step)
    radius_grid = coord.to_grid_dist(config.track_proximity_distance)
    cost_grid = config.cell_cost(config.track_proximity_cost)

    # Get pre-computed offset table (cached)
    offsets = _get_proximity_offsets(radius_grid, cost_grid)

    # Sample every ~1mm along segments (not every grid step) for performance
    sample_interval = max(1, int(1.0 / config.grid_step))

    for seg in pcb_data.segments:
        if seg.net_id != net_id:
            continue

        layer_idx = layer_map.get(seg.layer)
        if layer_idx is None:
            continue

        # Walk along segment using Bresenham, sampling every sample_interval points
        gx1, gy1 = coord.to_grid(seg.start_x, seg.start_y)
        gx2, gy2 = coord.to_grid(seg.end_x, seg.end_y)

        dx = abs(gx2 - gx1)
        dy = abs(gy2 - gy1)
        sx = 1 if gx1 < gx2 else -1
        sy = 1 if gy1 < gy2 else -1
        err = dx - dy

        gx, gy = gx1, gy1
        step_count = 0

        while True:
            # Only process every sample_interval'th point
            if step_count % sample_interval == 0:
                # Add proximity costs using pre-computed offset table
                for ex, ey, cost in offsets:
                    key = (layer_idx, gx + ex, gy + ey)
                    # Store max cost at each cell
                    if key not in result or cost > result[key]:
                        result[key] = cost

            if gx == gx2 and gy == gy2:
                break

            e2 = 2 * err
            if e2 > -dy:
                err -= dy
                gx += sx
            if e2 < dx:
                err += dx
                gy += sy
            step_count += 1

    # Convert to numpy array
    if not result:
        return np.empty((0, 4), dtype=np.int32)

    arr = np.array([[layer, gx, gy, cost] for (layer, gx, gy), cost in result.items()], dtype=np.int32)
    return arr


_MERGE_MEMO: dict = {}
# Cap: entries pin their keyed objects alive (keepalive against id() reuse), so
# the memo must not grow unbounded in a long-lived GUI session. Cleared wholesale
# on overflow -- this is a within-run speedup, not a correctness cache.
_MERGE_MEMO_MAX = 32


def merge_track_proximity_costs(obstacles: GridObstacleMap,
                                 per_net_costs: Dict[int, np.ndarray],
                                 ghost_costs: Dict[int, np.ndarray] = None,
                                 config: GridRouteConfig = None):
    """Merge pre-computed per-net track proximity costs into the obstacle map.

    Args:
        obstacles: The obstacle map to add costs to
        per_net_costs: Dict of net_id -> numpy array with columns [layer, gx, gy, cost]
        ghost_costs: optional ripped-corridor layer ghosts (C1-filtered by the
            caller), composed in the SAME pass. In max mode this is identical
            to the historical separate merge call (per-cell max commutes); the
            single pass exists for SUM mode, where "each source counted
            exactly once" is only enforceable with one composition point.

    Composition (soft-knobs sum experiment): per-cell MAX across sources by
    default; with KICAD_PROXIMITY_SUM, per-cell SUM across sources. Every
    array is one source (a routed net's corridor, a ripped net's ghost, the
    BGA/-1 field, congestion, fragility) and is already deduped internally
    (unique cells per array), so summing rows = summing sources -- a cell
    never inherits a source twice, and sampling density never inflates cost.
    The composed rows stay unique per cell, so the Rust max-insert write is a
    plain write on the cleared map every prepare starts from -- an accidental
    re-merge of the same composition re-writes identical values instead of
    doubling them (double-merge stays harmless in both modes).

    Double-merge guard: a net id appearing in BOTH dicts would count one
    net's copper twice in sum mode -- overlaps are dropped from the ghosts
    (the cache entry means the net has live copper again) with a warning.
    """
    if ghost_costs:
        overlap = per_net_costs.keys() & ghost_costs.keys()
        if overlap:
            print(f"    WARNING: ghost/cache key overlap dropped "
                  f"(double-merge guard): {sorted(overlap)}")
            ghost_costs = {k: v for k, v in ghost_costs.items()
                           if k not in overlap}
    # Concatenate all arrays for efficient batch processing
    arrays_to_merge = [arr for arr in per_net_costs.values() if len(arr) > 0]
    if ghost_costs:
        arrays_to_merge += [arr for arr in ghost_costs.values() if len(arr) > 0]
    if not arrays_to_merge:
        return

    # P5 (soft-knobs review): phase-3 and diff-pair paths re-prepare many
    # times between cache changes; memoize the concatenation keyed on the
    # cache's identity signature so unchanged caches skip the O(all-nets)
    # vstack. Exact-behavior: any entry add/replace changes the signature.
    # KEEPALIVE + BOUNDED. This memo is keyed on id(per_net_costs) and its
    # signature also mixes id(a) of the member arrays. Bare id() keys are unsafe
    # in a long-lived process (the GUI): once the keyed dict is garbage
    # collected, a NEW dict can be allocated at the SAME address, and if the
    # id-derived signature also collides, this returns ANOTHER run's merged
    # costs -- silently wrong track-proximity costs, i.e. different routing with
    # identical inputs. The CLI never sees it (fresh process per step).
    #
    # Two guards, both required:
    #   * store the keyed object (and the arrays) IN the entry, so nothing can
    #     be freed while its id is a live key -- ids can no longer be recycled
    #     out from under us;
    #   * bound the memo, so holding those references cannot grow without limit
    #     across a long GUI session.
    # Measured note: this was NOT the eth_tap step-11 divergence (disabling the
    # memo entirely left it unchanged), but the hazard is real and latent.
    _zone_rects = None
    if env_knobs.PROXIMITY_SUM_MODE == 'zoned' and config is not None:
        _zone_rects = proximity_max_zone_rects(config, GridCoord(config.grid_step))
    key = id(per_net_costs)
    sig = (len(arrays_to_merge),
           sum(len(a) for a in arrays_to_merge),
           sum(id(a) & 0xFFFFFFFF for a in arrays_to_merge),
           env_knobs.PROXIMITY_SUM_MODE,
           tuple(_zone_rects) if _zone_rects else None)
    memo = _MERGE_MEMO.get(key)
    if memo is not None and memo[0] == sig:
        all_costs = memo[1]
    else:
        all_costs = np.vstack(arrays_to_merge)
        if env_knobs.PROXIMITY_SUM:
            # Compose the SUM in numpy so the Rust write primitive stays the
            # idempotent max-insert: pack (layer, gx, gy) into one int64 key,
            # group, and sum each cell's per-source costs. Output rows are
            # unique per cell. In 'zoned' mode, cells inside a BGA escape
            # field take the per-cell MAX instead (mandatory approaches are
            # not an avoidable crowd).
            _OFF = 1 << 23  # grid coords are well inside +/-2^23
            packed = ((all_costs[:, 0].astype(np.int64) << 48)
                      | ((all_costs[:, 1].astype(np.int64) + _OFF) << 24)
                      | (all_costs[:, 2].astype(np.int64) + _OFF))
            uniq, inv = np.unique(packed, return_inverse=True)
            sums = np.bincount(inv, weights=all_costs[:, 3].astype(np.float64),
                               minlength=len(uniq))
            composed = np.empty((len(uniq), 4), dtype=np.int32)
            composed[:, 0] = (uniq >> 48).astype(np.int32)
            composed[:, 1] = ((uniq >> 24) & ((1 << 24) - 1)).astype(np.int32) - _OFF
            composed[:, 2] = (uniq & ((1 << 24) - 1)).astype(np.int32) - _OFF
            composed[:, 3] = np.minimum(sums, float(np.iinfo(np.int32).max)).astype(np.int32)
            if _zone_rects:
                maxs = np.zeros(len(uniq), dtype=np.int32)
                np.maximum.at(maxs, inv, all_costs[:, 3])
                in_zone = np.zeros(len(uniq), dtype=bool)
                for (x0, y0, x1, y1) in _zone_rects:
                    in_zone |= ((composed[:, 1] >= x0) & (composed[:, 1] <= x1)
                                & (composed[:, 2] >= y0) & (composed[:, 2] <= y1))
                composed[in_zone, 3] = maxs[in_zone]
            all_costs = composed
        # Bound first, then store WITH keepalive refs (see the note above):
        # per_net_costs and the member arrays are held so their ids cannot be
        # recycled while this entry is live.
        if len(_MERGE_MEMO) >= _MERGE_MEMO_MAX:
            _MERGE_MEMO.clear()
        _MERGE_MEMO[key] = (sig, all_costs, per_net_costs, arrays_to_merge)
    obstacles.set_layer_proximity_batch(all_costs)


def _maybe_build_attraction_field(obstacles, config):
    """P3 (Rust 0.18.5): precompute the per-layer attraction field so the
    hot path is an O(1) lookup. hasattr-guarded so an older .so (no method)
    keeps the exact scan fallback."""
    if getattr(config, 'vertical_attraction_cost', 0) and \
            hasattr(obstacles, 'build_attraction_field'):
        from routing_config import GridCoord
        coord = GridCoord(config.grid_step)
        radius = coord.to_grid_dist(config.vertical_attraction_radius)
        bonus = config.scaled_cell_units(config.vertical_attraction_cost)
        obstacles.build_attraction_field(radius, bonus)


def add_cross_layer_tracks(obstacles: GridObstacleMap, pcb_data: PCBData,
                            config: GridRouteConfig, layer_map: Dict[str, int],
                            exclude_net_ids: Set[int] = None):
    """Populate cross-layer track positions for vertical alignment attraction.

    NET-AGNOSTIC by design (soft-knobs C4): the per-cell layer bitmask carries
    no net id, so vertical attraction pulls the route toward ANY other net's
    tracks on other layers (corridor stacking with strangers included). It
    cannot express "stack with my own group" -- that needs a per-group mask.

    Adds positions of all routed tracks (excluding specified nets) to the
    obstacle map's cross-layer lookup structure. This enables the router
    to give a cost bonus for routing on top of existing tracks on other layers.

    Args:
        obstacles: The obstacle map to populate
        pcb_data: PCB data containing segments
        config: Routing configuration with vertical_attraction_radius
        layer_map: Mapping of layer names to layer indices
        exclude_net_ids: Set of net IDs to exclude (typically the net being routed)
    """
    if config.vertical_attraction_radius <= 0 or config.vertical_attraction_cost <= 0:
        # Feature disabled. The cost gate matters (soft-knobs review P1): the
        # default radius is 1.0 with cost 0.0, so gating on radius alone
        # walked every board segment with one FFI call per 0.5mm sample, on
        # every prepare, to build a map the Rust lookup never reads
        # (early-out on bonus <= 0).
        return

    coord = GridCoord(config.grid_step)
    exclude_set = exclude_net_ids or set()

    # Sample every ~0.5mm along segments for reasonable density
    sample_interval = max(1, int(0.5 / config.grid_step))

    for seg in pcb_data.segments:
        if seg.net_id in exclude_set:
            continue

        layer_idx = layer_map.get(seg.layer)
        if layer_idx is None:
            continue

        _add_segment_cross_layer_track(obstacles, seg, coord, layer_idx, sample_interval)


def _add_segment_cross_layer_track(obstacles: GridObstacleMap, seg, coord: GridCoord,
                                    layer_idx: int, sample_interval: int):
    """Add a single segment's positions to the cross-layer track data."""
    gx1, gy1 = coord.to_grid(seg.start_x, seg.start_y)
    gx2, gy2 = coord.to_grid(seg.end_x, seg.end_y)

    for step_count, (gx, gy) in enumerate(walk_line(gx1, gy1, gx2, gy2)):
        if step_count % sample_interval == 0:
            obstacles.add_cross_layer_track(gx, gy, layer_idx)


def compute_ripped_route_costs(saved_result: dict, config: GridRouteConfig,
                                layer_map: Dict[str, int]) -> Tuple[np.ndarray, List[Tuple[int, int]]]:
    """Compute avoidance costs for a ripped route's former segment/via locations.

    When a net is ripped up to route another net, we want to apply soft penalties
    to the ripped net's former corridor. This increases the chance the ripped net
    can be successfully rerouted later by keeping its path available.

    Args:
        saved_result: The saved routing result from the ripped net (contains new_segments, new_vias)
        config: Routing configuration with ripped_route_avoidance_radius and _cost
        layer_map: Mapping of layer names to layer indices

    Returns:
        (layer_costs, via_positions) where:
        - layer_costs: numpy array of [layer, gx, gy, cost] for segment proximity (layer-specific)
        - via_positions: list of (gx, gy) grid coordinates for vias
    """
    # Return empty results if feature is disabled
    if config.ripped_route_avoidance_cost <= 0 or config.ripped_route_avoidance_radius <= 0:
        return np.empty((0, 4), dtype=np.int32), []

    coord = GridCoord(config.grid_step)
    radius_grid = coord.to_grid_dist(config.ripped_route_avoidance_radius)
    cost_grid = config.cell_cost(config.ripped_route_avoidance_cost)

    # Get pre-computed offset table (cached)
    offsets = _get_proximity_offsets(radius_grid, cost_grid)

    # Use dict internally for efficient max tracking
    result: Dict[Tuple[int, int, int], int] = {}  # (layer, gx, gy) -> cost

    # Sample every ~1mm along segments (not every grid step) for performance
    sample_interval = max(1, int(1.0 / config.grid_step))

    # Process segments (layer-specific costs)
    new_segments = saved_result.get('new_segments', [])
    for seg in new_segments:
        layer_idx = layer_map.get(seg.layer)
        if layer_idx is None:
            continue

        # Walk along segment using walk_line, sampling every sample_interval points
        gx1, gy1 = coord.to_grid(seg.start_x, seg.start_y)
        gx2, gy2 = coord.to_grid(seg.end_x, seg.end_y)

        for step_count, (gx, gy) in enumerate(walk_line(gx1, gy1, gx2, gy2)):
            # Only process every sample_interval'th point
            if step_count % sample_interval == 0:
                # Add proximity costs using pre-computed offset table
                for ex, ey, cost in offsets:
                    key = (layer_idx, gx + ex, gy + ey)
                    # Store max cost at each cell
                    if key not in result or cost > result[key]:
                        result[key] = cost

    # Convert layer costs to numpy array
    if result:
        layer_costs = np.array([[layer, gx, gy, cost] for (layer, gx, gy), cost in result.items()], dtype=np.int32)
    else:
        layer_costs = np.empty((0, 4), dtype=np.int32)

    # Extract via positions (grid coords)
    via_positions = []
    new_vias = saved_result.get('new_vias', [])
    for via in new_vias:
        gx, gy = coord.to_grid(via.x, via.y)
        via_positions.append((gx, gy))

    return layer_costs, via_positions
