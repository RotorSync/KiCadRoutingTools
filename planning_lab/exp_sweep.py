"""Sweep via fixed_cost and congestion_threshold; report F.Cu + inner-layer r."""
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
    tw = float(sys.argv[2]) if len(sys.argv) > 2 else 0.1
    cl = float(sys.argv[3]) if len(sys.argv) > 3 else 0.1
    via_size = float(sys.argv[4]) if len(sys.argv) > 4 else 0.3
    pcb = parse_kicad_pcb(board)
    graphs = build_graphs(pcb, tw, cl)
    for fc in [50.0, 20.0, 10.0]:
        for th in [1, 2, 3]:
            t0 = time.time()
            res = plan_board_multi(pcb, graphs, tw, cl, via_size=via_size,
                                   fixed_cost=fc, congestion_threshold=th)
            dt = time.time() - t0
            val = validate_board(pcb, graphs, res)
            # collect per-layer pearson_all for layers with both planned and actual
            rows = []
            for layer, vl in val["per_layer"].items():
                if vl['total_actual_density'] == 0 and vl['total_planned_occ'] == 0:
                    continue
                rows.append((layer, vl['pearson_all'], vl['pearson_active'],
                             vl['total_planned_occ'], vl['total_actual_density']))
            fcu = next((r for r in rows if r[0] == 'F.Cu'), None)
            inner = [r for r in rows if r[0] != 'F.Cu' and r[1] == r[1]]  # non-nan
            inner_avg = sum(r[1] for r in inner) / len(inner) if inner else float('nan')
            fcu_pa = fcu[1] if fcu else float('nan')
            fcu_pa_act = fcu[2] if fcu else float('nan')
            print(f"fc={fc} th={th} t={dt:.1f}s vias={sum(n.via_count for n in res.nets)} "
                  f"F.Cu_pa={fcu_pa:.3f} F.Cu_pa_act={fcu_pa_act:.3f} inner_avg_pa={inner_avg:.3f} "
                  f"| " + " ".join(f"{l}:{pa:.2f}" for l, pa, _, _, _ in rows))


if __name__ == "__main__":
    main()
