"""Task 3: validation of the global plan against real routed copper.

Measures how well the planner predicts reality on boards that already have real
routed copper:
  (a) correlation between planned edge occupancy and actual routed copper
      density in the same regions (bin actual segments into triangle edges);
  (b) top-N predicted-congested regions per board and whether actual routing
      shows density there;
  (c) runtime.

Standalone prototype -- zero integration with the routing pipeline.
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple

from .capacity_graph import CapacityGraph


def _grid_index(node_pos, cell=2.0):
    """Spatial hash: cell -> list of node ids."""
    idx = {}
    for i, (x, y) in enumerate(node_pos):
        key = (int(x // cell), int(y // cell))
        idx.setdefault(key, []).append(i)
    return idx


def _nearest_in_cell(idx, node_pos, x, y, cell=2.0):
    """Find nearest node to (x,y) using the grid index."""
    cx, cy = int(x // cell), int(y // cell)
    best = None
    best_d = float("inf")
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            for i in idx.get((cx + dx, cy + dy), []):
                nx, ny = node_pos[i]
                d = (nx - x) ** 2 + (ny - y) ** 2
                if d < best_d:
                    best_d = d
                    best = i
    return best


def bin_segments_to_nodes(graph: CapacityGraph, segments_on_layer,
                          sample_step: float = 1.0) -> Dict[int, int]:
    """Bin actual routed segments into triangle-edge nodes they cross.

    Samples points along each segment every ~sample_step mm and increments the
    nearest node's density. Returns {node_id: density}.
    """
    idx = _grid_index(graph.node_pos)
    density = {}
    for seg in segments_on_layer:
        sx, sy = seg.start_x, seg.start_y
        ex, ey = seg.end_x, seg.end_y
        length = math.hypot(ex - sx, ey - sy)
        n = max(1, int(length / sample_step))
        for k in range(n + 1):
            t = k / n
            x = sx + t * (ex - sx)
            y = sy + t * (ey - sy)
            node = _nearest_in_cell(idx, graph.node_pos, x, y)
            if node is not None:
                density[node] = density.get(node, 0) + 1
    return density


def pearson(xs, ys):
    """Pearson correlation coefficient between two equal-length lists."""
    n = len(xs)
    if n == 0:
        return float("nan")
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if dx == 0 or dy == 0:
        return float("nan")
    return num / (dx * dy)


def spearman(xs, ys):
    """Spearman rank correlation."""
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
    rx = _rank(xs)
    ry = _rank(ys)
    return pearson(rx, ry)


def validate_layer(graph: CapacityGraph, planned_occ: Dict[int, int],
                   segments_on_layer) -> Dict:
    """Validate one layer: correlate planned occupancy vs actual density."""
    density = bin_segments_to_nodes(graph, segments_on_layer)
    nodes = list(range(graph.num_nodes()))
    occ = [planned_occ.get(n, 0) for n in nodes]
    dens = [density.get(n, 0) for n in nodes]
    cap = [graph.node_capacity[n] for n in nodes]

    # correlation over nodes with capacity > 0 (routable passages)
    routable = [i for i in nodes if cap[i] > 0]
    occ_r = [occ[i] for i in routable]
    dens_r = [dens[i] for i in routable]

    # congestion ratio (planned)
    ratio = [occ[i] / cap[i] if cap[i] > 0 else 0.0 for i in nodes]

    # active nodes: used by either planned or actual
    active = [i for i in nodes if occ[i] > 0 or dens[i] > 0]
    occ_a = [occ[i] for i in active]
    dens_a = [dens[i] for i in active]

    return {
        "layer": graph.layer,
        "nodes": len(nodes),
        "routable_nodes": len(routable),
        "active_nodes": len(active),
        "pearson_all": pearson(occ, dens),
        "pearson_routable": pearson(occ_r, dens_r),
        "spearman_routable": spearman(occ_r, dens_r),
        "pearson_active": pearson(occ_a, dens_a),
        "spearman_active": spearman(occ_a, dens_a),
        "total_planned_occ": sum(occ),
        "total_actual_density": sum(dens),
        "density": density,
        "occupancy": dict(planned_occ),
        "capacity": {n: cap[n] for n in nodes},
        "node_pos": list(graph.node_pos),
        "ratio": ratio,
    }


def top_congested(graph: CapacityGraph, planned_occ: Dict[int, int],
                  density: Dict[int, int], top_n: int = 5) -> List[Dict]:
    """Top-N predicted-congested regions (highest occupancy/capacity ratio)."""
    items = []
    for n in range(graph.num_nodes()):
        cap = graph.node_capacity[n]
        if cap <= 0:
            continue
        occ = planned_occ.get(n, 0)
        ratio = occ / cap
        items.append({
            "node": n,
            "pos": graph.node_pos[n],
            "occupancy": occ,
            "capacity": cap,
            "ratio": ratio,
            "actual_density": density.get(n, 0),
        })
    items.sort(key=lambda d: (-d["ratio"], -d["occupancy"]))
    return items[:top_n]


def validate_board(pcb, graphs: Dict[str, CapacityGraph],
                   plan_result, top_n: int = 5) -> Dict:
    """Validate a whole board across layers."""
    per_layer = {}
    for layer, graph in graphs.items():
        segs = [s for s in pcb.segments if s.layer == layer]
        planned_occ = plan_result.occupancy.get(layer, {})
        vl = validate_layer(graph, planned_occ, segs)
        vl["top_congested"] = top_congested(graph, planned_occ,
                                            vl["density"], top_n)
        per_layer[layer] = vl
    return {"per_layer": per_layer}
