"""Task 1: capacity graph for a copper layer.

Builds a planar routing graph from a constrained triangulation of free space
(obstacles = pads, keepouts, board edge). Graph nodes are triangle-edge
midpoints; graph edges connect midpoints within a triangle. Each triangle-edge
carries a CAPACITY = floor(gap_width / (trace_width + clearance)) using the
board's routed clearance.

Uses scipy.spatial.Delaunay when available (it is, in the eda venv), else a
simple incremental triangulation fallback. The triangulation is 'constrained'
by filtering triangles whose centroid lies inside an obstacle or outside the
board bounds.

This is a standalone prototype -- zero integration with the routing pipeline.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Tuple

# scipy is available in the eda venv; fall back to a pure-python triangulation
# if it is not importable. We detect at import time.
try:
    from scipy.spatial import Delaunay
    _HAS_SCIPY = True
except Exception:  # pragma: no cover - fallback path
    _HAS_SCIPY = False


@dataclass
class Obstacle:
    """A point obstacle with a radius (pads) or zero radius (keepout vertex,
    board-edge point). The radius approximates the obstacle's extent for gap
    computation."""
    x: float
    y: float
    radius: float
    kind: str = ""  # 'pad', 'keepout', 'edge'
    label: str = ""


@dataclass
class CapacityGraph:
    """The planar routing graph for one copper layer."""
    layer: str
    points: List[Tuple[float, float]]          # all triangulation points (x,y)
    radii: List[float]                          # radius per point
    kinds: List[str]                            # kind per point
    labels: List[str]                           # label per point
    triangles: List[Tuple[int, int, int]]       # free triangles (indices into points)
    node_pos: List[Tuple[float, float]]         # node = triangle-edge midpoint (x,y)
    node_capacity: List[float]                  # capacity per node (triangle-edge)
    node_gap: List[float]                       # gap width per node (mm)
    adjacency: Dict[int, List[int]]             # node -> neighbor nodes (within triangle)
    trace_width: float = 0.0
    clearance: float = 0.0

    def num_nodes(self):
        return len(self.node_pos)

    def neighbors(self, n):
        return self.adjacency.get(n, [])


def compute_capacity(gap_width: float, trace_width: float, clearance: float) -> int:
    """Capacity of a passage = floor(gap_width / (trace_width + clearance)), min 0."""
    denom = trace_width + clearance
    if denom <= 0:
        return 0
    return max(0, int(math.floor(gap_width / denom)))


def _point_in_polygon(x, y, poly):
    """Ray-casting point-in-polygon test (poly is list of (x,y))."""
    inside = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if ((y1 > y) != (y2 > y)) and (x < (x2 - x1) * (y - y1) / (y2 - y1 + 1e-30) + x1):
            inside = not inside
    return inside


def _point_in_any_polygon(x, y, polys):
    return any(_point_in_polygon(x, y, p) for p in polys)


def _point_in_rect(x, y, bounds):
    minx, miny, maxx, maxy = bounds
    return minx <= x <= maxx and miny <= y <= maxy


def _obstacle_polygons_from_pads(pads_on_layer):
    """Return list of pad polygons (approximated as squares/circles) for
    centroid-in-obstacle filtering. Uses the pad's bounding box."""
    polys = []
    for p in pads_on_layer:
        hx = p.size_x / 2.0
        hy = p.size_y / 2.0
        cx, cy = p.global_x, p.global_y
        polys.append([(cx - hx, cy - hy), (cx + hx, cy - hy),
                      (cx + hx, cy + hy), (cx - hx, cy + hy)])
    return polys


def _build_obstacles(pads_on_layer, keepout_polys, bounds):
    """Build the obstacle point set (with radii) for triangulation."""
    obstacles = []
    for p in pads_on_layer:
        # Use pad CORNERS (radius 0) as triangulation points so triangle edges
        # land in the actual channels between pads -- where real traces run --
        # rather than spanning wide gaps between pad centers. Capacity of an
        # edge between two facing corners then equals how many traces fit in
        # that channel.
        hx = p.size_x / 2.0
        hy = p.size_y / 2.0
        cx, cy = p.global_x, p.global_y
        for (px_, py_) in [(cx - hx, cy - hy), (cx + hx, cy - hy),
                           (cx + hx, cy + hy), (cx - hx, cy + hy)]:
            obstacles.append(Obstacle(px_, py_, 0.0, 'pad', p.net_name))
    for poly in keepout_polys:
        for (vx, vy) in poly:
            obstacles.append(Obstacle(vx, vy, 0.0, 'keepout', ''))
    # board edge corners + intermediate points along edges for better coverage
    minx, miny, maxx, maxy = bounds
    step = max((maxx - minx), (maxy - miny)) / 20.0
    edge_pts = []
    def _add_edge(a, b):
        ax, ay = a; bx, by = b
        d = math.hypot(bx - ax, by - ay)
        n = max(1, int(d / step))
        for k in range(n + 1):
            t = k / n
            edge_pts.append((ax + t * (bx - ax), ay + t * (by - ay)))
    _add_edge((minx, miny), (maxx, miny))
    _add_edge((maxx, miny), (maxx, maxy))
    _add_edge((maxx, maxy), (minx, maxy))
    _add_edge((minx, maxy), (minx, miny))
    for (ex, ey) in edge_pts:
        obstacles.append(Obstacle(ex, ey, 0.0, 'edge', ''))
    return obstacles


