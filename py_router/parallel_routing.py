"""Batch parallel routing orchestration for single-ended nets.

Implements batch-synchronous deterministic parallelism: pick batches of nets
whose windowed bounding boxes are pairwise disjoint (fall back to singleton
batches when nothing is disjoint), route each batch in parallel via
grid_router.route_batch against ONE immutable obstacle snapshot, then commit
results SEQUENTIALLY in net-id order re-checking each against already-committed
copper with the exact clearance kernels; any conflict loser is re-queued for
the next batch against an updated snapshot.

The core A* logic lives in Rust (route_batch); this module only orchestrates.
Per-net results are deterministic and independent of thread count/scheduling.
"""

from __future__ import annotations

import os
from typing import Dict, List, Optional, Tuple

import env_knobs
from kicad_parser import PCBData, Segment, Via
from routing_config import GridRouteConfig, GridCoord
from connectivity import get_net_endpoints
from geometry_utils import simplify_path
from obstacle_map import get_same_net_through_hole_positions
from single_ended_routing import (
    _augment_blocked_pad_terminals,
    _pour_launch_pair_anchors,
    _emit_via_size,
    _apply_neckdown_widths,
    _neck_terminal_grazes,
)

try:
    from grid_router import GridObstacleMap, RouteRequest, route_batch
except Exception:  # pragma: no cover - grid_router may not be importable in some envs
    GridObstacleMap = None
    RouteRequest = None
    route_batch = None


def _parallel_enabled() -> bool:
    """Env gate: KICAD_PARALLEL_BATCH=0 disables; default ON."""
    return os.environ.get('KICAD_PARALLEL_BATCH', '1') != '0'


def net_windowed_bbox(net_id: int, pcb_data: PCBData,
                      config: GridRouteConfig) -> Optional[Tuple[int, int, int, int]]:
    """Windowed bounding box (grid cells) of a net's endpoints, padded by a
    clearance window so spatially-adjacent nets are not batched together.
    Returns None if the net has no endpoints (can't be routed)."""
    sources, targets, error = get_net_endpoints(pcb_data, net_id, config)
    if error or not sources or not targets:
        return None
    coord = GridCoord(config.grid_step)
    # Window: clearance + track width + a safety margin in grid cells.
    pad = coord.to_grid_dist(config.track_width + config.clearance) + 4
    all_pts = [(s[0], s[1]) for s in sources] + [(t[0], t[1]) for t in targets]
    gx0 = min(p[0] for p in all_pts) - pad
    gy0 = min(p[1] for p in all_pts) - pad
    gx1 = max(p[0] for p in all_pts) + pad
    gy1 = max(p[1] for p in all_pts) + pad
    return (gx0, gy0, gx1, gy1)


def _bboxes_disjoint(a: Tuple[int, int, int, int],
                     b: Tuple[int, int, int, int]) -> bool:
    """True if two windowed bounding boxes do not overlap."""
    return (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])


def pick_disjoint_batches(net_ids: List[int],
                          bboxes: Dict[int, Tuple[int, int, int, int]]) -> List[List[int]]:
    """Greedily partition nets into batches of pairwise-disjoint windowed
    bounding boxes. Nets without a bbox (None) form singleton batches.
    Deterministic: processes nets in input order."""
    batches: List[List[int]] = []
    remaining = list(net_ids)
    while remaining:
        batch: List[int] = []
        batch_boxes: List[Tuple[int, int, int, int]] = []
        still: List[int] = []
        for nid in remaining:
            bb = bboxes.get(nid)
            if bb is None:
                # No bbox -> singleton batch (can't prove disjointness).
                still.append(nid)
                continue
            if all(_bboxes_disjoint(bb, other) for other in batch_boxes):
                batch.append(nid)
                batch_boxes.append(bb)
            else:
                still.append(nid)
        if not batch:
            # Nothing disjoint -> fall back to a singleton batch.
            batch = [remaining[0]]
            still = remaining[1:]
        batches.append(batch)
        remaining = still
    return batches


