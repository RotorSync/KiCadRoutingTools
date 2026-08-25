#!/usr/bin/env python3
"""render.py -- copper-layer PNG renders for KiCadRoutingTools.

Renders the parsed copper geometry of a KiCad PCB board to PNG images using
matplotlib (no external screenshot tools). One image per copper layer, plus an
optional overlay that colors traces by a routing-quality metric.

This is the RULER ONLY: it reads boards and never modifies them, and it makes
no changes to any product code under py_router/ or rust_router/.

Usage:
    render.py BOARD.kicad_pcb [--out DIR] [--overlay MODE] [--dpi N]

Overlay modes:
    none        -- plain copper render (default)
    pad_entries -- color traces red where they enter a flagged (acute/side) pad
    stubs       -- mark dangling segment endpoints in red
    off_angle   -- mark off-grid joints in red
"""

from __future__ import annotations

import argparse
import math
import os
import sys
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle, Circle, Polygon

# Ensure py_router / rust_router are importable.
_HERE = os.path.dirname(os.path.abspath(__file__))
_KRT = os.path.dirname(_HERE)
for _p in (os.path.join(_KRT, 'py_router'), os.path.join(_KRT, 'rust_router')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from kicad_parser import parse_kicad_pcb  # noqa: E402

import geometry as G  # noqa: E402


# ---------------------------------------------------------------------------
# Drawing helpers
# ---------------------------------------------------------------------------

def _pad_patch(pad, ax):
    """Add a matplotlib patch for a pad's copper shape."""
    x, y = pad.global_x, pad.global_y
    rot = getattr(pad, 'rotation', 0.0) or 0.0
    sx = pad.size_x or 0.0
    sy = pad.size_y or 0.0
    if pad.shape == 'circle':
        r = max(sx, sy) / 2.0
        return Circle((x, y), r, facecolor='#d9d9d9', edgecolor='#666666',
                      linewidth=0.4, zorder=3)
    # rectangle (or roundrect approximated as rect)
    w = sx
    h = sy
    # For a rect pad, size_x lies along rotation, size_y perpendicular.
    return Rectangle((x - w / 2.0, y - h / 2.0), w, h,
                     angle=rot, facecolor='#d9d9d9', edgecolor='#666666',
                     linewidth=0.4, zorder=3)


def _via_patch(via, ax):
    """Add a matplotlib patch for a via (annular ring)."""
    r = via.size / 2.0
    return Circle((via.x, via.y), r, facecolor='none', edgecolor='#333333',
                  linewidth=1.0, zorder=4)


def _board_bounds(pcb):
    bb = pcb.board_info.board_bounds
    if bb:
        return bb
    xs = [s.start_x for s in pcb.segments] + [s.end_x for s in pcb.segments]
    ys = [s.start_y for s in pcb.segments] + [s.end_y for s in pcb.segments]
    for pads in pcb.pads_by_net.values():
        for p in pads:
            xs.append(p.global_x)
            ys.append(p.global_y)
    for v in pcb.vias:
        xs.append(v.x)
        ys.append(v.y)
    if not xs:
        return (0, 0, 1, 1)
    return (min(xs), min(ys), max(xs), max(ys))


# ---------------------------------------------------------------------------
# Overlay computation
# ---------------------------------------------------------------------------

def _flagged_pad_positions(pcb):
    """Return set of (x,y) of pads flagged as acute/side entries."""
    flagged = set()
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
            if cls in ('acute', 'side'):
                flagged.add((round(pad.global_x, 3), round(pad.global_y, 3)))
    return flagged


def _off_angle_joint_positions(pcb):
    """Return set of (x,y) joint positions that are off the 0/45/90 grid."""
    joints = set()
    by_layer: Dict[str, List] = defaultdict(list)
    for s in pcb.segments:
        by_layer[s.layer].append(s)
    for layer, segs in by_layer.items():
        for poly in G.chain_segments(segs):
            for i in range(1, len(poly) - 1):
                a = math.degrees(math.atan2(poly[i][1] - poly[i - 1][1],
                                            poly[i][0] - poly[i - 1][0]))
                b = math.degrees(math.atan2(poly[i + 1][1] - poly[i][1],
                                            poly[i + 1][0] - poly[i][0]))
                turn = G.angle_between_deg(a % 180.0, b % 180.0)
                resid = turn % 45.0
                resid = min(resid, 45.0 - resid)
                if resid > 1.0:
                    joints.add((round(poly[i][0], 3), round(poly[i][1], 3)))
    return joints


def _stub_positions(pcb):
    """Return set of (x,y) dangling segment endpoints."""
    tol = 1e-3
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

    stubs = set()
    seen = set()
    for s in pcb.segments:
        for ep in ((s.start_x, s.start_y), (s.end_x, s.end_y)):
            key = (round(ep[0], 4), round(ep[1], 4))
            if key in seen:
                continue
            seen.add(key)
            if count_near(ep[0], ep[1]) < 2:
                stubs.add((round(ep[0], 3), round(ep[1], 3)))
    return stubs


def _compute_overlay(pcb, mode):
    """Return (flagged_segment_ids:set, marker_points:list)."""
    if mode == 'pad_entries':
        flagged_pads = _flagged_pad_positions(pcb)
        flagged_segs = set()
        tol = 1e-3
        for i, s in enumerate(pcb.segments):
            for (px, py) in ((s.start_x, s.start_y), (s.end_x, s.end_y)):
                if (round(px, 3), round(py, 3)) in flagged_pads:
                    flagged_segs.add(i)
        return flagged_segs, []
    elif mode == 'off_angle':
        joints = _off_angle_joint_positions(pcb)
        return set(), list(joints)
    elif mode == 'stubs':
        stubs = _stub_positions(pcb)
        return set(), list(stubs)
    return set(), []


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

# Layer colors (approximate KiCad copper colors).
LAYER_COLORS = {
    'F.Cu': '#c00000',
    'In1.Cu': '#0070c0',
    'In2.Cu': '#00b050',
    'B.Cu': '#7030a0',
    'In3.Cu': '#e36c09',
    'In4.Cu': '#0f7f7f',
}


def render_board(pcb, out_dir, overlay='none', dpi=150):
    """Render each copper layer to a PNG in out_dir. Returns list of paths."""
    os.makedirs(out_dir, exist_ok=True)
    flagged_segs, markers = _compute_overlay(pcb, overlay)

    # group segments by layer
    by_layer: Dict[str, List] = defaultdict(list)
    for i, s in enumerate(pcb.segments):
        by_layer[s.layer].append((i, s))

    # vias by layer (a via spans layers; draw on each spanned layer)
    via_by_layer: Dict[str, List] = defaultdict(list)
    for v in pcb.vias:
        for layer in v.layers:
            if layer in pcb.board_info.copper_layers or layer.endswith('.Cu'):
                via_by_layer[layer].append(v)

    # pads by layer
    pad_by_layer: Dict[str, List] = defaultdict(list)
    for pads in pcb.pads_by_net.values():
        for p in pads:
            for layer in p.layers:
                if layer in pcb.board_info.copper_layers or layer.endswith('.Cu'):
                    pad_by_layer[layer].append(p)

    bounds = _board_bounds(pcb)
    minx, miny, maxx, maxy = bounds
    padx = (maxx - minx) * 0.02 + 1.0
    pady = (maxy - miny) * 0.02 + 1.0

    paths = []
    for layer in pcb.board_info.copper_layers:
        fig, ax = plt.subplots(figsize=(10, 10 * (maxy - miny + 2 * pady) /
                                        (maxx - minx + 2 * padx)))
        ax.set_facecolor('#1a1a1a')
        color = LAYER_COLORS.get(layer, '#c00000')

        # pads
        for p in pad_by_layer.get(layer, []):
            ax.add_patch(_pad_patch(p, ax))
        # vias
        for v in via_by_layer.get(layer, []):
            ax.add_patch(_via_patch(v, ax))
        # traces
        for i, s in by_layer.get(layer, []):
            lw = max(s.width * dpi / 25.4 * 0.9, 0.5)
            seg_color = '#ff4444' if (overlay == 'pad_entries' and i in flagged_segs) \
                else color
            ax.plot([s.start_x, s.end_x], [s.start_y, s.end_y],
                    color=seg_color, linewidth=lw, solid_capstyle='round',
                    zorder=2)

        # overlay markers
        if markers:
            mx = [p[0] for p in markers]
            my = [p[1] for p in markers]
            ax.scatter(mx, my, s=30, c='#ff4444', marker='o', zorder=5,
                       label=overlay)

        ax.set_xlim(minx - padx, maxx + padx)
        ax.set_ylim(miny - pady, maxy + pady)
        ax.set_aspect('equal')
        ax.set_title(f"{os.path.basename(pcb.source_path)} - {layer}"
                     + (f" [overlay: {overlay}]" if overlay != 'none' else ""))
        ax.set_axis_off()
        if markers:
            ax.legend(loc='upper right')
        fig.tight_layout()
        out_path = os.path.join(out_dir,
                                f"{os.path.splitext(os.path.basename(pcb.source_path))[0]}"
                                f"_{layer.replace('.', '_')}"
                                f"{'_' + overlay if overlay != 'none' else ''}.png")
        fig.savefig(out_path, dpi=dpi)
        plt.close(fig)
        paths.append(out_path)

    return paths


def main(argv=None):
    ap = argparse.ArgumentParser(
        description='Render copper layers of a KiCad PCB board to PNG.')
    ap.add_argument('board', help='path to .kicad_pcb file')
    ap.add_argument('--out', dest='out_dir', default='render_out',
                    help='output directory for PNGs')
    ap.add_argument('--overlay', default='none',
                    choices=['none', 'pad_entries', 'stubs', 'off_angle'],
                    help='overlay mode to color traces by a metric')
    ap.add_argument('--dpi', type=int, default=150)
    args = ap.parse_args(argv)

    if not os.path.exists(args.board):
        print(f"error: board not found: {args.board}", file=sys.stderr)
        return 2

    pcb = parse_kicad_pcb(args.board)
    paths = render_board(pcb, args.out_dir, overlay=args.overlay, dpi=args.dpi)
    for p in paths:
        print(p)
    return 0


if __name__ == '__main__':
    sys.exit(main())
