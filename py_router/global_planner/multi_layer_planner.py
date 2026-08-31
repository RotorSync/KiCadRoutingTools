"""Phase B Task 1: layer + via modeling for the CDT global planner.

Extends the Phase A single-layer planner with:
  - via edges joining adjacent copper-layer capacity graphs (a via consumes
    size-based capacity at its site on BOTH layers it spans, plus a fixed cost);
  - multi-pin nets planned as MST-ordered sequential 2-pin plans.

The result is a multi-layer PlanResult whose occupancy is keyed per
(layer, node_id), so validation can correlate planned occupancy against actual
copper density per layer -- and F.Cu stops being over-predicted because nets
can now route onto inner layers through vias.

Standalone prototype -- zero integration with the routing pipeline (the
ordering adapter lives in the engine, see route.py's planner_ordering kwarg).
"""
from __future__ import annotations

import heapq
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .capacity_graph import CapacityGraph


@dataclass
class ViaEdge:
    """A via connecting two nodes on adjacent copper layers."""
    layer_a: str
    node_a: int
    layer_b: str
    node_b: int
    x: float
    y: float
    fixed_cost: float


@dataclass
class MultiLayerGraph:
    """Per-copper-layer capacity graphs joined by via edges."""
    graphs: Dict[str, CapacityGraph]          # layer -> capacity graph
    via_edges: List[ViaEdge]                  # all via edges
    via_by_node: Dict[Tuple[str, int], List[int]] = field(default_factory=dict)  # (layer,node) -> via edge indices
    node_idx: Dict[str, dict] = field(default_factory=dict)  # layer -> grid index
    trace_width: float = 0.0
    clearance: float = 0.0
    via_size: float = 0.3

    def layers(self):
        return list(self.graphs.keys())


@dataclass
class NetPlan:
    """Corridor assignment for one net (2-pin or multi-pin)."""
    net_id: int
    net_name: str
    # list of (layer, node_id) hops through the multi-layer graph
    path: List[Tuple[str, int]]
    # list of (layer, x, y) points along the path for corridor geometry
    path_pts: List[Tuple[str, float, float]]
    length_mm: float = 0.0
    via_count: int = 0


@dataclass
class PlanResult:
    """Result of a multi-layer global plan."""
    nets: List[NetPlan]
    occupancy: Dict[str, Dict[int, int]]      # layer -> node_id -> count
    capacity: Dict[str, Dict[int, int]]       # layer -> node_id -> capacity
    overfull: Dict[str, List[int]]            # layer -> [node_id] occ > cap
    trace_width: float = 0.0
    clearance: float = 0.0
    via_size: float = 0.3


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


def build_via_edges(graphs: Dict[str, CapacityGraph],
                    via_size: float,
                    via_radius: Optional[float] = None,
                    fixed_cost: float = 50.0) -> List[ViaEdge]:
    """Build via edges between ADJACENT copper layers.

    For each adjacent layer pair (A, B), for each node on A find the nearest
    node on B within via_radius; that pair becomes a via edge at the midpoint.
    A via consumes capacity at both endpoint nodes (handled at routing time).

    Args:
        graphs: layer -> CapacityGraph (ordered top-to-bottom)
        via_size: via outer diameter in mm (used for capacity consumption)
        via_radius: max distance for a via to bridge two nodes; defaults to
            via_size * 1.5.
        fixed_cost: fixed cost added to a path for each via transition.
    """
    layers = list(graphs.keys())
    if via_radius is None:
        via_radius = via_size * 1.5
    edges: List[ViaEdge] = []
    for i in range(len(layers) - 1):
        la, lb = layers[i], layers[i + 1]
        ga, gb = graphs[la], graphs[lb]
        idx_b = _grid_index(gb.node_pos)
        for na in range(ga.num_nodes()):
            ax, ay = ga.node_pos[na]
            nb = _nearest_in_cell(idx_b, gb.node_pos, ax, ay)
            if nb is None:
                continue
            bx, by = gb.node_pos[nb]
            d = math.hypot(ax - bx, ay - by)
            if d > via_radius:
                continue
            edges.append(ViaEdge(la, na, lb, nb,
                                 (ax + bx) / 2.0, (ay + by) / 2.0,
                                 fixed_cost))
    return edges


