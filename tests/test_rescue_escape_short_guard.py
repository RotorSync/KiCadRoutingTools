#!/usr/bin/env python3
"""
Regression test for the #666 bare-ball fanout-rescue escape would-short guard.

The fanout-rescue escape (net_rescue's bare-ball rung) calls
generate_bga_fanout, whose conflict model covers balls/teeth/passives but NOT
this run's already-routed tracks -- at rescue time the board is ROUTED, and an
unchecked escape ships a short. Measured on glasgow_revC post-merge: /FLAGB's
U30.K5 fanout-rescue escape crossed /CLKREF's F.Cu diagonal at (79.412,94.412)
-- 5 segment-segment overlaps + 1 crossing DRC.

_rescue_escape_clear exact-checks every emitted escape seg and via against
foreign copper (pads, same-layer tracks, via barrels) and declines the escape
like any other no-escape outcome rather than ship it.

Run:
    python3 tests/test_rescue_escape_short_guard.py
"""

import os
import sys

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(TESTS_DIR)
sys.path.insert(0, ROOT_DIR)
sys.path.insert(0, os.path.join(ROOT_DIR, 'py_router'))  # #522
sys.path.insert(0, os.path.join(ROOT_DIR, 'py_tools'))  # #522

from kicad_parser import BoardInfo
from routing_config import GridRouteConfig
from synth import make_pad, make_pcb, make_seg, make_via

import net_rescue
from net_rescue import _rescue_escape_clear

VICTIM, FOREIGN = 1, 2
CLEARANCE = 0.09
WIDTH = 0.2


def _cfg():
    c = GridRouteConfig()
    c.layers = ['F.Cu', 'B.Cu']
    c.grid_step = 0.05
    c.clearance = CLEARANCE
    c.track_width = WIDTH
    c.via_size = 0.45
    c.via_drill = 0.2
    c.hole_to_hole_clearance = 0.2
    return c


def _board():
    bi = BoardInfo(layers={0: 'F.Cu', 31: 'B.Cu'},
                   copper_layers=['F.Cu', 'B.Cu'],
                   board_bounds=(-1.0, -1.0, 4.0, 1.0))
    return make_pcb(
        nets={VICTIM: __import__('synth').make_net(VICTIM, 'VICTIM'),
              FOREIGN: __import__('synth').make_net(FOREIGN, 'FOREIGN')},
        segments=[], pads_by_net={}, board_info=bi)


def main():
    cfg = _cfg()
    results = []

    def check(name, ok, detail=""):
        results.append((name, ok))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}{('  ' + detail) if detail else ''}")

    # 1. The measured glasgow short: an escape seg crossing a foreign F.Cu
    #    diagonal must be declined.
    pcb = _board()
    pcb.segments.append(make_seg(77.90, 92.90, 79.70, 94.70,
                                 net_id=FOREIGN, width=WIDTH))
    # The /FLAGB escape seg that crossed /CLKREF at (79.412,94.412).
    escape_segs = [make_seg(79.450, 94.375, 79.375, 94.450,
                            net_id=VICTIM, width=0.0762)]
    r = _rescue_escape_clear(pcb, escape_segs, [], cfg, VICTIM)
    check("escape seg crossing foreign diagonal declined", r is not None,
          f"reason={r}" if r else "NOT detected")

    # 2. An escape seg clear of all foreign copper passes.
    pcb2 = _board()
    pcb2.segments.append(make_seg(77.90, 92.90, 79.70, 94.70,
                                  net_id=FOREIGN, width=WIDTH))
    clear_segs = [make_seg(80.0, 90.0, 81.0, 91.0, net_id=VICTIM, width=0.2)]
    r = _rescue_escape_clear(pcb2, clear_segs, [], cfg, VICTIM)
    check("clear escape seg passes", r is None)

    # 3. An escape via overlapping a foreign via barrel is declined.
    pcb3 = _board()
    pcb3.vias.append(make_via(1.0, 1.0, net_id=FOREIGN, size=0.45))
    escape_vias = [make_via(1.0 + 0.45 + CLEARANCE - 0.02, 1.0,
                            net_id=VICTIM, size=0.45)]
    r = _rescue_escape_clear(pcb3, [], escape_vias, cfg, VICTIM)
    check("escape via overlapping foreign via declined", r is not None,
          f"reason={r}" if r else "NOT detected")

    # 4. An escape seg grazing a foreign pad is declined.
    pcb4 = _board()
    pad = make_pad(FOREIGN, 1.0, 1.0, ref='U1', num='1', net_name='FOREIGN',
                   size_x=0.3, size_y=0.3)
    pcb4.pads_by_net[FOREIGN] = [pad]
    # Seg passing within clearance of the pad center: pad half 0.15 + seg
    # half-width 0.1 + clearance 0.09 = 0.34mm centerline need; place the
    # seg 0.30mm from the pad center (inside the band).
    graze_segs = [make_seg(1.0 - 0.30, 1.0, 1.0 + 0.30, 1.0,
                           net_id=VICTIM, width=0.2)]
    r = _rescue_escape_clear(pcb4, graze_segs, [], cfg, VICTIM)
    check("escape seg grazing foreign pad declined", r is not None,
          f"reason={r}" if r else "NOT detected")

    # 5. Own-net copper is exempt (the escape lands on its own ball).
    pcb5 = _board()
    pcb5.segments.append(make_seg(77.90, 92.90, 79.70, 94.70,
                                  net_id=VICTIM, width=WIDTH))
    r = _rescue_escape_clear(pcb5, escape_segs, [], cfg, VICTIM)
    check("own-net copper exempt", r is None)

    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print("\n" + "=" * 60)
    print(f"  {passed}/{total} checks passed")
    print("=" * 60)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
