#!/usr/bin/env python3
"""Regression test: nudge_grazing_vias must move a via that grazes a CIRCLE BGA
ball pad (the rescue-via-near-BGA-pad bug).

The #189/#339 via-in-pad unblock refit tolerates up to UNBLOCK_REFIT_MARGIN_MM
of sub-clearance at placement and relies on the post-route via-nudge to move the
residual graze to full clearance. But _nearest_pad_point modelled every pad as a
SHARP bounding box, so a round BGA ball's corner stuck out past its real copper:
a via 16um inside a ball's clearance read as 88um short -- beyond the nudge cap
(min(grid_step, via_size/4)) -- and the nudge refused to move a via that only
needed a 16um shift. The graze shipped.

Measured on glasgow_revC at heuristic_weight 1.2 (hw12 arm): the /CLKREF rescue
via at (79.4,94.2) landed 0.4243mm from U30.L4 (/FLAGC, a 0.35mm circle BGA
ball) vs the 0.44mm required (0.175 + 0.175 + 0.09) -- a 15.7um overlap DRC.
The fix models circle/oval/roundrect pads as rounded rects (matching check_drc's
point_to_pad_distance), so the nudge sees the true shortfall and moves the via.

    python3 tests/test_nudge_circle_pad_graze.py
"""
import math
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'py_router'))  # #522
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'py_tools'))  # #522

from kicad_parser import Pad, Segment, Via
from pcb_modification import nudge_grazing_vias

CLR = 0.09
H2H = 0.20


def _via(x, y, net, size=0.35, drill=0.2):
    return Via(x=x, y=y, size=size, drill=drill, layers=['F.Cu', 'B.Cu'], net_id=net)


def _seg(x1, y1, x2, y2, net, layer='F.Cu', w=0.2):
    return Segment(start_x=x1, start_y=y1, end_x=x2, end_y=y2, width=w,
                   layer=layer, net_id=net)


def _circle_pad(cx, cy, net, size=0.35):
    return Pad(component_ref='U30', pad_number='L4', net_id=net,
               net_name='/FLAGC', global_x=cx, global_y=cy,
               local_x=0.0, local_y=0.0, size_x=size, size_y=size,
               shape='circle', layers=['F.Cu', 'F.Mask', 'F.Paste'], drill=0)


def _pcb(segments, vias, pads_by_net):
    nets = {}
    for o in list(segments) + list(vias):
        nets.setdefault(o.net_id, SimpleNamespace(pads=[]))
    for nid, pads in pads_by_net.items():
        nets.setdefault(nid, SimpleNamespace(pads=pads))
    return SimpleNamespace(segments=list(segments), vias=list(vias),
                           pads_by_net=pads_by_net, footprints={}, nets=nets,
                           zones=[], board_info=SimpleNamespace(
                               copper_layers=['F.Cu', 'B.Cu'],
                               board_outline=[], board_cutouts=[],
                               board_bounds=None))


def run():
    fails = []

    def check(name, cond):
        print(('  PASS  ' if cond else '  FAIL  ') + name)
        if not cond:
            fails.append(name)

    # The measured glasgow geometry: /CLKREF rescue via at (79.4,94.2), size
    # 0.35/drill 0.2, vs U30.L4 (/FLAGC) circle ball at (79.7,93.9), size 0.35.
    # Center distance 0.4243mm vs required 0.175+0.175+0.09 = 0.44mm -> 15.7um
    # short. The nudge cap is min(max_shift, via_size/4) = min(0.1, 0.0875).
    # With exact circle geometry the shortfall is ~16um (well under cap) so the
    # via moves; with the old sharp-bbox model it read ~88um (over cap) and
    # stayed put.
    ball = _circle_pad(79.7, 93.9, net=1)
    v = _via(79.4, 94.2, net=2)
    segs = [_seg(79.4, 94.2, 79.5, 94.3, net=2)]  # anchor the via
    pcb = _pcb(segs, [v], {1: [ball]})
    moved, nets_changed, moves = nudge_grazing_vias(
        [{'new_vias': [v]}], pcb, {2}, clearance=CLR,
        hole_to_hole=H2H, max_shift=0.1)
    check("circle-ball graze: via moved", moved == 1)
    if moved:
        d = math.hypot(v.x - ball.global_x, v.y - ball.global_y)
        need = v.size / 2 + ball.size_x / 2 + CLR
        check("circle-ball graze: now clears the ball",
              d >= need - 1e-6)
        shift = math.hypot(v.x - 79.4, v.y - 94.2)
        check("circle-ball graze: shift under cap",
              0 < shift <= min(0.1, v.size / 4) + 1e-9)

    # A RECT pad must still be treated as a sharp bbox (no regression): a via
    # grazing a rect pad's FLAT EDGE by ~16um is genuinely ~16um short and must
    # move. (A rect pad's CORNER is legitimately closer -- the old bbox model is
    # exact for rect pads -- so a corner graze may be over-cap and correctly
    # stays put; that is unchanged behavior.)
    rect = Pad(component_ref='R1', pad_number='1', net_id=1,
               net_name='/X', global_x=79.7, global_y=93.9,
               local_x=0.0, local_y=0.0, size_x=0.35, size_y=0.35,
               shape='rect', layers=['F.Cu', 'F.Mask', 'F.Paste'], drill=0)
    # via 16um short of the rect's LEFT edge (x=79.525): the via centre must be
    # at 79.525 - (0.175 + 0.09 - 0.016) = 79.276 so it is 16um inside clearance.
    v2 = _via(79.276, 93.9, net=2)
    pcb2 = _pcb([_seg(79.276, 93.9, 79.276, 94.5, net=2)], [v2], {1: [rect]})
    moved2, _, _ = nudge_grazing_vias(
        [{'new_vias': [v2]}], pcb2, {2}, clearance=CLR,
        hole_to_hole=H2H, max_shift=0.1)
    check("rect pad graze: via moved (bbox model unchanged)", moved2 == 1)

    print('\n' + '=' * 60)
    print(f'  {len(fails) == 0 and "ALL PASS" or f"{len(fails)} FAILED"}')
    print('=' * 60)
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(run())