def build_multi_layer_graph(graphs: Dict[str, CapacityGraph],
                            via_size: float = 0.3,
                            via_radius: Optional[float] = None,
                            fixed_cost: float = 50.0) -> MultiLayerGraph:
    """Build the multi-layer graph from per-layer capacity graphs."""
    via_edges = build_via_edges(graphs, via_size, via_radius, fixed_cost)
    via_by_node: Dict[Tuple[str, int], List[int]] = {}
    for ei, e in enumerate(via_edges):
        via_by_node.setdefault((e.layer_a, e.node_a), []).append(ei)
        via_by_node.setdefault((e.layer_b, e.node_b), []).append(ei)
    node_idx = {layer: _grid_index(g.node_pos) for layer, g in graphs.items()}
    return MultiLayerGraph(graphs=graphs, via_edges=via_edges,
                           via_by_node=via_by_node,
                           node_idx=node_idx,
                           trace_width=next(iter(graphs.values())).trace_width if graphs else 0.0,
                           clearance=next(iter(graphs.values())).clearance if graphs else 0.0,
                           via_size=via_size)


def _congestion_cost(occ: int, cap: int, base_len: float,
                     alpha: float = 2.0, overfull_penalty: float = 50.0) -> float:
    if cap <= 0:
        return base_len * (1.0 + overfull_penalty)
    ratio = occ / cap
    return base_len * (1.0 + alpha * ratio * ratio)


def _via_capacity_units(via_size: float, trace_width: float,
                        clearance: float) -> int:
    """Size-based capacity consumption of a via at its site on one layer."""
    denom = trace_width + clearance
    if denom <= 0:
        return 1
    return max(1, int(math.ceil(via_size / denom)))


def _nearest_node(graph: CapacityGraph, x: float, y: float,
                   idx: Optional[dict] = None) -> int:
    if idx is not None:
        n = _nearest_in_cell(idx, graph.node_pos, x, y)
        if n is not None:
            return n
    best = 0
    best_d = float("inf")
    for i, (nx, ny) in enumerate(graph.node_pos):
        d = (nx - x) ** 2 + (ny - y) ** 2
        if d < best_d:
            best_d = d
            best = i
    return best


def _dijkstra_ml(mg: MultiLayerGraph,
                 start_layer: str, start_node: int,
                 goal_layer: str, goal_node: int,
                 occupancy: Dict[str, Dict[int, int]],
                 alpha: float = 2.0,
                 overfull_penalty: float = 50.0,
                 via_units: int = 1,
                 fidelity_weight: float = 4.0,
                 fidelity_power: float = 2.0) -> Optional[List[Tuple[str, int]]]:
    """Congestion-aware shortest path through the multi-layer graph.

    State is (layer, node_id). Within-layer transitions use the per-layer
    congestion cost; via transitions add fixed_cost plus congestion at both
    endpoint nodes. Returns ordered list of (layer, node) hops inclusive of
    start and goal, or None.

    Path-fidelity term (fidelity_weight > 0): each within-layer edge is
    penalized by its deviation from the straight pad-to-pad chord -- the
    perpendicular distance of the edge midpoint from the chord line between
    the start and goal nodes -- scaled by edge length and raised to
    fidelity_power. A path that hugs the chord pays ~nothing extra; a path
    that detours off it pays proportionally to how far it wanders. This is
    the tunable balance against the congestion term (alpha): raise
    fidelity_weight to make plans hug straight lines, lower it to let them
    thread low-occupancy space.
    """
    start = (start_layer, start_node)
    goal = (goal_layer, goal_node)
    if start == goal:
        return [start]
    # chord from start to goal (2D projection of the straight pad-to-pad line)
    gs = mg.graphs[start_layer]
    gg = mg.graphs[goal_layer]
    ax_, ay_ = gs.node_pos[start_node]
    bx_, by_ = gg.node_pos[goal_node]
    chord_dx = bx_ - ax_
    chord_dy = by_ - ay_
    chord_len = math.hypot(chord_dx, chord_dy)
    if chord_len > 0:
        chord_nx = chord_dx / chord_len
        chord_ny = chord_dy / chord_len
    else:
        chord_nx = chord_ny = 0.0
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
        ulayer, unode = u
        g = mg.graphs[ulayer]
        ux, uy = g.node_pos[unode]
        # within-layer neighbors
        for vnode in g.neighbors(unode):
            vx, vy = g.node_pos[vnode]
            seg_len = math.hypot(vx - ux, vy - uy)
            occ = occupancy[ulayer].get(vnode, 0)
            cap = g.node_capacity[vnode]
            cost = _congestion_cost(occ, cap, seg_len, alpha, overfull_penalty)
            if fidelity_weight > 0.0 and seg_len > 0:
                # perpendicular distance of the edge midpoint from the chord
                mx = (ux + vx) / 2.0
                my = (uy + vy) / 2.0
                dev = abs((mx - ax_) * chord_ny - (my - ay_) * chord_nx)
                cost += fidelity_weight * seg_len * (dev ** fidelity_power)
            vstate = (ulayer, vnode)
            nd = d + cost
            if nd < dist.get(vstate, float("inf")):
                dist[vstate] = nd
                prev[vstate] = u
                heapq.heappush(pq, (nd, vstate))
        # via transitions out of this node on this layer
        for ei in mg.via_by_node.get((ulayer, unode), []):
            e = mg.via_edges[ei]
            if e.layer_a == ulayer and e.node_a == unode:
                other_layer, other_node = e.layer_b, e.node_b
            else:
                other_layer, other_node = e.layer_a, e.node_a
            og = mg.graphs[other_layer]
            occ_other = occupancy[other_layer].get(other_node, 0)
            cap_other = og.node_capacity[other_node]
            # congestion at the far endpoint + fixed cost
            cost_via = _congestion_cost(occ_other, cap_other,
                                        e.fixed_cost / max(1.0, mg.trace_width),
                                        alpha, overfull_penalty) + e.fixed_cost
            vstate = (other_layer, other_node)
            nd = d + cost_via
            if nd < dist.get(vstate, float("inf")):
                dist[vstate] = nd
                prev[vstate] = u
                heapq.heappush(pq, (nd, vstate))
    if goal not in dist:
        return None
    path = []
    cur = goal
    while cur != start:
        path.append(cur)
        cur = prev[cur]
    path.append(start)
    path.reverse()
    return path


