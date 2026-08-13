"""Global planning pass (#589): fast rough routes for every net BEFORE
detailed routing, whose predicted paths become soft corridor reservations
and a smarter (plan-informed) net order.

Each net is probe-routed once -- high heuristic weight (near-greedy A*),
small STATIC iteration cap (<= 10k disarms the #529 dynamic extension) --
against a throwaway BASE-style obstacle map that excludes every net being
routed. Rough paths therefore never see each other and never dead-end on
future nets' stubs: the issue's "relaxed legality", with no Rust search
mode. Existing committed copper stays hard. Probes commit nothing (the
bus_corridor precedent); the probe map is throwaway because
route_net_with_obstacles leaves endpoint-exempt / allowed-cell /
unblock-via residue on the map it routes on, which must not leak into the
run's real maps. (Known benign residue shared with bus corridor probes:
a boxed-endpoint probe can register small-rung via cells in
pcb_data._unblock_via_sizes -- a tighten-only statement about board
geometry that stays correct whether or not the probe path ships.)

The plan's three outputs:

1. **Corridor reservations** -- per-net [layer, gx, gy, cost] fields built
   by the ripped-route-ghost stamp (compute_ripped_route_costs, width-aware
   per #585 item 3) at the plan's own cost/radius knobs. They ride
   merge_track_proximity_costs' ghost_costs (the congestion2/history
   pattern) via add_plan_source below, so composition (max/sum/softcap,
   #584) applies unchanged. Owner-exempt by construction: the fold skips
   the net being routed, skips nets already routed (real copper + its
   track-proximity entry supersede the prediction), and skips nets with an
   ACTIVE ripped-route ghost (the actual vacated corridor outranks the
   prediction; in sum mode the two would double-charge).

2. **Plan-informed ordering** -- pairwise corridor conflicts measured on
   the predicted paths (same-layer bucket co-occupancy), not on MPS's
   straight MST chords. 'planar' peels fewest-conflicts-first (MPS
   semantics with honest inputs); 'contended' routes the most-contended
   corridors first (claim scarce lanes early -- the #472 / bus-corridor
   doctrine). The #472 direct-first partition is preserved: blocks are
   reordered independently, never merged.

3. **Congestion-v2 demand seeding** (optional, 'c2') -- rough path points
   join the terminal demand map, replacing endpoints-only demand with
   actual predicted corridors ("rough paths ARE the demand map").

Env-gated experiment (KICAD_GLOBAL_PLAN=1; see env_knobs.GLOBAL_PLAN): no
CLI flag and no GUI control -- both fronts reach it through batch_route, so
GUI/CLI parity holds by construction. Promotion to a real flag requires the
full parity wiring per CLAUDE.md. Evaluation per the #584/#586 discipline:
12-board chain screen ranked by the pad verdict, wall time and via counts
graded, then full-set A/B. Probe iterations are added to the run's
total_iterations -- the pass competes honestly with just spending those
iterations on detailed search.

Deliberately NOT in v1: via-site reservations (the plan collects each rough
route's via positions in via_sites, but folding them into the stub map
prices them at the RIP-avoidance knobs and needs #588's scarcity thinking);
negotiated-congestion re-probing (PathFinder v2 -- measure one cheap pass
first); diff pairs (route_diff has the same seam; port after the
single-ended verdict).
"""
from __future__ import annotations

import heapq
import time
from dataclasses import dataclass, field, replace
from typing import Dict, List, Optional, Tuple

import numpy as np

import env_knobs
from bresenham_utils import walk_line


def global_plan_knobs() -> dict:
    """Read-once knob family; copy so a caller mutating its dict cannot
    poison the cached values (the congestion2_knobs pattern)."""
    return dict(env_knobs.GLOBAL_PLAN)


