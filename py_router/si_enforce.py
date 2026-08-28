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


# Adaptive per-net enforcement radius (KICAD_SI_ADAPTIVE). Read INLINE from the
# environment (like KICAD_SMOOTH_PREWINDOW in pcb_modification.py) rather than
# through env_knobs, so this lane owns its knob entirely inside si_enforce.py
# and env_knobs.py stays clean for concurrent lanes. Default OFF during
# development; flipped ON at commit after the gate validation passes.
# KICAD_SI_ADAPTIVE=0 always restores the fixed R0.8/C0.1 behaviour exactly.
import os as _si_os


def _adaptive_enabled() -> bool:
    return (_si_os.environ.get('KICAD_SI_ADAPTIVE', '0').strip().lower()
            not in ('0', 'false', 'off', 'no'))


def _adaptive_force_radius() -> float:
    """Debug: force every victim's adaptive radius to a fixed mm value (sweep
    testing only -- lets a harness probe radius points without editing the
    heuristic). Unset/0 = normal adaptive behaviour."""
    try:
        return float(_si_os.environ.get('KICAD_SI_ADAPTIVE_FORCE_RADIUS', '0') or 0)
    except ValueError:
        return 0.0


# ---------------------------------------------------------------------------
# Adaptive per-net enforcement radius (KICAD_SI_ADAPTIVE)
# ---------------------------------------------------------------------------
#
# The fixed radius/cost knobs (default R0.8/C0.1) were tuned on the carrier
# board, whose victims sit >2mm from aggressor copper -- but the corpus probe
# (carrier_lab/si_corpus_findings.md "Middle-point probe") proved the knob is
# board-dependent: dense boards whose victims hug aggressors (watchy,
# haasoscope) need a WIDE band to recover their si_coupling win, while sparse
# boards (carrier, ulx3s) need a NARROW band to avoid wasted steering and
# DRC/connectivity regressions. No single fixed radius/cost pair dominates.
#
# Adaptive rule: at stamp time the only victim geometry available is the
# victim net's PADS (the chain routes aggressors first, then victims, so a
# victim has no own copper yet). We measure the distance from every victim
# pad to the nearest AGGRESSOR copper on the pad's own layer(s), and size
# the enforcement band from that distribution:
#
#   board_median = median over ALL victim pads of distance to nearest
#                  aggressor copper
#   if board_median >= 2.5mm (SPARSE board -- ulx3s, carrier, tigard,
#       kitdev, glasgow): every victim gets the narrow floor (0.5mm). Wide
#       bands on a dense BGA board over-steer victims into blocking GND and
#       cascade into mass connectivity failures (ulx3s adaptive probe).
#   else (DENSE board -- watchy, haasoscope): victims get a moderate base
#       band (1.0mm), and exposed multi-pad nets (>=4 pads, frac_close >
#       0.30) are widened toward 1.2mm.
#
#   frac_close = fraction of the net's pads within the metric's own 1.0mm
#                coupling window of aggressor copper
#
# A dense board whose victims hug aggressors gets real steering (SI win);
# a sparse board whose victims sit far away gets no wasted steering and no
# timing cost. Cost scales gently with radius so a wide band does not
# over-price.
#
# KICAD_SI_ADAPTIVE=0 restores the fixed R0.8/C0.1 behaviour exactly.
_ADAPTIVE_MIN_R = 0.5
_ADAPTIVE_MAX_R = 1.2
_ADAPTIVE_WINDOW = 1.0   # the metric's own coupling window (mm)
_ADAPTIVE_SPARSE_MED = 2.5   # board median pad-dist (mm) above which = sparse
_ADAPTIVE_DENSE_BASE_R = 1.0   # moderate band for dense boards
_ADAPTIVE_DENSE_FRAC = 0.30   # frac of pads within window that means "exposed"
_ADAPTIVE_MIN_PADS = 4   # a net needs >= this many pads to earn a wide band