def prepare_request(pcb_data: PCBData, net_id: int, config: GridRouteConfig,
                    obstacles: GridObstacleMap,
                    attraction_path: Optional[List[Tuple[int, int, int]]] = None,
                    reverse_direction: bool = False,
                    bounds: Optional[Tuple[int, int, int, int]] = None,
                    sources_override: Optional[List[Tuple]] = None,
                    targets_override: Optional[List[Tuple]] = None):
    """Prepare a RouteRequest for one net against a shared obstacle snapshot.
    Mirrors route_net_with_obstacles' endpoint derivation + router config.
    Returns (request_kwargs_dict, meta) or None if unroutable."""
    if sources_override is not None and targets_override is not None:
        sources, targets, error = list(sources_override), list(targets_override), None
    else:
        sources, targets, error = get_net_endpoints(pcb_data, net_id, config)
    if error or not sources or not targets:
        return None

    if reverse_direction:
        sources, targets = targets, sources

    sources = _augment_blocked_pad_terminals(sources, pcb_data, net_id, config, obstacles)
    targets = _augment_blocked_pad_terminals(targets, pcb_data, net_id, config, obstacles)

    if bounds is not None:
        bgx0, bgy0, bgx1, bgy1 = bounds
        sources = [s for s in sources if bgx0 <= s[0] <= bgx1 and bgy0 <= s[1] <= bgy1]
        targets = [t for t in targets if bgx0 <= t[0] <= bgx1 and bgy0 <= t[1] <= bgy1]
        if not sources or not targets:
            return None

    coord = GridCoord(config.grid_step)

    if sources_override is None:
        _pl_src, _pl_tgt = _pour_launch_pair_anchors(
            pcb_data, net_id, sources, targets, config.layers, coord, config, bounds)
        if _pl_src or _pl_tgt:
            sources = sources + _pl_src
            targets = targets + _pl_tgt

    _exempt_r = coord.to_grid_dist(config.track_width + config.clearance)
    obstacles.set_endpoint_exempt(
        [(s0[0], s0[1]) for s0 in sources] + [(t0[0], t0[1]) for t0 in targets],
        _exempt_r)
    layer_names = config.layers

    sources_grid = [(s[0], s[1], s[2]) for s in sources]
    targets_grid = [(t[0], t[1], t[2]) for t in targets]

    free_end_sources, free_end_targets, _ = get_net_endpoints(pcb_data, net_id, config, use_stub_free_ends=True)
    prox_check_sources = [(s[0], s[1], s[2]) for s in free_end_sources] if free_end_sources else sources_grid
    prox_check_targets = [(t[0], t[1], t[2]) for t in free_end_targets] if free_end_targets else targets_grid

    def _exempt_ok(gx, gy):
        return bounds is None or (bounds[0] <= gx <= bounds[2] and bounds[1] <= gy <= bounds[3])
    allow_radius = 10
    for gx, gy, _ in sources_grid + targets_grid:
        for dx in range(-allow_radius, allow_radius + 1):
            for dy in range(-allow_radius, allow_radius + 1):
                if _exempt_ok(gx + dx, gy + dy):
                    obstacles.add_allowed_cell(gx + dx, gy + dy)
    for gx, gy, layer in sources_grid + targets_grid:
        if _exempt_ok(gx, gy):
            obstacles.add_source_target_cell(gx, gy, layer)

    attraction_radius_grid = coord.to_grid_dist(config.vertical_attraction_radius) if config.vertical_attraction_radius > 0 else 0
    attraction_bonus = config.cell_cost(config.vertical_attraction_cost) if config.vertical_attraction_cost > 0 else 0

    src_in_stub = any(obstacles.get_stub_proximity_cost(gx, gy) > 0 for gx, gy, _ in prox_check_sources)
    src_in_bga = any(obstacles.is_in_bga_proximity(gx, gy) for gx, gy, _ in prox_check_sources)
    tgt_in_stub = any(obstacles.get_stub_proximity_cost(gx, gy) > 0 for gx, gy, _ in prox_check_targets)
    tgt_in_bga = any(obstacles.is_in_bga_proximity(gx, gy) for gx, gy, _ in prox_check_targets)
    prox_h_cost = config.get_proximity_heuristic_for_zones(src_in_stub, src_in_bga, tgt_in_stub, tgt_in_bga)

    bus_attraction_radius_grid = coord.to_grid_dist(config.bus_attraction_radius) if config.bus_attraction_radius > 0 else 0
    bus_attraction_bonus = config.scaled_cell_units(config.bus_attraction_bonus) if config.bus_attraction_bonus > 0 else 0
    bus_xlayer_pct = 0
    if bus_attraction_bonus > 0 and (getattr(config, 'bus_enabled', False) or env_knobs.GLOBAL_PLAN.get('attract')):
        try:
            bus_xlayer_pct = env_knobs.BUS_XLAYER_PCT
        except ValueError:
            bus_xlayer_pct = 35

    track_margin = config.track_margins_for_net(net_id)
    start_backwards = config.direction_order in ("backwards", "backward")
    if start_backwards:
        forward_sources, forward_targets = targets_grid, sources_grid
        direction_labels = ("backward", "forward")
    else:
        forward_sources, forward_targets = sources_grid, targets_grid
        direction_labels = ("forward", "backward")
    use_single_direction = reverse_direction

    request_kwargs = dict(
        sources=forward_sources,
        targets=forward_targets,
        max_iterations=config.max_probe_iterations,
        collinear_vias=env_knobs.COLLINEAR_VIAS,
        via_exclusion_radius=0,
        start_direction=None,
        end_direction=None,
        direction_steps=2,
        track_margin=track_margin,
        max_iterations_ceiling=0,
        quantum_cells=2.0,
        quantum_pct=2.0,
        grace_tranches=0,
        via_rung=0,
        via_cost=config.via_cost_units(),
        h_weight=config.heuristic_weight,
        turn_cost=config.turn_cost,
        via_proximity_cost=config.via_proximity_cost_int(),
        vertical_attraction_radius=attraction_radius_grid,
        vertical_attraction_bonus=attraction_bonus,
        layer_costs=config.get_layer_costs(),
        proximity_heuristic_cost=prox_h_cost,
        layer_direction_preferences=config.get_layer_direction_preferences(),
        direction_preference_cost=config.direction_preference_cost,
        attraction_radius=bus_attraction_radius_grid,
        attraction_bonus=bus_attraction_bonus,
        attraction_cross_layer_pct=bus_xlayer_pct,
        attraction_potential=env_knobs.GLOBAL_PLAN.get('attract_potential', 0),
        attraction_path=list(attraction_path) if attraction_path else [],
    )
    meta = dict(
        sources=sources,
        targets=targets,
        forward_sources=forward_sources,
        forward_targets=forward_targets,
        start_backwards=start_backwards,
        direction_labels=direction_labels,
        use_single_direction=use_single_direction,
        track_margin=track_margin,
        layer_names=layer_names,
        coord=coord,
        net_id=net_id,
    )
    return request_kwargs, meta


