#!/usr/bin/env python3
"""Regression test: a #189 via-in-pad unblock must not land within hole-to-hole
of an EARLIER same-net tap-edge via (the VIN_PROT-family drill-spacing bug).

Phase-3 tap routing places vias edge by edge. Each edge's vias are accumulated
in route_multipoint_taps' all_vias but are NOT yet in pcb_data and NOT in any
inflight window -- so _place_shrunk_via_in_pad_impl's local via obstacle map
never saw them and could drop a #189 unblock via within hole-to-hole of one.

Measured on glasgow_revC at heuristic_weight 1.2 (hw12 arm): /~{ALERT} vias at
(76.2,90.3) and (75.915,90.3) -- the second a via-in-pad at R17.1's centre --
0.285mm apart vs the 0.4mm the board's min_hole_to_hole demands (0.2 drill +
0.2 hole_to_hole). A same-net via pair violating hole-to-hole is a fab defect
regardless of net.

The fix threads the net's in-progress tap-edge vias into
_place_shrunk_via_in_pad_impl as same_net_inprogress_vias, which merges them
into try_tap_pad's extra_vias so the local via obstacle map blocks cells within
hole-to-hole of them (build_via_obstacle_map blocks ALL vias' drills, same-net
included).

    python3 tests/test_same_net_unblock_via_drill.py
"""
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'py_router'))  # #522
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'py_tools'))  # #522

from kicad_parser import Pad, PCBData, BoardInfo, Via
from routing_config import GridRouteConfig, GridCoord
from obstacle_map import GridObstacleMap
from single_ended_routing import _place_shrunk_via_in_pad_impl

LAYERS = ['F.Cu', 'In1.Cu', 'In2.Cu', 'B.Cu']
NET = 29          # /~{ALERT}
EARLIER_X = 76.2  # the earlier tap-edge via's x (same y=90.3)
PAD_X = 75.915    # R17.1 pad centre (via-in-pad site)
PAD_Y = 90.3
HOLE_TO_HOLE = 0.2


def _make_pcb():
    """R17.1 (/~{ALERT}) SMD pad; NO vias in pcb_data (the earlier tap-edge via
    lives only in all_vias, exactly like the real Phase-3 state)."""
    pad = Pad(component_ref='R17', pad_number='1', net_id=NET,
              net_name='/~{ALERT}', global_x=PAD_X, global_y=PAD_Y,
              local_x=0.0, local_y=0.0, size_x=0.59, size_y=0.64,
              shape='rect', layers=['F.Cu', 'F.Mask', 'F.Paste'], drill=0)
    board_info = BoardInfo(layers={}, copper_layers=list(LAYERS),
                           board_bounds=(60.0, 70.0, 140.0, 110.0))
    pcb = PCBData(board_info=board_info, nets={}, footprints={}, vias=[],
                  segments=[], pads_by_net={NET: [pad]})
    return pcb, pad


def _config():
    return GridRouteConfig(grid_step=0.05, clearance=0.09, track_width=0.2,
                           via_size=0.45, via_drill=0.2,
                           hole_to_hole_clearance=HOLE_TO_HOLE,
                           layers=list(LAYERS))


def _earlier_via():
    return Via(x=EARLIER_X, y=PAD_Y, size=0.45, drill=0.2,
               layers=['F.Cu', 'B.Cu'], net_id=NET)


def run():
    fails = []

    def check(name, cond):
        print(('  PASS  ' if cond else '  FAIL  ') + name)
        if not cond:
            fails.append(name)

    config = _config()
    coord = GridCoord(config.grid_step)
    layer_names = list(LAYERS)

    # 1. WITHOUT the fix (no same_net_inprogress_vias): the unblock places a
    #    via-in-pad at R17.1's centre, 0.285mm from the earlier same-net via --
    #    a hole-to-hole violation (needs 0.4mm).
    pcb, pad = _make_pcb()
    obs = GridObstacleMap(len(LAYERS))
    r = _place_shrunk_via_in_pad_impl(pad, obs, config, pcb, NET, coord,
                                      layer_names)
    check("pre-fix: unblock places a via (bug reproduces)", r is not None)
    if r is not None:
        via = r[0]
        d = math.hypot(via.x - EARLIER_X, via.y - PAD_Y)
        need = via.drill / 2 + 0.2 / 2 + HOLE_TO_HOLE
        check("pre-fix: placed via violates hole-to-hole vs earlier same-net via",
              d < need)

    # 2. WITH the fix: the earlier same-net via is threaded in as
    #    same_net_inprogress_vias; the local map blocks cells within hole-to-hole
    #    of it, so the unblock either moves away or declines -- never ships a
    #    violation. On PRE-FIX code the kwarg does not exist and this call raises
    #    TypeError -- that is the test failing on pre-fix code (the fix is what
    #    makes the kwarg exist), so it is reported as a FAIL, not a crash.
    pcb2, pad2 = _make_pcb()
    obs2 = GridObstacleMap(len(LAYERS))
    try:
        r2 = _place_shrunk_via_in_pad_impl(pad2, obs2, config, pcb2, NET, coord,
                                           layer_names,
                                           same_net_inprogress_vias=[_earlier_via()])
    except TypeError:
        check("post-fix: same_net_inprogress_vias supported (pre-fix code lacks it)",
              False)
        r2 = None
    if r2 is None:
        check("post-fix: unblock declines rather than ship a violation", True)
    else:
        via2 = r2[0]
        d2 = math.hypot(via2.x - EARLIER_X, via2.y - PAD_Y)
        need2 = via2.drill / 2 + 0.2 / 2 + HOLE_TO_HOLE
        check("post-fix: placed via clears hole-to-hole vs earlier same-net via",
              d2 >= need2)

    # 3. A far earlier via must NOT block the unblock (no over-blocking): place
    #    the earlier via 5mm away and confirm the unblock still succeeds.
    pcb3, pad3 = _make_pcb()
    obs3 = GridObstacleMap(len(LAYERS))
    far = Via(x=PAD_X + 5.0, y=PAD_Y, size=0.45, drill=0.2,
              layers=['F.Cu', 'B.Cu'], net_id=NET)
    try:
        r3 = _place_shrunk_via_in_pad_impl(pad3, obs3, config, pcb3, NET, coord,
                                           layer_names,
                                           same_net_inprogress_vias=[far])
    except TypeError:
        r3 = None
    check("post-fix: a far same-net via does not block the unblock",
          r3 is not None)

    print('\n' + '=' * 60)
    print(f'  {len(fails) == 0 and "ALL PASS" or f"{len(fails)} FAILED"}')
    print('=' * 60)
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(run())
