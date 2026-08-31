"""Corridor quality measurement: does each net's planned corridor contain its real copper?

For each board:
  1. Run the Phase B multi-layer planner (trace=0.1 clearance=0.1 via=0.3).
  2. For each net with both a plan and real copper:
     - containment_2d[W]: fraction of actual track length within W mm of the
       corridor polyline projected to 2D (isolates geometry quality from the
       known-bad layer model).
     - containment_layer[W]: fraction of actual track length within W mm of the
       corridor sub-polyline ON THE SAME LAYER as each track (isolates how much
       damage the broken layer model does).
  3. Search-space ratio at width W: corridor tube area / (board area * n_layers).
  4. Failure-mode stats at the honest width.
"""
import sys, time, math, json
sys.path.insert(0, "/home/austin/krt_work/py_router")
sys.path.insert(0, "/home/austin/krt_work/rust_router")
from kicad_parser import parse_kicad_pcb
from global_planner.capacity_graph import build_capacity_graph
from global_planner.multi_layer_planner import plan_board_multi

WIDTHS = [0.25, 0.5, 1.0, 2.0, 3.0, 4.0]
SAMPLE_STEP = 0.2

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
    """segs: list of (x0,y0,x1,y1); pts: list of (x,y). Fraction of length within W."""
    if len(pts) < 2:
        return 0.0 if segs else float("nan")
    inside = 0.0
    total = 0.0
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

def measure(board):
    t0 = time.time()
    pcb = parse_kicad_pcb(board)
    graphs = build_graphs(pcb, 0.1, 0.1)
    t1 = time.time()
    res = plan_board_multi(pcb, graphs, 0.1, 0.1, via_size=0.3)
    t2 = time.time()

    bi = pcb.board_info
    minx, miny, maxx, maxy = bi.board_bounds
    board_area = (maxx - minx) * (maxy - miny)
    n_layers = len(bi.copper_layers)
    full_area = board_area * n_layers

    # actual copper per net: segments grouped by layer
    act_segs_by_net_layer = {}
    act_len_by_net = {}
    for s in pcb.segments:
        nid = s.net_id
        if nid <= 0:
            continue
        d = math.hypot(s.end_x - s.start_x, s.end_y - s.start_y)
        act_segs_by_net_layer.setdefault(nid, {}).setdefault(s.layer,
            []).append((s.start_x, s.start_y, s.end_x, s.end_y))
        act_len_by_net[nid] = act_len_by_net.get(nid, 0.0) + d

    # planned corridors per net: path_pts grouped by layer + full 2D
    plan_by_net = {}
    for np_ in res.nets:
        pts_by_layer = {}
        pts_2d = []
        for (layer_, x_, y_) in np_.path_pts:
            pts_by_layer.setdefault(layer_, []).append((x_, y_))
            pts_2d.append((x_, y_))
        plan_by_net[np_.net_id] = {
            "name": np_.net_name,
            "pts_by_layer": pts_by_layer,
            "pts_2d": pts_2d,
            "len_2d": polyline_len(pts_2d),
            "vias": np_.via_count,
        }

    nets_out = []
    routed_nets = [nid for nid in act_len_by_net if act_len_by_net[nid] > 0]
    for nid in routed_nets:
        plan = plan_by_net.get(nid)
        if plan is None:
            nets_out.append({"net_id": nid, "name": "?", "planned": False})
            continue
        # all actual segments flattened to 2D
        all_segs = []
        for L in act_segs_by_net_layer.get(nid, {}).values():
            all_segs.extend(L)
        c2d = {}
        for W in WIDTHS:
            c2d[W] = sample_containment(all_segs, plan["pts_2d"], W)
        # per-layer containment: each track vs corridor sub-polyline on its layer
        clayer = {}
        for W in WIDTHS:
            ins = tot = 0.0
            for layer_, segs in act_segs_by_net_layer.get(nid, {}).items():
                subpts = plan["pts_by_layer"].get(layer_, [])
                if len(subpts) < 2:
                    # no corridor on this layer -> nothing contained
                    for (x0,y0,x1,y1) in segs:
                        tot += math.hypot(x1-x0,y1-y0)
                    continue
                f = sample_containment(segs, subpts, W)
                seglen = sum(math.hypot(x1-x0,y1-y0) for (x0,y0,x1,y1) in segs)
                ins += f * seglen
                tot += seglen
            clayer[W] = ins / tot if tot > 0 else float("nan")
        nets_out.append({
            "net_id": nid,
            "name": plan["name"],
            "planned": True,
            "act_len": act_len_by_net[nid],
            "plan_len_2d": plan["len_2d"],
            "plan_vias": plan["vias"],
            "c2d": c2d,
            "clayer": clayer,
        })

    result = {
        "board": board,
        "plan_s": round(t2 - t1, 3),
        "total_s": round(t2 - t0, 3),
        "board_area": board_area,
        "n_layers": n_layers,
        "full_area": full_area,
        "nets_routed": len(routed_nets),
        "nets_planned": len(res.nets),
        "nets": nets_out,
    }
    return result

if __name__ == "__main__":
    boards = sys.argv[1:]
    out_all = []
    for b in boards:
        try:
            r = measure(b)
            out_all.append(r)
            print(f"== {b} == plan {r['plan_s']}s total {r['total_s']}s "
                  f"routed={r['nets_routed']} planned={r['nets_planned']} "
                  f"area={r['board_area']:.0f}mm2 x{r['n_layers']} layers", flush=True)
        except Exception as e:
            import traceback; traceback.print_exc()
            print(b, "ERROR", type(e).__name__, e)
    with open("/home/austin/krt_work/planning_lab/corridor_data.json", "w") as f:
        json.dump(out_all, f, indent=1)
    print("wrote planning_lab/corridor_data.json")