def _adaptive_radius_for(frac_close: float) -> float:
    """Per-net enforcement radius from the fraction of the net's pads within
    the metric's coupling window of aggressor copper.

    Only meaningful on DENSE boards (see _adaptive_radius_for_net); on sparse
    boards every net gets the narrow floor.
    """
    f = max(0.0, min(1.0, frac_close))
    r = _ADAPTIVE_DENSE_BASE_R + (_ADAPTIVE_MAX_R - _ADAPTIVE_DENSE_BASE_R) * min(
        1.0, f / _ADAPTIVE_DENSE_FRAC)
    return r


def _adaptive_cost_for(radius: float) -> float:
    """Per-net cost scaled gently with radius.

    A wide band must not over-price every cell inside it (the R1.0/C0.2 probe
    showed wide+expensive over-steers into connectivity regressions), so the
    cost grows only ~linearly from 0.5x at the narrow floor to ~1.4x at the
    wide ceiling -- the steering comes from the band WIDTH, not from a cost
    spike.
    """
    return env_knobs.SI_ENFORCE_COST * (0.5 + 0.5 * radius / 0.8)


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


def _aggressor_point_trees(pcb_data, classes, fp=None):
    """Per-layer cKDTree of sampled AGGRESSOR copper points, cached on the
    board's aggressor fingerprint so N victim nets pay one tree build."""
    if fp is None:
        fp = _aggressor_fingerprint(pcb_data, classes)
    cache = getattr(pcb_data, '_si_aggr_pt_cache', None)
    if cache is not None and cache[0] == fp:
        return cache[1]
    from collections import defaultdict as _dd
    aggr_pts = _dd(list)
    for s in pcb_data.segments:
        if s.net_id in classes and classes[s.net_id]['class'] == 'AGGRESSOR':
            dx = s.end_x - s.start_x
            dy = s.end_y - s.start_y
            L = (dx * dx + dy * dy) ** 0.5
            if L < 1e-9:
                aggr_pts[s.layer].append((s.start_x, s.start_y))
                continue
            n = max(1, int(L / 0.2))
            for i in range(n + 1):
                t = i / n
                aggr_pts[s.layer].append((s.start_x + dx * t,
                                          s.start_y + dy * t))
    trees = {}
    if aggr_pts:
        try:
            from scipy.spatial import cKDTree
        except ImportError:
            trees = {}
        else:
            for layer, pts in aggr_pts.items():
                trees[layer] = cKDTree(pts)
    try:
        pcb_data._si_aggr_pt_cache = (fp, trees)
    except Exception:
        pass
    return trees


def _pad_to_aggressor_distances(pcb_data, classes, net_id,
                              fp=None):
    """Distances (mm) from a victim net's pads to the nearest AGGRESSOR copper
    on the pad's own layer(s). Empty list when no aggressor copper exists.

    This is the stamp-time signal: the chain routes aggressors first, so when
    a victim net is being routed its pads are the only victim geometry, and
    the aggressor copper is already on the board.
    """
    trees = _aggressor_point_trees(pcb_data, classes, fp=fp)
    if not trees:
        return []
    out = []
    for p in pcb_data.pads_by_net.get(net_id, []):
        best = None
        for pl in p.layers:
            t = trees.get(pl)
            if t is None:
                continue
            d, _ = t.query([[p.global_x, p.global_y]])
            d0 = float(d[0])
            if best is None or d0 < best:
                best = d0
        if best is not None:
            out.append(best)
    return out