def build_result_from_path(pcb_data: PCBData, net_id: int, config: GridRouteConfig,
                           obstacles: GridObstacleMap, meta: dict,
                           path: List[Tuple[int, int, int]],
                           total_iterations: int) -> dict:
    """Build a route result dict from a found path (mirrors route_net_with_obstacles'
    result-building tail). Returns a dict with new_segments/new_vias/iterations/path."""
    coord = meta['coord']
    layer_names = meta['layer_names']
    sources = meta['sources']
    targets = meta['targets']
    start_backwards = meta['start_backwards']
    reversed_path = False

    if start_backwards and path is not None:
        reversed_path = not reversed_path

    if reversed_path:
        sources, targets = targets, sources

    path_start = path[0]
    path_end = path[-1]

    start_original = None
    for s in sources:
        if s[0] == path_start[0] and s[1] == path_start[1] and s[2] == path_start[2]:
            start_original = (s[3], s[4], layer_names[s[2]])
            break
    end_original = None
    for t in targets:
        if t[0] == path_end[0] and t[1] == path_end[1] and t[2] == path_end[2]:
            end_original = (t[3], t[4], layer_names[t[2]])
            break

    through_hole_positions = get_same_net_through_hole_positions(pcb_data, net_id, config)
    path = simplify_path(path)

    new_segments = []
    new_vias = []

    if start_original:
        first_grid_x, first_grid_y = coord.to_float(path_start[0], path_start[1])
        orig_x, orig_y, orig_layer = start_original
        if abs(orig_x - first_grid_x) > 0.001 or abs(orig_y - first_grid_y) > 0.001:
            seg = Segment(
                start_x=orig_x, start_y=orig_y,
                end_x=first_grid_x, end_y=first_grid_y,
                width=config.get_net_track_width(net_id, orig_layer),
                layer=orig_layer,
                net_id=net_id)
            new_segments.append(seg)

    for i in range(len(path) - 1):
        gx1, gy1, layer1 = path[i]
        gx2, gy2, layer2 = path[i + 1]
        x1, y1 = coord.to_float(gx1, gy1)
        x2, y2 = coord.to_float(gx2, gy2)
        if layer1 != layer2:
            if (gx1, gy1) not in through_hole_positions:
                _vsz, _vdr = _emit_via_size(pcb_data, gx1, gy1, config,
                                            net_id=net_id, x=x1, y=y1)
                via = Via(x=x1, y=y1, size=_vsz, drill=_vdr,
                          layers=["F.Cu", "B.Cu"], net_id=net_id)
                new_vias.append(via)
        else:
            if (x1, y1) != (x2, y2):
                layer_name = layer_names[layer1]
                seg = Segment(
                    start_x=x1, start_y=y1,
                    end_x=x2, end_y=y2,
                    width=config.get_net_track_width(net_id, layer_name),
                    layer=layer_name,
                    net_id=net_id)
                new_segments.append(seg)

    if end_original:
        last_grid_x, last_grid_y = coord.to_float(path_end[0], path_end[1])
        orig_x, orig_y, orig_layer = end_original
        if abs(orig_x - last_grid_x) > 0.001 or abs(orig_y - last_grid_y) > 0.001:
            seg = Segment(
                start_x=last_grid_x, start_y=last_grid_y,
                end_x=orig_x, end_y=orig_y,
                width=config.get_net_track_width(net_id, orig_layer),
                layer=orig_layer,
                net_id=net_id)
            new_segments.append(seg)

    return {
        'new_segments': new_segments,
        'new_vias': new_vias,
        'iterations': total_iterations,
        'path_length': len(path),
        'path': path,
    }


