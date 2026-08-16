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
    layer_pref: Dict[int, int] = field(default_factory=dict)  # nid -> layer idx
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
                       verbose: bool = False,
                       net_clearances=None) -> Optional[GlobalPlan]:
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
        pcb_data, config, ids,
        net_clearances=(net_clearances if net_clearances is not None
                        else config.net_clearances))
    rough_cfg = replace(config,
                        heuristic_weight=k['hweight'],
                        max_iterations=max(1, int(k['iters'])),
                        power_tap_neckdown=False,
                        plan_probe=True,
                        **({'via_cost': int(k['probe_via_cost'])}
                           if k['probe_via_cost'] > 0 else {}))
    # The reservation stamp reuses the ripped-ghost machinery at the plan's
    # own knobs (a replace clone, never a shared-config mutation).
    res_cfg = replace(config,
                      ripped_route_avoidance_cost=k['cost'],
                      ripped_route_avoidance_radius=k['radius'])
    collar_rects = (_escape_collar_rects(config)
                    if k['zone_scale'] != 1.0 else None)

    # #589 v2-lite (KICAD_GLOBAL_PLAN_SEQ=1): stamp each successful probe's
    # corridor into the SHARED probe map as a soft ghost before the next
    # probe runs -- one sequential negotiated-congestion pass. Blind probes
    # all pick the same cheapest layer (glasgow: 98.7% of probe length on
    # F.Cu vs the human board's 50%), so the demand map every downstream
    # signal (conflict graphs, cliques, layer assignment) consumes is fake;
    # sequential awareness makes probes spread the way real routing must.
    seq_cfg = (replace(config,
                       ripped_route_avoidance_cost=k['seq_cost'],
                       ripped_route_avoidance_radius=k['seq_radius'])
               if k['seq'] else None)
    if seq_cfg is not None:
        from obstacle_costs import merge_track_proximity_costs

    plan = GlobalPlan()
    max_iters = 0
    for _name, nid in net_ids:
        result = route_net_with_obstacles(pcb_data, nid, rough_cfg, probe_map)
        _it = int((result or {}).get('iterations', 0) or 0)
        plan.probe_iterations += _it
        max_iters = max(max_iters, _it)
        if not result or result.get('failed') or not result.get('path'):
            plan.probe_failures += 1
            continue
        plan.rough_paths[nid] = result['path']
        if seq_cfg is not None:
            seq_rows, _ = compute_ripped_route_costs(result, seq_cfg,
                                                     layer_map)
            if len(seq_rows):
                merge_track_proximity_costs(probe_map, {},
                                            ghost_costs={nid: seq_rows},
                                            config=rough_cfg)
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
    _assign_clique_layers(plan, config)
    n_cross = sum(1 for w in plan.conflict_w.values() if w)
    n_share = sum(1 for w in plan.share_w.values() if w)
    res_cells = sum(len(r) for r in plan.reservations.values())
    print(f"Global plan: {len(plan.rough_paths)}/{len(ids)} rough routes in "
          f"{time.time() - t0:.1f}s ({plan.probe_iterations} probe "
          f"iterations, {plan.probe_failures} failed); "
          f"{len(plan.reservations)} corridor reservation(s) "
          f"({res_cells} cells at {k['cost']}mm-equiv), "
          f"{n_cross} net(s) with predicted crossings, "
          f"{n_share} sharing corridors; heaviest probe {max_iters} "
          f"iters summed over its legs/directions (per-leg cap "
          f"{int(k['iters'])})")
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


# A pair shares a corridor (is a "corridor-mate") only above this many
# shared buckets; 1-2 bucket touches are incidental crossings/brushes and
# would daisy-chain the whole board into one clique.
MIN_SHARE_FOR_CLIQUE = 3


