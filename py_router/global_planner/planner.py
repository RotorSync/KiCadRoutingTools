"""Task 2: global route all 2-pin connections through the capacity graph.

Congestion-aware shortest paths (edge cost grows as occupancy approaches
capacity), greedy with one rip-up/re-plan round for overfull edges. Outputs
per-net corridor assignments + per-edge occupancy. Deterministic (fixed net
order).

This is a standalone prototype -- zero integration with the routing pipeline.
"""
from __future__ import annotations

import heapq
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .capacity_graph import CapacityGraph


@dataclass
class NetPlan:
    """Corridor assignment for one 2-pin net."""
    net_id: int
    net_name: str
    layer: str
    path_nodes: List[int]          # ordered node ids through the graph
    path_pts: List[Tuple[float, float]]  # node positions along the path
    length_mm: float = 0.0


@dataclass
class PlanResult:
    """Result of a global plan: per-net corridors + per-edge occupancy."""
    nets: List[NetPlan]
    occupancy: Dict[str, Dict[int, int]]   # layer -> node_id -> count
    capacity: Dict[str, Dict[int, int]]    # layer -> node_id -> capacity
    overfull: Dict[str, List[int]]         # layer -> [node_id] with occ > cap
    trace_width: float = 0.0
    clearance: float = 0.0


def _nearest_node(graph: CapacityGraph, x: float, y: float) -> int:
    best = 0
    best_d = float("inf")
    for i, (nx, ny) in enumerate(graph.node_pos):
        d = (nx - x) ** 2 + (ny - y) ** 2
        if d < best_d:
            best_d = d
            best = i
    return best


def _congestion_cost(occ: int, cap: int, base_len: float,
                     alpha: float = 2.0, overfull_penalty: float = 50.0) -> float:
    """Edge cost grows as occupancy approaches capacity."""
    if cap <= 0:
        # No room at all: heavy penalty so it is used only as a last resort.
        return base_len * (1.0 + overfull_penalty)
    ratio = occ / cap
    return base_len * (1.0 + alpha * ratio * ratio)


def _dijkstra(graph: CapacityGraph, start: int, goal: int,
              occupancy: Dict[int, int], alpha: float = 2.0,
              overfull_penalty: float = 50.0) -> Optional[List[int]]:
    """Shortest path from start to goal with congestion-aware node costs.

    Returns ordered node list (inclusive of start and goal), or None.
    """
    if start == goal:
        return [start]
    n = graph.num_nodes()
    dist = {start: 0.0}
    prev = {}
    pq = [(0.0, start)]
    visited = set()
    while pq:
        d, u = heapq.heappop(pq)
        if u in visited:
            continue
        visited.add(u)
        if u == goal:
            break
        ux, uy = graph.node_pos[u]
        for v in graph.neighbors(u):
            if v in visited:
                continue
            vx, vy = graph.node_pos[v]
            seg_len = math.hypot(vx - ux, vy - uy)
            occ = occupancy.get(v, 0)
            cap = graph.node_capacity[v]
            cost = _congestion_cost(occ, cap, seg_len, alpha, overfull_penalty)
            nd = d + cost
            if nd < dist.get(v, float("inf")):
                dist[v] = nd
                prev[v] = u
                heapq.heappush(pq, (nd, v))
    if goal not in dist:
        return None
    # reconstruct
    path = []
    cur = goal
    while cur != start:
        path.append(cur)
        cur = prev[cur]
    path.append(start)
    path.reverse()
    return path


def _route_net(graph: CapacityGraph, pad_a, pad_b,
               occupancy: Dict[int, int], alpha: float) -> Optional[List[int]]:
    sa = _nearest_node(graph, pad_a.global_x, pad_a.global_y)
    sb = _nearest_node(graph, pad_b.global_x, pad_b.global_y)
    return _dijkstra(graph, sa, sb, occupancy, alpha)


def _two_pin_nets(pcb):
    """Return list of (net_id, net_name, pad_a, pad_b) for nets with exactly 2 pads."""
    result = []
    for net_id, net in pcb.nets.items():
        pads = net.pads
        if len(pads) == 2:
            result.append((net_id, net.name, pads[0], pads[1]))
    # deterministic order by net_id
    result.sort(key=lambda t: t[0])
    return result


def _shared_layers(pad_a, pad_b):
    """Copper layers both pads are on."""
    la = set(pad_a.layers)
    lb = set(pad_b.layers)
    return sorted(la & lb)


