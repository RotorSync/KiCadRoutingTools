"""Via placement agreement vs distance threshold."""
import sys, math
sys.path.insert(0, "/home/austin/krt_work/py_router")
sys.path.insert(0, "/home/austin/krt_work/rust_router")
from kicad_parser import parse_kicad_pcb
from global_planner.capacity_graph import build_capacity_graph
from global_planner.multi_layer_planner import plan_board_multi


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


def measure(board, tw=0.1, cl=0.1, via=0.3):
    pcb = parse_kicad_pcb(board)
    graphs = build_graphs(pcb, tw, cl)
    res = plan_board_multi(pcb, graphs, tw, cl, via_size=via)

    act_vias = {}
    for v in pcb.vias:
        if v.net_id > 0:
            act_vias.setdefault(v.net_id, []).append((v.x, v.y))
    plan_via_pos = {np_.net_id: [] for np_ in res.nets}
    for np_ in res.nets:
        pts = np_.path_pts
        for i in range(1, len(pts)):
            if pts[i-1][0] != pts[i][0]:
                xa, ya = pts[i-1][1], pts[i-1][2]
                xb, yb = pts[i][1], pts[i][2]
                plan_via_pos[np_.net_id].append(((xa+xb)/2, (ya+yb)/2))

    print(f"== {board} ==")
    for thresh in [1.0, 2.0, 3.0, 5.0, 8.0]:
        ok = tot = 0
        for nid, avs in act_vias.items():
            pvs = plan_via_pos.get(nid, [])
            if not pvs:
                continue
            for (ax, ay) in avs:
                tot += 1
                if min(math.hypot(px-ax, py-ay) for (px, py) in pvs) <= thresh:
                    ok += 1
        print(f"  actual vias within {thresh}mm of a planned via (same net): "
              f"{ok}/{tot} ({ok/max(1,tot)*100:.1f}%)")


if __name__ == "__main__":
    for b in sys.argv[1:]:
        try:
            measure(b)
        except Exception as e:
            import traceback; traceback.print_exc()