def _assign_clique_layers(plan: GlobalPlan, config) -> None:
    """#589 layer assignment: spread each corridor clique's members across
    the clique's viable layers, round-robin, so the corridor packs N layers
    deep instead of every member fighting for the probes' shared favorite
    layer (probes are mutually blind, so a bus's probes ALL pick the same
    best layer -- the one bias the ordering lever cannot fix).

    Cliques = connected components of the share graph at corridor-mate
    strength (>= MIN_SHARE_FOR_CLIQUE shared buckets). Viable layers = the
    union of the members' predicted-path layers (they are proven reachable
    for this corridor), ordered by total predicted usage, minus forbidden
    (negative-cost) layers. Singletons get NO assignment -- the router's
    own judgment is already fine for an uncontested net. Consumers:
    plan_layer_config (soft discount, 'layer' knob) and
    apply_plan_layer_swaps (stub moves, 'swaps' knob)."""
    k = global_plan_knobs()
    if not (k['layer'] or k['swaps']):
        return
    if k.get('layer_mode') == 'probe':
        # #589 'probe' mode: trust each net's OWN negotiated rough path --
        # its length-weighted majority layer IS the plan's layer choice
        # (SEQ probes spread like the human board; the clique round-robin
        # below exists to correct BLIND probes' one-layer pile-up).
        # Nets whose majority is their path's start layer get no
        # assignment (nothing to move).
        base_costs = list(config.layer_costs or []) or [1.0] * len(config.layers)
        while len(base_costs) < len(config.layers):
            base_costs.append(1.0)
        for nid, path in plan.rough_paths.items():
            h: Dict[int, int] = {}
            for (x1, y1, l1), (x2, y2, l2) in zip(path, path[1:]):
                if l1 == l2:
                    h[l1] = h.get(l1, 0) + max(abs(x2 - x1), abs(y2 - y1))
            if not h:
                continue
            maj = max(sorted(h.items()), key=lambda t: t[1])[0]
            if maj < len(base_costs) and base_costs[maj] >= 0 \
                    and maj != path[0][2]:
                plan.layer_pref[nid] = maj
        if plan.layer_pref:
            print(f"Global plan layer assignment (probe-majority): "
                  f"{len(plan.layer_pref)} net(s) prefer a non-start "
                  f"layer of their own rough path")
        return
    if not plan.share_w:
        return
    n_layers = len(config.layers)
    base_costs = list(config.layer_costs or []) or [1.0] * n_layers
    while len(base_costs) < n_layers:
        base_costs.append(1.0)
    allowed = {i for i in range(n_layers) if base_costs[i] >= 0}
    hist: Dict[int, Dict[int, int]] = {}
    for nid, path in plan.rough_paths.items():
        h: Dict[int, int] = {}
        for (x1, y1, l1), (x2, y2, l2) in zip(path, path[1:]):
            if l1 == l2:
                h[l1] = h.get(l1, 0) + max(abs(x2 - x1), abs(y2 - y1))
        hist[nid] = h
    adj = {nid: [p for p, w in ws.items() if w >= MIN_SHARE_FOR_CLIQUE]
           for nid, ws in plan.share_w.items()}
    seen: set = set()
    comps: List[List[int]] = []
    for nid in sorted(adj):
        if nid in seen:
            continue
        stack, comp = [nid], []
        seen.add(nid)
        while stack:
            a = stack.pop()
            comp.append(a)
            for b in adj.get(a, ()):
                if b not in seen:
                    seen.add(b)
                    stack.append(b)
        comps.append(sorted(comp))
    for comp in comps:
        if len(comp) < 2:
            continue
        totals: Dict[int, int] = {}
        for nid in comp:
            for l, c in hist.get(nid, {}).items():
                totals[l] = totals.get(l, 0) + c
        # Spread across ALL allowed layers, probe-used ones first (by
        # usage): probes are biased toward the cheapest layer, so a clique
        # whose probes all stayed on F.Cu would otherwise collapse to a
        # one-layer "spread" -- the exact bias this assignment exists to
        # break. Unprobed-but-allowed layers join at the tail; both levers
        # stay soft/validated, so a genuinely bad layer is refused
        # downstream (discounts never force, swaps must validate).
        used = [l for l, _ in sorted(totals.items(),
                                     key=lambda t: (-t[1], t[0]))
                if l in allowed]
        viable = used + [l for l in sorted(allowed) if l not in used]
        if not viable:
            continue
        for i, nid in enumerate(comp):
            plan.layer_pref[nid] = viable[i % len(viable)]
    if plan.layer_pref:
        sizes = sorted((len(c) for c in comps if len(c) >= 2), reverse=True)
        print(f"Global plan layer assignment: {len(plan.layer_pref)} net(s) "
              f"in {len(sizes)} clique(s) (sizes {sizes[:8]}"
              f"{'...' if len(sizes) > 8 else ''}) spread across their "
              f"corridors' viable layers")


