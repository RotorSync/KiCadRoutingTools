"""si_enforce.py -- Phase 2 SI routing enforcement (aggressor-proximity costs).

For every VICTIM net being routed this module stamps elevated per-layer proximity
costs in cells near AGGRESSOR copper -- same-layer parallel exposure plus broadside
coupling from adjacent unshielded copper layers -- so sensitive nets naturally route
away from noisy copper or cross it perpendicular instead of running parallel beside it.

Mechanism: reuses the existing Rust cost-stamping infrastructure
(GridObstacleMap.set_layer_proximity_batch), exactly like stub/track proximity.
The field is computed per victim net from the CURRENT board copper; aggressor and
neutral nets route exactly as today (no costs stamped).

The coupling rule mirrors quality/score.py metric_si_coupling (v1.4):
  * same-layer term: cells within SI_PROXIMITY_RADIUS of an aggressor segment on
    the victim's own layer;
  * broadside term: cells within SI_PROXIMITY_RADIUS (in-plane) of an aggressor
    segment on an ADJACENT copper layer with no GND plane between them
    (stackup-aware via _layer_is_shielded).

Perpendicular crossings stay cheap because a crossing traverses only ~2x radius
cells once while a parallel run pays per cell along its whole length -- exactly how
metric_si_coupling counts exposure length.

Same-interface pairs (a victim beside its OWN bus clock/strobe -- SPI MOSI beside
SCK, DDR DQ beside DQS) are excluded from enforcement exactly as they are excluded
from the metric: those are intentional routing and must not be pushed apart.

Performance: the aggressor geometry (per-layer segments, offset tables, sampled
points) is cached per board state keyed on a fingerprint of the aggressor copper,
so N victim nets pay the geometry build once and only the fast cell accumulation
per net.
"""
from __future__ import annotations

import math
import re
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

import numpy as np

import env_knobs

_EPS = 1e-9


def _radius_mm() -> float:
    return env_knobs.SI_ENFORCE_RADIUS


def _cost_mm() -> float:
    return env_knobs.SI_ENFORCE_COST


# ---------------------------------------------------------------------------
# Small self-contained geometry helpers (mirror quality/geometry.py semantics)
# ---------------------------------------------------------------------------

def _layer_is_shielded(layer_a: str,
                       layer_b: str,
                       copper_layers: List[str],
                       shield_nets_by_layer: Dict[str, set]) -> bool:
    """True if a solid GND plane lies between two copper layers."""
    if layer_a == layer_b:
        return False
    try:
        ia = copper_layers.index(layer_a)
        ib = copper_layers.index(layer_b)
    except ValueError:
        return False
    lo, hi = (ia, ib) if ia < ib else (ib, ia)
    for i in range(lo + 1, hi):
        mid = copper_layers[i]
        if shield_nets_by_layer.get(mid):
            return True
    return False


def _norm_name(n: str) -> str:
    n = n.lstrip('/')
    m = re.search(r'\\((.*)\\)$', n)
    if m:
        n = m.group(1)
    return n.lower()


def _same_interface(name_a: str, name_b: str) -> bool:
    """True if two nets belong to the SAME functional interface (bus).

    Mirrors quality/score.py metric_si_coupling's exclusion: same-bus pairs are
    intentional routing and must not count as SI violations -- nor be pushed apart
    by enforcement.
    """
    a = _norm_name(name_a)
    b = _norm_name(name_b)
    i = 0
    while i < len(a) and i < len(b) and a[i] == b[i]:
        i += 1
    prefix = a[:i]
    if len(prefix) >= 4 and any(c.isalpha() for c in prefix):
        return True
    ta = set(re.findall(r'u[0-9]+', a))
    tb = set(re.findall(r'u[0-9]+', b))
    return bool(ta & tb)


# ---------------------------------------------------------------------------
# Board-level classification cache (per pcb_data object)
# ---------------------------------------------------------------------------

def _classes_for(pcb_data):
    cache = getattr(pcb_data, '_si_classes_cache', None)
    if cache is None:
        import si_classes as si   # local import keeps this module importable standalone-ish
        board_path = getattr(pcb_data, 'source_path', '') or ''
        cache = si.classify_board(pcb_data, board_path=board_path or None)
        try:
            pcb_data._si_classes_cache = cache
        except Exception:
            pass   # frozen/slotted object -- recompute each call instead of caching
    return cache


