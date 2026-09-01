#!/usr/bin/env python3
"""Regression test: a stub layer switch must not drill its pad via within
hole-to-hole of an EXISTING same-net via (writer-gap findings 2026-09-01,
residual 1 -- the ulx3s SDRAM_D15 family).

switch_boxed_stub_near / apply_stub_layer_switch validated the switch's pad
via against FOREIGN copper only (via_barrel_clear_of_foreign_copper excludes
the via's own net), but KiCad's hole-to-hole rule is net-independent: a pad
via drilled next to the net's OWN earlier via ships a fab drill-spacing
violation. Per the #468 doctrine the writer must exact-check and DECLINE.

The fix routes the check through fitting_pad_via (the shared fit funnel both
validate_single_swap/validate_swap and apply_stub_layer_switch call):
pad_via_drill_conflict prices the candidate hole against every existing via
and drilled pad on ANY net, with the coincident same-net barrel exempt
(that is the reuse case -- no second hole is drilled).

    python3 tests/test_stub_switch_same_net_via_drill.py
"""
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'py_router'))  # #522
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'py_tools'))  # #522

from kicad_parser import Pad, PCBData, BoardInfo, Via, Segment, Net
from routing_config import GridRouteConfig

LAYERS = ['F.Cu', 'In1.Cu', 'In2.Cu', 'B.Cu']
NET = 7
PAD_X, PAD_Y = 10.0, 10.0     # SMD pad whose stub the switch moves off F.Cu
HOLE_TO_HOLE = 0.2


def _make_pcb(extra_vias):
    """Two disconnected stub groups (get_stub_endpoints needs >= 2 segments in
    >= 2 groups to report free ends): the switch candidate at R1.1 and a far
    target stub at R2.1."""
    pad = Pad(component_ref='R1', pad_number='1', net_id=NET,
              net_name='/SIG', global_x=PAD_X, global_y=PAD_Y,
              local_x=0.0, local_y=0.0, size_x=0.5, size_y=0.5,
              shape='rect', layers=['F.Cu', 'F.Mask', 'F.Paste'], drill=0)
    pad_b = Pad(component_ref='R2', pad_number='1', net_id=NET,
                net_name='/SIG', global_x=20.0, global_y=PAD_Y,
                local_x=0.0, local_y=0.0, size_x=0.5, size_y=0.5,
                shape='rect', layers=['F.Cu', 'F.Mask', 'F.Paste'], drill=0)
    # R1.1's F.Cu escape stub, free end at (10.6, 10.0).
    stubs = [
        Segment(start_x=PAD_X, start_y=PAD_Y, end_x=PAD_X + 0.3, end_y=PAD_Y,
                width=0.2, layer='F.Cu', net_id=NET),
        Segment(start_x=PAD_X + 0.3, start_y=PAD_Y, end_x=PAD_X + 0.6, end_y=PAD_Y,
                width=0.2, layer='F.Cu', net_id=NET),
        # R2.1's own stub -- the second (disconnected) group.
        Segment(start_x=20.0, start_y=PAD_Y, end_x=19.7, end_y=PAD_Y,
                width=0.2, layer='F.Cu', net_id=NET),
    ]
    board_info = BoardInfo(layers={}, copper_layers=list(LAYERS),
                           board_bounds=(0.0, 0.0, 30.0, 30.0))
    pcb = PCBData(board_info=board_info,
                  nets={NET: Net(net_id=NET, name='/SIG', pads=[pad, pad_b])},
                  footprints={}, vias=list(extra_vias), segments=stubs,
                  pads_by_net={NET: [pad, pad_b]})
    return pcb


def _config():
    return GridRouteConfig(grid_step=0.05, clearance=0.09, track_width=0.2,
                           via_size=0.45, via_drill=0.2,
                           hole_to_hole_clearance=HOLE_TO_HOLE,
                           layers=list(LAYERS))


def _via(x, y, net_id=NET, drill=0.2, size=0.45):
    return Via(x=x, y=y, size=size, drill=drill,
               layers=['F.Cu', 'B.Cu'], net_id=net_id)