def plan_layer_config(cfg_route, config, net_id):
    """#589 option 2 (KICAD_GLOBAL_PLAN_LAYER=pref): soft per-net layer
    preference -- scale the plan-assigned layer's step cost by
    KICAD_GLOBAL_PLAN_LAYER_DISCOUNT (< 1). Soft by construction: nothing
    gets MORE expensive, forbidden layers stay forbidden, and the router
    still defects for real obstacle-cost reasons. Returns cfg_route
    unchanged when the plan/knob is off or the net has no assignment --
    callers may wire it unconditionally (the SE loop does)."""
    plan = getattr(config, '_global_plan', None)
    if plan is None or not plan.layer_pref:
        return cfg_route
    if env_knobs.GLOBAL_PLAN.get('layer') != 'pref':
        return cfg_route
    li = plan.layer_pref.get(net_id)
    if li is None or li >= len(cfg_route.layers):
        return cfg_route
    d = env_knobs.GLOBAL_PLAN['layer_discount']
    if d == 1.0:
        return cfg_route
    base = list(cfg_route.layer_costs or []) or [1.0] * len(cfg_route.layers)
    while len(base) < len(cfg_route.layers):
        base.append(1.0)
    if base[li] < 0:
        return cfg_route
    base[li] = base[li] * d
    return replace(cfg_route, layer_costs=base)


def apply_plan_layer_swaps(pcb_data, config, plan: GlobalPlan,
                           net_ids: List[Tuple[str, int]],
                           all_segment_modifications: List,
                           all_swap_vias: List,
                           all_stubs_by_layer=None,
                           can_swap_to_top_layer: bool = True,
                           verbose: bool = False) -> int:
    """#589 option 1 (KICAD_GLOBAL_PLAN_SWAPS=1): move stub copper onto the
    plan-assigned layer BEFORE any obstacle map is built (the MPS-swap
    precedent: swaps mutate copper, so they must precede every map/cache
    build). Reuses the validated swap path end to end -- get_stub_info ->
    validate_single_swap -> apply_stub_layer_switch -> the #277/#299
    via-fit gate with revert. Each end is treated independently; an end
    already on the assigned layer is untouched, and a declined validation
    just leaves that end where it was (soft, like everything in the plan).
    """
    k = global_plan_knobs()
    if not k['swaps'] or plan is None or not plan.layer_pref:
        return 0
    from connectivity import get_net_endpoints
    from stub_layer_switching import (get_stub_info, apply_stub_layer_switch,
                                      revert_stub_layer_switch,
                                      validate_single_swap)
    from layer_swap_optimization import _swap_vias_fit_or_shrink
    from collections import Counter
    swaps = 0
    declined = 0
    decline_reasons: Counter = Counter()
    same_layer = 0
    no_stub = 0
    no_endpoints = 0
    stubs_by_layer = all_stubs_by_layer if all_stubs_by_layer is not None else {}
    for name, nid in net_ids:
        li = plan.layer_pref.get(nid)
        if li is None or li >= len(config.layers):
            continue
        target_layer = config.layers[li]
        if target_layer == 'F.Cu' and not can_swap_to_top_layer:
            continue
        sources, targets, error = get_net_endpoints(pcb_data, nid, config)
        if error or not sources or not targets:
            no_endpoints += 1
            if verbose:
                print(f"  [plan] swap skipped {name}: endpoints -- "
                      f"{error or 'empty side'}")
            continue
        for end in (sources, targets):
            cur_layer = config.layers[end[0][2]]
            if cur_layer == target_layer:
                same_layer += 1
                continue
            stub = get_stub_info(pcb_data, nid, end[0][3], end[0][4],
                                 cur_layer)
            if stub is None:
                no_stub += 1
                if verbose:
                    print(f"  [plan] swap skipped {name} "
                          f"{cur_layer}->{target_layer}: no stub copper at "
                          f"({end[0][3]:.2f}, {end[0][4]:.2f})")
                continue
            valid, reason = validate_single_swap(
                stub, target_layer, stubs_by_layer, pcb_data, config)
            if not valid:
                declined += 1
                decline_reasons[reason.split('(')[0].strip()[:60]] += 1
                if verbose:
                    print(f"  [plan] swap declined {name} "
                          f"{cur_layer}->{target_layer}: {reason}")
                continue
            new_vias, seg_mods = apply_stub_layer_switch(
                pcb_data, stub, target_layer, config, debug=verbose)
            if not _swap_vias_fit_or_shrink(pcb_data, new_vias, config):
                revert_stub_layer_switch(pcb_data, seg_mods, new_vias)
                declined += 1
                decline_reasons['pad via unfit'] += 1
                if verbose:
                    print(f"  [plan] swap reverted {name} "
                          f"{cur_layer}->{target_layer}: pad via unfit")
                continue
            all_swap_vias.extend(new_vias)
            all_segment_modifications.extend(seg_mods)
            swaps += 1
            if verbose:
                print(f"  Plan layer swap: {name} {cur_layer}->{target_layer}"
                      + (f" (+{len(new_vias)} via)" if new_vias else ""))
    if decline_reasons:
        print("  [plan] swap decline reasons: " + ", ".join(
            f"{r} x{c}" for r, c in decline_reasons.most_common(8)))
    print(f"Global plan stub swaps: {swaps} applied, {declined} declined, "
          f"{no_stub} end(s) without stub copper, {same_layer} already on "
          f"the assigned layer, {no_endpoints} net(s) without derivable "
          f"endpoints")
    return swaps


