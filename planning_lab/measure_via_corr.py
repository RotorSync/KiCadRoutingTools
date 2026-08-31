"""Per-net via-count predictability + layer-choice driver analysis."""
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


def pearson(xs, ys):
    n = len(xs)
    if n == 0:
        return float("nan")
    mx, my = sum(xs) / n, sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    return num / (dx * dy) if dx and dy else float("nan")


def spearman(xs, ys):
    def _rank(vals):
        order = sorted(range(len(vals)), key=lambda i: vals[i])
        ranks = [0] * len(vals)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and vals[order[j + 1]] == vals[order[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1
            for k in range(i, j + 1):
                ranks[order[k]] = avg
            i = j + 1
        return ranks
    return pearson(_rank(xs), _rank(ys))


def measure(board, tw=0.1, cl=0.1, via=0.3):
    pcb = parse_kicad_pcb(board)
    graphs = build_graphs(pcb, tw, cl)
    res = plan_board_multi(pcb, graphs, tw, cl, via_size=via)

    act_vias = {}
    for v in pcb.vias:
        if v.net_id > 0:
            act_vias[v.net_id] = act_vias.get(v.net_id, 0) + 1
    plan_vias = {np_.net_id: np_.via_count for np_ in res.nets}

    common = sorted(set(plan_vias) & set(act_vias))
    pv = [plan_vias[n] for n in common]
    av = [act_vias[n] for n in common]
    print(f"== {board} ==")
    print(f"via-count per net: plan mean={sum(pv)/len(pv):.2f} actual mean={sum(av)/len(av):.2f} "
          f"(n={len(common)})")
    print(f"via-count pearson={pearson(pv,av):.3f} spearman={spearman(pv,av):.3f}")
    # how many nets have zero planned vias but nonzero actual (and vice versa)
    zero_plan_nonzero_act = sum(1 for n in common if plan_vias[n] == 0 and act_vias[n] > 0)
    nonzero_plan_zero_act = sum(1 for n in common if plan_vias[n] > 0 and act_vias[n] == 0)
    print(f"nets plan=0 & actual>0: {zero_plan_nonzero_act}   nets plan>0 & actual=0: {nonzero_plan_zero_act}")

    # layer-choice driver: what fraction of planned nets have majority on a pad's own layer?
    from collections import Counter
    own_layer_maj = 0
    total = 0
    for np_ in res.nets:
        # find pads of this net
        net = pcb.nets.get(np_.net_id)
        if not net or not net.pads:
            continue
        pad_layers = set()
        for p in net.pads:
            pad_layers |= set(p.layers)
        # majority planned layer
        lens = {}
        pts = np_.path_pts
        for i in range(1, len(pts)):
            la, xa, ya = pts[i-1]
            lb, xb, yb = pts[i]
            if la == lb:
                lens[la] = lens.get(la, 0.0) + math.hypot(xb-xa, yb-ya)
        if not lens:
            continue
        maj = max(lens.items(), key=lambda kv: kv[1])[0]
        total += 1
        if maj in pad_layers:
            own_layer_maj += 1
    print(f"planned nets whose majority layer is a pad's own copper layer: "
          f"{own_layer_maj}/{total} ({own_layer_maj/max(1,total)*100:.1f}%)")


if __name__ == "__main__":
    for b in sys.argv[1:]:
        try:
            measure(b)
        except Exception as e:
            import traceback; traceback.print_exc()