def build_batch_snapshot(working_obstacles, pcb_data, config, batch_net_ids,
                          all_unrouted_net_ids, routed_net_ids,
                          track_proximity_cache, layer_map,
                          net_obstacles_cache):
    """Build ONE immutable obstacle snapshot for a batch of nets: clone the
    working map (all copper) and remove every batch net's own copper so each
    net can route to its own endpoints/pads exactly as it would sequentially.
    Batch nets are spatially disjoint, so removing their copper does not let
    one route through another; any conflict loser is caught by
    route_batch_parallel's per-net re-check against working_obstacles."""
    from obstacle_cache import remove_net_obstacles_from_cache
    from obstacle_costs import (apply_stub_proximity, merge_track_proximity_costs,
                                add_cross_layer_tracks)
    from obstacle_map import (add_same_net_via_clearance,
                              add_same_net_pad_drill_via_clearance)
    from routing_context import _add_free_via_positions
    from connectivity import get_stub_endpoints
    from net_queries import get_chip_pad_positions

    obstacles = working_obstacles.clone_fresh()
    batch_set = set(batch_net_ids)
    for nid in batch_net_ids:
        if nid in net_obstacles_cache:
            remove_net_obstacles_from_cache(obstacles, net_obstacles_cache[nid])

    stub_proximity_net_ids = [nid for nid in all_unrouted_net_ids
                              if nid not in batch_set and nid not in routed_net_ids]
    unrouted_stubs = get_stub_endpoints(pcb_data, stub_proximity_net_ids)
    chip_pads = get_chip_pad_positions(pcb_data, stub_proximity_net_ids)
    all_stubs = unrouted_stubs + chip_pads
    _stub_surplus = apply_stub_proximity(obstacles, pcb_data,
                                         stub_proximity_net_ids, all_stubs,
                                         config, layer_map=layer_map)
    merge_track_proximity_costs(
        obstacles,
        track_proximity_cache,
        ghost_costs=_stub_surplus or None,
        config=config)
    add_cross_layer_tracks(obstacles, pcb_data, config, layer_map,
                           exclude_net_ids=batch_set)
    for nid in batch_net_ids:
        add_same_net_via_clearance(obstacles, pcb_data, nid, config)
        add_same_net_pad_drill_via_clearance(obstacles, pcb_data, nid, config)
        _add_free_via_positions(obstacles, pcb_data, [nid], config)
    return obstacles