def apply_plan_escape_fanout(pcb_data, config, plan: GlobalPlan,
                             net_ids: List[Tuple[str, int]],
                             all_swap_vias: List,
                             all_swap_segments: List,
                             verbose: bool = False) -> int:
    """#589 escape fanout (KICAD_GLOBAL_PLAN_ESCAPE=1): dogbone each
    plan-assigned end that is still stuck on a non-assigned layer --
    an OFFSET via + pad->via trace found by tap_pad_with_escalation
    (via-in-pad clamp, fab-ladder rungs, fine-pitch escalation), placed
    BEFORE any obstacle map exists so every later build sees the copper.
    This is the human's escape-first idiom, and it reaches exactly the
    two buckets the stub-swap path cannot: bare pads (no stub to move)
    and pads where no via size fits at the pad CENTER (wave26: 53 of 66
    declines). Runs AFTER apply_plan_layer_swaps -- an end the swap
    already moved shows target-layer copper and is skipped.

    Commit protocol (the #292/#508 write-list lesson): via/segment
    OBJECTS append to pcb_data (obstacle maps + endpoint derivation) AND
    ride all_swap_vias / all_swap_segments (the writer and the oracle
    gate's model read those; nothing else carries this copper).
    A dogbone whose net later fails ships as connected same-net copper
    (pad-attached stub+via) -- visible, DRC-checked, and a candidate for
    a future unused-dogbone sweep; deliberately not silently removed."""
    k = global_plan_knobs()
    if not k['escape'] or plan is None or not plan.layer_pref:
        return 0
    from connectivity import get_net_endpoints
    from kicad_parser import Segment, Via
    from plane_pad_tap import tap_pad_with_escalation

    placed = 0
    no_pad = 0
    failed = 0
    already = 0
    thru = 0
    placed_via_dicts: List[dict] = []
    placed_seg_dicts: List[dict] = []
    for name, nid in net_ids:
        li = plan.layer_pref.get(nid)
        if li is None or li >= len(config.layers):
            continue
        target_layer = config.layers[li]
        sources, targets, error = get_net_endpoints(pcb_data, nid, config)
        if error or not sources or not targets:
            continue
        net = pcb_data.nets.get(nid)
        net_pads = net.pads if net else []
        for end in (sources, targets):
            cur_layer = config.layers[end[0][2]]
            if cur_layer == target_layer:
                already += 1
                continue
            ex, ey = end[0][3], end[0][4]
            # Skip ends that already own copper on the target layer nearby
            # (a stub swap or existing routing did the job).
            near = False
            for s in pcb_data.segments:
                if s.net_id != nid or s.layer != target_layer:
                    continue
                if (min(abs(s.start_x - ex), abs(s.end_x - ex)) < 2.0
                        and min(abs(s.start_y - ey),
                                abs(s.end_y - ey)) < 2.0):
                    near = True
                    break
            if near:
                already += 1
                continue
            pad = None
            for p in net_pads:
                if abs(p.global_x - ex) < 0.05 and abs(p.global_y - ey) < 0.05:
                    pad = p
                    break
            if pad is None:
                no_pad += 1
                continue
            if pad.drill > 0:
                thru += 1  # a barrel already reaches every layer
                continue
            res = tap_pad_with_escalation(
                pad, cur_layer, nid, pcb_data, config,
                max_search_radius=k['escape_radius'],
                via_size=config.via_size, via_drill=config.via_drill,
                extra_vias=placed_via_dicts,
                extra_segments=placed_seg_dicts,
                verbose=verbose, fine_for_all=True)
            if not res.success:
                failed += 1
                if verbose:
                    print(f"  [plan] escape declined {name} @{cur_layer} "
                          f"({ex:.2f},{ey:.2f}): via_blocked="
                          f"{res.via_blocked}")
                continue
            if res.via is not None:
                v = res.via
                via_obj = Via(x=v['x'], y=v['y'], size=v['size'],
                              drill=v['drill'],
                              layers=v.get('layers', ['F.Cu', 'B.Cu']),
                              net_id=nid)
                pcb_data.vias.append(via_obj)
                all_swap_vias.append(via_obj)
                placed_via_dicts.append(dict(v, net_id=nid))
            for s in res.segments:
                seg_obj = Segment(start_x=s['start'][0],
                                  start_y=s['start'][1],
                                  end_x=s['end'][0], end_y=s['end'][1],
                                  width=s['width'], layer=s['layer'],
                                  net_id=nid)
                pcb_data.segments.append(seg_obj)
                all_swap_segments.append(seg_obj)
                placed_seg_dicts.append(dict(s, net_id=nid))
            placed += 1
            if verbose:
                where = (f"via ({res.via['x']:.2f},{res.via['y']:.2f})"
                         if res.via else "reused via")
                print(f"  Plan escape fanout: {name} {cur_layer} pad "
                      f"({ex:.2f},{ey:.2f}) -> {where} "
                      f"[{res.params_label}]")
    print(f"Global plan escape fanout: {placed} dogbone(s) placed, "
          f"{failed} declined, {already} end(s) already served, "
          f"{thru} through-hole end(s), {no_pad} end(s) without a pad")
    return placed


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
    # (glasgow wave 1's winning arm ran exactly this peel); 'share_rev'
    # REVERSES that peel -- entangled cliques route FIRST with maximal free
    # space (still consecutive, the cascade survives reversal), loners last
    # (they find room anyway); 'contended' sorts by descending share
    # statically (no cascade -- glasgow wave 1: 8, tied baseline).
    src = (plan.share_w if mode in ('contended', 'share', 'share_rev')
           else plan.conflict_w)
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
    if mode == 'share_rev':
        order.reverse()
    name_of = {nid: name for name, nid in block}
    return [(name_of[nid], nid) for nid in order]