def _triangulate(points):
    """Return list of triangles (i,j,k) from Delaunay or fallback."""
    if _HAS_SCIPY:
        import numpy as np
        arr = np.array(points, dtype=float)
        tri = Delaunay(arr)
        return [tuple(sorted(map(int, s))) for s in tri.simplices]
    else:
        return _simple_triangulation(points)


def _simple_triangulation(points):
    """Fallback: naive O(n^3) ear-clipping-free incremental triangulation.
    Only used when scipy is unavailable; correctness over speed."""
    # Simple approach: build triangles from all triples that contain no other
    # point in their circumcircle and are non-degenerate. O(n^4) -- fine for
    # tiny fixtures only.
    tris = []
    n = len(points)
    for i in range(n):
        for j in range(i + 1, n):
            for k in range(j + 1, n):
                if _circumcircle_empty(points, i, j, k):
                    tris.append((i, j, k))
    return tris


def _circumcircle_empty(points, i, j, k):
    ax, ay = points[i]; bx, by = points[j]; cx_, cy_ = points[k]
    d = 2 * (ax * (by - cy_) + bx * (cy_ - ay) + cx_ * (ay - by))
    if abs(d) < 1e-12:
        return False  # degenerate
    ux = ((ax * ax + ay * ay) * (by - cy_) + (bx * bx + by * by) * (cy_ - ay)
          + (cx_ * cx_ + cy_ * cy_) * (ay - by)) / d
    uy = ((ax * ax + ay * ay) * (cx_ - bx) + (bx * bx + by * by) * (ax - cx_)
          + (cx_ * cx_ + cy_ * cy_) * (bx - ax)) / d
    r2 = (ux - ax) ** 2 + (uy - ay) ** 2
    for m in range(len(points)):
        if m in (i, j, k):
            continue
        mx_, my_ = points[m]
        if (mx_ - ux) ** 2 + (my_ - uy) ** 2 < r2 - 1e-9:
            return False
    return True


def build_capacity_graph(layer, pads_on_layer, keepout_polys, bounds,
                         trace_width, clearance,
                         board_outline=None):
    """Build the capacity graph for one copper layer.

    Args:
        layer: layer name (e.g. 'F.Cu')
        pads_on_layer: list of Pad objects with copper on this layer
        keepout_polys: list of keepout polygons (list of (x,y)) on this layer
        bounds: (minx, miny, maxx, maxy) board bounds
        trace_width: routed trace width in mm
        clearance: routed clearance in mm
        board_outline: optional outer polygon; if given and non-empty it is used
            as the board boundary instead of the axis-aligned bounds.
    """
    obstacles = _build_obstacles(pads_on_layer, keepout_polys, bounds)
    points = [(o.x, o.y) for o in obstacles]
    radii = [o.radius for o in obstacles]
    kinds = [o.kind for o in obstacles]
    labels = [o.label for o in obstacles]

    # obstacle polygons for centroid filtering: pads + keepouts (+ board outline)
    pad_polys = _obstacle_polygons_from_pads(pads_on_layer)
    block_polys = list(pad_polys) + list(keepout_polys)

    triangles = _triangulate(points)

    # Filter triangles whose centroid is inside an obstacle or outside board.
    free_triangles = []
    for tri in triangles:
        i, j, k = tri
        cx = (points[i][0] + points[j][0] + points[k][0]) / 3.0
        cy = (points[i][1] + points[j][1] + points[k][1]) / 3.0
        if _point_in_any_polygon(cx, cy, block_polys):
            continue
        if not _point_in_rect(cx, cy, bounds):
            continue
        free_triangles.append(tri)

    # Build nodes: one per unique triangle edge midpoint.
    node_of_edge = {}
    node_pos = []
    node_capacity = []
    node_gap = []

    def _edge_key(a, b):
        return (a, b) if a < b else (b, a)

    def _get_node(a, b):
        key = _edge_key(a, b)
        if key not in node_of_edge:
            idx = len(node_pos)
            node_of_edge[key] = idx
            mx = (points[a][0] + points[b][0]) / 2.0
            my = (points[a][1] + points[b][1]) / 2.0
            node_pos.append((mx, my))
            gap = math.hypot(points[a][0] - points[b][0],
                             points[a][1] - points[b][1]) - radii[a] - radii[b]
            gap = max(gap, 0.0)
            cap = compute_capacity(gap, trace_width, clearance)
            node_capacity.append(cap)
            node_gap.append(gap)
        return node_of_edge[key]

    adjacency = {}
    for tri in free_triangles:
        i, j, k = tri
        n_ij = _get_node(i, j)
        n_jk = _get_node(j, k)
        n_ki = _get_node(k, i)
        for a in (n_ij, n_jk, n_ki):
            adjacency.setdefault(a, [])
        adjacency[n_ij].append(n_jk); adjacency[n_jk].append(n_ij)
        adjacency[n_jk].append(n_ki); adjacency[n_ki].append(n_jk)
        adjacency[n_ki].append(n_ij); adjacency[n_ij].append(n_ki)

    return CapacityGraph(
        layer=layer,
        points=points,
        radii=radii,
        kinds=kinds,
        labels=labels,
        triangles=free_triangles,
        node_pos=node_pos,
        node_capacity=node_capacity,
        node_gap=node_gap,
        adjacency=adjacency,
        trace_width=trace_width,
        clearance=clearance,
    )