def _board_median_pad_dist(pcb_data, classes, fp=None):
    """Median distance (mm) over ALL victim pads to nearest aggressor copper.

    The board-level density signal: sparse boards (high median) get narrow
    bands everywhere; dense boards (low median) get moderate/wide bands.
    Cached on the aggressor fingerprint.
    """
    fp2 = fp if fp is not None else _aggressor_fingerprint(pcb_data, classes)
    cache = getattr(pcb_data, '_si_board_med_cache', None)
    if cache is not None and cache[0] == fp2:
        return cache[1]
    all_dists = []
    for nid, inf in classes.items():
        if nid == -1 or inf['class'] != 'VICTIM':
            continue
        all_dists.extend(_pad_to_aggressor_distances(pcb_data, classes, nid,
                                                     fp=fp2))
    if not all_dists:
        med = None
    else:
        all_dists.sort()
        med = all_dists[len(all_dists) // 2]
    try:
        pcb_data._si_board_med_cache = (fp2, med)
    except Exception:
        pass
    return med


def _adaptive_radius_for_net(pcb_data, classes, net_id,
                              fp=None):
    """Per-net adaptive radius (mm) for a victim net.

    Board-level density gate first: sparse boards (victim pads sit far from
    aggressors -- ulx3s, carrier, tigard, kitdev, glasgow) get the narrow
    floor for EVERY net, because wide bands on a dense BGA board over-steer
    victims into blocking GND and cascade into mass connectivity failures
    (ulx3s adaptive probe). Dense boards (watchy, haasoscope) get a moderate
    base band, with exposed multi-pad nets widened further.

    Cached per (board fingerprint, net_id). Falls back to the fixed knob
    radius when adaptive is off or no aggressor copper exists.
    """
    if not _adaptive_enabled():
        return _radius_mm()
    _force = _adaptive_force_radius()
    if _force > 0:
        return _force
    cache = getattr(pcb_data, '_si_adaptive_radius_cache', None)
    if cache is None:
        cache = {}
        try:
            pcb_data._si_adaptive_radius_cache = cache
        except Exception:
            pass
    if net_id in cache:
        return cache[net_id]
    med = _board_median_pad_dist(pcb_data, classes, fp=fp)
    if med is None:
        r = _radius_mm()
    elif med >= _ADAPTIVE_SPARSE_MED:
        # Sparse board: narrow floor everywhere -- no wasted steering.
        r = _ADAPTIVE_MIN_R
    else:
        # Dense board: moderate base, widen exposed multi-pad nets.
        dists = _pad_to_aggressor_distances(pcb_data, classes, net_id,
                                            fp=fp)
        if len(dists) >= _ADAPTIVE_MIN_PADS:
            frac_close = sum(1 for d in dists if d <= _ADAPTIVE_WINDOW) / len(dists)
            r = _adaptive_radius_for(frac_close)
        else:
            r = _ADAPTIVE_DENSE_BASE_R
    cache[net_id] = r
    return r


def _adaptive_cost_for_net(pcb_data, classes, net_id, fp=None):
    """Per-net adaptive cost (mm-equivalent).

    fp: optional pre-computed aggressor fingerprint (C4) -- threaded through so
    the O(all-segments) string build happens once per victim instead of once
    here AND once in _adaptive_radius_for_net.
    """
    if not _adaptive_enabled():
        return _cost_mm()
    r = _adaptive_radius_for_net(pcb_data, classes, net_id, fp=fp)
    return _adaptive_cost_for(r)


def _build_aggressor_geometry(pcb_data, config, classes,
                     radius_mm=None, cost_mm=None):
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

    radius_mm = _radius_mm() if radius_mm is None else radius_mm
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
    cost_grid = config.cell_cost(_cost_mm() if cost_mm is None else cost_mm)
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


def _get_aggressor_geometry(pcb_data, config, classes, fp=None,
                     radius_mm=None, cost_mm=None):
    """Cached aggressor geometry keyed on the board's aggressor fingerprint
    AND the radius/cost pair (adaptive per-net bands differ per victim).

    fp: optional pre-computed fingerprint (C4: compute_victim_si_field computes
    it once and threads it through, so the O(all-segments) string build happens
    once per victim instead of once here AND once in _get_union_field). When
    None, the historical self-compute is used.
    """
    if fp is None:
        fp = _aggressor_fingerprint(pcb_data, classes)
    key = (fp, radius_mm, cost_mm)
    cache = getattr(pcb_data, '_si_aggr_geo_cache', None)
    if cache is None or cache[0] != key:
        geo = _build_aggressor_geometry(pcb_data, config, classes,
                                        radius_mm=radius_mm, cost_mm=cost_mm)
        try:
            pcb_data._si_aggr_geo_cache = (key, geo)
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


def _get_union_field(pcb_data, config, classes, fp=None,
                     radius_mm=None, cost_mm=None):
    """Cached union field (ALL aggressors, no same-interface filter).

    Keyed on the aggressor fingerprint + radius/cost. Victims with no
    same-interface aggressor reuse this directly -- a huge win on boards with
    extensive aggressor copper.

    fp: optional pre-computed fingerprint (C4) -- see _get_aggressor_geometry.
    """
    if fp is None:
        fp = _aggressor_fingerprint(pcb_data, classes)
    key = (fp, radius_mm, cost_mm)
    cache = getattr(pcb_data, '_si_union_field_cache', None)
    if cache is None or cache[0] != key:
        geo = _get_aggressor_geometry(pcb_data, config, classes, fp=fp,
                                      radius_mm=radius_mm, cost_mm=cost_mm)
        arr = _accumulate_field(geo, classes) if geo else np.empty((0, 4), dtype=np.int32)
        try:
            pcb_data._si_union_field_cache = (key, arr)
        except Exception:
            pass
        return arr
    return cache[1]


# ---------------------------------------------------------------------------
# Field computation + stamping entry points
# ---------------------------------------------------------------------------

def _own_pad_exempt_cells(pcb_data, config, net_id):
    """Grid cells (N,2) within a small radius of the victim net's own pads.

    The enforcement field must never block a victim's mandatory pad approach,
    even when aggressor copper hugs the pad (watchy mid probe: the +3V3 U4 pad
    sits 0.01mm from +3V3 aggressor copper; the wide band blocked its approach
    -> conn 1). Radius = pad half-diagonal + 0.2mm margin, so the pad's own
    landing zone stays free of SI cost.
    """
    grid_step = config.grid_step or 0.1
    cells = []
    for p in pcb_data.pads_by_net.get(net_id, []):
        r = (max(p.size_x, p.size_y) / 2.0) + 0.2
        rg = max(1, int(round(r / grid_step)))
        gx = round(p.global_x / grid_step)
        gy = round(p.global_y / grid_step)
        for ex in range(-rg, rg + 1):
            for ey in range(-rg, rg + 1):
                if ex * ex + ey * ey <= rg * rg:
                    cells.append((gx + ex, gy + ey))
    if not cells:
        return np.empty((0, 2), dtype=np.int32)
    return np.asarray(cells, dtype=np.int32)


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
    # Adaptive per-net radius/cost (KICAD_SI_ADAPTIVE=0 -> fixed knobs).
    _r = _adaptive_radius_for_net(pcb_data, classes, net_id,
                                  fp=_fp)
    _c = _adaptive_cost_for_net(pcb_data, classes, net_id, fp=_fp)
    geo = _get_aggressor_geometry(pcb_data, config, classes, fp=_fp,
                                  radius_mm=_r, cost_mm=_c)
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
        arr = _get_union_field(pcb_data, config, classes, fp=_fp,
                               radius_mm=_r, cost_mm=_c)
    else:
        # Rare path: this victim shares an interface with an aggressor --
        # compute its field with that aggressor excluded.
        arr = _accumulate_field(geo, classes, exclude_nids=exclude_nids)

    # Own-pad exemption (adaptive only): a victim must ALWAYS be able to
    # reach its own pads, even when aggressor copper hugs them (watchy mid
    # probe: the +3V3 U4 pad at (82.68,85.31) sits 0.01mm from +3V3 aggressor
    # copper and the wide band blocked its approach -> conn 1). Drop
    # enforcement cells within a small radius of the victim's own pads so the
    # mandatory pad approach is never priced. Gated behind adaptive so
    # KICAD_SI_ADAPTIVE=0 restores the fixed R0.8/C0.1 behaviour exactly.
    if len(arr) and _adaptive_enabled():
        _own_pad_cells = _own_pad_exempt_cells(pcb_data, config, net_id)
        if len(_own_pad_cells):
            # Key on (gx, gy) only -- the exemption is layer-agnostic (a pad
            # approach can come from any layer, and the field may price the
            # same cell on several layers).
            _key = arr[:, 1].astype(np.int64) * 100000 + arr[:, 2].astype(np.int64)
            _ex = (_own_pad_cells[:, 0].astype(np.int64) * 100000
                   + _own_pad_cells[:, 1].astype(np.int64))
            _mask = ~np.isin(_key, _ex)
            arr = arr[_mask]
    return arr


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