def _pad_layers(pad):
    return set(pad.layers)


def _mst_over_pads(pads):
    """Minimum spanning tree over pads by Euclidean distance.

    Returns list of (pad_a_idx, pad_b_idx) MST edges sorted by weight ascending.
    """
    n = len(pads)
    if n <= 1:
        return []
    # Prim's algorithm from pad 0.
    in_tree = [False] * n
    min_d = [float("inf")] * n
    parent = [-1] * n
    min_d[0] = 0.0
    edges = []
    for _ in range(n):
        u = -1
        best = float("inf")
        for i in range(n):
            if not in_tree[i] and min_d[i] < best:
                best = min_d[i]
                u = i
        if u == -1:
            break
        in_tree[u] = True
        if parent[u] != -1:
            edges.append((parent[u], u))
        ux, uy = pads[u].global_x, pads[u].global_y
        for v in range(n):
            if in_tree[v]:
                continue
            vx, vy = pads[v].global_x, pads[v].global_y
            d = math.hypot(vx - ux, vy - uy)
            if d < min_d[v]:
                min_d[v] = d
                parent[v] = u
    # sort MST edges by weight ascending (shortest connections first)
    weighted = []
    for (a, b) in edges:
        ax, ay = pads[a].global_x, pads[a].global_y
        bx, by = pads[b].global_x, pads[b].global_y
        weighted.append((math.hypot(bx - ax, by - ay), a, b))
    weighted.sort(key=lambda t: t[0])
    return [(a, b) for (_, a, b) in weighted]


def _choose_start_layer(pads_a_layers, pads_b_layers,
                        mg: MultiLayerGraph,
                        occupancy: Dict[str, Dict[int, int]],
                        pa_x, pa_y, pb_x, pb_y):
    """Choose the layer with most free capacity at both pads' regions."""
    shared = sorted(pads_a_layers & pads_b_layers)
    candidates = shared or sorted(pads_a_layers | pads_b_layers)
    best_layer = None
    best_score = -1.0
    for layer in candidates:
        if layer not in mg.graphs:
            continue
        g = mg.graphs[layer]
        sa = _nearest_node(g, pa_x, pa_y)
        sb = _nearest_node(g, pb_x, pb_y)
        occ_a = occupancy[layer].get(sa, 0)
        occ_b = occupancy[layer].get(sb, 0)
        cap_a = g.node_capacity[sa]
        cap_b = g.node_capacity[sb]
        score = (cap_a - occ_a) + (cap_b - occ_b)
        if score > best_score:
            best_score = score
            best_layer = layer
    return best_layer