def _shield_nets_by_layer(pcb_data):
    out: Dict[str, set] = defaultdict(set)
    for z in pcb_data.zones:
        if z.net_id == 0:
            continue
        zname = z.net_name or ''
        low = zname.lstrip('/').lower()
        if low.startswith('gnd') or low == 'ground':
            out[z.layer].add(zname)
    return out


# ---------------------------------------------------------------------------
# Aggressor-geometry cache (per board state)
# ---------------------------------------------------------------------------

def _aggressor_fingerprint(pcb_data, classes) -> str:
    """Fingerprint of the aggressor copper that carries SI fields.

    Changes only when aggressor segments are added/removed/moved -- i.e. when
    another aggressor net routes between victim nets. Cheap to compute.
    """
    parts = []
    for s in pcb_data.segments:
        if s.net_id in classes and classes[s.net_id]['class'] == 'AGGRESSOR':
            parts.append(f"{s.net_id}:{s.layer}:{round(s.start_x,3)}:{round(s.start_y,3)}"
                         f":{round(s.end_x,3)}:{round(s.end_y,3)}:{round(s.width or 0,3)}")
    return '|'.join(sorted(parts))


def _build_aggressor_geometry(pcb_data, config, classes):
    """Build per-layer aggressor segment geometry + offset tables.

    Returns dict layer_idx -> list of (pts_arr, off_cells, off_cost) tuples,
    where pts_arr is the sampled walk-line points and off_cells/off_cost are the
    cached proximity offset table for that segment's width.
    """
    seg_nets = set()
    for s in pcb_data.segments:
        seg_nets.add(s.net_id)

    # All aggressor nets with copper (same-interface filtering is per-victim,
    # done at accumulation time via the victim's name).
    aggressor_nets = set()
    for nid, inf in classes.items():
        if nid == -1 or nid not in seg_nets:
            continue
        if inf['class'] == 'AGGRESSOR':
            aggressor_nets.add(nid)
    if not aggressor_nets:
        return {}

    by_layer: Dict[str, List] = defaultdict(list)
    for s in pcb_data.segments:
        if s.net_id in aggressor_nets:
            by_layer[s.layer].append(s)

    shield_by_layer = _shield_nets_by_layer(pcb_data)
    copper_layers = list(getattr(pcb_data.board_info,
                                 'copper_layers', []) or [])
    layer_map = {name: i for i, name in enumerate(config.layers)}

    radius_mm = _radius_mm()
    grid_step = config.grid_step or 0.1

    # Which routing layers receive fields from which aggressor layers.
    layer_sources: Dict[int, List] = defaultdict(list)
    for alayer in by_layer:
        if alayer in layer_map:
            layer_sources[layer_map[alayer]].extend(by_layer[alayer])
        try:
            ai = copper_layers.index(alayer)
        except ValueError:
            continue   # aggressor layer not in stackup order -- same-layer only
        for adj in (ai - 1, ai + 1):
            if adj < 0 or adj >= len(copper_layers):
                continue
            vlayer = copper_layers[adj]
            if vlayer not in layer_map:
                continue   # not a routing layer this run -- skip broadside onto it
            if _layer_is_shielded(alayer, vlayer,
                                  copper_layers,
                                  shield_by_layer):
                continue   # GND plane between them -- broadside shielded off
            layer_sources[layer_map[vlayer]].extend(by_layer[alayer])

    radius_grid = max(1, int(round(radius_mm / grid_step)))
    cost_grid = config.cell_cost(_cost_mm())
    sample_every = max(1, int(round(0.5 / grid_step)))

    from obstacle_costs import _get_proximity_offsets_np   # cached offset tables
    from bresenham_utils import walk_line

    geo: Dict[int, List] = defaultdict(list)
    for lidx in sorted(layer_sources):
        segs_list = layer_sources[lidx]
        by_width: Dict[int, List] = defaultdict(list)
        for s in segs_list:
            hw_grid = round(((s.width or 0) / 2.0) / grid_step)
            by_width[hw_grid].append(s)
        for hw_grid in sorted(by_width):
            offsets_np = _get_proximity_offsets_np(radius_grid,
                                                   cost_grid,
                                                   hw_grid)
            if len(offsets_np) == 0:
                continue
            off_cells = offsets_np[:, :2].astype(np.int64)   # (O,2)
            off_cost = offsets_np[:, 2].astype(np.int64)     # (O,)
            for s in by_width[hw_grid]:
                gx1 = round(s.start_x / grid_step)
                gy1 = round(s.start_y / grid_step)
                gx2 = round(s.end_x / grid_step)
                gy2 = round(s.end_y / grid_step)
                pts = list(walk_line(gx1, gy1, gx2, gy2))[::sample_every]
                if not pts:
                    continue
                pts_arr = np.asarray(pts, dtype=np.int64)     # (M,2)
                # Each entry carries its aggressor net_id so the per-victim
                # same-interface filter can drop a victim's OWN bus clock/strobe.
                geo[lidx].append((s.net_id, pts_arr, off_cells, off_cost))
    return dict(geo)


