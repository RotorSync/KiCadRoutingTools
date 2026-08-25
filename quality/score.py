#!/usr/bin/env python3
"""score.py -- QUALITY SCORING harness for KiCadRoutingTools.

Measures how "professionally routed" a board looks by computing a set of
per-net and board-aggregate metrics over the parsed copper geometry, then
aggregating them into a single 0-100 score with a visible weight table.

This is the RULER ONLY: it reads boards and never modifies them, and it makes
no changes to any product code under py_router/ or rust_router/.

Usage:
    score.py BOARD.kicad_pcb [--json out.json] [--verbose]

Metrics (v1):
    bends_per_mm            -- direction changes per mm of trace length
    off_angle_joints        -- joints not on the 0/45/90 grid, per mm
    vias_per_net            -- vias per routed net
    pad_entry_flags         -- fraction of pad entries that are acute/side
    segments_per_mm         -- segment fragmentation per mm of trace
    parallel_spacing_sd     -- std-dev of spacing between co-running traces (mm)
    channel_asymmetry       -- mean side-clearance asymmetry along traces
    layer_direction         -- fraction of trace length within 22.5 deg of the
                               layer's dominant axis
    stubs                   -- dangling / orphaned segment endpoints
    jog_chains              -- stair-stepping: clusters of 2+ bends within a
                               short arc-length window, plus excess bends over
                               the minimal octilinear count

Each metric has a docstring definition + units below.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

# Ensure py_router / rust_router are importable.
_HERE = os.path.dirname(os.path.abspath(__file__))
_KRT = os.path.dirname(_HERE)
for _p in (os.path.join(_KRT, 'py_router'), os.path.join(_KRT, 'rust_router')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from kicad_parser import parse_kicad_pcb  # noqa: E402

import geometry as G  # noqa: E402

# si_classes is imported lazily inside metric_si_coupling so that score.py
# still imports cleanly when py_router is not on the path (self-containment).
# The metric needs the net classifier to know which nets are VICTIM/AGGRESSOR.


# ---------------------------------------------------------------------------
# Metric definitions (docstrings are the canonical definitions)
# ---------------------------------------------------------------------------

def metric_bends(by_net_layer):
    """Bends per net.

    Definition: a bend is a joint where two consecutive segments of a chained
    trace change direction by more than 2 degrees (i.e. not a straight
    continuation). We count bends per net and normalise by total trace length
    to give bends-per-mm, which is comparable across nets of different sizes.

    Units: bends / mm of trace length.
    """
    total_bends = 0
    total_len = 0.0
    per_net = {}
    for nid, layers in by_net_layer.items():
        nb = 0
        nl = 0.0
        for layer, segs in layers.items():
            for poly in G.chain_segments(segs):
                nb += G.polyline_bends(poly)
                nl += G.polyline_length(poly)
        total_bends += nb
        total_len += nl
        per_net[nid] = (nb / nl) if nl > 0 else 0.0
    return {
        'value': (total_bends / total_len) if total_len > 0 else 0.0,
        'total_bends': total_bends,
        'total_length_mm': total_len,
        'per_net': per_net,
        'units': 'bends/mm',
    }


def metric_off_angle(by_net_layer):
    """Off-grid joints.

    Definition: at each interior joint of a chained trace we measure the turn
    angle between the two segment directions. A joint whose turn is not a
    multiple of 45 degrees (within 1 degree) is an off-grid joint -- the
    signature of an arbitrary-angle jog that looks unprofessional.

    Units: off-grid joints / mm of trace length.
    """
    total_off = 0
    total_len = 0.0
    per_net = {}
    for nid, layers in by_net_layer.items():
        no = 0
        nl = 0.0
        for layer, segs in layers.items():
            for poly in G.chain_segments(segs):
                no += G.polyline_off_angle_joints(poly)
                nl += G.polyline_length(poly)
        total_off += no
        total_len += nl
        per_net[nid] = (no / nl) if nl > 0 else 0.0
    return {
        'value': (total_off / total_len) if total_len > 0 else 0.0,
        'total_off_angle_joints': total_off,
        'total_length_mm': total_len,
        'per_net': per_net,
        'units': 'joints/mm',
    }


def metric_vias(vias_by_net, routed_nets):
    """Vias per net.

    Definition: number of vias belonging to each routed net, averaged over all
    nets that carry at least one track segment. Vias on segment-less nets
    (e.g. GND stitching vias) belong to no routed net and are excluded from the
    numerator -- they are not part of any routed trace.

    Units: vias / routed net.
    """
    n_routed = len(routed_nets)
    total_vias = sum(len(v) for nid, v in vias_by_net.items() if nid in routed_nets)
    per_net = {nid: len(v) for nid, v in vias_by_net.items() if nid in routed_nets}
    return {
        'value': (total_vias / n_routed) if n_routed else 0.0,
        'total_vias': total_vias,
        'n_routed_nets': n_routed,
        'per_net': per_net,
        'units': 'vias/net',
    }


def metric_pad_entries(pcb):
    """Pad-entry flags.

    Definition: for every pad that has a track segment touching it, we measure
    the angle between the trace's entry direction and the pad's face normal.
    Entries are classified good / acute / side (see geometry.classify_pad_entry).
    The metric is the fraction of all measured pad entries that are flagged as
    acute or side -- i.e. potential acid traps / slivers where the trace enters
    at a shallow angle or along the pad's long axis.

    Units: fraction (0..1) of pad entries flagged.
    """
    flagged = 0
    total = 0
    flagged_pads = []
    tol = 1e-3

    for nid, pads in pcb.pads_by_net.items():
        segs = [s for s in pcb.segments if s.net_id == nid]
        if not segs:
            continue
        for pad in pads:
            if pad.shape == 'circle':
                continue
            best = None
            best_d = tol + 1e9
            for s in segs:
                for (px, py) in ((s.start_x, s.start_y), (s.end_x, s.end_y)):
                    d = G.dist(px, py, pad.global_x, pad.global_y)
                    if d < best_d:
                        best_d = d
                        best = s
            if best is None:
                continue
            if G.dist(best.start_x, best.start_y, pad.global_x, pad.global_y) <= \
               G.dist(best.end_x, best.end_y, pad.global_x, pad.global_y):
                dx = best.start_x - best.end_x
                dy = best.start_y - best.end_y
            else:
                dx = best.end_x - best.start_x
                dy = best.end_y - best.start_y
            entry_dir = math.degrees(math.atan2(dy, dx))
            d_deg = G.pad_entry_angle_deg(pad, entry_dir)
            cls = G.classify_pad_entry(d_deg)
            total += 1
            if cls in ('acute', 'side'):
                flagged += 1
                flagged_pads.append({
                    'pad': f"{pad.component_ref}.{pad.pad_number}",
                    'net': pcb.nets[nid].name if nid in pcb.nets else str(nid),
                    'class': cls,
                    'angle_deg': round(d_deg, 1) if d_deg is not None else None,
                    'x': round(pad.global_x, 3),
                    'y': round(pad.global_y, 3),
                })
    return {
        'value': (flagged / total) if total else 0.0,
        'flagged': flagged,
        'total': total,
        'flagged_pads': flagged_pads,
        'units': 'fraction',
    }


def metric_fragmentation(by_net_layer):
    """Segment fragmentation.

    Definition: number of track segments per mm of trace length. A professional
    route merges collinear runs into long single segments; a fragmented route
    chops them into many short segments (often from iterative rip-up/reroute).

    Units: segments / mm.
    """
    total_segs = 0
    total_len = 0.0
    per_net = {}
    for nid, layers in by_net_layer.items():
        ns = sum(len(segs) for segs in layers.values())
        nl = 0.0
        for segs in layers.values():
            for poly in G.chain_segments(segs):
                nl += G.polyline_length(poly)
        total_segs += ns
        total_len += nl
        per_net[nid] = (ns / nl) if nl > 0 else 0.0
    return {
        'value': (total_segs / total_len) if total_len > 0 else 0.0,
        'total_segments': total_segs,
        'total_length_mm': total_len,
        'per_net': per_net,
        'units': 'segments/mm',
    }




def metric_jog_chains(by_net_layer):
    """Jog chains & excess bends.

    Captures the board owner's #1 visual complaint about autorouted output --
    stair-stepping (bunches of small jogs). Two raw signals close the gap that
    bends/mm and fragmentation leave open (they cannot tell three clustered
    jogs from three well-separated corners):

      * JOG CHAINS: along each chained trace we detect clusters of 2+ direction
        changes whose consecutive arc-length gaps are all within a short window.
        The window is proportional to trace width -- max(2.0mm, 8x width) -- so a
        tight cluster on a narrow trace (8x a 0.2mm trace = 1.6mm) is still
        caught while wide traces get a proportionally larger window (their jogs
        are physically larger). The 2.0mm floor keeps hairline traces from
        needing an impractically tiny window that would fragment one clean
        corner into a false chain. Three clustered jogs count as ONE chain;
        three well-separated corners do not cluster at all.

      * EXCESS BENDS: for each two-point connection (each chained polyline) we
        compute actual bends minus the minimal octilinear bend count for its
        endpoints (geometry.minimal_octilinear_bends): aligned -> 0, on a single
        45-deg diagonal -> 1 (ONE clean corner), otherwise -> 2 (the pattern
        jog -> long straight run -> jog). Excess = max(0, actual - minimal). A
        professional route has ~0 excess; an autorouter stair-step has many.

    Sub-score thresholds live in compute_sub_scores; both raw rates decay
    exponentially from a perfect board (rate=0 -> 100). ref_jog_chains_per_mm =
    0.15 and ref_excess_bends_per_mm = 0.6 were calibrated on the corpus so the
    worst stair-steppers (orangecrab ~0.21 chains/mm & ~0.99 excess/mm) land
    near ~20/100 while clean fanout/QFN boards (~0) score ~100.

    Units: jog chains / mm and excess bends / mm.
    """
    total_jog_chains = 0
    total_excess_bends = 0
    total_len = 0.0
    per_net = {}
    details = []

    for nid, layers in by_net_layer.items():
        njc = 0
        nex = 0
        nl = 0.0
        for layer, segs in layers.items():
            widths = [s.width for s in segs]
            width = max(widths) if widths else 0.25
            window = max(2.0, 8.0 * width)
            for poly in G.chain_segments(segs):
                plen = G.polyline_length(poly)
                nl += plen
                nb = G.polyline_bends(poly)
                m = G.minimal_octilinear_bends(poly)
                nex += max(0, nb - m)
                for cluster in G.detect_jog_chains(poly, window):
                    njc += 1
                    details.append({
                        'net': nid,
                        'layer': layer,
                        'window_mm': round(window, 3),
                        'n_bends': len(cluster),
                        'bends': [{'index': i, 'arc_mm': round(a, 3),
                                   'x': round(poly[i][0], 3), 'y': round(poly[i][1], 3)}
                                  for (i, a) in cluster],
                    })
        total_jog_chains += njc
        total_excess_bends += nex
        total_len += nl
        per_net[nid] = {'jog_chains': njc, 'excess_bends': nex,
                        'length_mm': round(nl, 3)}

    jc_per_mm = (total_jog_chains / total_len) if total_len > 0 else 0.0
    ex_per_mm = (total_excess_bends / total_len) if total_len > 0 else 0.0
    return {
        'value': round(jc_per_mm + ex_per_mm, 6),
        'jog_chains': total_jog_chains,
        'excess_bends': total_excess_bends,
        'total_length_mm': round(total_len, 3),
        'jog_chains_per_mm': round(jc_per_mm, 6),
        'excess_bends_per_mm': round(ex_per_mm, 6),
        'per_net': per_net,
        'jog_chain_details': details,
        'units': 'chains/mm & excess/mm',
    }


# ---------------------------------------------------------------------------
# Spatial metrics (parallel-run coherence, channel centering)
# ---------------------------------------------------------------------------

class _SegIndex:
    """Spatial grid index over segment bounding boxes for fast nearest queries."""

    def __init__(self, segments, cell=1.0):
        self.cell = cell
        self.grid = defaultdict(list)
        self.segments = list(segments)
        for i, s in enumerate(self.segments):
            minx = min(s.start_x, s.end_x)
            maxx = max(s.start_x, s.end_x)
            miny = min(s.start_y, s.end_y)
            maxy = max(s.start_y, s.end_y)
            for cx in range(int(minx // cell), int(maxx // cell) + 1):
                for cy in range(int(miny // cell), int(maxy // cell) + 1):
                    self.grid[(cx, cy)].append(i)

    def _cells_near(self, x, y, r):
        out = set()
        for cx in range(int((x - r) // self.cell), int((x + r) // self.cell) + 1):
            for cy in range(int((y - r) // self.cell), int((y + r) // self.cell) + 1):
                out.update(self.grid.get((cx, cy), []))
        return out


def _point_seg_dist(px, py, s):
    """Perpendicular distance from point to segment."""
    ax, ay = s.start_x, s.start_y
    bx, by = s.end_x, s.end_y
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    if L2 < G.EPS:
        return G.dist(px, py, ax, ay)
    t = ((px - ax) * dx + (py - ay) * dy) / L2
    t = max(0.0, min(1.0, t))
    cx, cy = ax + t * dx, ay + t * dy
    return G.dist(px, py, cx, cy)


def _obs_x(o):
    return getattr(o, 'start_x', getattr(o, 'x', 0.0))


def _obs_y(o):
    return getattr(o, 'start_y', getattr(o, 'y', 0.0))


class _ViaObstacle:
    def __init__(self, v):
        self.start_x = v.x - v.size / 2.0
        self.start_y = v.y - v.size / 2.0
        self.end_x = v.x + v.size / 2.0
        self.end_y = v.y + v.size / 2.0


def metric_parallel_coherence(pcb):
    """Parallel-run coherence.

    Definition: along each trace we sample points and look for a nearby trace on
    the same layer running roughly parallel (within 10 degrees). For each such
    co-running neighbour we record the perpendicular spacing between the two.
    The metric is the standard deviation of all recorded spacings across the
    board -- low variance means traces run in tidy parallel bundles with even
    spacing; high variance means ragged, unevenly-spaced parallel runs.

    Units: mm (std-dev of parallel spacing).
    """
    by_layer: Dict[str, List] = defaultdict(list)
    for s in pcb.segments:
        by_layer[s.layer].append(s)
    indexes = {layer: _SegIndex(segs) for layer, segs in by_layer.items()}

    spacings: List[float] = []
    max_dist = 3.0   # only consider neighbours within this perpendicular distance
    ang_tol = 10.0   # parallel within this many degrees

    for layer, segs in by_layer.items():
        idx = indexes[layer]
        for s in segs:
            slen = G.seg_len(s)
            if slen < 1e-6:
                continue
            n_samples = max(1, int(slen / 1.0))
            sdir = G.seg_angle_deg(s)
            for k in range(n_samples):
                t = (k + 0.5) / n_samples
                px = s.start_x + t * (s.end_x - s.start_x)
                py = s.start_y + t * (s.end_y - s.start_y)
                cands = idx._cells_near(px, py, max_dist + 1.0)
                best_d = None
                for ci in cands:
                    o = idx.segments[ci]
                    if o is s:
                        continue
                    odir = G.seg_angle_deg(o)
                    if G.angle_between_deg(sdir % 180.0, odir % 180.0) > ang_tol:
                        continue
                    d = _point_seg_dist(px, py, o)
                    if d < max_dist:
                        if best_d is None or d < best_d:
                            best_d = d
                if best_d is not None:
                    spacings.append(best_d)

    sd = _std(spacings) if spacings else None
    return {
        'value': sd if sd is not None else None,
        'n_samples': len(spacings),
        'mean_spacing_mm': _mean(spacings) if spacings else None,
        'units': 'mm',
    }


def metric_channel_centering(pcb):
    """Channel centering.

    Definition: along each trace we sample points and measure the clearance to
    the nearest obstacle (other copper trace or via) on each side perpendicular
    to the trace direction. The asymmetry at a sample is |left - right| /
    (left + right). The metric is the mean asymmetry across all samples -- low
    means traces sit centered in their routing channels; high means they hug one
    side.

    Units: dimensionless mean asymmetry (0..1).
    """
    obstacles: List[object] = list(pcb.segments)
    for v in pcb.vias:
        obstacles.append(_ViaObstacle(v))
    idx = _SegIndex(obstacles)

    asyms: List[float] = []
    max_dist = 5.0

    for s in pcb.segments:
        slen = G.seg_len(s)
        if slen < 1e-6:
            continue
        n_samples = max(1, int(slen / 1.5))
        ang = math.radians(G.seg_angle_deg(s))
        nx, ny = -math.sin(ang), math.cos(ang)
        for k in range(n_samples):
            t = (k + 0.5) / n_samples
            px = s.start_x + t * (s.end_x - s.start_x)
            py = s.start_y + t * (s.end_y - s.start_y)
            cands = idx._cells_near(px, py, max_dist + 1.0)
            left_d = None
            right_d = None
            for ci in cands:
                o = idx.segments[ci]
                if o is s:
                    continue
                dvecx = px - _obs_x(o)
                dvecy = py - _obs_y(o)
                proj = dvecx * nx + dvecy * ny
                d = _point_seg_dist(px, py, o)
                if d > max_dist:
                    continue
                if proj >= 0:
                    if right_d is None or d < right_d:
                        right_d = d
                else:
                    if left_d is None or d < left_d:
                        left_d = d
            if left_d is not None and right_d is not None:
                denom = left_d + right_d
                if denom > G.EPS:
                    asyms.append(abs(left_d - right_d) / denom)

    return {
        'value': _mean(asyms) if asyms else None,
        'n_samples': len(asyms),
        'units': 'dimensionless',
    }


# ---------------------------------------------------------------------------
# Layer direction discipline & stubs
# ---------------------------------------------------------------------------

def _run_class(ang_deg, axis_deg):
    """Classify a run direction against the layer's dominant axis.

    The [0,180) angle circle is partitioned into three 45-degree bands centred
    on the dominant axis (A), the diagonal (A+45), and the anti-axis (A+90):

      * ON-AXIS   -- within 22.5 deg of A
      * DIAGONAL  -- within 22.5 deg of A+45 (the clean 45/135 diagonal)
      * ANTI-AXIS -- within 22.5 deg of A+90 (the perpendicular axis)

    Returns 'on_axis' | 'diagonal' | 'anti_axis'.
    """
    d = G.angle_between_deg(ang_deg % 180.0, axis_deg % 180.0)
    if d <= 22.5:
        return 'on_axis'
    d_diag = G.angle_between_deg(ang_deg % 180.0, (axis_deg + 45.0) % 180.0)
    if d_diag <= 22.5:
        return 'diagonal'
    return 'anti_axis'


def metric_layer_direction(pcb):
    """Layer direction discipline (v1.3).

    Definition: on each copper layer we find the dominant axis A -- H (0/180)
    or V (90) -- as the one carrying the most total run length. We decompose
    every chained trace into RUNS (maximal collinear spans) and classify each
    run LONGER THAN 3 mm into one of three bands around A:

      * ON-AXIS   -- within 22.5 deg of A
      * DIAGONAL  -- within 22.5 deg of the 45/135 diagonal
      * ANTI-AXIS -- within 22.5 deg of the perpendicular axis

    The metric penalises ONLY the anti-axis fraction:

        raw = anti_axis_length / total_long_run_length   (per layer)

    length-weighted across layers. Diagonals are NEUTRAL -- a clean single
    45-degree diagonal is ideal routing per the board owner's spec (see
    geometry.minimal_octilinear_bends), so it must not be penalised. Short
    connector runs (<= 3 mm) are ignored: they are the unavoidable little jogs
    that stitch a route together and carry no direction-discipline signal.

    Rationale for the v1.3 change: v1.2 penalised ALL off-axis length, which
    wrongly docked clean diagonal-dominated fanout boards (their escapes are
    single clean 45s). carrier_lab/beautify2_findings.md (pass-2 gate failure)
    showed layer_direction could not improve on those boards without regressing
    jog_chains or parallel -- because the off-axis traces were already ideal
    single diagonals, not stair-steps. v1.3 therefore rewards a board for
    keeping its long runs on ONE axis and its clean diagonals, and only docks
    the genuinely bad pattern: long runs running the WRONG way (perpendicular
    to the layer's dominant axis).

    Units: fraction (0..1) of long-run length that is anti-axis (lower is better).
    """
    by_layer: Dict[str, List] = defaultdict(list)
    for s in pcb.segments:
        by_layer[s.layer].append(s)

    LONG_RUN_MIN = 3.0   # mm; runs at or below this are ignored connectors

    layer_results = {}
    total_anti = 0.0
    total_long = 0.0

    for layer, segs in by_layer.items():
        # Collect all long runs (> LONG_RUN_MIN) with their direction + length.
        long_runs = []   # (ang_deg, length_mm)
        for poly in G.chain_segments(segs):
            for run in G.polyline_runs(poly):
                length = G.run_length(poly, run)
                if length <= LONG_RUN_MIN:
                    continue
                a, b = run
                ang = math.degrees(math.atan2(
                    poly[b][1] - poly[a][1], poly[b][0] - poly[a][0])) % 180.0
                long_runs.append((ang, length))

        if not long_runs:
            continue

        # Dominant axis A: H (0) or V (90), whichever carries more long-run length.
        h_len = sum(length for (ang, length) in long_runs
                    if G.angle_between_deg(ang, 0.0) <= 22.5)
        v_len = sum(length for (ang, length) in long_runs
                    if G.angle_between_deg(ang, 90.0) <= 22.5)
        axis = 0.0 if h_len >= v_len else 90.0

        layer_long = sum(length for (_ang, length) in long_runs)
        layer_anti = sum(length for (ang, length) in long_runs
                         if _run_class(ang, axis) == 'anti_axis')
        frac = layer_anti / layer_long if layer_long > 0 else 0.0

        layer_results[layer] = {
            'dominant_axis': 'H' if axis == 0.0 else 'V',
            'dominant_axis_deg': round(axis, 1),
            'anti_axis_fraction': round(frac, 4),
            'anti_axis_length_mm': round(layer_anti, 3),
            'long_run_length_mm': round(layer_long, 3),
            'n_long_runs': len(long_runs),
        }
        total_anti += layer_anti
        total_long += layer_long

    return {
        'value': (total_anti / total_long) if total_long > 0 else 0.0,
        'total_long_run_length_mm': total_long,
        'anti_axis_length_mm': total_anti,
        'long_run_min_mm': LONG_RUN_MIN,
        'by_layer': layer_results,
        'units': 'fraction anti-axis',
    }


def _point_in_polygon(x: float, y: float, poly) -> bool:
    """Ray-casting point-in-polygon test (works for convex & concave)."""
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if ((yi > y) != (yj > y)) and \
           (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def metric_stubs(pcb):
    """Stubs / orphans.

    Definition: a dangling segment endpoint is one that connects to no other
    copper -- no other track segment endpoint within tolerance and no pad and no
    via within tolerance. An endpoint that lands inside a filled copper zone of
    the SAME net (same layer) is legitimately connected to that pour and is not
    counted as dangling.

    Units: count of dangling endpoints.
    """
    tol = 1e-3

    # Zone index: (net_id, layer) -> list of outline polygons. The board file
    # does not store filled polygons; the zone outline is the filled-copper
    # region proxy (KiCad fills the outline on load). Keepout / no-net zones
    # carry no copper and are skipped.
    zone_index: Dict[Tuple[int, str], List] = defaultdict(list)
    for z in pcb.zones:
        if z.net_id == 0:
            continue
        zone_index[(z.net_id, z.layer)].append(z.polygon)

    seg_endpoints: List[Tuple[float, float]] = []
    for s in pcb.segments:
        seg_endpoints.append((s.start_x, s.start_y))
        seg_endpoints.append((s.end_x, s.end_y))

    pad_points: List[Tuple[float, float]] = []
    for pads in pcb.pads_by_net.values():
        for p in pads:
            pad_points.append((p.global_x, p.global_y))

    via_points: List[Tuple[float, float]] = [(v.x, v.y) for v in pcb.vias]

    all_points = seg_endpoints + pad_points + via_points

    def count_near(px, py):
        c = 0
        for (qx, qy) in all_points:
            if G.dist(px, py, qx, qy) <= tol:
                c += 1
                if c >= 2:
                    return c
        return c

    def in_same_net_zone(px, py, net_id, layer):
        for poly in zone_index.get((net_id, layer), []):
            if _point_in_polygon(px, py, poly):
                return True
        return False

    dangling_endpoints: List[Tuple[float, float]] = []
    seen_endpoints: set = set()
    for s in pcb.segments:
        for ep in ((s.start_x, s.start_y), (s.end_x, s.end_y)):
            key = (round(ep[0], 4), round(ep[1], 4))
            if key in seen_endpoints:
                continue
            seen_endpoints.add(key)
            nconn = count_near(ep[0], ep[1])
            if nconn < 2 and not in_same_net_zone(ep[0], ep[1], s.net_id, s.layer):
                dangling_endpoints.append((round(ep[0], 3), round(ep[1], 3)))

    return {
        'value': len(dangling_endpoints),
        'dangling_endpoints': dangling_endpoints,
        'units': 'count',
    }


# ---------------------------------------------------------------------------
# Signal-integrity coupling metric (v1.4)
# ---------------------------------------------------------------------------

def metric_si_coupling(pcb):
    """Signal-integrity coupling exposure (v1.4).

    Definition: for every VICTIM segment we accumulate PARALLEL-EXPOSURE to
    any AGGRESSOR copper on the SAME layer within a coupling window, weighted
    by 1/separation, PLUS broadside overlap on ADJACENT copper layers with no
    plane between them (a GND plane layer between them shields).

    The coupling window starts at max(3x trace pitch, 1.0 mm) -- 3x the victim
    trace's own width is the classic "keep 3W away from a noisy neighbor"
    rule of thumb (3x the trace width is where crosstalk coupling has dropped
    to a small fraction of its worst-case value for microstrip; the 1.0 mm
    floor keeps hairline traces from needing an impractically tiny window).
    Only co-running (parallel) length counts: a perpendicular crossing
    contributes ~nothing (the overlap is a single point), so a serial line
    hugging a switch node for 40 mm scores dramatically worse than one
    crossing it perpendicular once.

    Exposure is accumulated per victim net and normalised by the net's total
    routed length, giving a per-mm exposure that is comparable across nets of
    different sizes:

        raw = sum over victim segments of
                sum over aggressor segments (same layer, within window) of
                  exposed_length * (1 / separation)
              + broadside term (adjacent layer, no shield) of
                  overlap_length * (1 / separation)
              / total victim-net length

    Units: dimensionless exposure per mm of victim trace (higher = worse).

    The sub-score thresholds (see compute_sub_scores) are calibrated so that a
    board with NO victim/aggressor pairs scores 100, a board with a few short
    parallel runs scores ~70-90, and a board with a serial line hugging a
    switch node for tens of mm scores near 0. Weight ~10 (see WEIGHTS).
    """
    # Lazy import: keeps score.py importable without py_router on the path.
    try:
        import si_classes as si
    except ImportError:
        return {
            'value': None,
            'n_victim_nets': 0,
            'n_aggressor_nets': 0,
            'n_exposed_pairs': 0,
            'units': 'exposure/mm',
            'note': 'si_classes not importable',
        }

    board_path = getattr(pcb, 'source_path', '') or ''
    classes = si.classify_board(pcb, board_path=board_path or None)

    # net_id -> class for nets that carry copper
    seg_nets = set()
    for s in pcb.segments:
        seg_nets.add(s.net_id)

    victim_nets = set()
    aggressor_nets = set()
    for nid, info in classes.items():
        if nid == -1:
            continue
        if nid not in seg_nets:
            continue
        if info['class'] == si.VICTIM:
            victim_nets.add(nid)
        elif info['class'] == si.AGGRESSOR:
            aggressor_nets.add(nid)

    if not victim_nets or not aggressor_nets:
        return {
            'value': 0.0,
            'n_victim_nets': len(victim_nets),
            'n_aggressor_nets': len(aggressor_nets),
            'n_exposed_pairs': 0,
            'units': 'exposure/mm',
        }

    # Build per-layer segment indexes for aggressor segments.
    by_layer: Dict[str, List] = defaultdict(list)
    for s in pcb.segments:
        if s.net_id in aggressor_nets:
            by_layer[s.layer].append(s)
    indexes = {layer: _SegIndex(segs) for layer, segs in by_layer.items()}

    # Shield layers: which copper layers carry a GND/ground plane zone.
    shield_nets_by_layer: Dict[str, set] = defaultdict(set)
    for z in pcb.zones:
        if z.net_id == 0:
            continue
        zname = z.net_name or ''
        if zname.lstrip('/').lower().startswith('gnd') or zname.lstrip('/').lower() == 'ground':
            shield_nets_by_layer[z.layer].add(zname)

    copper_layers = list(pcb.board_info.copper_layers)

    # Coupling window: max(3x victim trace width, 1.0 mm).
    def window_for(s):
        return max(3.0 * s.width, 1.0)

    def _dielectric_thickness(layer_a, layer_b):
        """Total dielectric thickness (mm) between two copper layers.

        Sums the stackup layers strictly between the two copper layers. Falls
        back to a nominal 0.2 mm prepreg when the stackup is missing or the
        layers are not found (2-layer boards with no stackup).
        """
        st = pcb.board_info.stackup
        if not st:
            return 0.2
        names = [s.name for s in st]
        try:
            ia = names.index(layer_a)
            ib = names.index(layer_b)
        except ValueError:
            return 0.2
        lo, hi = (ia, ib) if ia < ib else (ib, ia)
        total = 0.0
        for s in st[lo + 1:hi]:
            if s.layer_type != 'copper':
                total += s.thickness
        return total if total > 0 else 0.2

    def _norm_name(n):
        n = n.lstrip('/')
        m = re.search(r'\((.*)\)$', n)
        if m:
            n = m.group(1)
        return n.lower()

    def _same_interface(name_a, name_b):
        """True if two nets belong to the SAME functional interface (bus).

        Same-interface pairs are INTENTIONAL routing -- DDR data running beside
        its own DQS strobe, LVDS data lanes beside their own clock lane, SPI
        MOSI beside SCK -- and must NOT count as SI violations (they are how
        those buses are designed to route). Cross-interface pairs -- serial
        data beside switching power or an unrelated clock -- DO count.

        Heuristic: two nets are same-interface when their normalized names
        share a common prefix of >= 4 chars containing a letter (they are
        indexed members of one bus / one functional block), OR when both names
        carry the same component token (e.g. 'u1a' / 'u1b' -- routed_output's
        DDR CA bus on U1B beside the DQS strobe on U1A is one memory interface,
        not a cross-bus violation). This excludes routed_output's DDR/LVDS/FX2
        buses and orangecrab's SPI block while keeping d1's SSTX-vs-VBUS and
        rp2350's FPGA.MOSI-vs-+1V1 as genuine cross-bus violations.
        """
        a = _norm_name(name_a)
        b = _norm_name(name_b)
        i = 0
        while i < len(a) and i < len(b) and a[i] == b[i]:
            i += 1
        prefix = a[:i]
        if len(prefix) >= 4 and any(c.isalpha() for c in prefix):
            return True
        # shared component token: both names mention the same u<ref> -- u1a
        # and u1b are two halves of ONE DDR chip, so strip the trailing letter
        # (routed_output's DDR CA bus on U1B beside the DQS strobe on U1A is
        # one memory interface, not a cross-bus violation).
        ta = set(re.findall(r'u[0-9]+', a))
        tb = set(re.findall(r'u[0-9]+', b))
        return bool(ta & tb)

    # Per-victim-net accumulation.
    per_net: Dict[int, dict] = {}
    total_exposure = 0.0
    total_victim_len = 0.0
    n_exposed_pairs = 0
    offender_pairs = []  # (victim_name, aggressor_name, layer, exposed_mm, mean_sep_mm)

    for s in pcb.segments:
        if s.net_id not in victim_nets:
            continue
        slen = G.seg_len(s)
        if slen < G.EPS:
            continue
        total_victim_len += slen

        win = window_for(s)
        idx = indexes.get(s.layer)
        exposure = 0.0

        # --- same-layer parallel exposure ---
        if idx is not None:
            midx = (s.start_x + s.end_x) / 2.0
            midy = (s.start_y + s.end_y) / 2.0
            cands = idx._cells_near(midx, midy, win + 1.0)
            for ci in cands:
                o = idx.segments[ci]
                if o is s:
                    continue
                # Same-interface (same-bus) pairs are intentional routing --
                # skip them so only cross-bus coupling counts as a violation.
                if _same_interface(pcb.nets[s.net_id].name if s.net_id in pcb.nets else '',
                                   pcb.nets[o.net_id].name if o.net_id in pcb.nets else ''):
                    continue
                exp_len, mean_sep = G.parallel_exposure_length(s, o, win)
                if exp_len <= G.EPS or mean_sep is None:
                    continue
                w = 1.0 / mean_sep
                exposure += exp_len * w
                total_exposure += exp_len * w
                n_exposed_pairs += 1
                offender_pairs.append((s.net_id, o.net_id, s.layer,
                                       round(exp_len, 3), round(mean_sep, 3)))

        # --- broadside overlap on adjacent copper layers (no shield between) ---
        # The separation for broadside coupling is the DIELECTRIC THICKNESS
        # between the two copper layers (from the stackup), not the in-plane
        # distance -- two traces stacked directly over each other on adjacent
        # layers couple through the dielectric. Without a stackup we fall back
        # to a nominal 0.2 mm prepreg thickness.
        try:
            li = copper_layers.index(s.layer)
        except ValueError:
            li = -1
        if li >= 0:
            for adj in (li - 1, li + 1):
                if adj < 0 or adj >= len(copper_layers):
                    continue
                alayer = copper_layers[adj]
                if G.layer_is_shielded(s.layer, alayer, copper_layers,
                                       shield_nets_by_layer):
                    continue
                aidx = indexes.get(alayer)
                if aidx is None:
                    continue
                midx = (s.start_x + s.end_x) / 2.0
                midy = (s.start_y + s.end_y) / 2.0
                cands = aidx._cells_near(midx, midy, win + 1.0)
                for ci in cands:
                    o = aidx.segments[ci]
                    if _same_interface(pcb.nets[s.net_id].name if s.net_id in pcb.nets else '',
                                       pcb.nets[o.net_id].name if o.net_id in pcb.nets else ''):
                        continue
                    exp_len, mean_sep = G.parallel_exposure_length(s, o, win)
                    if exp_len <= G.EPS or mean_sep is None:
                        continue
                    # Broadside separation = dielectric thickness between the
                    # two copper layers (stackup), else nominal 0.2 mm.
                    sep = _dielectric_thickness(s.layer, alayer)
                    w = 1.0 / max(sep, 0.05)
                    exposure += exp_len * w
                    total_exposure += exp_len * w
                    n_exposed_pairs += 1
                    offender_pairs.append((s.net_id, o.net_id, alayer,
                                           round(exp_len, 3), round(sep, 3)))

        per_net[s.net_id] = per_net.get(s.net_id, {'exposure': 0.0}) 
        per_net[s.net_id]['exposure'] += exposure

    # Normalise per victim net by its total routed length.
    per_net_out = {}
    for nid, d in per_net.items():
        nlen = sum(G.seg_len(x) for x in pcb.segments if x.net_id == nid)
        per_net_out[nid] = {
            'name': pcb.nets[nid].name if nid in pcb.nets else str(nid),
            'exposure': round(d['exposure'], 4),
            'length_mm': round(nlen, 3),
            'exposure_per_mm': round(d['exposure'] / nlen, 6) if nlen > 0 else 0.0,
        }

    value = total_exposure / total_victim_len if total_victim_len > 0 else 0.0

    # Top offender pairs by exposed length * weight.
    pair_rows = []
    for (vnid, anid, layer, exp_len, mean_sep) in offender_pairs:
        pair_rows.append({
            'victim': pcb.nets[vnid].name if vnid in pcb.nets else str(vnid),
            'aggressor': pcb.nets[anid].name if anid in pcb.nets else str(anid),
            'layer': layer,
            'exposed_mm': exp_len,
            'mean_sep_mm': mean_sep,
            'score': round(exp_len / mean_sep, 3) if mean_sep > 0 else round(exp_len, 3),
        })
    pair_rows.sort(key=lambda r: -r['score'])

    return {
        'value': round(value, 6),
        'n_victim_nets': len(victim_nets),
        'n_aggressor_nets': len(aggressor_nets),
        'n_exposed_pairs': n_exposed_pairs,
        'total_victim_length_mm': round(total_victim_len, 3),
        'per_net': per_net_out,
        'top_offender_pairs': pair_rows[:40],
        'units': 'exposure/mm',
    }


# ---------------------------------------------------------------------------
# Aggregation helpers & scoring model
# ---------------------------------------------------------------------------

def _mean(xs):
    return sum(xs) / len(xs) if xs else None


def _std(xs):
    m = _mean(xs)
    if m is None or len(xs) < 2:
        return None
    return math.sqrt(sum((x - m) ** 2 for x in xs) / len(xs))


def _subscore_from_fraction(fraction: float) -> float:
    """fraction=0 -> 100; fraction=1 -> ~low."""
    return max(0.0, min(100.0, (1.0 - fraction) * 100.0))


def _subscore_from_rate(rate: float, ref: float) -> float:
    """rate=0 -> 100; rate=ref -> ~37; rate>>ref -> ~0."""
    return max(0.0, min(100.0, 100.0 * math.exp(-rate / ref)))


def _subscore_from_count(count: float, ref: float) -> float:
    return max(0.0, min(100.0, 100.0 * math.exp(-count / ref)))


# Provisional weight table (sums to 100). Tune as more boards are measured.
# v1.2 adds jog_chains at 12 -- heavier than layer_direction (10) because
# stair-stepping is the owner's #1 visual complaint. Room was made by trimming
# the two heaviest (off_angle, pad_entry) and the mid-weight spatial metrics.
WEIGHTS = {
    'bends':           9,
    'off_angle':      13,
    'vias':            9,
    'pad_entry':      13,
    'fragmentation':   9,
    'parallel':        8,
    'channel':         8,
    'layer_direction':10,
    'stubs':           9,
    'jog_chains':     12,
    # v1.4: signal-integrity coupling exposure. Weight ~10 -- comparable to the
    # other headline metrics. It is the ONE quality/-fence exception: this
    # session owns both quality/score.py and py_router/si_classes.py.
    'si_coupling':    10,
}


def compute_sub_scores(m):
    """Turn raw metric dicts into {name: {raw, sub_score, weight}}."""
    out: Dict[str, dict] = {}

    out['bends'] = {
        'raw': m['bends']['value'],
        'sub_score': _subscore_from_rate(m['bends']['value'], ref=2.0),
        'units': m['bends']['units'],
        'direction': 'lower is better',
        'definition': metric_bends.__doc__,
    }
    out['off_angle'] = {
        'raw': m['off_angle']['value'],
        'sub_score': _subscore_from_rate(m['off_angle']['value'], ref=1.0),
        'units': m['off_angle']['units'],
        'direction': 'lower is better',
        'definition': metric_off_angle.__doc__,
    }
    out['vias'] = {
        'raw': m['vias']['value'],
        'sub_score': _subscore_from_count(m['vias']['value'], ref=3.0),
        'units': m['vias']['units'],
        'direction': 'lower is better',
        'definition': metric_vias.__doc__,
    }
    out['pad_entry'] = {
        'raw': m['pad_entry']['value'],
        'sub_score': _subscore_from_fraction(m['pad_entry']['value']),
        'units': m['pad_entry']['units'],
        'direction': 'lower is better',
        'definition': metric_pad_entries.__doc__,
    }
    out['fragmentation'] = {
        'raw': m['fragmentation']['value'],
        'sub_score': _subscore_from_rate(m['fragmentation']['value'], ref=3.0),
        'units': m['fragmentation']['units'],
        'direction': 'lower is better',
        'definition': metric_fragmentation.__doc__,
    }
    sd_val = m['parallel']['value']
    out['parallel'] = {
        'raw': sd_val,
        'sub_score': _subscore_from_rate(sd_val if sd_val and sd_val > 1e-6 else 1e-6,
                                         ref=1.0),
        'units': m['parallel']['units'],
        'direction': 'lower is better',
        'definition': metric_parallel_coherence.__doc__,
    }
    asym_val = m['channel']['value']
    out['channel'] = {
        'raw': asym_val,
        'sub_score': (_subscore_from_rate(asym_val if asym_val > 1e-6 else 1e-6,
                                          ref=1.0)
                      if asym_val is not None else None),
        'units': m['channel']['units'],
        'direction': 'lower is better',
        'definition': metric_channel_centering.__doc__,
    }
    # layer_direction (v1.3): raw is the anti-axis FRACTION of long-run length
    # (lower is better) -- a board with zero anti-axis length scores 100, one
    # whose long runs all run the wrong way scores ~0. _subscore_from_fraction
    # maps fraction=0 -> 100 and fraction=1 -> 0, exactly the sensible linear
    # threshold for this "how much long-run length runs the wrong way" signal
    # (same mapping pad_entry uses for its flagged-entry fraction). Weight stays
    # at 10 (unchanged from v1.2).
    out['layer_direction'] = {
        'raw': m['layer_direction']['value'],
        'sub_score': _subscore_from_fraction(m['layer_direction']['value']),
        'units': m['layer_direction']['units'],
        'direction': 'lower is better',
        'definition': metric_layer_direction.__doc__,
    }
    out['stubs'] = {
        'raw': m['stubs']['value'],
        'sub_score': _subscore_from_count(m['stubs']['value'], ref=20.0),
        'units': m['stubs']['units'],
        'direction': 'lower is better',
        'definition': metric_stubs.__doc__,
    }
    # jog_chains: combine the two raw rates (jog chains/mm and excess bends/mm)
    # into one 0-100 sub-score. Both decay exponentially from a perfect board
    # (rate=0 -> 100). Thresholds calibrated on the corpus (see metric docstring):
    #   ref_jog_chains_per_mm = 0.15  -> orangecrab (0.21) ~24, d1 (0.12) ~43
    #   ref_excess_bends_per_mm = 0.6 -> orangecrab (0.99) ~19, d1 (0.86) ~24
    # The two are averaged so a board must be clean on BOTH to score high -- a
    # board with few chains but many excess bends (or vice versa) is still
    # penalised. Lower raw = better.
    jc = m['jog_chains']['jog_chains_per_mm']
    ex = m['jog_chains']['excess_bends_per_mm']
    sub_jc = _subscore_from_rate(jc, ref=0.15)
    sub_ex = _subscore_from_rate(ex, ref=0.6)
    out['jog_chains'] = {
        'raw': m['jog_chains']['value'],
        'sub_score': round((sub_jc + sub_ex) / 2.0, 4),
        'units': m['jog_chains']['units'],
        'direction': 'lower is better',
        'definition': metric_jog_chains.__doc__,
        '_jog_chains_per_mm': round(jc, 6),
        '_excess_bends_per_mm': round(ex, 6),
        '_sub_jog_chains': round(sub_jc, 3),
        '_sub_excess_bends': round(sub_ex, 3),
    }
    # si_coupling (v1.4): raw is the exposure-per-mm of victim trace. A board
    # with no victim/aggressor pairs scores 100 (raw=0 -> 100). Thresholds:
    #   ref = 0.5 exposure/mm -- a board whose victims run a few mm parallel to
    #   an aggressor at ~2 mm separation (0.5 mm * 1/2 = 0.25 per pair) lands
    #   around ~60; a serial line hugging a switch node for 40 mm at 0.4 mm
    #   separation (40 * 2.5 = 100 exposure / ~40 mm victim length = 2.5/mm)
    #   lands near 0. The exponential decay makes the metric sharply
    #   discriminating: clean boards ~100, mild exposure ~70-90, egregious
    #   parallel runs near 0.
    sic = m['si_coupling']
    sic_raw = sic['value'] if sic['value'] is not None else None
    out['si_coupling'] = {
        'raw': sic_raw,
        'sub_score': (_subscore_from_rate(sic_raw, ref=0.5)
                      if sic_raw is not None else None),
        'units': sic['units'],
        'direction': 'lower is better',
        'definition': metric_si_coupling.__doc__,
        '_n_victim_nets': sic.get('n_victim_nets', 0),
        '_n_aggressor_nets': sic.get('n_aggressor_nets', 0),
        '_n_exposed_pairs': sic.get('n_exposed_pairs', 0),
    }

    # attach weights from the provisional table.
    equal_wt = round(100.0 / len(out), 2) if out else 100.0
    for name in out:
        out[name]['weight'] = WEIGHTS.get(name, equal_wt)

    return out


def aggregate_score(sub_scores: Dict[str, dict]) -> Tuple[Optional[float], Dict[str, dict]]:
    """Weighted combination of sub-scores into a single 0-100 score.

    The final score is the weighted mean of the sub-scores:

        score = sum(eff_weight_i * sub_i) / sum(eff_weight_i)

    where eff_weight_i are the provisional weights scaled so that only metrics
    with data contribute (metrics with no data are dropped and their weight is
    redistributed proportionally over the rest). Because each sub-score is on a
    0-100 scale and the scaled weights sum to the full budget after scaling,
    this yields a value on [0,100].
    """
    have_data = {n: v for n, v in sub_scores.items() if v['sub_score'] is not None}
    if not have_data:
        return None, {n: dict(v) for n, v in sub_scores.items()}

    missing_wt_sum = sum(v['weight'] for n, v in sub_scores.items()
                         if n not in have_data)
    have_wt_sum = sum(v['weight'] for v in have_data.values())
    scale = (have_wt_sum + missing_wt_sum) / have_wt_sum if have_wt_sum else 1.0

    contributions: Dict[str, dict] = {}
    score_num = 0.0
    weight_sum_scaled = 0.0

    # First pass to compute scaled weight sum.
    for n, v in have_data.items():
        weight_sum_scaled += v['weight'] * scale

    # Second pass to build contributions.
    for n, v in have_data.items():
        eff_wt = v['weight'] * scale
        score_num += eff_wt * v['sub_score']
        contributions[n] = {
            **v,
            'effective_weight': round(eff_wt, 3),
            # points this metric contributes to the final score.
            '_contrib_points': round(eff_wt * v['sub_score'] / weight_sum_scaled, 3),
            '_weight_note':
                f"weight {v['weight']} scaled by {scale:.4f} -> {eff_wt:.3f}",
            '_weight_sum_scaled_note':
                f"scaled weights sum to {weight_sum_scaled:.3f}",
        }

    final_score = score_num / weight_sum_scaled if weight_sum_scaled else None
    return final_score, contributions


# ---------------------------------------------------------------------------
# Board-level driver
# ---------------------------------------------------------------------------

def score_board(pcb):
    """Compute all metrics for a parsed board and return the full result dict."""
    by_net_layer = G.group_segments_by_net_layer(pcb.segments)
    vias_by_net = G.group_vias_by_net(pcb.vias)
    routed_nets = set(by_net_layer.keys())

    m = {
        'bends': metric_bends(by_net_layer),
        'off_angle': metric_off_angle(by_net_layer),
        'vias': metric_vias(vias_by_net, routed_nets),
        'pad_entry': metric_pad_entries(pcb),
        'fragmentation': metric_fragmentation(by_net_layer),
        'parallel': metric_parallel_coherence(pcb),
        'channel': metric_channel_centering(pcb),
        'layer_direction': metric_layer_direction(pcb),
        'stubs': metric_stubs(pcb),
        'jog_chains': metric_jog_chains(by_net_layer),
        'si_coupling': metric_si_coupling(pcb),
    }

    sub_scores = compute_sub_scores(m)
    final_score, contributions = aggregate_score(sub_scores)

    return {
        'board': pcb.source_path,
        'n_segments': len(pcb.segments),
        'n_vias': len(pcb.vias),
        'n_routed_nets': len(routed_nets),
        'metrics': m,
        'sub_scores': sub_scores,
        'contributions': contributions,
        'final_score': round(final_score, 2) if final_score is not None else None,
        'weights': WEIGHTS,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _fmt(v, nd=4):
    if v is None:
        return 'n/a'
    if isinstance(v, float):
        return f"{v:.{nd}f}"
    return str(v)


def print_human(result):
    print(f"\nBoard: {result['board']}")
    print(f"Segments: {result['n_segments']}   Vias: {result['n_vias']}   "
          f"Routed nets: {result['n_routed_nets']}")
    print(f"\nFINAL SCORE: {result['final_score']} / 100\n")

    print("Metric                          raw        sub-score  weight  contrib")
    print("-" * 72)
    for name in WEIGHTS:
        ss = result['sub_scores'][name]
        contrib = result['contributions'].get(name, {})
        cp = contrib.get('_contrib_points')
        raw = ss['raw']
        sub = ss['sub_score']
        print(f"{name:<30} {_fmt(raw):>10}  {_fmt(sub,1):>9}  "
              f"{ss['weight']:>5}  {_fmt(cp,2):>8}")
    print("-" * 72)

    pe = result['metrics']['pad_entry']
    if pe['flagged_pads']:
        print(f"\nFlagged pad entries ({pe['flagged']}/{pe['total']}):")
        for fp in pe['flagged_pads'][:20]:
            print(f"  {fp['pad']:<16} net={fp['net']:<20} class={fp['class']:<6} "
                  f"angle={fp['angle_deg']} deg @ ({fp['x']},{fp['y']})")
        if len(pe['flagged_pads']) > 20:
            print(f"  ... and {len(pe['flagged_pads']) - 20} more")

    st = result['metrics']['stubs']
    if st['dangling_endpoints']:
        print(f"\nDangling endpoints ({st['value']}):")
        for (x, y) in st['dangling_endpoints'][:20]:
            print(f"  ({x}, {y})")
        if len(st['dangling_endpoints']) > 20:
            print(f"  ... and {len(st['dangling_endpoints']) - 20} more")

    jc = result['metrics']['jog_chains']
    print(f"\nJog chains ({jc['jog_chains']}) / excess bends ({jc['excess_bends']}):")
    print(f"  {jc['jog_chains_per_mm']:.4f} chains/mm, "
          f"{jc['excess_bends_per_mm']:.4f} excess bends/mm")
    for d in jc['jog_chain_details'][:15]:
        coords = ', '.join(f"({b['x']},{b['y']})" for b in d['bends'])
        print(f"  net={d['net']:<4} {d['layer']:<8} window={d['window_mm']:.2f}mm "
              f"{d['n_bends']} bends @ {coords}")
    if len(jc['jog_chain_details']) > 15:
        print(f"  ... and {len(jc['jog_chain_details']) - 15} more")

    ld = result['metrics']['layer_direction']
    print("\nLayer direction discipline (v1.3, anti-axis fraction of long runs):")
    for layer, info in ld['by_layer'].items():
        print(f"  {layer:<8} dominant axis={info['dominant_axis']}  "
              f"anti-axis fraction={info['anti_axis_fraction']:.3f}  "
              f"anti={info['anti_axis_length_mm']:.1f}/{info['long_run_length_mm']:.1f} mm "
              f"({info['n_long_runs']} long runs)")

    sic = result['metrics']['si_coupling']
    print("\nSignal-integrity coupling (v1.4):")
    print(f"  exposure = {sic['value']} exposure/mm  "
          f"({sic['n_victim_nets']} victim nets, {sic['n_aggressor_nets']} aggressor nets, "
          f"{sic['n_exposed_pairs']} exposed pairs)")
    for r in sic.get('top_offender_pairs', [])[:15]:
        print(f"  {r['victim']:<28} runs {r['exposed_mm']:>6.1f}mm at "
              f"{r['mean_sep_mm']:.2f}mm from {r['aggressor']:<24} on {r['layer']}")
    print()


def main(argv=None):
    ap = argparse.ArgumentParser(
        description='Score the routing quality of a KiCad PCB board (0-100).')
    ap.add_argument('board', help='path to .kicad_pcb file')
    ap.add_argument('--json', dest='json_out', default=None,
                    help='write full JSON result to this path')
    ap.add_argument('--verbose', action='store_true',
                    help='print per-net metric detail')
    args = ap.parse_args(argv)

    if not os.path.exists(args.board):
        print(f"error: board not found: {args.board}", file=sys.stderr)
        return 2

    pcb = parse_kicad_pcb(args.board)
    result = score_board(pcb)

    if args.json_out:
        with open(args.json_out, 'w', encoding='utf-8') as f:
            json.dump(result, f, indent=2, default=str)
        print(f"wrote JSON to {args.json_out}")

    print_human(result)

    if args.verbose:
        print("Per-net bends/mm (top 10):")
        top = sorted(result['metrics']['bends']['per_net'].items(),
                     key=lambda kv: -kv[1])[:10]
        for nid, v in top:
            name = pcb.nets[nid].name if nid in pcb.nets else str(nid)
            print(f"  net {nid} '{name}': {v:.4f} bends/mm")

    return 0


if __name__ == '__main__':
    sys.exit(main())
