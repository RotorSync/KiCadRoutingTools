"""Unit tests for Phase B Task 1 (layer + via modeling) and Task 2 (ordering).

Covers:
  - via edges joining adjacent copper layers
  - cross-layer nets routed through vias (via capacity consumption on BOTH layers)
  - multi-pin nets planned as MST-ordered sequential 2-pin plans
  - plan-informed net ordering determinism
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'py_router'))

from global_planner.capacity_graph import build_capacity_graph
from global_planner.multi_layer_planner import (
    build_multi_layer_graph, build_via_edges, plan_board_multi,
    _via_capacity_units,
)
from global_planner.ordering import planner_net_order


class _Pad:
    def __init__(self, x, y, sx=1, sy=1, net='', layers=None, net_id=0):
        self.global_x = x
        self.global_y = y
        self.size_x = sx
        self.size_y = sy
        self.net_name = net
        self.net_id = net_id
        self.layers = layers or ['F.Cu']


class _Net:
    def __init__(self, net_id, name, pads):
        self.net_id = net_id
        self.name = name
        self.pads = pads


class _BoardInfo:
    def __init__(self, copper_layers, bounds):
        self.copper_layers = copper_layers
        self.board_bounds = bounds
        self.keepouts = []


class _PCB:
    def __init__(self, nets, bounds=(0, 0, 10, 10), copper_layers=('F.Cu', 'B.Cu')):
        self.nets = {n.net_id: n for n in nets}
        self.pads_by_net = {n.net_id: n.pads for n in nets}
        self.segments = []
        self.board_info = _BoardInfo(list(copper_layers), bounds)


def _build_graphs(pads_by_layer, bounds=(0, 0, 10, 10), tw=0.2, cl=0.3):
    graphs = {}
    for layer, pads in pads_by_layer.items():
        graphs[layer] = build_capacity_graph(layer, pads, [], bounds, tw, cl)
    return graphs


def test_via_edges_join_adjacent_layers():
    # Two layers with pads; via edges should connect nodes across them.
    pa = _Pad(3, 5, 1, 1, 'A', layers=['F.Cu'])
    pb = _Pad(7, 5, 1, 1, 'B', layers=['B.Cu'])
    graphs = _build_graphs({'F.Cu': [pa], 'B.Cu': [pb]})
    edges = build_via_edges(graphs, via_size=0.3)
    assert len(edges) > 0
    for e in edges:
        assert e.layer_a != e.layer_b
        assert {e.layer_a, e.layer_b} == {'F.Cu', 'B.Cu'}


def test_via_capacity_units_size_based():
    # via_size 0.3 / (trace 0.1 + clearance 0.1) = ceil(1.5) = 2
    assert _via_capacity_units(0.3, 0.1, 0.1) == 2
    # via_size 0.2 / 0.2 = 1
    assert _via_capacity_units(0.2, 0.1, 0.1) == 1


def test_cross_layer_net_routed_with_via():
    # Pad A on F.Cu only, pad B on B.Cu only -> must use a via.
    pa = _Pad(2, 5, 1, 1, 'N1', layers=['F.Cu'], net_id=1)
    pb = _Pad(8, 5, 1, 1, 'N1', layers=['B.Cu'], net_id=1)
    pcb = _PCB([_Net(1, 'N1', [pa, pb])], copper_layers=('F.Cu', 'B.Cu'))
    graphs = _build_graphs({'F.Cu': [pa], 'B.Cu': [pb]})
    res = plan_board_multi(pcb, graphs, 0.2, 0.3, via_size=0.3)
    assert len(res.nets) == 1
    plan = res.nets[0]
    assert plan.via_count >= 1
    # The path must touch both layers.
    layers_used = {layer for (layer, _node) in plan.path}
    assert 'F.Cu' in layers_used and 'B.Cu' in layers_used


def test_via_consumes_capacity_on_both_layers():
    # A cross-layer net's via should consume capacity at nodes on BOTH layers.
    pa = _Pad(2, 5, 1, 1, 'N1', layers=['F.Cu'], net_id=1)
    pb = _Pad(8, 5, 1, 1, 'N1', layers=['B.Cu'], net_id=1)
    pcb = _PCB([_Net(1, 'N1', [pa, pb])], copper_layers=('F.Cu', 'B.Cu'))
    graphs = _build_graphs({'F.Cu': [pa], 'B.Cu': [pb]})
    res = plan_board_multi(pcb, graphs, 0.2, 0.3, via_size=0.3)
    plan = res.nets[0]
    # Find a via transition in the path and check both endpoint nodes have occupancy.
    found_via_pair = False
    for i in range(1, len(plan.path)):
        prev_layer, prev_node = plan.path[i - 1]
        layer, node = plan.path[i]
        if prev_layer != layer:
            found_via_pair = True
            assert res.occupancy[prev_layer].get(prev_node, 0) >= 1
            assert res.occupancy[layer].get(node, 0) >= 1
            break
    assert found_via_pair


def test_multi_pin_mst_planning():
    # A 3-pin net should be planned (MST-ordered sequential 2-pin plans).
    pads = [
        _Pad(2, 5, 1, 1, 'N1', layers=['F.Cu'], net_id=1),
        _Pad(8, 5, 1, 1, 'N1', layers=['F.Cu'], net_id=1),
        _Pad(5, 8, 1, 1, 'N1', layers=['F.Cu'], net_id=1),
    ]
    pcb = _PCB([_Net(1, 'N1', pads)], copper_layers=('F.Cu',))
    graphs = _build_graphs({'F.Cu': pads})
    res = plan_board_multi(pcb, graphs, 0.2, 0.3)
    assert len(res.nets) == 1
    plan = res.nets[0]
    assert len(plan.path) >= 4  # at least two sub-paths joined


def test_plan_deterministic():
    pa = _Pad(2, 5, 1, 1, 'N1', layers=['F.Cu'], net_id=1)
    pb = _Pad(8, 5, 1, 1, 'N1', layers=['B.Cu'], net_id=1)
    pc = _Pad(5, 2, 1, 1, 'N2', layers=['F.Cu'], net_id=2)
    pd = _Pad(5, 8, 1, 1, 'N2', layers=['B.Cu'], net_id=2)
    pcb = _PCB([_Net(1, 'N1', [pa, pb]), _Net(2, 'N2', [pc, pd])],
               copper_layers=('F.Cu', 'B.Cu'))
    graphs = _build_graphs({'F.Cu': [pa, pc], 'B.Cu': [pb, pd]})
    r1 = plan_board_multi(pcb, graphs, 0.2, 0.3)
    r2 = plan_board_multi(pcb, graphs, 0.2, 0.3)
    assert [p.path for p in r1.nets] == [p.path for p in r2.nets]
    assert r1.occupancy == r2.occupancy


def test_ordering_deterministic():
    pa = _Pad(2, 5, 1, 1, 'N1', layers=['F.Cu'], net_id=1)
    pb = _Pad(8, 5, 1, 1, 'N1', layers=['B.Cu'], net_id=1)
    pc = _Pad(5, 2, 1, 1, 'N2', layers=['F.Cu'], net_id=2)
    pd = _Pad(5, 8, 1, 1, 'N2', layers=['B.Cu'], net_id=2)
    pcb = _PCB([_Net(1, 'N1', [pa, pb]), _Net(2, 'N2', [pc, pd])],
               copper_layers=('F.Cu', 'B.Cu'))
    o1 = planner_net_order(pcb, 0.2, 0.3)
    o2 = planner_net_order(pcb, 0.2, 0.3)
    assert o1 == o2
    # Both nets present.
    assert {nid for _nm, nid in o1} == {1, 2}
