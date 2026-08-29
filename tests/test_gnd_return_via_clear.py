#!/usr/bin/env python3
"""
Regression test for the GND return-via would-short guard (#468 doctrine,
GND-return-via edition).

route_diff's companion-GND via placer (_create_gnd_vias) computes each GND
return via's position as pure geometry -- a perpendicular offset from the P/N
centerline at every layer change -- and NEVER checked it against foreign
copper. At route_diff time the board is ROUTED, so an unchecked return via
ships a short. Measured on carrier post-merge: TRD1's GND return via at
(72.514,56.048) landed 0.0085mm from TRD1_P's own F.Cu segment
(72.770,55.805)->(72.005,56.570) -- a real Via:GND <-> Seg:TRD1_P DRC short.

_gnd_via_clear exact-checks a candidate GND via site against foreign tracks
(on the outer layers the barrel spans), foreign pads, and foreign via barrels
(copper + drill rules). Own-net (GND) copper is exempt.

Run:
    python3 tests/test_gnd_return_via_clear.py
"""

import os
import sys

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(TESTS_DIR)
sys.path.insert(0, ROOT_DIR)
sys.path.insert(0, os.path.join(ROOT_DIR, 'py_router'))  # #522
sys.path.insert(0, os.path.join(ROOT_DIR, 'py_tools'))  # #522

from kicad_parser import Pad, Segment, Via, PCBData
from routing_config import GridRouteConfig
from diff_pair_routing import _gnd_via_clear

GND_NET = 2
FOREIGN_NET = 80
CLEARANCE = 0.1
VIA_SIZE = 0.3
VIA_DRILL = 0.15


def _cfg():
    cfg = GridRouteConfig()
    cfg.clearance = CLEARANCE
    cfg.via_size = VIA_SIZE
    cfg.via_drill = VIA_DRILL
    cfg.hole_to_hole_clearance = 0.25
    cfg.layers = ['F.Cu', 'B.Cu']
    return cfg


def _seg(net_id, x1, y1, x2, y2, width=0.26, layer='F.Cu'):
    return Segment(start_x=x1, start_y=y1, end_x=x2, end_y=y2,
                   width=width, layer=layer, net_id=net_id)


def _via(net_id, x, y):
    return Via(x=x, y=y, size=VIA_SIZE, drill=VIA_DRILL,
               layers=['F.Cu', 'B.Cu'], net_id=net_id)


def _pcb(segments=None, vias=None, pads_by_net=None):
    return PCBData(footprints={}, nets={}, segments=segments or [],
                   vias=vias or [], board_info=None,
                   pads_by_net=pads_by_net or {})


def main():
    cfg = _cfg()
    results = []

    def check(name, ok, detail=""):
        results.append((name, ok))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}{('  ' + detail) if detail else ''}")

    # 1. The measured carrier short: a GND via 0.0085mm from a foreign F.Cu
    #    segment must be rejected.
    pcb = _pcb(segments=[_seg(FOREIGN_NET, 72.770, 55.805, 72.005, 56.570)])
    r = _gnd_via_clear(pcb, 72.514, 56.048, cfg, GND_NET)
    check("GND via overlapping foreign F.Cu segment rejected", r is False)

    # 2. A clear site far from any copper passes.
    pcb2 = _pcb(segments=[_seg(FOREIGN_NET, 72.770, 55.805, 72.005, 56.570)])
    r = _gnd_via_clear(pcb2, 10.0, 10.0, cfg, GND_NET)
    check("clear GND via site passes", r is True)

    # 3. Own-net (GND) copper is exempt -- the return via lands near GND pours.
    pcb3 = _pcb(segments=[_seg(GND_NET, 72.770, 55.805, 72.005, 56.570)])
    r = _gnd_via_clear(pcb3, 72.514, 56.048, cfg, GND_NET)
    check("own-net GND copper exempt", r is True)

    # 4. A foreign via barrel within copper clearance is rejected.
    pcb4 = _pcb(vias=[_via(FOREIGN_NET, 72.514 + VIA_SIZE + CLEARANCE - 0.02,
                           56.048)])
    r = _gnd_via_clear(pcb4, 72.514, 56.048, cfg, GND_NET)
    check("foreign via within copper clearance rejected", r is False)

    # 5. A foreign via clearly beyond both copper and drill clearance passes.
    pcb5 = _pcb(vias=[_via(FOREIGN_NET, 72.514 + VIA_SIZE + CLEARANCE + 0.02,
                           56.048)])
    r = _gnd_via_clear(pcb5, 72.514, 56.048, cfg, GND_NET)
    check("foreign via beyond clearance passes", r is True)

    # 6. A foreign pad within clearance is rejected.
    pad = Pad(component_ref='U1', pad_number='A1',
              global_x=72.514 + 0.15 + CLEARANCE - 0.02,
              global_y=56.048, local_x=0.0, local_y=0.0,
              size_x=0.3, size_y=0.3, shape='rect', layers=['F.Cu'],
              net_id=FOREIGN_NET, net_name='/TRD1_P')
    pcb6 = _pcb(pads_by_net={FOREIGN_NET: [pad]})
    r = _gnd_via_clear(pcb6, 72.514, 56.048, cfg, GND_NET)
    check("foreign pad within clearance rejected", r is False)

    # 7. A foreign pad on B.Cu only is still rejected (the barrel spans it).
    pad_b = Pad(component_ref='U1', pad_number='A1',
                global_x=72.514 + 0.15 + CLEARANCE - 0.02,
                global_y=56.048, local_x=0.0, local_y=0.0,
                size_x=0.3, size_y=0.3, shape='rect', layers=['B.Cu'],
                net_id=FOREIGN_NET, net_name='/TRD1_P')
    pcb7 = _pcb(pads_by_net={FOREIGN_NET: [pad_b]})
    r = _gnd_via_clear(pcb7, 72.514, 56.048, cfg, GND_NET)
    check("foreign B.Cu pad within clearance rejected", r is False)

    # 8. The measured carrier case: the overlapping TRD1_P segment is the
    #    pair's OWN new copper (not yet in pcb_data) -- it must be caught via
    #    extra_segments, or the return via ships a short.
    pcb8 = _pcb()  # empty board: the P/N segs are only in extra_segments
    new_segs = [_seg(FOREIGN_NET, 72.770, 55.805, 72.005, 56.570)]
    r = _gnd_via_clear(pcb8, 72.514, 56.048, cfg, GND_NET,
                       extra_segments=new_segs)
    check("own new P/N segment (extra_segments) rejected", r is False)

    # 9. Without extra_segments the same site passes (old behavior) -- the
    #    guard must be called WITH the pair's new segments to catch this.
    r = _gnd_via_clear(pcb8, 72.514, 56.048, cfg, GND_NET)
    check("same site passes without extra_segments (caller must pass them)",
          r is True)

    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print("\n" + "=" * 60)
    print(f"  {passed}/{total} checks passed")
    print("=" * 60)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