def _get_aggressor_geometry(pcb_data, config, classes, fp=None):
    """Cached aggressor geometry keyed on the board's aggressor fingerprint.

    fp: optional pre-computed fingerprint (C4: compute_victim_si_field computes
    it once and threads it through, so the O(all-segments) string build happens
    once per victim instead of once here AND once in _get_union_field). When
    None, the historical self-compute is used.
    """
    if fp is None:
        fp = _aggressor_fingerprint(pcb_data, classes)
    cache = getattr(pcb_data, '_si_aggr_geo_cache', None)
    if cache is None or cache[0] != fp:
        geo = _build_aggressor_geometry(pcb_data, config, classes)
        try:
            pcb_data._si_aggr_geo_cache = (fp, geo)
        except Exception:
            pass
        return geo
    return cache[1]


def _accumulate_field(geo, classes, exclude_nids=None):
    """Accumulate the SI field from aggressor geometry into an (N,4) array.

    exclude_nids: set of aggressor net_ids to skip (same-interface victims).
    """
    out_layers: List[int] = []
    out_cells: List[np.ndarray] = []
    out_costs: List[np.ndarray] = []
    for lidx in sorted(geo):
        layer_cells: List[np.ndarray] = []
        layer_costs: List[np.ndarray] = []
        for aggr_nid, pts_arr, off_cells, off_cost in geo[lidx]:
            if exclude_nids and aggr_nid in exclude_nids:
                continue
            cells = pts_arr[:, None, :] + off_cells[None, :, :]   # (M,O,2)
            cells = cells.reshape(-1, 2)
            costs = np.broadcast_to(off_cost[None, :],
                                    (len(pts_arr), len(off_cost))).reshape(-1)
            layer_cells.append(cells)
            layer_costs.append(costs)
        if not layer_cells:
            continue
        all_cells = np.concatenate(layer_cells, axis=0)   # (N,2)
        all_costs = np.concatenate(layer_costs, axis=0)   # (N,)
        keyed = all_cells[:, 0].astype(np.int64) * 100000 + all_cells[:, 1].astype(np.int64)
        uniq, inv = np.unique(keyed, return_inverse=True)
        max_cost = np.zeros(len(uniq), dtype=np.int64)
        np.maximum.at(max_cost, inv, all_costs.astype(np.int64))
        gx = uniq // 100000
        gy = uniq % 100000
        out_layers.append(np.full(len(uniq), lidx, dtype=np.int32))
        out_cells.append(np.stack([gx.astype(np.int32), gy.astype(np.int32)], axis=1))
        out_costs.append(max_cost.astype(np.int32))
    if not out_layers:
        return np.empty((0, 4), dtype=np.int32)
    layers_arr = np.concatenate(out_layers)
    cells_arr = np.concatenate(out_cells)
    costs_arr = np.concatenate(out_costs)
    return np.concatenate([layers_arr[:, None], cells_arr, costs_arr[:, None]],
                          axis=1).astype(np.int32)


