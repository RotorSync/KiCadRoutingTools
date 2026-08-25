"""Experiment: sweep via fixed_cost and report per-layer correlation + distribution."""
import sys
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
    tw = float(sys.argv[2]) if len(sys.argv) > 2 else 0.1
    cl = float(sys.argv[3]) if len(sys.argv) > 3 else 0.1
    via_size = float(sys.argv[4]) if len(sys.argv) > 4 else 0.3
    pcb = parse_kicad_pcb(board)
    graphs = build_graphs(pcb, tw, cl)
    for fc in [50.0, 20.0, 10.0, 5.0, 2.0]:
        res = plan_board_multi(pcb, graphs, tw, cl, via_size=via_size,
                               fixed_cost=fc)
        val = validate_board(pcb, graphs, res)
        print(f"== fixed_cost={fc}  nets={len(res.nets)} vias={sum(n.via_count for n in res.nets)}")
        for layer, vl in val["per_layer"].items():
            if vl['total_actual_density'] == 0 and vl['total_planned_occ'] == 0:
                continue
            print(f"  [{layer}] pa={vl['pearson_all']:.3f} pa_r={vl['pearson_routable']:.3f} "
                  f"pa_act={vl['pearson_active']:.3f} sp_act={vl['spearman_active']:.3f} "
                  f"occ={vl['total_planned_occ']} dens={vl['total_actual_density']}")


if __name__ == "__main__":
    main()