def _choose_pad_layer(pad_layers, mg: MultiLayerGraph,
                         occupancy: Dict[str, Dict[int, int]],
                         x, y, congestion_threshold: int = 1):
    """Choose the layer with most free capacity at this pad's region.

    Defaults to the pad's OWN copper layer while it still has room (>= the
    congestion_threshold free capacity units at the pad node) -- so most nets
    stay on F.Cu where their pads live. Only when the own layer is congested
    at the pad does the plan consider routing out onto another copper layer
    through a via at the pad. This is what lets load spread off a congested
    F.Cu onto inner layers (the Phase A funnel fix) without over-spreading.
    """
    own = set(pad_layers)
    # First try the pad's own copper layers.
    best_own = None
    best_own_free = -1
    for layer in sorted(own):
        if layer not in mg.graphs:
            continue
        g = mg.graphs[layer]
        s = _nearest_node(g, x, y, mg.node_idx.get(layer))
        occ = occupancy[layer].get(s, 0)
        cap = g.node_capacity[s]
        free = cap - occ
        if free > best_own_free:
            best_own_free = free
            best_own = layer
    if best_own is not None and best_own_free >= congestion_threshold:
        return best_own
    # Own layer congested -> consider all copper layers (via at pad reaches any).
    best_layer = None
    best_score = -1.0
    for layer in mg.graphs.keys():
        g = mg.graphs[layer]
        s = _nearest_node(g, x, y, mg.node_idx.get(layer))
        occ = occupancy[layer].get(s, 0)
        cap = g.node_capacity[s]
        score = cap - occ
        if layer in own:
            score += 0.5
        if score > best_score:
            best_score = score
            best_layer = layer
    return best_layer


def _route_two_pin(mg: MultiLayerGraph,
                   pad_a, pad_b,
                   occupancy: Dict[str, Dict[int, int]],
                   alpha: float,
                   via_units: int,
                   congestion_threshold: int = 1,
                   fidelity_weight: float = 4.0,
                   fidelity_power: float = 2.0) -> Optional[NetPlan]:
    """Route one 2-pin connection through the multi-layer graph.

    Start layer is chosen from pad A's actual copper layers; goal layer from
    pad B's actual copper layers. If they differ, the path must cross layers
    through vias -- which is exactly the layer/via model Phase B adds.
    """
    la_set = _pad_layers(pad_a)
    lb_set = _pad_layers(pad_b)
    start_layer = _choose_pad_layer(la_set, mg, occupancy,
                                    pad_a.global_x, pad_a.global_y,
                                    congestion_threshold)
    goal_layer = _choose_pad_layer(lb_set, mg, occupancy,
                                   pad_b.global_x, pad_b.global_y,
                                   congestion_threshold)
    if start_layer is None or goal_layer is None:
        return None
    g_start = mg.graphs[start_layer]
    g_goal = mg.graphs[goal_layer]
    sa = _nearest_node(g_start, pad_a.global_x, pad_a.global_y,
                        mg.node_idx.get(start_layer))
    sb = _nearest_node(g_goal, pad_b.global_x, pad_b.global_y,
                       mg.node_idx.get(goal_layer))
    path = _dijkstra_ml(mg, start_layer, sa, goal_layer, sb,
                        occupancy, alpha=alpha,
                        overfull_penalty=50.0 * (alpha / 2.0),
                        via_units=via_units,
                        fidelity_weight=fidelity_weight,
                        fidelity_power=fidelity_power)
    if path is None:
        return None
    # record occupancy along path nodes (each node on its own layer).
    # A via transition consumes size-based capacity at BOTH endpoint nodes:
    # a node that is a via endpoint gets via_units total (1 base + extra),
    # matching "via cost = size-based capacity consumption at the via site on
    # both layers it spans + a fixed cost".
    for i, state in enumerate(path):
        layer, node = state
        extra = 0
        if i > 0 and path[i - 1][0] != layer:
            extra += via_units - 1
        if i < len(path) - 1 and path[i + 1][0] != layer:
            extra += via_units - 1
        occupancy[layer][node] = occupancy[layer].get(node, 0) + 1 + extra
    # count vias and compute length + points
    pts: List[Tuple[str, float, float]] = []
    length = 0.0
    via_count = 0
    for i in range(len(path)):
        layer, node = path[i]
        x, y = mg.graphs[layer].node_pos[node]
        pts.append((layer, x, y))
        if i > 0:
            player, pnode = path[i - 1]
            px_, py_ = mg.graphs[player].node_pos[pnode]
            if player == layer:
                length += math.hypot(x - px_, y - py_)
            else:
                via_count += 1
                length += math.hypot(x - px_, y - py_) * 0.5
    return NetPlan(net_id=pad_a.net_id if hasattr(pad_a, 'net_id') else -1,
                   net_name=pad_a.net_name,
                   path=path,
                   path_pts=pts,
                   length_mm=length,
                   via_count=via_count)


