#!/usr/bin/env python3
"""Regression test: the emit-time same-net via drill hole-to-hole gate
(writer-gap findings 2026-09-01, residual 2 -- the carrier GNDA family).

A single A* path can dive under an obstacle and pop back up 1-2 cells later,
and nothing prices the spacing between the path's OWN vias or against the
net's committed barrels: same-net copper is deliberately not an obstacle,
but KiCad's hole-to-hole floor is net-independent. Measured: carrier GNDA
vias (138.10,81.70)/(137.90,81.80), 0.224mm apart vs 0.40mm needed, emitted
by one route_multipoint_taps run inside the Phase-3 rip-reroute
(tripwire-attributed 2026-09-01).

The fix is _same_net_via_drill_pairs (single_ended_routing), consumed by
three emit gates per the #468 doctrine (exact-check, then decline rather
than ship): the Phase-3 tap loop (blocks the offending cell and RETRIES the
edge, declining after repeated violations), the Phase-1 main-edge
conversion, and the single-ended conversion (both decline like their
sibling terminal-bridge short gates).

    python3 tests/test_same_net_via_pair_gate.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'py_router'))  # #522
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'py_tools'))  # #522

from kicad_parser import Via
from routing_config import GridRouteConfig

NET = 200
H2H = 0.25


def _config(h2h=H2H):
    return GridRouteConfig(grid_step=0.1, clearance=0.1, track_width=0.2,
                           via_size=0.3, via_drill=0.15,
                           hole_to_hole_clearance=h2h,
                           layers=['F.Cu', 'In1.Cu', 'In2.Cu', 'B.Cu'])


def _via(x, y, net_id=NET, drill=0.15, size=0.3):
    return Via(x=x, y=y, size=size, drill=drill,
               layers=['F.Cu', 'B.Cu'], net_id=net_id)


def run():
    fails = []

    def check(name, cond):
        print(('  PASS  ' if cond else '  FAIL  ') + name)
        if not cond:
            fails.append(name)

    try:
        from single_ended_routing import _same_net_via_drill_pairs
    except ImportError:
        check("post-fix: _same_net_via_drill_pairs exists (pre-fix code lacks it)",
              False)
        print('\n' + '=' * 60)
        print('  1 FAILED')
        print('=' * 60)
        return 1
    check("post-fix: _same_net_via_drill_pairs exists (pre-fix code lacks it)",
          True)
    config = _config()

    # 1. The measured carrier GNDA pair: two new vias 0.224mm apart, drills
    #    0.15 + h2h 0.25 -> need 0.40mm. Both emitted by one conversion.
    pair = [_via(138.10, 81.70), _via(137.90, 81.80)]
    bad = _same_net_via_drill_pairs(pair, [], config)
    check("own-path pair 0.224mm apart is flagged (carrier GNDA repro)",
          len(bad) == 1)
    if bad:
        v, o, d, need = bad[0]
        check("the SECOND via of the pair is reported as the new one "
              "(the one the tap gate blocks)",
              (v.x, v.y) == (137.90, 81.80) and (o.x, o.y) == (138.10, 81.70))
        check("distance and requirement are the measured 0.224/0.400",
              abs(d - 0.2236) < 0.001 and abs(need - 0.40) < 1e-9)

    # 2. New via vs a committed board via of the same net.
    bad2 = _same_net_via_drill_pairs([_via(10.0, 10.0)], [_via(10.2, 10.1)],
                                     config)
    check("new via vs committed same-net barrel is flagged", len(bad2) == 1)

    # 3. A FOREIGN via does not pair here (foreign spacing is priced by the
    #    obstacle map's #441 h2h disc; this gate is the same-net writer gap).
    bad3 = _same_net_via_drill_pairs([_via(10.0, 10.0)],
                                     [_via(10.2, 10.1, net_id=NET + 1)],
                                     config)
    check("foreign via is not this gate's business", bad3 == [])

    # 4. Coincident barrels are the reuse/stacked case -- exempt (via dedup
    #    and the stacked_copper disclosure own those).
    bad4 = _same_net_via_drill_pairs([_via(10.0, 10.0)], [_via(10.0, 10.0)],
                                     config)
    check("coincident barrel is exempt", bad4 == [])

    # 5. Spacing is judged on the REAL drills: two 0.15 drills at 0.41mm pass,
    #    a 0.3-drill neighbour at the same distance fails (need 0.475).
    ok5 = _same_net_via_drill_pairs([_via(10.0, 10.0)], [_via(10.41, 10.0)],
                                    config)
    big = _same_net_via_drill_pairs([_via(10.0, 10.0)],
                                    [_via(10.41, 10.0, drill=0.3, size=0.5)],
                                    config)
    check("clear pair at 0.41mm passes with 0.15 drills", ok5 == [])
    check("same distance fails against a 0.3-drill neighbour", len(big) == 1)

    # 6. h2h <= 0 disables the gate.
    check("h2h <= 0 disables the gate",
          _same_net_via_drill_pairs(pair, [], _config(h2h=0.0)) == [])

    # 7. Wiring: the three emit gates consume the helper. Source-level check
    #    so a refactor that silently drops a gate fails here (the chains that
    #    measured the bug are minutes-long; this is the cheap change detector).
    src = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), 'py_router',
        'single_ended_routing.py')).read()
    check("Phase-3 tap gate wired (blocks cell + retries, then declines)",
          src.count('_same_net_via_drill_pairs(') >= 4  # def + 3 gates
          and '_h2h_declines' in src)

    print('\n' + '=' * 60)
    print(f'  {len(fails) == 0 and "ALL PASS" or f"{len(fails)} FAILED"}')
    print('=' * 60)
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(run())
