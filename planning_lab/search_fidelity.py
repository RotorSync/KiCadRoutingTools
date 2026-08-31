"""Search path-fidelity parameters for the multi-layer planner.

Fitness = containment of real copper by planned corridors (the instrumented
metric). TRAIN boards are searched; HELD-OUT boards are never touched during
search and reported separately.

Search strategy: deterministic fitness (planner is deterministic), low dims
(3 params), ~40s/eval on train -> coarse grid to map the landscape, then
coordinate-descent refinement around the best cell.
"""
import sys, time, math, json, itertools
sys.path.insert(0, "/home/austin/krt_work/py_router")
sys.path.insert(0, "/home/austin/krt_work/rust_router")
from kicad_parser import parse_kicad_pcb
from global_planner.capacity_graph import build_capacity_graph
from global_planner.multi_layer_planner import plan_board_multi

WIDTHS = [0.25, 0.5, 1.0, 2.0]
SAMPLE_STEP = 0.2

TRAIN = [
    "kicad_files/rp2350_fpga_eensy_prePlane.kicad_pcb",
    "kicad_files/routed_output.kicad_pcb",
    "carrier_lab/routed.kicad_pcb",
]
HELDOUT = [
    "/home/austin/eda/kstudio-workspace/helisync-carrier/helisync-carrier.kicad_pcb",
    "carrier_lab/d1_routed.kicad_pcb",
]

_cache = {}


def keepout_polys_for_layer(bi, layer):
    polys = []
    for k in bi.keepouts:
        if layer in k.get("layers", set()):
            if k.get("tracks_allowed", True):
                continue
            polys.append(k["polygon"])
    return polys


def build_graphs(pcb, tw, cl):
    bi = pcb.board_info
    graphs = {}
    for layer in bi.copper_layers:
        pads_on_layer = [p for plist in pcb.pads_by_net.values() for p in plist
                         if layer in p.layers]
        graphs[layer] = build_capacity_graph(layer, pads_on_layer,
                                             keepout_polys_for_layer(bi, layer),
                                             bi.board_bounds, tw, cl)
    return graphs


def load_board(path):
    if path in _cache:
        return _cache[path]
    t0 = time.time()
    pcb = parse_kicad_pcb(path)
    graphs = build_graphs(pcb, 0.1, 0.1)
    # actual copper per net (2D flattened)
    act_segs_by_net = {}
    act_len_by_net = {}
    for s in pcb.segments:
        nid = s.net_id
        if nid <= 0:
            continue
        d = math.hypot(s.end_x - s.start_x, s.end_y - s.start_y)
        act_segs_by_net.setdefault(nid, []).append(
            (s.start_x, s.start_y, s.end_x, s.end_y))
        act_len_by_net[nid] = act_len_by_net.get(nid, 0.0) + d
    entry = {"pcb": pcb, "graphs": graphs,
             "act_segs_by_net": act_segs_by_net,
             "act_len_by_net": act_len_by_net,
             "load_s": time.time() - t0}
    _cache[path] = entry
    return entry


def point_seg_dist(px, py, ax, ay, bx, by):
    vx, vy = bx - ax, by - ay
    wx, wy = px - ax, py - ay
    c1 = vx * wx + vy * wy
    if c1 <= 0:
        return math.hypot(px - ax, py - ay)
    c2 = vx * vx + vy * vy
    if c2 <= c1:
        return math.hypot(px - bx, py - by)
    t = c1 / c2
    return math.hypot(px - (ax + t * vx), py - (ay + t * vy))


def point_polyline_dist(px, py, pts):
    best = float("inf")
    for i in range(len(pts) - 1):
        d = point_seg_dist(px, py, pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1])
        if d < best:
            best = d
    return best


def polyline_len(pts):
    L = 0.0
    for i in range(len(pts) - 1):
        L += math.hypot(pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1])
    return L


def sample_containment(segs, pts, W):
    if len(pts) < 2:
        return 0.0 if segs else float("nan")
    inside = total = 0.0
    for (x0, y0, x1, y1) in segs:
        L = math.hypot(x1 - x0, y1 - y0)
        if L <= 0:
            continue
        n = max(1, int(L / SAMPLE_STEP))
        w = L / n
        for k in range(n):
            t = (k + 0.5) / n
            px = x0 + t * (x1 - x0)
            py = y0 + t * (y1 - y0)
            total += w
            if point_polyline_dist(px, py, pts) <= W:
                inside += w
    return inside / total if total > 0 else float("nan")


def eval_board(path, params):
    """Run planner with params on one board; return per-net containment dict."""
    entry = load_board(path)
    pcb = entry["pcb"]
    graphs = entry["graphs"]
    res = plan_board_multi(pcb, graphs, 0.1, 0.1, via_size=0.3,
                           alpha=params["alpha"],
                           fidelity_weight=params["fidelity_weight"],
                           fidelity_power=params["fidelity_power"])
    plan_by_net = {}
    for np_ in res.nets:
        pts_2d = [(x_, y_) for (_l_, x_, y_) in np_.path_pts]
        plan_by_net[np_.net_id] = {
            "pts_2d": pts_2d,
            "len_2d": polyline_len(pts_2d),
        }
    nets_out = []
    for nid in entry["act_len_by_net"]:
        if entry["act_len_by_net"][nid] <= 0:
            continue
        plan = plan_by_net.get(nid)
        if plan is None:
            continue
        segs = entry["act_segs_by_net"].get(nid, [])
        c2d = {W: sample_containment(segs, plan["pts_2d"], W) for W in WIDTHS}
        nets_out.append({
            "net_id": nid,
            "act_len": entry["act_len_by_net"][nid],
            "plan_len": plan["len_2d"],
            "c2d": c2d,
        })
    return nets_out