def _result_conflicts(snapshot: GridObstacleMap, result: dict,
                        config: GridRouteConfig) -> bool:
    """Re-check a routed result's new copper against the shared snapshot (which
    holds ALL already-committed copper but NOT this net's own copper). Uses the
    exact clearance kernels (segment_blocked / is_via_blocked) on the
    clearance-expanded obstacle map. Returns True if the result conflicts."""
    coord = GridCoord(config.grid_step)
    layer_map = {name: i for i, name in enumerate(config.layers)}
    for seg in result['new_segments']:
        gx1, gy1 = coord.to_grid(seg.start_x, seg.start_y)
        gx2, gy2 = coord.to_grid(seg.end_x, seg.end_y)
        layer = layer_map.get(seg.layer)
        if layer is None:
            continue
        # r=0.5 checks every cell along the segment (r<=0 only checks the
        # endpoint in Rust); blocked cells are clearance-expanded obstacles, so
        # a blocked cell on the segment means a clearance violation.
        if snapshot.segment_blocked(gx1, gy1, gx2, gy2, layer, 0.5):
            return True
    for via in result.get('new_vias', []):
        gx, gy = coord.to_grid(via.x, via.y)
        if snapshot.is_via_blocked(gx, gy):
            return True
    return False


def _add_result_to_snapshot(snapshot: GridObstacleMap, pcb_data: PCBData,
                            net_id: int, result: dict, config: GridRouteConfig):
    """Add a committed net's new copper to the shared snapshot so subsequent
    nets/batches see it as an obstacle (clearance-expanded)."""
    from obstacle_cache import update_net_obstacles_after_routing
    from obstacle_cache import add_net_obstacles_from_cache
    # Recompute the net's obstacles from pcb_data (now includes the new route)
    # and add them to the snapshot. update_net_obstacles_after_routing writes
    # into the passed cache dict; use a throwaway dict and read it back.
    tmp_cache = {}
    update_net_obstacles_after_routing(pcb_data, net_id, result, config, tmp_cache)
    add_net_obstacles_from_cache(snapshot, tmp_cache[net_id])


