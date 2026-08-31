"""Deeper layer-gate analysis: presence + majority distributions, confusion
matrix, per-actual-layer agreement, non-F.Cu agreement, via placement.
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

    act_vias = {}
    for v in pcb.vias:
        if v.net_id > 0:
            act_vias.setdefault(v.net_id, []).append((v.x, v.y))
    plan_via_pos = {np_.net_id: [] for np_ in res.nets}
    for np_ in res.nets:
        pts = np_.path_pts
        for i in range(1, len(pts)):
            la = pts[i - 1][0]
            lb = pts[i][0]
            if la != lb:
                # via at midpoint of the transition
                xa, ya = pts[i - 1][1], pts[i - 1][2]
                xb, yb = pts[i][1], pts[i][2]
                plan_via_pos[np_.net_id].append(((xa + xb) / 2, (ya + yb) / 2))

    plan_maj = {nid: majority_layer(l) for nid, l in plan_lens.items()}
    act_maj = {nid: majority_layer(l) for nid, l in act_lens.items()}
    routed_nets = [nid for nid, l in act_lens.items() if l]
    both = [nid for nid in routed_nets if nid in plan_maj and plan_maj[nid]]

    from collections import Counter
    # presence-based distribution (nets touching each layer)
    plan_presence = Counter()
    act_presence = Counter()
    for nid in both:
        for L in plan_lens[nid]:
            plan_presence[L] += 1
        for L in act_lens[nid]:
            act_presence[L] += 1
    # majority-based distribution
    plan_dist = Counter(plan_maj[nid] for nid in both)
    act_dist = Counter(act_maj[nid] for nid in both)
    tv_maj = tv_distance(dict(plan_dist), dict(act_dist))
    tv_pres = tv_distance(dict(plan_presence), dict(act_presence))

    agree = sum(1 for nid in both if plan_maj[nid] == act_maj[nid])
    agree_rate = agree / len(both) if both else float("nan")

    # confusion matrix + per-actual-layer agreement
    layers = sorted(set(list(plan_dist) + list(act_dist)))
    conf = {a: Counter() for a in layers}
    for nid in both:
        conf[act_maj[nid]][plan_maj[nid]] += 1

    # agreement excluding F.Cu-majority nets (the non-trivial cases)
    non_fc = [nid for nid in both if act_maj[nid] != "F.Cu"]
    non_fc_agree = sum(1 for nid in non_fc if plan_maj[nid] == act_maj[nid])

    # --- via placement: nearest planned via to each actual via (same net) ---
    placed_ok = 0
    placed_tot = 0
    for nid, avs in act_vias.items():
        pvs = plan_via_pos.get(nid, [])
        if not pvs:
            continue
        for (ax, ay) in avs:
            placed_tot += 1
            best = min(math.hypot(px - ax, py - ay) for (px, py) in pvs)
            if best <= 2.0:  # within 2mm of a planned via of same net
                placed_ok += 1

    print(f"== {board} ==")
    print(f"parse+graphs {t1-t0:.2f}s  plan {t2-t1:.2f}s  TOTAL {t2-t0:.2f}s")
    print(f"nets planned={len(res.nets)} routed={len(routed_nets)} both={len(both)}")

    print("\n-- MAJORITY-layer distribution (nets whose longest copper is on L) --")
    print(f"{'layer':<8}{'planned':>9}{'actual':>9}{'|p-a|':>9}")
    for L in layers:
        p_, a_ = plan_dist.get(L, 0), act_dist.get(L, 0)
        print(f"{L:<8}{p_:>9}{a_:>9}{abs(p_-a_):>9}")
    print(f"TV(majority)={tv_maj:.3f}")

    print("\n-- PRESENCE distribution (nets with ANY copper on L) --")
    print(f"{'layer':<8}{'planned':>9}{'actual':>9}{'|p-a|':>9}")
    for L in layers:
        p_, a_ = plan_presence.get(L, 0), act_presence.get(L, 0)
        print(f"{L:<8}{p_:>9}{a_:>9}{abs(p_-a_):>9}")
    print(f"TV(presence)={tv_pres:.3f}")

    print("\n-- CONFUSION (rows=actual majority, cols=planned majority) --")
    hdr = "actual\\planned " + " ".join(f"{L:>8}" for L in layers) + f"{'agree':>7}"
    print(hdr)
    for a in layers:
        row = conf[a]
        ag = row.get(a, 0)
        tot = sum(row.values())
        cells = " ".join(f"{row.get(L,0):>8}" for L in layers)
        print(f"{a:<14}{cells}{ag:>7}/{tot}")

    print("\n-- PER-NET AGREEMENT --")
    print(f"all nets: {agree}/{len(both)} rate={agree_rate:.3f}")
    print(f"non-F.Cu-majority nets: {non_fc_agree}/{len(non_fc)} rate={non_fc_agree/max(1,len(non_fc)):.3f}")

    print("\n-- VIA PREDICTION --")
    tot_plan_via = sum(len(v) for v in plan_via_pos.values())
    tot_act_via = sum(len(v) for v in act_vias.values())
    print(f"planned vias={tot_plan_via} ({tot_plan_via/max(1,len(res.nets)):.2f}/net)  "
          f"actual vias={tot_act_via} ({tot_act_via/max(1,len(routed_nets)):.2f}/net)")
    print(f"via placement: {placed_ok}/{placed_tot} actual vias within 2mm of a planned via of same net "
          f"({placed_ok/max(1,placed_tot)*100:.1f}%)")


if __name__ == "__main__":
    for b in sys.argv[1:]:
        try:
            measure(b)
        except Exception as e:
            import traceback; traceback.print_exc()
            print(b, "ERROR", type(e).__name__, e)