def _get_union_field(pcb_data, config, classes, fp=None):
    """Cached union field (ALL aggressors, no same-interface filter).

    Keyed on the aggressor fingerprint. Victims with no same-interface aggressor
    reuse this directly -- a huge win on boards with extensive aggressor copper.

    fp: optional pre-computed fingerprint (C4) -- see _get_aggressor_geometry.
    """
    if fp is None:
        fp = _aggressor_fingerprint(pcb_data, classes)
    cache = getattr(pcb_data, '_si_union_field_cache', None)
    if cache is None or cache[0] != fp:
        geo = _get_aggressor_geometry(pcb_data, config, classes, fp=fp)
        arr = _accumulate_field(geo, classes) if geo else np.empty((0, 4), dtype=np.int32)
        try:
            pcb_data._si_union_field_cache = (fp, arr)
        except Exception:
            pass
        return arr
    return cache[1]


# ---------------------------------------------------------------------------
# Field computation + stamping entry points
# ---------------------------------------------------------------------------

def compute_victim_si_field(pcb_data,
                            config,
                            net_id: int,
                            classes=None) -> np.ndarray:
    """Return (N,4) int32 [layer_idx,gx,gy,cost] for a VICTIM net being routed.

    Empty array when enforcement is off or net_id is not a VICTIM.
    """
    if not env_knobs.SI_ENFORCE:
        return np.empty((0, 4), dtype=np.int32)
    if classes is None:
        classes = _classes_for(pcb_data)
    info = classes.get(net_id)
    if info is None or info['class'] != 'VICTIM':
        return np.empty((0, 4), dtype=np.int32)

    victim_name = info['name'] or ''
    # C4 (#bulk-profile): compute the aggressor fingerprint ONCE per victim and
    # thread it through _get_aggressor_geometry / _get_union_field, so the
    # O(all-segments) string build happens once instead of twice per victim
    # (the historical path recomputed it inside each helper). Gated by
    # KICAD_CACHE_BY_NET (default ON; '0' restores the historical double
    # compute). The fingerprint is a pure function of the current board state,
    # so results are bit-for-bit identical.
    import env_knobs as _ek
    _fp = (_aggressor_fingerprint(pcb_data, classes) if _ek.CACHE_BY_NET else None)
    geo = _get_aggressor_geometry(pcb_data, config, classes, fp=_fp)
    if not geo:
        return np.empty((0, 4), dtype=np.int32)

    # Same-interface aggressors for THIS victim (its own bus clock/strobe).
    exclude_nids = set()
    for aggr_nid in set(a[0] for segs in geo.values() for a in segs):
        _ainf = classes.get(aggr_nid)
        if _ainf is not None and _same_interface(victim_name,
                                                 _ainf.get('name') or ''):
            exclude_nids.add(aggr_nid)

    if not exclude_nids:
        # No same-interface aggressor: reuse the cached union field directly.
        return _get_union_field(pcb_data, config, classes, fp=_fp)

    # Rare path: this victim shares an interface with an aggressor -- compute
    # its field with that aggressor excluded.
    return _accumulate_field(geo, classes, exclude_nids=exclude_nids)


def stamp_victim_si_field(obstacles,
                          pcb_data,
                          config,
                          net_id: int,
                          classes=None) -> int:
    """Stamp SI enforcement costs for a victim net onto an obstacle map.

    Returns number of stamped cells (0 when inert). No-op unless KICAD_SI_ENFORCE
    is on and net_id is classified VICTIM.
    """
    arr = compute_victim_si_field(pcb_data, config, net_id,
                                  classes=classes)
    if len(arr):
        obstacles.set_layer_proximity_batch(arr)
    return len(arr)


def si_field_summary(pcb_data,
                     config,
                     classes=None) -> Dict[str, int]:
    """Count victim/aggressor nets with copper (diagnostic)."""
    if classes is None:
        classes = _classes_for(pcb_data)
    seg_nets = set()
    for s in pcb_data.segments:
        seg_nets.add(s.net_id)
    victims = sum(1 for nid, inf in classes.items()
                  if nid != -1 and nid in seg_nets and inf['class'] == 'VICTIM')
    aggressors = sum(1 for nid, inf in classes.items()
                     if nid != -1 and nid in seg_nets and inf['class'] == 'AGGRESSOR')
    return {'victim_nets_with_copper': victims,
            'aggressor_nets_with_copper': aggressors}