def route_batch_parallel(pcb_data: PCBData, config: GridRouteConfig,
                         working_obstacles: GridObstacleMap,
                         net_specs: List[dict],
                         all_unrouted_net_ids: List[int],
                         routed_net_ids: List[int],
                         track_proximity_cache: Dict,
                         layer_map: Dict,
                         net_obstacles_cache: Dict,
                         commit_fn) -> Tuple[Dict[int, dict], List[int]]:
    """Route plain-net specs in parallel batches against ONE immutable snapshot,
    committing results SEQUENTIALLY in net-id order and re-checking each against
    already-committed copper with the exact clearance kernels; conflict losers
    are re-queued for the next batch against an updated snapshot.

    commit_fn(net_id, result) commits a clean result to pcb_data + state and
    returns True; returns False to re-queue. Returns (committed_results,
    requeued_ids)."""
    results: Dict[int, dict] = {}
    requeued: List[int] = []
    if route_batch is None or RouteRequest is None:
        return results, [s['net_id'] for s in net_specs]

    batch_ids = [s['net_id'] for s in net_specs]
    snapshot = build_batch_snapshot(
        working_obstacles, pcb_data, config, batch_ids,
        all_unrouted_net_ids, routed_net_ids, track_proximity_cache,
        layer_map, net_obstacles_cache)

    prepared: List[Tuple[int, dict]] = []
    for spec in net_specs:
        prep = prepare_request(
            pcb_data, spec['net_id'], config, snapshot,
            attraction_path=spec.get('attraction_path'),
            reverse_direction=spec.get('reverse_direction', False),
            bounds=spec.get('bounds'))
        if prep is not None:
            prepared.append((spec['net_id'], prep))

    bboxes: Dict[int, Optional[Tuple[int,int,int,int]]] = {}
    prepared_by_id: Dict[int, Tuple[dict, dict]] = {}
    for nid, (req_kwargs, meta) in prepared:
        bboxes[nid] = net_windowed_bbox(nid, pcb_data, config)
        prepared_by_id[nid] = (req_kwargs, meta)

    batches = pick_disjoint_batches([nid for nid, _ in prepared], bboxes)

    for batch in batches:
        # Only route MULTI-net disjoint batches in parallel. A singleton batch
        # holds a net that is not spatially disjoint from any other plain net,
        # so routing it here would change its position in the routing sequence
        # vs sequential routing (order-dependent rip-ups/blockers). Leaving it
        # for the sequential loop preserves identical results.
        if len(batch) < 2:
            requeued.extend(batch)
            continue
        reqs = []
        metas_by_id = {}
        for nid in batch:
            req_kwargs, meta = prepared_by_id[nid]
            reqs.append(RouteRequest(**req_kwargs))
            metas_by_id[nid] = meta
        try:
            batch_results = route_batch(reqs, snapshot)
        except Exception as e:
            print(f"  [parallel] route_batch failed ({e}); falling back")
            requeued.extend(batch)
            continue
        # Commit sequentially in net-id order.
        for nid in sorted(batch):
            idx = batch.index(nid)
            path, iters, blocked = batch_results[idx]
            if path is None:
                requeued.append(nid)
                continue
            meta = metas_by_id[nid]
            result = build_result_from_path(pcb_data, nid, config,
                                            snapshot, meta,
                                            path[:], iters)
            # Re-check against already-committed copper + ALL other nets'
            # pads/stubs, EXCLUDING this net's own obstacles (its path
            # legitimately touches its own source/target pads). working_obstacles
            # holds every net's copper and is updated by commit_fn as nets are
            # committed, so it reflects all committed copper + other batch nets'
            # pads. Clone it and remove this net's obstacles for the check.
            from obstacle_cache import remove_net_obstacles_from_cache
            _recheck = working_obstacles.clone_fresh()
            if nid in net_obstacles_cache:
                remove_net_obstacles_from_cache(_recheck, net_obstacles_cache[nid])
            if _result_conflicts(_recheck, result, config):
                requeued.append(nid)
                continue
            if commit_fn(nid, result):
                results[nid] = result
                _add_result_to_snapshot(snapshot, pcb_data, nid, result, config)
            else:
                requeued.append(nid)
    if os.environ.get('KICAD_PARALLEL_DEBUG'):
        print(f"  [parallel-debug] prepared={len(prepared)} batches={len(batches)} "
              f"routed={len(results)} requeued={len(requeued)}")
    return results, requeued