@dataclass
class GlobalPlan:
    """Rough-pass output. rough_paths hold the SIMPLIFIED (gx, gy, layer)
    waypoint paths; reservations are ready-to-merge (N,4) int32 rows (fresh
    arrays, never mutated in place -- the _MERGE_MEMO id-based signature
    must see a new array if these are ever recomputed)."""
    rough_paths: Dict[int, List[Tuple[int, int, int]]] = field(default_factory=dict)
    reservations: Dict[int, np.ndarray] = field(default_factory=dict)
    via_sites: Dict[int, List[Tuple[int, int]]] = field(default_factory=dict)
    conflict_w: Dict[int, Dict[int, int]] = field(default_factory=dict)  # crossings
    share_w: Dict[int, Dict[int, int]] = field(default_factory=dict)     # corridor share
    probe_iterations: int = 0
    probe_failures: int = 0

    def demand_points(self, config) -> Dict[int, List[Tuple[float, float]]]:
        """Rough-path sample points in mm for congestion-v2 demand seeding,
        ~one per demand bin along each predicted corridor."""
        from congestion_field import congestion2_knobs
        bin_mm = max(0.25, congestion2_knobs()['bin'])
        stride = max(1, int(round(bin_mm / config.grid_step)))
        pts: Dict[int, List[Tuple[float, float]]] = {}
        for nid, path in self.rough_paths.items():
            out = []
            for (x1, y1, l1), (x2, y2, l2) in zip(path, path[1:]):
                for i, (gx, gy) in enumerate(walk_line(x1, y1, x2, y2)):
                    if i % stride == 0:
                        out.append((gx * config.grid_step,
                                    gy * config.grid_step))
            if path:
                out.append((path[-1][0] * config.grid_step,
                            path[-1][1] * config.grid_step))
            if out:
                pts[nid] = out
        return pts


def plan_global_routes(pcb_data, config, net_ids: List[Tuple[str, int]],
                       layer_map: Dict[str, int],
                       verbose: bool = False) -> Optional[GlobalPlan]:
    """Run the rough pass over net_ids ([(name, id)]). Returns None when the
    gate is off or there is nothing to probe; failed probes are counted and
    simply contribute no reservation and no ordering signal."""
    k = global_plan_knobs()
    if not k['enable'] or not net_ids:
        return None
    # Deferred imports: single_ended_routing must not import this module
    # (the bus_corridor convention), and routing_context imports
    # add_plan_source from here.
    from obstacle_map import build_base_obstacle_map
    from obstacle_costs import compute_ripped_route_costs
    from single_ended_routing import route_net_with_obstacles

    t0 = time.time()
    ids = [nid for _, nid in net_ids]
    print(f"\n=== Global planning pass (#589): rough-routing {len(ids)} "
          f"net(s) at h-weight {k['hweight']}, cap {k['iters']} ===")
    # Throwaway probe map excluding every net being routed: rough paths see
    # only permanent copper -- collisions with other FUTURE nets are free by
    # construction (never a dead end, never a rip).
    probe_map = build_base_obstacle_map(
        pcb_data, config, ids, net_clearances=config.net_clearances)
    rough_cfg = replace(config,
                        heuristic_weight=k['hweight'],
                        max_iterations=max(1, int(k['iters'])),
                        power_tap_neckdown=False)
    # The reservation stamp reuses the ripped-ghost machinery at the plan's
    # own knobs (a replace clone, never a shared-config mutation).
    res_cfg = replace(config,
                      ripped_route_avoidance_cost=k['cost'],
                      ripped_route_avoidance_radius=k['radius'])
    collar_rects = (_escape_collar_rects(config)
                    if k['zone_scale'] != 1.0 else None)

    plan = GlobalPlan()
    for _name, nid in net_ids:
        result = route_net_with_obstacles(pcb_data, nid, rough_cfg, probe_map)
        plan.probe_iterations += int((result or {}).get('iterations', 0) or 0)
        if not result or result.get('failed') or not result.get('path'):
            plan.probe_failures += 1
            continue
        plan.rough_paths[nid] = result['path']
        if k['cost'] > 0:
            rows, via_pos = compute_ripped_route_costs(result, res_cfg,
                                                       layer_map)
            rows = _scale_rows_in_collars(rows, collar_rects,
                                          k['zone_scale'])
            if len(rows):
                plan.reservations[nid] = rows
            if via_pos:
                plan.via_sites[nid] = via_pos

    plan.conflict_w, plan.share_w = _conflict_graphs(plan.rough_paths,
                                                     config)
    n_cross = sum(1 for w in plan.conflict_w.values() if w)
    n_share = sum(1 for w in plan.share_w.values() if w)
    res_cells = sum(len(r) for r in plan.reservations.values())
    print(f"Global plan: {len(plan.rough_paths)}/{len(ids)} rough routes in "
          f"{time.time() - t0:.1f}s ({plan.probe_iterations} probe "
          f"iterations, {plan.probe_failures} failed); "
          f"{len(plan.reservations)} corridor reservation(s) "
          f"({res_cells} cells at {k['cost']}mm-equiv), "
          f"{n_cross} net(s) with predicted crossings, "
          f"{n_share} sharing corridors")
    if verbose or env_knobs.GLOBAL_PLAN.get('debug'):
        name_of = {nid: nm for nm, nid in net_ids}
        hot = sorted(((sum(w.values()), nid)
                      for nid, w in plan.conflict_w.items() if w),
                     reverse=True)[:20]
        for total, nid in hot:
            parts = sorted(plan.conflict_w[nid].items(),
                           key=lambda t: -t[1])[:5]
            print(f"  [plan] {name_of.get(nid, nid)}: {total} crossing(s) "
                  f"with " + ", ".join(
                      f"{name_of.get(p, p)}({c})" for p, c in parts))
    return plan


