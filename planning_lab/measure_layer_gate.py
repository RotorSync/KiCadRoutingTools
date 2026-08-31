"""Phase B layer-gate measurement: distribution + per-net agreement + vias.

For each board: run the multi-layer planner, then compare its LAYER CHOICES
against the real routed copper:
  - planned per-layer net distribution vs actual per-layer net distribution
    (the design-doc gate 3 metric), with total-variation distance
  - per-net agreement: does the plan's majority-copper layer for a net match
    the real router's majority-copper layer for that net?
  - via prediction: planned via_count vs actual vias per net

Layer majority is length-weighted copper on each side.
"""
import sys, time, math
sys.path.insert(0, "/home/austin/krt_work/py_router")
sys.path.insert(0, "/home/austin/krt_work/rust_router")
from kicad_parser import parse_kicad_pcb
from global_planner.capacity_graph import build_capacity_graph
from global_planner.multi_layer_planner import plan_board_multi


def keepout_polys_for_layer(board_info, layer):
    polys = []
    for k in board_info.keepouts:
        if layer in k.get("layers", set()):
            if k.get("tracks_allowed", True):
                continue
            polys.append(k["polygon"])
    return polys


def build_graphs(pcb, trace_width, clearance):
    bi = pcb.board_info
    bounds = bi.board_bounds
    graphs = {}
    for layer in bi.copper_layers:
        pads_on_layer = []
        for plist in pcb.pads_by_net.values():
            for p in plist:
                if layer in p.layers:
                    pads_on_layer.append(p)
        kpolys = keepout_polys_for_layer(bi, layer)
        graphs[layer] = build_capacity_graph(layer, pads_on_layer, kpolys,
                                             bounds, trace_width, clearance)
    return graphs


def planned_layer_lengths(plan):
    """net_id -> {layer: copper length} from path_pts (same-layer hops)."""
    out = {}
    for np_ in plan.nets:
        pts = np_.path_pts
        lens = {}
        for i in range(1, len(pts)):
            la, xa, ya = pts[i - 1]
            lb, xb, yb = pts[i]
            if la == lb:
                lens[la] = lens.get(la, 0.0) + math.hypot(xb - xa, yb - ya)
        out[np_.net_id] = lens
    return out


def actual_layer_lengths(pcb):
    """net_id -> {layer: copper length} from real segments."""
    out = {}
    for s in pcb.segments:
        d = math.hypot(s.end_x - s.start_x, s.end_y - s.start_y)
        out.setdefault(s.net_id, {}).setdefault(s.layer, 0.0)
        out[s.net_id][s.layer] += d
    return out


def majority_layer(lens):
    if not lens:
        return None
    return max(lens.items(), key=lambda kv: kv[1])[0]


def tv_distance(d1, d2):
    """Total variation distance over the union of keys."""
    keys = set(d1) | set(d2)
    s1 = sum(d1.values()) or 1.0
    s2 = sum(d2.values()) or 1.0
    return 0.5 * sum(abs(d1.get(k, 0) / s1 - d2.get(k, 0) / s2) for k in keys)


def measure(board, trace_width=0.1, clearance=0.1, via_size=0.3):
    t0 = time.time()
    pcb = parse_kicad_pcb(board)
    graphs = build_graphs(pcb, trace_width, clearance)
    t1 = time.time()
    res = plan_board_multi(pcb, graphs, trace_width, clearance,
                           via_size=via_size)
    t2 = time.time()
    plan_lens = planned_layer_lengths(res)
    act_lens = actual_layer_lengths(pcb)

    # actual vias per net
    act_vias = {}
    for v in pcb.vias:
        if v.net_id > 0:
            act_vias[v.net_id] = act_vias.get(v.net_id, 0) + 1
    plan_vias = {np_.net_id: np_.via_count for np_ in res.nets}

    # --- distribution gate: planned vs actual majority-layer per net ---
    plan_maj = {nid: majority_layer(l) for nid, l in plan_lens.items()}
    act_maj = {nid: majority_layer(l) for nid, l in act_lens.items()}

    # nets with real copper (the ones reality routed)
    routed_nets = [nid for nid, l in act_lens.items() if l]
    # nets planned AND routed
    both = [nid for nid in routed_nets if nid in plan_maj and plan_maj[nid]]

    from collections import Counter
    plan_dist = Counter(plan_maj[nid] for nid in both)
    act_dist = Counter(act_maj[nid] for nid in both)
    tv = tv_distance(dict(plan_dist), dict(act_dist))

    agree = sum(1 for nid in both if plan_maj[nid] == act_maj[nid])
    agree_rate = agree / len(both) if both else float("nan")

    # planned-but-never-routed nets (plan says copper where reality has none)
    planned_not_routed = [nid for nid in plan_maj if plan_maj[nid] and nid not in act_lens]

    # --- via prediction ---
    common_via = [nid for nid in plan_vias if nid in act_vias]
    via_errs = [plan_vias[nid] - act_vias[nid] for nid in common_via]
    via_mae = sum(abs(e) for e in via_errs) / len(via_errs) if via_errs else float("nan")
    via_bias = sum(via_errs) / len(via_errs) if via_errs else float("nan")
    tot_plan_via = sum(plan_vias.values())
    tot_act_via = sum(act_vias.values())

    print(f"== {board} ==  trace={trace_width} clearance={clearance} via={via_size}")
    print(f"parse+graphs {t1-t0:.2f}s  plan {t2-t1:.2f}s  TOTAL {t2-t0:.2f}s")
    print(f"nets planned={len(res.nets)}  nets with real copper={len(routed_nets)}"
          f"  both(planned&routed)={len(both)}")
    print(f"planned-but-never-routed nets={len(planned_not_routed)}")

    print("\n-- LAYER-DISTRIBUTION GATE (majority-copper layer per net) --")
    all_layers = sorted(set(list(plan_dist) + list(act_dist)))
    print(f"{'layer':<8}{'planned':>9}{'actual':>9}{'|p-a|':>9}")
    for L in all_layers:
        p_, a_ = plan_dist.get(L, 0), act_dist.get(L, 0)
        print(f"{L:<8}{p_:>9}{a_:>9}{abs(p_-a_):>9}")
    print(f"TV distance (normalized L1 over nets): {tv:.3f}")

    print("\n-- PER-NET LAYER AGREEMENT --")
    print(f"nets agreeing on majority layer: {agree}/{len(both)}  rate={agree_rate:.3f}")

    print("\n-- VIA PREDICTION --")
    print(f"planned vias total={tot_plan_via} ({tot_plan_via/max(1,len(res.nets)):.2f}/net)"
          f"  actual vias total={tot_act_via} ({tot_act_via/max(1,len(routed_nets)):.2f}/net)")
    print(f"per-net MAE={via_mae:.2f}  bias={via_bias:+.2f}  (over {len(common_via)} common nets)")

    return {"board": board, "both": len(both), "agree": agree,
            "agree_rate": agree_rate, "tv": tv,
            "plan_dist": dict(plan_dist), "act_dist": dict(act_dist),
            "tot_plan_via": tot_plan_via, "tot_act_via": tot_act_via,
            "via_mae": via_mae, "via_bias": via_bias,
            "planned_not_routed": len(planned_not_routed)}


if __name__ == "__main__":
    boards = sys.argv[1:]
    results = []
    for b in boards:
        try:
            results.append(measure(b))
        except Exception as e:
            import traceback; traceback.print_exc()
            print(b, "ERROR", type(e).__name__, e)