def run():
    fails = []

    def check(name, cond):
        print(('  PASS  ' if cond else '  FAIL  ') + name)
        if not cond:
            fails.append(name)

    config = _config()

    try:
        from stub_layer_switching import (switch_boxed_stub_near,
                                          fitting_pad_via,
                                          pad_via_drill_conflict)
        have_fix = True
    except ImportError:
        from stub_layer_switching import switch_boxed_stub_near, fitting_pad_via
        pad_via_drill_conflict = None
        have_fix = False
    check("post-fix: pad_via_drill_conflict exists (pre-fix code lacks it)",
          have_fix)

    # 1. Same-net via 0.25mm from the pad centre: a pad via there needs
    #    0.1 + 0.1 + 0.2 = 0.4mm of hole spacing, so the fit funnel must
    #    decline and the whole switch must decline with it.
    pcb = _make_pcb([_via(PAD_X + 0.25, PAD_Y)])
    fit = fitting_pad_via(PAD_X, PAD_Y, NET, pcb, config, set())
    check("fit funnel declines a pad via within hole-to-hole of a same-net via",
          fit is None)

    r = switch_boxed_stub_near(pcb, NET, config, PAD_X + 0.6, PAD_Y,
                               radius_mm=2.5)
    check("switch declines rather than drill the violating pad via", r is None)
    check("no via was appended by the declined switch", len(pcb.vias) == 1)
    check("stub copper untouched by the declined switch",
          all(s.layer == 'F.Cu' for s in pcb.segments))

    # 2. No over-blocking: the same same-net via 5mm away must not block the
    #    switch -- it applies and drills its pad via clear of the far hole.
    pcb2 = _make_pcb([_via(PAD_X + 5.0, PAD_Y)])
    r2 = switch_boxed_stub_near(pcb2, NET, config, PAD_X + 0.6, PAD_Y,
                                radius_mm=2.5)
    check("a far same-net via does not block the switch", r2 is not None)
    if r2 is not None:
        new_v = [v for v in pcb2.vias if abs(v.x - PAD_X) < 0.01
                 and abs(v.y - PAD_Y) < 0.01]
        check("applied switch drilled its pad via at the pad", len(new_v) == 1)
        if new_v:
            d = math.hypot(new_v[0].x - (PAD_X + 5.0), new_v[0].y - PAD_Y)
            need = new_v[0].drill / 2 + 0.2 / 2 + HOLE_TO_HOLE
            check("applied pad via clears hole-to-hole", d >= need)

    if pad_via_drill_conflict is not None:
        # 3. The coincident same-net barrel is the REUSE case: exempt.
        pcb3 = _make_pcb([_via(PAD_X, PAD_Y)])
        check("coincident same-net barrel is exempt (reuse, no second hole)",
              pad_via_drill_conflict(PAD_X, PAD_Y, 0.2, NET, pcb3, config) == "")

        # 4. A FOREIGN via's hole conflicts too (the rule is net-independent).
        pcb4 = _make_pcb([_via(PAD_X + 0.25, PAD_Y, net_id=NET + 1)])
        check("foreign via hole within hole-to-hole conflicts",
              pad_via_drill_conflict(PAD_X, PAD_Y, 0.2, NET, pcb4, config) != "")

        # 5. A drilled PAD's hole conflicts (any net).
        pcb5 = _make_pcb([])
        th = Pad(component_ref='J1', pad_number='2', net_id=NET + 2,
                 net_name='/OTHER', global_x=PAD_X + 0.3, global_y=PAD_Y,
                 local_x=0.0, local_y=0.0, size_x=1.0, size_y=1.0,
                 shape='circle', layers=['*.Cu'], drill=0.4)
        pcb5.pads_by_net[NET + 2] = [th]
        check("drilled pad hole within hole-to-hole conflicts",
              pad_via_drill_conflict(PAD_X, PAD_Y, 0.2, NET, pcb5, config) != "")

        # 6. hole-to-hole disabled (<= 0) is inert.
        cfg0 = _config()
        cfg0.hole_to_hole_clearance = 0.0
        check("h2h <= 0 disables the check",
              pad_via_drill_conflict(PAD_X, PAD_Y, 0.2, NET, pcb, cfg0) == "")

    print('\n' + '=' * 60)
    print(f'  {len(fails) == 0 and "ALL PASS" or f"{len(fails)} FAILED"}')
    print('=' * 60)
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(run())