def plan_board(pcb, graphs: Dict[str, CapacityGraph],
               trace_width: float, clearance: float,
               alpha: float = 2.0,
               ripup_rounds: int = 1,
               overfull_penalty: float = 50.0) -> PlanResult:
    """Globally route all 2-pin nets through the per-layer capacity graphs.

    Greedy in fixed net order (by net_id), then one rip-up/re-plan round for
    overfull edges.

    Args:
        pcb: parsed PCBData
        graphs: layer name -> CapacityGraph
        trace_width / clearance: routing geometry (mm)
        alpha: congestion exponent weight
        ripup_rounds: number of rip-up/re-plan rounds for overfull edges
        overfull_penalty: extra cost multiplier for capacity-0 edges
    """
    nets = _two_pin_nets(pcb)

    # occupancy per layer
    occupancy = {layer: {} for layer in graphs}

    plans: List[NetPlan] = []

    def _route_one(net_id, net_name, pad_a, pad_b):
        layers = _shared_layers(pad_a, pad_b)
        if not layers:
            return None
        # choose layer with most free capacity at the pads' region; fall back to
        # first shared layer. Deterministic.
        best_layer = None
        best_score = -1.0
        for layer in layers:
            if layer not in graphs:
                continue
            g = graphs[layer]
            sa = _nearest_node(g, pad_a.global_x, pad_a.global_y)
            sb = _nearest_node(g, pad_b.global_x, pad_b.global_y)
            occ_a = occupancy[layer].get(sa, 0)
            occ_b = occupancy[layer].get(sb, 0)
            cap_a = g.node_capacity[sa]
            cap_b = g.node_capacity[sb]
            score = (cap_a - occ_a) + (cap_b - occ_b)
            if score > best_score:
                best_score = score
                best_layer = layer
        if best_layer is None:
            return None
        g = graphs[best_layer]
        path = _route_net(g, pad_a, pad_b, occupancy[best_layer], alpha)
        if path is None:
            return None
        # record occupancy along path nodes (skip endpoints? include all)
        for node in path:
            occupancy[best_layer][node] = occupancy[best_layer].get(node, 0) + 1
        pts = [g.node_pos[node] for node in path]
        length = 0.0
        for i in range(1, len(pts)):
            length += math.hypot(pts[i][0] - pts[i-1][0], pts[i][1] - pts[i-1][1])
        return NetPlan(net_id=net_id, net_name=net_name, layer=best_layer,
                       path_nodes=path, path_pts=pts, length_mm=length)

    # Greedy pass in fixed order.
    for net_id, net_name, pa, pb in nets:
        plan = _route_one(net_id, net_name, pa, pb)
        if plan is not None:
            plans.append(plan)

    # Rip-up / re-plan rounds for overfull edges.
    for _round in range(ripup_rounds):
        overfull = {}
        for layer in graphs:
            overfull[layer] = [n for n in range(graphs[layer].num_nodes())
                               if occupancy[layer].get(n, 0) > graphs[layer].node_capacity[n]]
        if not any(overfull.values()):
            break
        # Rip up nets that use any overfull edge and re-route with higher penalty.
        overfull_set = {layer: set(overfull[layer]) for layer in graphs}
        affected = [p for p in plans if any(n in overfull_set.get(p.layer, set()) for n in p.path_nodes)]
        # remove affected from occupancy and plans
        affected_ids = {id(p) for p in affected}
        remaining = [p for p in plans if id(p) not in affected_ids]
        for p in affected:
            for node in p.path_nodes:
                occupancy[p.layer][node] -= 1
                if occupancy[p.layer][node] <= 0:
                    del occupancy[p.layer][node]
        # re-route affected with higher penalty (alpha bump)
        new_alpha = alpha * 2.0
        new_penalty = overfull_penalty * 2.0
        re_routed = []
        for p in affected:
            # find the net's pads again by net_id
            entry = next((e for e in nets if e[0] == p.net_id), None)
            if entry is None:
                continue
            _, net_name, pa, pb = entry
            layers = _shared_layers(pa, pb)
            best_layer = None
            best_score = -1.0
            for layer in layers:
                if layer not in graphs:
                    continue
                g = graphs[layer]
                sa = _nearest_node(g, pa.global_x, pa.global_y)
                sb = _nearest_node(g, pb.global_x, pb.global_y)
                score = (g.node_capacity[sa] - occupancy[layer].get(sa, 0)) +                         (g.node_capacity[sb] - occupancy[layer].get(sb, 0))
                if score > best_score:
                    best_score = score
                    best_layer = layer
            if best_layer is None:
                continue
            g = graphs[best_layer]
            path = _dijkstra(g,
                             _nearest_node(g, pa.global_x, pa.global_y),
                             _nearest_node(g, pb.global_x, pb.global_y),
                             occupancy[best_layer], new_alpha, new_penalty)
            if path is None:
                continue
            for node in path:
                occupancy[best_layer][node] = occupancy[best_layer].get(node, 0) + 1
            pts = [g.node_pos[node] for node in path]
            length = sum(math.hypot(pts[i][0]-pts[i-1][0], pts[i][1]-pts[i-1][1])
                         for i in range(1, len(pts)))
            re_routed.append(NetPlan(net_id=p.net_id, net_name=p.net_name,
                                     layer=best_layer, path_nodes=path,
                                     path_pts=pts, length_mm=length))
        plans = remaining + re_routed

    # Final overfull report.
    overfull_final = {}
    for layer in graphs:
        overfull_final[layer] = [n for n in range(graphs[layer].num_nodes())
                                 if occupancy[layer].get(n, 0) > graphs[layer].node_capacity[n]]
    capacity_map = {layer: {n: graphs[layer].node_capacity[n]
                            for n in range(graphs[layer].num_nodes())}
                    for layer in graphs}

    return PlanResult(nets=plans,
                      occupancy=occupancy,
                      capacity=capacity_map,
                      overfull=overfull_final,
                      trace_width=trace_width,
                      clearance=clearance)