def _escape_collar_rects(config):
    """Inclusive grid rects of the BGA escape collars (zone expanded by
    bga_proximity_radius) -- same geometry as obstacle_costs.
    proximity_max_zone_rects but independent of the composition mode (that
    helper is deliberately gated on zoned sum). None when no zones."""
    from routing_config import GridCoord
    coord = GridCoord(config.grid_step)
    if getattr(config, 'package_proximity_zones', None) is not None:
        zone_list = list(config.package_proximity_zones)
    else:
        zone_list = [(z[0], z[1], z[2], z[3], config.bga_proximity_radius)
                     for z in (config.bga_exclusion_zones or [])]
    if not zone_list:
        return None
    rects = []
    for min_x, min_y, max_x, max_y, radius_mm in zone_list:
        r = coord.to_grid_dist(radius_mm)
        gx0, gy0 = coord.to_grid(min_x, min_y)
        gx1, gy1 = coord.to_grid(max_x, max_y)
        rects.append((gx0 - r, gy0 - r, gx1 + r, gy1 + r))
    return rects


def _scale_rows_in_collars(rows: np.ndarray, rects, scale: float) -> np.ndarray:
    """Scale reservation cost inside the escape collars (zone_scale knob):
    corridors converge on a BGA's collar by necessity, so full-price
    reservations there tax every net's MANDATORY approach (#584's zoned
    lesson). scale 0 drops the rows entirely. Returns a fresh array (the
    merge memo keys on array identity)."""
    if rects is None or not len(rows):
        return rows
    inside = np.zeros(len(rows), dtype=bool)
    for gx0, gy0, gx1, gy1 in rects:
        inside |= ((rows[:, 1] >= gx0) & (rows[:, 1] <= gx1)
                   & (rows[:, 2] >= gy0) & (rows[:, 2] <= gy1))
    if not inside.any():
        return rows
    out = rows.copy()
    out[inside, 3] = (out[inside, 3].astype(np.float64) * scale).astype(
        np.int32)
    return out[out[:, 3] > 0]


def _path_segments(path):
    """Same-layer (x1, y1, x2, y2, layer) spans of a simplified path
    (layer-change steps contribute no lateral extent)."""
    segs = []
    for (x1, y1, l1), (x2, y2, l2) in zip(path, path[1:]):
        if l1 == l2 and (x1, y1) != (x2, y2):
            segs.append((x1, y1, x2, y2, l1))
    return segs