def apply_plan_order(net_ids: List[Tuple[str, int]], plan: GlobalPlan,
                     front_ids=None) -> List[Tuple[str, int]]:
    """Apply the plan-informed order, preserving the #472 direct-first
    partition: the front block and the rest are reordered independently and
    never merged. No-op when the order knob is '' or the plan is empty."""
    k = global_plan_knobs()
    # #589 escape-risk front-load (ORDER_FILE): listed nets first within
    # their partition block, independent of the graph-order modes below.
    if k.get('order_file') and plan is not None:
        try:
            listed = [ln.strip() for ln in open(k['order_file'])
                      if ln.strip()]
        except OSError as e:
            print(f"Global plan order file unreadable: {e}")
            listed = []
        if listed:
            rank = {n: i for i, n in enumerate(listed)}
            front_ids = front_ids or set()

            def _front_load(block):
                pri = [t for t in block if t[0] in rank]
                pri.sort(key=lambda t: rank[t[0]])
                return pri + [t for t in block if t[0] not in rank]
            fr = [t for t in net_ids if t[1] in front_ids]
            rest = [t for t in net_ids if t[1] not in front_ids]
            out = _front_load(fr) + _front_load(rest)
            n_hit = sum(1 for t in net_ids if t[0] in rank)
            print(f"Global plan order file: front-loaded {n_hit}/"
                  f"{len(listed)} listed net(s)")
            return out
    mode = k['order']
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


def dump_plan(path: str, plan: GlobalPlan, nets_in, nets_ordered,
              front_ids, config) -> None:
    """#589 offline-analysis dump (KICAD_GLOBAL_PLAN_DUMP): everything the
    plan-quality scorer needs to evaluate orders/layers WITHOUT re-running
    the route step -- rough paths, both conflict graphs, layer prefs, the
    order actually applied, the #472 front partition, and the config
    scalars the bucket geometry depends on. JSON keys become strings;
    consumers must int() net ids."""
    import json
    doc = {
        'nets_input': [[n, i] for n, i in nets_in],
        'order_used': [[n, i] for n, i in nets_ordered],
        'front_ids': sorted(front_ids or ()),
        'rough_paths': {str(nid): p for nid, p in plan.rough_paths.items()},
        'share_w': {str(a): {str(b): w for b, w in ws.items()}
                    for a, ws in plan.share_w.items()},
        'conflict_w': {str(a): {str(b): w for b, w in ws.items()}
                       for a, ws in plan.conflict_w.items()},
        'layer_pref': {str(nid): l for nid, l in plan.layer_pref.items()},
        'via_sites': {str(nid): v for nid, v in plan.via_sites.items()},
        'probe_iterations': plan.probe_iterations,
        'probe_failures': plan.probe_failures,
        'config': {
            'grid_step': config.grid_step,
            'track_width': config.track_width,
            'clearance': config.clearance,
            'layers': list(config.layers),
            'layer_costs': list(config.layer_costs or []),
            'via_size': config.via_size,
        },
        'knobs': global_plan_knobs(),
    }
    with open(path, 'w') as f:
        json.dump(doc, f)
    print(f"Global plan dump: wrote {path}")


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
