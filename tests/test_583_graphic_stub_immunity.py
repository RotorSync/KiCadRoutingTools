#!/usr/bin/env python3
"""
Gate for issue #583: net-tagged copper GRAPHICS must never enter the stub
layer-swap machinery.

On apple1_aci_card (stress set 19, hand-digitized copper stored as net-tagged
gr_lines) the multipoint common-layer collapse walked a net's graphics as
"fanout stubs" and relayered 96 F.Cu graphics to B.Cu -- in pcb_data ONLY. The
writer (correctly) emits graphics from the original file on their original
layer, so the obstacle map lost the F.Cu copper and other nets routed straight
through its clearance zone: 6 confirmed KiCad DRC clearance violations from a
2-net route.

Invariants gated here:
  1. get_stub_segments never walks a graphic (graphic-only chain -> []).
  2. get_stub_info at a graphic free end -> None (nothing movable).
  3. A mixed chain yields only the track part.
  4. apply_stub_layer_switch never mutates a graphic's layer -- neither one
     smuggled in via StubInfo.segments nor one touching the pad position.
  5. connected_stub_segments_on_layer does not extend through graphics.
  6. revert_stub_layer_switch never relabels a coincident same-net graphic
     twin on the destination layer (apple1-style F/B twin geometry).
  7. get_stub_direction ignores a coincident graphic.

Run:
    python3 tests/test_583_graphic_stub_immunity.py
"""

import os
import sys

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(TESTS_DIR)
sys.path.insert(0, ROOT_DIR)
sys.path.insert(0, os.path.join(ROOT_DIR, 'py_router'))  # #522
sys.path.insert(0, os.path.join(ROOT_DIR, 'py_tools'))  # #522

from kicad_parser import PCBData, Segment, Pad, Net
from routing_config import GridRouteConfig
from connectivity import get_stub_segments, get_stub_direction
from stub_layer_switching import (StubInfo, get_stub_info,
                                  apply_stub_layer_switch,
                                  revert_stub_layer_switch,
                                  connected_stub_segments_on_layer)


def _seg(x1, y1, x2, y2, net_id=1, layer='F.Cu', graphic=False, width=0.2):
    return Segment(start_x=x1, start_y=y1, end_x=x2, end_y=y2,
                   width=width, layer=layer, net_id=net_id, graphic=graphic)


def _pad(x, y, net_id=1, drill=0.8):
    return Pad(pad_number='1', net_id=net_id, net_name='N1',
               global_x=x, global_y=y, local_x=0, local_y=0,
               size_x=1.6, size_y=1.6, shape='circle',
               layers=['*.Cu'], drill=drill, pad_type='thru_hole',
               component_ref='A1')


def _pcb(segments, pads=None):
    return PCBData(board_info=None, nets={1: Net(1, 'N1'), 2: Net(2, 'N2')},
                   footprints={}, vias=[], segments=segments,
                   pads_by_net=pads or {})


def main():
    fails = []

    def check(name, cond):
        print(f"  {'PASS' if cond else 'FAIL'}: {name}")
        if not cond:
            fails.append(name)

    config = GridRouteConfig(layers=['F.Cu', 'B.Cu'])

    # -- 1: graphic-only chain is not a walkable stub ------------------------
    print("1: get_stub_segments skips graphics")
    gfx = [_seg(0, 0, 5, 0, graphic=True), _seg(5, 0, 10, 0, graphic=True)]
    pcb = _pcb(list(gfx), pads={1: [_pad(0, 0)]})
    walked = get_stub_segments(pcb, 1, 10, 0, 'F.Cu')
    check("graphic chain walks to []", walked == [])

    # -- 2: get_stub_info at a graphic free end -> None ----------------------
    print("2: get_stub_info returns None for a graphic free end")
    info = get_stub_info(pcb, 1, 10, 0, 'F.Cu')
    check("no StubInfo built from graphics", info is None)

    # -- 3: mixed chain yields only the track part ---------------------------
    print("3: mixed track+graphic chain yields only the track")
    track = _seg(0, 0, 2, 0)
    pcb3 = _pcb([track, _seg(2, 0, 8, 0, graphic=True)], pads={1: [_pad(0, 0)]})
    walked = get_stub_segments(pcb3, 1, 2, 0, 'F.Cu')
    check("only the track is walked", walked == [track])

    # -- 4: apply_stub_layer_switch never relayers a graphic -----------------
    print("4: apply_stub_layer_switch leaves graphics on their layer")
    track = _seg(0, 0, 2, 0)
    smuggled = _seg(2, 0, 8, 0, graphic=True)      # rides in StubInfo.segments
    pad_touch = _seg(0, 0, 0, 3, graphic=True)     # touches the pad position
    pcb4 = _pcb([track, smuggled, pad_touch], pads={1: [_pad(0, 0)]})
    stub = StubInfo(net_id=1, x=2, y=0, layer='F.Cu',
                    segments=[track, smuggled], pad_x=0, pad_y=0,
                    has_pad_via=True)
    vias, mods = apply_stub_layer_switch(pcb4, stub, 'B.Cu', config, debug=False)
    check("track moved to B.Cu", track.layer == 'B.Cu')
    check("smuggled graphic untouched", smuggled.layer == 'F.Cu')
    check("pad-touching graphic untouched", pad_touch.layer == 'F.Cu')
    check("no graphic in the mod records",
          all(m['start'] != (2, 0) or m['end'] != (8, 0) for m in mods)
          and all(m['start'] != (0, 0) or m['end'] != (0, 3) for m in mods))

    # -- 5: component walk does not extend through graphics ------------------
    print("5: connected_stub_segments_on_layer stops at graphics")
    t1 = _seg(0, 0, 2, 0)
    g = _seg(2, 0, 4, 0, graphic=True)
    t2 = _seg(4, 0, 6, 0)   # only "connected" through the graphic
    pcb5 = _pcb([t1, g, t2])
    comp = connected_stub_segments_on_layer(pcb5, 1, 'F.Cu', [t1])
    check("component is just the seed track", comp == [t1])

    # -- 6: revert never relabels a coincident graphic twin ------------------
    print("6: revert_stub_layer_switch skips a graphic twin on the dest layer")
    moved = _seg(0, 0, 2, 0, layer='B.Cu')          # track already swapped F->B
    twin = _seg(0, 0, 2, 0, layer='B.Cu', graphic=True, width=0.3)
    # graphic twin FIRST so a matcher without the guard hits it first
    pcb6 = _pcb([twin, moved])
    mods = [{'start': (0, 0), 'end': (2, 0), 'net_id': 1, 'net_name': 'N1',
             'old_layer': 'F.Cu', 'new_layer': 'B.Cu'}]
    revert_stub_layer_switch(pcb6, mods, [])
    check("track reverted to F.Cu", moved.layer == 'F.Cu')
    check("graphic twin stays on B.Cu", twin.layer == 'B.Cu')

    # -- 7: direction never comes from a graphic -----------------------------
    print("7: get_stub_direction ignores a coincident graphic")
    g_dir = _seg(2, 0, 2, 5, graphic=True)   # would give (0, 1)
    t_dir = _seg(0, 0, 2, 0)                 # gives (1, 0)
    d = get_stub_direction([g_dir, t_dir], 2, 0, 'F.Cu')
    check("direction from the track, not the art", d == (1.0, 0.0))

    print()
    if fails:
        print(f"FAILED: {len(fails)} check(s): {fails}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == '__main__':
    sys.exit(main())
