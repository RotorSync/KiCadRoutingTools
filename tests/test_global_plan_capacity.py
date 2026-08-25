"""Unit tests for the capacity-graph math (Task 1).

The capacity math is tested directly via compute_capacity on hand-computable
fixtures; graph-level tests check structural invariants.
"""
import math
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'py_router'))

from global_planner.capacity_graph import build_capacity_graph, compute_capacity


class _Pad:
    def __init__(self, x, y, sx, sy, net=''):
        self.global_x = x
        self.global_y = y
        self.size_x = sx
        self.size_y = sy
        self.net_name = net
        self.layers = ['F.Cu']


def test_compute_capacity_hand_computed():
    # gap 3.0, trace 0.2 + clearance 0.3 = 0.5 -> floor(3.0/0.5) = 6
    assert compute_capacity(3.0, 0.2, 0.3) == 6
    # gap 1.0, sum 0.5 -> 2
    assert compute_capacity(1.0, 0.2, 0.3) == 2
    # gap 0.4, sum 0.5 -> 0 (not enough room for one trace)
    assert compute_capacity(0.4, 0.2, 0.3) == 0
    # exact fit: gap 1.0, sum 0.5 -> 2
    assert compute_capacity(1.0, 0.5, 0.5) == 1
    # zero/negative denominator -> 0
    assert compute_capacity(5.0, 0.0, 0.0) == 0


def test_compute_capacity_monotonic():
    # wider gap -> capacity never decreases
    prev = -1
    for gap in [0.1, 0.5, 1.0, 2.0, 5.0, 10.0]:
        c = compute_capacity(gap, 0.2, 0.3)
        assert c >= prev
        prev = c


def test_graph_capacities_nonnegative():
    pads = [_Pad(3, 5, 1, 1, 'A'), _Pad(7, 5, 1, 1, 'B')]
    g = build_capacity_graph('F.Cu', pads, [], (0, 0, 10, 10), 0.2, 0.3)
    assert g.num_nodes() > 0
    for c in g.node_capacity:
        assert isinstance(c, int) and c >= 0


def test_graph_nodes_are_edge_midpoints():
    pads = [_Pad(3, 5, 1, 1, 'A'), _Pad(7, 5, 1, 1, 'B')]
    g = build_capacity_graph('F.Cu', pads, [], (0, 0, 10, 10), 0.2, 0.3)
    for (nx, ny) in g.node_pos:
        found = False
        for i in range(len(g.points)):
            for j in range(i + 1, len(g.points)):
                mx = (g.points[i][0] + g.points[j][0]) / 2.0
                my = (g.points[i][1] + g.points[j][1]) / 2.0
                if abs(mx - nx) < 1e-9 and abs(my - ny) < 1e-9:
                    found = True
                    break
            if found:
                break
        assert found


def _node_pos_set(g):
    return {(round(x, 9), round(y, 9)): i for i, (x, y) in enumerate(g.node_pos)}


def test_edges_connect_within_triangle():
    pads = [_Pad(3, 5, 1, 1, 'A'), _Pad(7, 5, 1, 1, 'B')]
    g = build_capacity_graph('F.Cu', pads, [], (0, 0, 10, 10), 0.2, 0.3)
    pos_to_node = _node_pos_set(g)
    for u in range(g.num_nodes()):
        for v in g.neighbors(u):
            assert u in g.neighbors(v), "adjacency must be symmetric"
    for tri in g.triangles:
        a, b, c = tri
        edges = [(a, b), (b, c), (c, a)]
        tri_nodes = []
        for (p, q) in edges:
            mx = (g.points[p][0] + g.points[q][0]) / 2.0
            my = (g.points[p][1] + g.points[q][1]) / 2.0
            n = pos_to_node.get((round(mx, 9), round(my, 9)))
            if n is not None:
                tri_nodes.append(n)
        for i in range(len(tri_nodes)):
            for j in range(i + 1, len(tri_nodes)):
                assert tri_nodes[j] in g.neighbors(tri_nodes[i])


def test_keepout_blocks_triangles():
    pads = [_Pad(3, 5, 1, 1, 'A'), _Pad(7, 5, 1, 1, 'B')]
    keepout = [(4.0, 4.0), (6.0, 4.0), (6.0, 6.0), (4.0, 6.0)]
    g = build_capacity_graph('F.Cu', pads, [keepout], (0, 0, 10, 10), 0.2, 0.3)
    for (nx, ny) in g.node_pos:
        assert not (4.0 < nx < 6.0 and 4.0 < ny < 6.0)


def test_fallback_triangulation():
    from global_planner.capacity_graph import _simple_triangulation
    pts = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
    tris = _simple_triangulation(pts)
    assert len(tris) >= 2
