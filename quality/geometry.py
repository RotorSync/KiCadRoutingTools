"""Geometry helpers for the KiCadRoutingTools quality harness.

This module is pure geometry / topology over the parsed PCBData from
py_router.kicad_parser. It contains no product-code changes -- it only reads
the parsed board and computes derived quantities used by score.py and
render.py.

All coordinates are in millimetres (the parser already normalises to mm).
Angles are in degrees unless stated otherwise.
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Basic vector helpers
# ---------------------------------------------------------------------------

EPS = 1e-9


def dist(ax: float, ay: float, bx: float, by: float) -> float:
    return math.hypot(ax - bx, ay - by)


def seg_len(s) -> float:
    return dist(s.start_x, s.start_y, s.end_x, s.end_y)


def seg_angle_deg(s) -> float:
    """Direction angle of a segment in degrees, normalized to [0, 180)."""
    dx = s.end_x - s.start_x
    dy = s.end_y - s.start_y
    ang = math.degrees(math.atan2(dy, dx)) % 180.0
    return ang


def angle_between_deg(a1: float, a2: float) -> float:
    """Smallest absolute angular difference between two [0,180) directions."""
    d = abs(a1 - a2) % 180.0
    if d > 90.0:
        d = 180.0 - d
    return d


def is_on_45_grid(ang: float, tol: float = 1.0) -> bool:
    """True if the direction angle is within tol degrees of a 0/45/90/135 axis."""
    d = ang % 45.0
    d = min(d, 45.0 - d)
    return d <= tol


def normalize_angle_180(ang: float) -> float:
    return ang % 180.0


# ---------------------------------------------------------------------------
# Segment chaining into polylines (per net, per layer)
# ---------------------------------------------------------------------------
#
# A "trace" is a maximal chain of collinear-or-turning segments connected
# endpoint-to-endpoint on the same layer and same net. We build these by
# matching segment endpoints within a small tolerance (the parser quantizes to
# an integer grid in internal units, so endpoints that touch agree to ~1e-4 mm).

def _endpoints(s) -> Tuple[Tuple[float, float], Tuple[float, float]]:
    return ((s.start_x, s.start_y), (s.end_x, s.end_y))


def chain_segments(segments: Sequence) -> List[List]:
    """Chain a list of segments (same net+layer assumed by caller) into
    polylines. Returns a list of polylines; each polyline is a list of
    (x, y) points in order. Segments that cannot be joined (isolated) become
    their own 2-point polyline.

    The algorithm greedily grows chains from unvisited segments by matching
    endpoints within `tol`. It is O(n^2) worst case but fine for the board
    sizes here.
    """
    tol = 1e-3
    segs = list(segments)
    n = len(segs)
    used = [False] * n

    # adjacency: for each endpoint key -> list of segment indices
    adj: Dict[Tuple[float, float], List[int]] = defaultdict(list)
    for i, s in enumerate(segs):
        for p in _endpoints(s):
            adj[p].append(i)

    def find_at(p):
        # find any unused segment touching p (within tol)
        for i in adj.get(p, []):
            if not used[i]:
                return i
        return None

    polylines: List[List] = []

    for start_i in range(n):
        if used[start_i]:
            continue
        used[start_i] = True
        # grow forward from start_i's end
        chain = [(_endpoints(segs[start_i])[0]), (_endpoints(segs[start_i])[1])]
        # extend forward
        cur = chain[-1]
        while True:
            nxt = find_at(cur)
            if nxt is None:
                break
            used[nxt] = True
            p0, p1 = _endpoints(segs[nxt])
            if dist(*p0, *cur) <= tol:
                chain.append(p1)
                cur = p1
            else:
                chain.append(p0)
                cur = p0
        # extend backward
        cur = chain[0]
        while True:
            nxt = find_at(cur)
            if nxt is None:
                break
            used[nxt] = True
            p0, p1 = _endpoints(segs[nxt])
            if dist(*p1, *cur) <= tol:
                chain.insert(0, p0)
                cur = p0
            else:
                chain.insert(0, p1)
                cur = p1
        polylines.append(chain)

    return polylines


def polyline_length(poly: Sequence[Tuple[float, float]]) -> float:
    return sum(dist(poly[i][0], poly[i][1], poly[i + 1][0], poly[i + 1][1])
               for i in range(len(poly) - 1))


def polyline_bends(poly: Sequence[Tuple[float, float]], tol_deg: float = 2.0) -> int:
    """Number of direction changes along a polyline (bends). A bend is a joint
    where the incoming and outgoing segment directions differ by more than
    tol_deg (i.e. not a straight continuation)."""
    bends = 0
    for i in range(1, len(poly) - 1):
        a = math.degrees(math.atan2(poly[i][1] - poly[i - 1][1],
                                    poly[i][0] - poly[i - 1][0]))
        b = math.degrees(math.atan2(poly[i + 1][1] - poly[i][1],
                                    poly[i + 1][0] - poly[i][0]))
        if angle_between_deg(a % 180.0, b % 180.0) > tol_deg:
            bends += 1
    return bends


def polyline_arc_positions(poly):
    """Cumulative arc-length position of each vertex along the polyline.
    Returns a list of the same length as `poly`; pos[0] == 0.0 and pos[-1] is
    the total polyline length."""
    pos = [0.0]
    acc = 0.0
    for i in range(len(poly) - 1):
        acc += dist(poly[i][0], poly[i][1], poly[i + 1][0], poly[i + 1][1])
        pos.append(acc)
    return pos


def polyline_bend_indices(poly, tol_deg=2.0):
    """Indices of interior vertices that are direction changes (> tol_deg)."""
    idx = []
    for i in range(1, len(poly) - 1):
        a = math.degrees(math.atan2(poly[i][1] - poly[i - 1][1],
                                    poly[i][0] - poly[i - 1][0]))
        b = math.degrees(math.atan2(poly[i + 1][1] - poly[i][1],
                                    poly[i + 1][0] - poly[i][0]))
        if angle_between_deg(a % 180.0, b % 180.0) > tol_deg:
            idx.append(i)
    return idx


def polyline_runs(poly, tol_deg=2.0):
    """Decompose a polyline into maximal collinear runs.

    A run is a maximal span of consecutive segments that are collinear (their
    directions agree within tol_deg). Returns a list of (start_idx, end_idx)
    vertex-index ranges; run (a, b) covers vertices poly[a..b] inclusive and
    segments a..b-1. A polyline with n vertices yields at least one run.
    """
    n = len(poly)
    if n < 2:
        return []

    def seg_dir(i):
        return math.degrees(math.atan2(poly[i + 1][1] - poly[i][1],
                                       poly[i + 1][0] - poly[i][0])) % 180.0

    runs = []
    start = 0
    cur_dir = seg_dir(0)
    for i in range(1, n - 1):
        d = seg_dir(i)
        if angle_between_deg(cur_dir % 180.0, d % 180.0) > tol_deg:
            runs.append((start, i))
            start = i
            cur_dir = d
    runs.append((start, n - 1))
    return runs


def run_length(poly, run):
    """Arc length (mm) of a run (start_idx, end_idx) over polyline poly."""
    a, b = run
    return sum(dist(poly[k][0], poly[k][1], poly[k + 1][0], poly[k + 1][1])
               for k in range(a, b))


def minimal_octilinear_bends(poly):
    """Minimal number of direction changes a professional octilinear route
    would use to connect the polyline's two endpoints.

    Encodes the owner's hand-routing spec:
      * endpoints aligned on an orthogonal axis (dx==0 or dy==0) -> 0 bends
      * endpoints on a single 45-degree diagonal (|dx|==|dy|) -> 1 bend
        (ONE clean 45-degree corner where geometry allows).
      * otherwise -> 2 bends (the pattern jog -> long straight run -> jog).
    """
    x1, y1 = poly[0]
    x2, y2 = poly[-1]
    dx = abs(x2 - x1)
    dy = abs(y2 - y1)
    if dx < EPS and dy < EPS:
        return 0
    if dx < EPS or dy < EPS:
        return 0
    if abs(dx - dy) < EPS:
        return 1
    return 2


def detect_jog_chains(poly, window):
    """Detect jog chains (stair-stepping) along a polyline.

    A jog chain is a cluster of 2+ bends whose consecutive arc-length gaps are
    all within `window` mm of each other -- several short segments with
    alternating direction changes bunched close together. Well-separated
    corners (gaps > window) do NOT cluster.

    Returns a list of clusters; each cluster is a list of (vertex_index,
    arc_position) for the bends in that cluster, in order along the trace.
    """
    pos = polyline_arc_positions(poly)
    bidx = polyline_bend_indices(poly)
    if len(bidx) < 2:
        return []
    bpos = [(i, pos[i]) for i in bidx]
    clusters = []
    cur = [bpos[0]]
    for item in bpos[1:]:
        if item[1] - cur[-1][1] <= window:
            cur.append(item)
        else:
            if len(cur) >= 2:
                clusters.append(cur)
            cur = [item]
    if len(cur) >= 2:
        clusters.append(cur)
    return clusters


def polyline_off_angle_joints(poly: Sequence[Tuple[float, float]],
                              tol_deg: float = 1.0) -> int:
    """Number of joints whose turn angle is NOT on the 0/45/90 grid.

    For each interior joint we compute the turn angle between the two segment
    directions; if that turn is not a multiple of 45 degrees (within tol), it
    counts as an off-grid joint -- the signature of an unprofessional,
    arbitrary-angle jog."""
    off = 0
    for i in range(1, len(poly) - 1):
        a = math.degrees(math.atan2(poly[i][1] - poly[i - 1][1],
                                    poly[i][0] - poly[i - 1][0]))
        b = math.degrees(math.atan2(poly[i + 1][1] - poly[i][1],
                                    poly[i + 1][0] - poly[i][0]))
        turn = angle_between_deg(a % 180.0, b % 180.0)
        # turn should be a multiple of 45 (0,45,90). Check residual.
        resid = turn % 45.0
        resid = min(resid, 45.0 - resid)
        if resid > tol_deg:
            off += 1
    return off


def resample_polyline(poly: Sequence[Tuple[float, float]],
                      step: float = 0.25) -> List[Tuple[float, float]]:
    """Resample a polyline at roughly `step` mm intervals along its length.
    Returns a list of points including both endpoints."""
    pts: List[Tuple[float, float]] = []
    total = polyline_length(poly)
    if total <= EPS:
        return [poly[0]]
    # walk segment by segment accumulating distance
    acc = 0.0
    pts.append(poly[0])
    target = step
    for i in range(len(poly) - 1):
        x0, y0 = poly[i]
        x1, y1 = poly[i + 1]
        segd = dist(x0, y0, x1, y1)
        while target <= acc + segd + EPS:
            t = (target - acc) / segd if segd > EPS else 0.0
            pts.append((x0 + t * (x1 - x0), y0 + t * (y1 - y0)))
            target += step
        acc += segd
    # ensure last point included
    if dist(*pts[-1], *poly[-1]) > EPS:
        pts.append(poly[-1])
    return pts


# ---------------------------------------------------------------------------
# Per-net grouping helpers
# ---------------------------------------------------------------------------

def group_segments_by_net(segments: Sequence) -> Dict[int, List]:
    by_net: Dict[int, List] = defaultdict(list)
    for s in segments:
        by_net[s.net_id].append(s)
    return dict(by_net)


def group_segments_by_net_layer(segments: Sequence) -> Dict[int, Dict[str, List]]:
    """net_id -> {layer -> [segments]}"""
    out: Dict[int, Dict[str, List]] = defaultdict(lambda: defaultdict(list))
    for s in segments:
        out[s.net_id][s.layer].append(s)
    return {k: dict(v) for k, v in out.items()}


def group_vias_by_net(vias: Sequence) -> Dict[int, List]:
    by_net: Dict[int, List] = defaultdict(list)
    for v in vias:
        by_net[v.net_id].append(v)
    return dict(by_net)


# ---------------------------------------------------------------------------
# Pad-entry geometry
# ---------------------------------------------------------------------------

def pad_entry_angle_deg(pad, entry_dir_deg: float) -> float:
    """Angle between the trace's entry direction and the pad's face normal.

    The pad's "face" is its short axis (the direction a trace should approach
    from to enter cleanly). For a rect/roundrect pad we use the pad's rotation;
    for a circle pad there is no preferred direction so we return None.

    We return the acute angle between the entry direction and the pad's long
    axis normalised to [0,90]. A value near 90 means the trace enters along the
    pad's long axis (a "side entry" / acid trap); near 0 means it enters head-on.
    """
    if pad.shape == 'circle':
        return None
    # pad rotation is absolute board angle in degrees.
    rot = getattr(pad, 'rotation', 0.0) or 0.0
    # The pad's long axis: for a rect/roundrect pad, size_x lies along the pad's
    # rotation direction and size_y perpendicular to it. The LONG axis is the
    # larger of the two; the face normal (the direction a trace should approach
    # from for a clean head-on entry) is perpendicular to the long axis.
    sx = getattr(pad, 'size_x', 0.0) or 0.0
    sy = getattr(pad, 'size_y', 0.0) or 0.0
    if sy >= sx:
        # long axis along local Y = rotation + 90
        long_axis = (rot + 90.0) % 180.0
    else:
        long_axis = rot % 180.0
    face_normal = (long_axis + 90.0) % 180.0
    d = angle_between_deg(entry_dir_deg % 180.0, face_normal)
    return d


def classify_pad_entry(d_deg: Optional[float],
                       acute_thresh: float = 30.0,
                       side_thresh: float = 60.0) -> str:
    """Classify a pad entry angle into 'good' / 'acute' / 'side' / 'n/a'.

    - good: head-on entry (angle <= acute_thresh from face normal)
    - acute: shallow / acute entry (angle between acute_thresh and side_thresh)
      -- a potential acid trap / sliver.
    - side: entry along the pad's long axis (angle >= side_thresh) -- the worst,
      a classic acid trap where the trace runs along the pad edge.
    """
    if d_deg is None:
        return 'n/a'
    if d_deg <= acute_thresh:
        return 'good'
    if d_deg <= side_thresh:
        return 'acute'
    return 'side'


# ---------------------------------------------------------------------------
# Signal-integrity coupling geometry (v1.4)
# ---------------------------------------------------------------------------
#
# These helpers back metric_si_coupling in score.py. They are pure geometry:
# given two segments on the same layer they compute how much of one runs
# parallel-and-close to the other; given two layers they decide whether a GND/
# plane layer between them shields them.

def seg_point_dist_sq(px, py, ax, ay, bx, by):
    """Squared distance from point (px,py) to segment AB and the parameter t.

    Returns (d2, t) where t in [0,1] is the closest-point parameter on AB.
    """
    dx = bx - ax
    dy = by - ay
    L2 = dx * dx + dy * dy
    if L2 < EPS:
        return dist(px, py, ax, ay) ** 2, 0.0
    t = ((px - ax) * dx + (py - ay) * dy) / L2
    t = max(0.0, min(1.0, t))
    cx = ax + t * dx
    cy = ay + t * dy
    return (px - cx) ** 2 + (py - cy) ** 2, t


def parallel_exposure_length(s_victim, s_aggr,
                             window_mm,
                             ang_tol_deg=15.0,
                             sample_step=0.25):
    """Length of victim segment that runs parallel-and-close to an aggressor.

    Definition: sample points along the VICTIM segment at `sample_step` mm.
    A sample contributes to exposure when BOTH hold:

      * PARALLEL -- the aggressor segment's direction is within `ang_tol_deg`
        of the victim's direction (modulo 180). A perpendicular crossing is NOT
        parallel and contributes nothing.
      * CLOSE -- the perpendicular distance from the sample to the aggressor
        segment is <= `window_mm`.

    Returns (exposed_length_mm, mean_separation_mm). mean_separation is the
    mean perpendicular distance over exposed samples (None if none exposed).
    Crossings are near-free because a perpendicular aggressor contributes no
    parallel samples; only co-running length counts.
    """
    slen = seg_len(s_victim)
    if slen < EPS:
        return 0.0, None

    vdir = seg_angle_deg(s_victim)
    adir = seg_angle_deg(s_aggr)
    if angle_between_deg(vdir % 180.0, adir % 180.0) > ang_tol_deg:
        return 0.0, None

    n_samples = max(2, int(slen / sample_step))
    exposed_len = 0.0
    seps = []
    step_len = slen / n_samples
    for k in range(n_samples):
        t = (k + 0.5) / n_samples
        px = s_victim.start_x + t * (s_victim.end_x - s_victim.start_x)
        py = s_victim.start_y + t * (s_victim.end_y - s_victim.start_y)
        d2, _t2 = seg_point_dist_sq(px, py,
                                    s_aggr.start_x, s_aggr.start_y,
                                    s_aggr.end_x, s_aggr.end_y)
        d = math.sqrt(d2)
        if d <= window_mm:
            exposed_len += step_len
            seps.append(max(d, EPS))

    mean_sep = sum(seps) / len(seps) if seps else None
    return exposed_len, mean_sep


def layer_is_shielded(layer_a, layer_b,
                      copper_layers,
                      shield_nets_by_layer):
    """True if a solid ground/plane layer lies between layer_a and layer_b.

    Uses the board's copper-layer order (top to bottom). A layer between the
    two counts as a shield if it carries a SHIELD net (GND/ground/plane) as a
    zone -- i.e. a solid plane the designer poured on that layer. If no zone
    data is available for a layer it does NOT shield (we only credit explicit
    ground planes).

    2-layer boards (F.Cu / B.Cu) have no internal layer -> never shielded.
    4-layer boards (F.Cu / In1.Cu / In2.Cu / B.Cu): In1.Cu or In2.Cu between
      F.Cu and B.Cu shields them.
    6+ layer boards: any internal copper layer between the two shields if it
      carries a ground plane.
    """
    if layer_a == layer_b:
        return False
    try:
        ia = copper_layers.index(layer_a)
        ib = copper_layers.index(layer_b)
    except ValueError:
        return False
    lo, hi = (ia, ib) if ia < ib else (ib, ia)
    for i in range(lo + 1, hi):
        mid = copper_layers[i]
        if shield_nets_by_layer.get(mid):
            return True
    return False