def _orient(ox, oy, ax, ay, bx, by):
    return (ax - ox) * (by - oy) - (ay - oy) * (bx - ox)


def _segments_cross(s, t):
    """Proper crossing of two integer segments (shared endpoints and
    collinear overlap -- a bus lane -- do NOT count)."""
    d1 = _orient(t[0], t[1], t[2], t[3], s[0], s[1])
    d2 = _orient(t[0], t[1], t[2], t[3], s[2], s[3])
    d3 = _orient(s[0], s[1], s[2], s[3], t[0], t[1])
    d4 = _orient(s[0], s[1], s[2], s[3], t[2], t[3])
    return (d1 != 0 and d2 != 0 and d3 != 0 and d4 != 0
            and (d1 > 0) != (d2 > 0) and (d3 > 0) != (d4 > 0))


def _conflict_graphs(rough_paths: Dict[int, List[Tuple[int, int, int]]],
                     config) -> Tuple[Dict[int, Dict[int, int]],
                                      Dict[int, Dict[int, int]]]:
    """Two pairwise weights over the predicted paths, one per ordering
    semantic:

    - crossings: count of same-layer PROPER polyline crossings -- the MPS
      conflict semantic measured on predicted paths instead of straight MST
      chords. Parallel corridor-sharing (a bus) is deliberately NOT a
      conflict here: neighbors that run alongside never force a detour, and
      weighting them scattered glasgow's bank buses.
    - share: shared same-layer corridor buckets (one track pitch) -- the
      DEMAND/capacity pressure metric for 'contended' mode, where parallel
      packing does compete for the same gap.

    Bucket co-occupancy prefilters the crossing tests (only co-located
    pairs are tested); integer orientation tests keep it exact and
    deterministic."""
    bucket = max(2, int(round((config.track_width + config.clearance)
                              / config.grid_step)))
    occupancy: Dict[Tuple[int, int, int], set] = {}
    for nid, path in rough_paths.items():
        cells = set()
        for (x1, y1, l1), (x2, y2, l2) in zip(path, path[1:]):
            if l1 != l2:
                continue  # via transition, no lateral extent
            for gx, gy in walk_line(x1, y1, x2, y2):
                cells.add((l1, gx // bucket, gy // bucket))
        for c in cells:
            occupancy.setdefault(c, set()).add(nid)
    share: Dict[int, Dict[int, int]] = {nid: {} for nid in rough_paths}
    candidates = set()
    for nets in occupancy.values():
        if len(nets) < 2:
            continue
        ordered = sorted(nets)
        for i, a in enumerate(ordered):
            sa = share[a]
            for b in ordered[i + 1:]:
                sa[b] = sa.get(b, 0) + 1
                share[b][a] = share[b].get(a, 0) + 1
                candidates.add((a, b))
    cross: Dict[int, Dict[int, int]] = {nid: {} for nid in rough_paths}
    segs = {nid: _path_segments(p) for nid, p in rough_paths.items()}
    for a, b in sorted(candidates):
        n = 0
        for s in segs[a]:
            for t in segs[b]:
                if s[4] == t[4] and _segments_cross(s, t):
                    n += 1
        if n:
            cross[a][b] = n
            cross[b][a] = n
    return cross, share


def _order_block(block: List[Tuple[str, int]], plan: GlobalPlan,
                 mode: str) -> List[Tuple[str, int]]:
    """Reorder one partition block by the plan's conflict graphs: 'planar'
    peels the CROSSING graph (MPS semantics on predicted paths -- parallel
    bus neighbors are not conflicts), 'contended' sorts by corridor-SHARE
    weight (demand pressure -- most-contended first). Nets the rough pass
    could not route carry no signal (weight 0): in 'planar' they lead (no
    known conflicts, like MPS's conflict-free nets), in 'contended' they
    trail. Incoming position is always the tiebreak, so the result is
    deterministic and degrades to the incoming order when the graph is
    empty."""
    if len(block) < 2:
        return block
    idx = {nid: i for i, (_, nid) in enumerate(block)}
    members = set(idx)
    # 'planar' peels crossings; 'share' peels the corridor-share graph
    # (glasgow wave 1's winning arm ran exactly this peel); 'contended'
    # sorts by descending share.
    src = plan.share_w if mode in ('contended', 'share') else plan.conflict_w
    w = {nid: {p: c for p, c in src.get(nid, {}).items() if p in members}
         for _, nid in block}
    if mode == 'contended':
        score = {nid: sum(w[nid].values()) for _, nid in block}
        return sorted(block, key=lambda t: (-score[t[1]], idx[t[1]]))
    # 'planar': min-degree peel -- repeatedly emit the net with the fewest
    # remaining predicted conflicts (lazy heap; stale entries re-pushed).
    deg = {nid: sum(w[nid].values()) for _, nid in block}
    heap = [(deg[nid], idx[nid], nid) for _, nid in block]
    heapq.heapify(heap)
    emitted = set()
    order: List[int] = []
    while heap:
        d, _i, nid = heapq.heappop(heap)
        if nid in emitted:
            continue
        if d != deg[nid]:
            heapq.heappush(heap, (deg[nid], _i, nid))
            continue
        emitted.add(nid)
        order.append(nid)
        for p, c in w[nid].items():
            if p not in emitted:
                deg[p] -= c
                heapq.heappush(heap, (deg[p], idx[p], p))
    name_of = {nid: name for name, nid in block}
    return [(name_of[nid], nid) for nid in order]


def apply_plan_order(net_ids: List[Tuple[str, int]], plan: GlobalPlan,
                     front_ids=None) -> List[Tuple[str, int]]:
    """Apply the plan-informed order, preserving the #472 direct-first
    partition: the front block and the rest are reordered independently and
    never merged. No-op when the order knob is '' or the plan is empty."""
    mode = global_plan_knobs()['order']
    if not mode or plan is None:
        return net_ids
    if not (plan.share_w if mode == 'contended' else plan.conflict_w):
        return net_ids
    front_ids = front_ids or set()
    front = [t for t in net_ids if t[1] in front_ids]
    rest = [t for t in net_ids if t[1] not in front_ids]
    reordered = _order_block(front, plan, mode) + _order_block(rest, plan, mode)
    moved = sum(1 for a, b in zip(net_ids, reordered) if a[1] != b[1])
    print(f"Global plan order ({mode}): {moved}/{len(net_ids)} net(s) "
          f"changed position" + (f" ({len(front)} direct-first net(s) "
                                 f"kept in front)" if front else ""))
    if env_knobs.GLOBAL_PLAN.get('debug'):
        print("  [plan] order head: "
              + ", ".join(nm for nm, _ in reordered[:20]))
        print("  [plan] order tail: "
              + ", ".join(nm for nm, _ in reordered[-10:]))
    return reordered


def add_plan_source(ghosts, config, net_id, routed_net_ids):
    """Fold the plan's corridor reservations into a
    merge_track_proximity_costs ghost dict (the add_history_source
    pattern; keys are ('plan', nid) tuples, which coexist with the int
    ripped-ghost keys exactly like ('history',)/('congestion2',)).

    Skips: the net being routed (owner exemption -- its own corridor must
    not repel it), nets already routed (their real copper and
    track-proximity entry supersede the prediction), and nets whose int key
    is already in the incoming dict (an ACTIVE ripped-route ghost -- the
    actual vacated corridor outranks the prediction, and sum mode would
    double-charge). Returns the input unchanged when the plan is off/empty.
    """
    plan = getattr(config, '_global_plan', None)
    if plan is None or not plan.reservations:
        return ghosts
    done = set(routed_net_ids or ())
    merged = dict(ghosts) if ghosts else {}
    for nid, rows in plan.reservations.items():
        if nid == net_id or nid in done or nid in merged:
            continue
        merged[('plan', nid)] = rows
    return merged or None
