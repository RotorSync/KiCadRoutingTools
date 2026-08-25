"""Phase B Task 1 validation: multi-layer (via) planner vs real routed copper.

Same honest discipline as Phase A's run_validate.py, but the plan now routes
nets across layers through vias, so F.Cu should stop being over-predicted and
inner layers should gain planned occupancy.
"""
import sys, time
sys.path.insert(0, "/home/austin/krt_work/py_router")
sys.path.insert(0, "/home/austin/krt_work/rust_router")
from kicad_parser import parse_kicad_pcb
from global_planner.capacity_graph import build_capacity_graph
from global_planner.multi_layer_planner import plan_board_multi
from global_planner.validate import validate_board


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
        g = build_capacity_graph(layer, pads_on_layer, kpolys, bounds,
                                 trace_width, clearance)
        graphs[layer] = g
    return graphs


def main():
    board = sys.argv[1]
    trace_width = float(sys.argv[2]) if len(sys.argv) > 2 else 0.1
    clearance = float(sys.argv[3]) if len(sys.argv) > 3 else 0.1
    via_size = float(sys.argv[4]) if len(sys.argv) > 4 else 0.3
    t0 = time.time()
    pcb = parse_kicad_pcb(board)
    graphs = build_graphs(pcb, trace_width, clearance)
    t1 = time.time()
    res = plan_board_multi(pcb, graphs, trace_width, clearance,
                           via_size=via_size)
    t2 = time.time()
    val = validate_board(pcb, graphs, res)
    t3 = time.time()
    print(f"== {board} ==  trace={trace_width} clearance={clearance} via={via_size}")
    print(f"parse+graphs {t1-t0:.2f}s  plan {t2-t1:.2f}s  validate {t3-t2:.2f}s  TOTAL {t3-t0:.2f}s")
    print(f"nets planned: {len(res.nets)}  total vias planned: {sum(n.via_count for n in res.nets)}")
    for layer, vl in val["per_layer"].items():
        if vl['total_actual_density'] == 0 and vl['total_planned_occ'] == 0:
            print(f"  [{layer}] (no copper, no plan) skipped")
            continue
        print(f"  [{layer}] nodes={vl['nodes']} routable={vl['routable_nodes']} active={vl['active_nodes']} "
              f"pearson_all={vl['pearson_all']:.3f} pearson_routable={vl['pearson_routable']:.3f} "
              f"spearman_routable={vl['spearman_routable']:.3f} "
              f"pearson_active={vl['pearson_active']:.3f} spearman_active={vl['spearman_active']:.3f} "
              f"planned_occ={vl['total_planned_occ']} actual_dens={vl['total_actual_density']}")


if __name__ == "__main__":
    main()