def board_stats(nets_out):
    """Aggregate per-board stats from per-net containment."""
    stats = {}
    for W in WIDTHS:
        vals = [n["c2d"][W] for n in nets_out if n["c2d"][W] == n["c2d"][W]]
        if not vals:
            stats[f"median_{W}"] = float("nan")
            stats[f"frac80_{W}"] = float("nan")
            continue
        s = sorted(vals)
        med = s[len(s) // 2]
        stats[f"median_{W}"] = med
        stats[f"frac80_{W}"] = sum(1 for v in vals if v >= 0.8) / len(vals)
        stats[f"frac90_{W}"] = sum(1 for v in vals if v >= 0.9) / len(vals)
    ratios = [n["plan_len"] / n["act_len"] for n in nets_out if n["act_len"] > 0]
    s = sorted(ratios)
    stats["median_len_ratio"] = s[len(s) // 2] if s else float("nan")
    stats["mean_len_ratio"] = sum(ratios) / len(ratios) if ratios else float("nan")
    stats["n_nets"] = len(nets_out)
    return stats


def fitness(params):
    """Weighted frac80 across widths on TRAIN boards (smaller W weighted more)."""
    total_w = {0.5: 0.4, 1.0: 0.35, 2.0: 0.25}
    score = 0.0
    per_board = {}
    for b in TRAIN:
        st = board_stats(eval_board(b, params))
        s = sum(total_w[W] * st[f"frac80_{W}"] for W in total_w)
        per_board[b.split("/")[-1]] = {"score": s,
                                       **{k: st[k] for k in st}}
        score += s
    return score / len(TRAIN), per_board


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "grid"
    out_path = "/home/austin/krt_work/planning_lab/fidelity_search_results.json"
    results = []

    def record(params, fit_val, per_board):
        results.append({"params": params,
                        "fitness": fit_val,
                        "per_board": per_board})
        with open(out_path + ".tmp", "w") as f:
            json.dump(results[-20:], f)

    if mode == "grid":
        # coarse grid over the three params
        fw_grid = [0.0, 0.5, 1.0, 2.0, 4.0]
        fp_grid = [1.0, 2.0]
        al_grid = [1.0, 2.0]
        best = None
        best_fit = -1
        t_start = time.time()
        for fw in fw_grid:
            for fp in fp_grid:
                for al in al_grid:
                    params = {"fidelity_weight": fw,
                              "fidelity_power": fp,
                              "alpha": al}
                    fit_val, per_board = fitness(params)
                    record(params, fit_val, per_board)
                    tag = f"fw={fw} fp={fp} al={al}"
                    print(f"[{time.time()-t_start:6.1f}s] {tag} fit={fit_val:.4f}", flush=True)
                    if fit_val > best_fit:
                        best_fit = fit_val
                        best = params
        print("BEST:", best, best_fit)
        with open(out_path + ".grid", "w") as f:
            json.dump({"best": best, "best_fit": best_fit,
                       "results": results}, f)

    elif mode == "refine":
        # coordinate descent around a starting point
        start_fw = float(sys.argv[2]) if len(sys.argv) > 2 else 2.0
        start_fp = float(sys.argv[3]) if len(sys.argv) > 3 else 2.0
        start_al = float(sys.argv[4]) if len(sys.argv) > 4 else 2.0
        params = {"fidelity_weight": start_fw,
                  "fidelity_power": start_fp,
                  "alpha": start_al}
        fit_val, per_board = fitness(params)
        record(params, fit_val, per_board)
        print(f"start {params} fit={fit_val:.4f}", flush=True)
        improved = True
        step_fw = max(0.25, start_fw * 0.25)
        step_fp = max(0.25, start_fp * 0.25)
        step_al = max(0.25, start_al * 0.25)
        while improved:
            improved = False
            for key in ["fidelity_weight", "fidelity_power", "alpha"]:
                step = {"fidelity_weight": step_fw,
                        "fidelity_power": step_fp,
                        "alpha": step_al}[key]
                for delta in (+step, -step):
                    cand = dict(params)
                    cand[key] += delta
                    if cand[key] < 0:
                        continue
                    fv, pb = fitness(cand)
                    record(cand, fv, pb)
                    print(f"try {key}{delta:+.2f} -> {cand} fit={fv:.4f}", flush=True)
                    if fv > fit_val + 1e-6:
                        fit_val = fv
                        params = cand
                        per_board = pb
                        improved = True
                        print(f"ACCEPT {params} fit={fit_val:.4f}", flush=True)
                        break
        print("REFINED BEST:", params, fit_val)
        with open(out_path + ".refine", "w") as f:
            json.dump({"best": params, "best_fit": fit_val,
                       "results": results}, f)

    elif mode == "eval":
        # evaluate a specific param set on ALL boards (train + holdout)
        fw = float(sys.argv[2])
        fp = float(sys.argv[3])
        al = float(sys.argv[4])
        params = {"fidelity_weight": fw,
                  "fidelity_power": fp,
                  "alpha": al}
        out_all = {}
        for b in TRAIN + HELDOUT:
            st = board_stats(eval_board(b, params))
            out_all[b.split("/")[-1]] = st
            print(f"== {b.split('/')[-1]} == n={st['n_nets']} "
                  f"med@0.5={st['median_0.5']*100:.1f}% "
                  f"frac80@0.5={st['frac80_0.5']*100:.1f}% "
                  f"frac80@1={st['frac80_1.0']*100:.1f}% "
                  f"frac80@2={st['frac80_2.0']*100:.1f}% "
                  f"lenratio={st['median_len_ratio']:.2f}", flush=True)
        with open(out_path + ".eval", "w") as f:
            json.dump({"params": params,
                       "boards": out_all}, f)


if __name__ == "__main__":
    main()