def _all_nets(pcb):
    """Return list of (net_id, net_name, pads) for all nets with >=2 pads."""
    result = []
    for net_id, net in pcb.nets.items():
        pads = net.pads
        if len(pads) >= 2:
            result.append((net_id, net.name, pads))
    result.sort(key=lambda t: t[0])
    return result


def plan_board_multi(pcb,
                     graphs: Dict[str, CapacityGraph],
                     trace_width: float,
                     clearance: float,
                     via_size: float = 0.3,
                     alpha: float = 1.0,
                     fixed_cost: float = 50.0,
                     ripup_rounds: int = 1,
                     overfull_penalty: float = 50.0,
                     congestion_threshold: int = 2,
                     fast: bool = False,
                     fidelity_weight: float = 4.0,
                     fidelity_power: float = 2.0) -> PlanResult:
    """Globally route all nets (2-pin and multi-pin) through the multi-layer graph.

    Multi-pin nets are planned as MST-ordered sequential 2-pin plans.

    Args:
        pcb: parsed PCBData
        graphs: layer -> CapacityGraph (all copper layers)
        trace_width / clearance / via_size: routing geometry (mm)
        alpha: congestion exponent weight (default 1.0 tuned with the
            fidelity term in planning_lab/fidelity_findings.md; 0 disables
            congestion weighting entirely but also disables the capacity<=0
            obstacle penalty -- prefer a small positive value)
        fixed_cost: fixed cost per via transition
        ripup_rounds: number of rip-up/re-plan rounds for overfull edges
        overfull_penalty: extra cost multiplier for capacity-0 edges
        congestion_threshold: free-capacity units a pad's own layer must have
            before the plan keeps the net on it; below this the plan may route
            out onto another layer through a via at the pad.
        fidelity_weight: weight of the path-fidelity term (deviation from the
            straight pad-to-pad chord) vs the congestion term; 0 disables it.
            Default 4.0 is the value tuned in planning_lab/fidelity_findings.md
            (search over TRAIN boards, validated on HELD-OUT boards).
        fidelity_power: exponent applied to the perpendicular deviation before
            scaling by fidelity_weight (shape parameter; higher punishes large
            detours super-linearly). Default 2.0 (tuned).
    """
    mg = build_multi_layer_graph(graphs, via_size=via_size,
                                 fixed_cost=fixed_cost)
    nets = _all_nets(pcb)

    occupancy: Dict[str, Dict[int, int]] = {layer: {} for layer in graphs}
    via_units = _via_capacity_units(via_size, trace_width or mg.trace_width,
                                    clearance or mg.clearance)

    plans: List[NetPlan] = []

    def _route_one(net_id, net_name, pads):
        if len(pads) == 2:
            plan = _route_two_pin(mg, pads[0], pads[1], occupancy,
                                  alpha=alpha * (overfull_penalty / 50.0),
                                  via_units=via_units,
                                  congestion_threshold=congestion_threshold,
                                  fidelity_weight=fidelity_weight,
                                  fidelity_power=fidelity_power)
            if plan is not None:
                plan.net_id = net_id
                plan.net_name = net_name
            return plan
        # Fast mode (ordering): plan a multi-pin net as a single 2-pin
        # connection between its two most-distant pads -- a cheap congestion
        # proxy that avoids the full MST (many fewer Dijkstra calls).
        if fast:
            pa_f, pb_f = max(
                ((pads[a], pads[b]) for a in range(len(pads))
                 for b in range(a + 1, len(pads))),
                key=lambda ab: math.hypot(ab[0].global_x - ab[1].global_x,
                                          ab[0].global_y - ab[1].global_y))
            plan = _route_two_pin(mg, pa_f, pb_f, occupancy,
                                  alpha=alpha * (overfull_penalty / 50.0),
                                  via_units=via_units,
                                  congestion_threshold=congestion_threshold,
                                  fidelity_weight=fidelity_weight,
                                  fidelity_power=fidelity_power)
            if plan is not None:
                plan.net_id = net_id
                plan.net_name = net_name
            return plan
        # multi-pin: MST-ordered sequential 2-pin plans.
        mst_edges = _mst_over_pads(pads)
        sub_paths: List[List[Tuple[str, int]]] = []
        total_len = 0.0
        total_vias = 0
        pts_all: List[Tuple[str, float, float]] = []
        for (a_idx, b_idx) in mst_edges:
            pa_, pb_ = pads[a_idx], pads[b_idx]
            subplan = _route_two_pin(mg, pa_, pb_, occupancy,
                                     alpha=alpha * (overfull_penalty / 50.0),
                                     via_units=via_units,
                                     congestion_threshold=congestion_threshold,
                                     fidelity_weight=fidelity_weight,
                                     fidelity_power=fidelity_power)
            if subplan is None:
                continue
            sub_paths.append(subplan.path)
            total_len += subplan.length_mm
            total_vias += subplan.via_count
            pts_all.extend(subplan.path_pts)
        if not sub_paths:
            return None
        # merge into one NetPlan (path is concatenation of sub-paths)
        merged_path: List[Tuple[str, int]] = []
        for sp in sub_paths:
            merged_path.extend(sp)
        return NetPlan(net_id=net_id, net_name=net_name,
                       path=merged_path,
                       path_pts=pts_all,
                       length_mm=total_len,
                       via_count=total_vias)

    # Greedy pass in fixed net order.
    for net_id, net_name, pads in nets:
        plan = _route_one(net_id, net_name, pads)
        if plan is not None:
            plans.append(plan)

    # Rip-up / re-plan rounds for overfull edges.
    for _round in range(ripup_rounds):
        overfull_nodes: Dict[str, List[int]] = {}
        for layer in graphs:
            overfull_nodes[layer] = [n for n in range(graphs[layer].num_nodes())
                                     if occupancy[layer].get(n, 0) > graphs[layer].node_capacity[n]]
        if not any(overfull_nodes.values()):
            break
        overfull_set = {layer: set(overfull_nodes[layer]) for layer in graphs}
        affected_ids_set = set()
        affected_paths_by_id: Dict[int, List[List[Tuple[str, int]]]] = {}
        for p in plans:
            hits = [n for (layer_, n) in p.path if n in overfull_set.get(layer_, set())]
            if hits:
                affected_ids_set.add(p.net_id)
                affected_paths_by_id[p.net_id] = p.path

        remaining_ids_set = set(id(p) for p in plans)
        remaining_ids_set -= affected_ids_set

        # remove affected from occupancy and plans (rebuild occupancy from scratch is simpler)
        # Rebuild occupancy from remaining plans.
        new_occ: Dict[str, Dict[int, int]] = {layer: {} for layer in graphs}
        remaining_plans: List[NetPlan] = []
        for p in plans:
            if p.net_id in affected_ids_set:
                continue
            remaining_plans.append(p)
            for (layer_, node) in p.path:
                new_occ[layer_][node] = new_occ[layer_].get(node, 0) + 1

        # re-route affected with higher penalty.
        new_alpha = alpha * 2.0 * (overfull_penalty / 50.0)
        re_routed: List[NetPlan] = []
        for net_id in sorted(affected_ids_set):
            entry = next((e for e in nets if e[0] == net_id), None)
            if entry is None:
                continue
            _, net_name_, pads_ = entry
            plan2 = _route_one(net_id, net_name_, pads_)
            if plan2 is not None:
                re_routed.append(plan2)

        plans = remaining_plans + re_routed

        # rebuild occupancy from all plans after re-route round.
        occ_final: Dict[str, Dict[int, int]] = {layer: {} for layer in graphs}
        for p in plans:
            for (layer_, node) in p.path:
                occ_final[layer_][node] = occ_final[layer_].get(node, 0) + 1

    # Final occupancy from plans.
    occ_final2: Dict[str, Dict[int, int]] = {layer: {} for layer in graphs}
    for p in plans:
        for (layer_, node) in p.path:
            occ_final2[layer_][node] = occ_final2[layer_].get(node, 0) + 1

    overfull_final: Dict[str, List[int]] = {}
    capacity_map: Dict[str, Dict[int, int]] = {}
    for layer in graphs:
        g = graphs[layer]
        overfull_final[layer] = [n for n in range(g.num_nodes())
                                 if occ_final2[layer].get(n, 0) > g.node_capacity[n]]
        capacity_map[layer] = {n: g.node_capacity[n] for n in range(g.num_nodes())}

    return PlanResult(nets=plans,
                      occupancy=occ_final2,
                      capacity=capacity_map,
                      overfull=overfull_final,
                      trace_width=trace_width or mg.trace_width,
                      clearance=clearance or mg.clearance,
                      via_size=via_size)
