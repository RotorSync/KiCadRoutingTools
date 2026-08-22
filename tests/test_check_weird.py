#!/usr/bin/env python3
"""Tests for check_weird.py (read-only copper hygiene checker).

Synthetic Segment/Via/Pad cases:
  * a dangling spur end            -> dangling-end flagged
  * a fanout stub ending on a pad  -> NOT flagged (clean)
  * a soft joint (cap overlap)     -> soft-joint flagged (and NOT dangling)
  * a duplicate segment            -> stacked-copper flagged
  * a square loop of segments      -> redundant-cycle flagged
  * a clean two-pad net            -> no findings at all
  * a half-segment tail past a mid-body via anchor -> dangling-end (tail)
  * a floating via                 -> unsupported-via flagged
  * a corner-graze terminal cap    -> narrow-pad-joint flagged (#696/#416)
  * the same cap landing in the pad body -> NOT flagged (clean)

Plus two guards on the REPORTER, which is what #696 actually broke:
  * every category any _finding(...) emits is registered in CATEGORIES
  * print_report names a category that is NOT in CATEGORIES, and its headline
    count equals the sum of the per-category counts

    python3 tests/test_check_weird.py
"""
import ast
import io
import os
import re
import sys
from collections import Counter
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'py_router'))  # #522
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'py_tools'))  # #522

from kicad_parser import Pad, Segment, Via, Zone, Net, BoardInfo, PCBData
import check_weird as check_weird_mod
from check_weird import check_weird, print_report, CATEGORIES

NET = 1
NAME = '/TEST'


def _pad(x, y, size=0.6, layers=('F.Cu',), drill=0.0, num='1', ref='U1'):
    return Pad(component_ref=ref, pad_number=num, global_x=x, global_y=y,
               local_x=0, local_y=0, size_x=size, size_y=size, shape='circle',
               layers=list(layers), net_id=NET, net_name=NAME,
               rotation=0.0, drill=drill)


def _rect_pad(x, y, w, h, layers=('F.Cu',), num='1', ref='U1'):
    # _check_terminal_web only considers rect/roundrect/oval pads -- a circle
    # has no corner to graze, so _pad() above cannot exercise it.
    return Pad(component_ref=ref, pad_number=num, global_x=x, global_y=y,
               local_x=0, local_y=0, size_x=w, size_y=h, shape='rect',
               layers=list(layers), net_id=NET, net_name=NAME,
               rotation=0.0, drill=0.0)


def _emitted_categories():
    """Every category literal any `_finding(...)` call in check_weird.py emits,
    read from the source. This is how #696 is caught at test time: the category
    existed and the emission worked, and only the CATEGORIES entry was missing
    -- so nothing that merely RUNS the checker could see the gap."""
    tree = ast.parse(io.open(check_weird_mod.__file__, encoding='utf-8').read())
    out = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == '_finding' and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)):
            out.add(node.args[0].value)
    return out


def _seg(x1, y1, x2, y2, layer='F.Cu', width=0.2):
    return Segment(start_x=x1, start_y=y1, end_x=x2, end_y=y2,
                   width=width, layer=layer, net_id=NET)


def _via(x, y, size=0.6, drill=0.3):
    return Via(x=x, y=y, size=size, drill=drill,
               layers=['F.Cu', 'B.Cu'], net_id=NET)


def _pcb(segments, vias=(), pads=(), zones=()):
    return PCBData(
        board_info=BoardInfo(layers={}, copper_layers=['F.Cu', 'B.Cu']),
        nets={NET: Net(net_id=NET, name=NAME, pads=list(pads))},
        footprints={},
        vias=list(vias),
        segments=list(segments),
        pads_by_net={NET: list(pads)},
        zones=list(zones))


def _cats(findings):
    return Counter(f['category'] for f in findings)


def main():
    results = []

    # 1. Dangling spur: pad-to-pad trunk plus a spur teeing into its middle;
    #    the spur's free end at (5, 3) connects nothing.
    pads = [_pad(0, 0, num='1'), _pad(10, 0, num='2', ref='U2')]
    pcb = _pcb([_seg(0, 0, 10, 0), _seg(5, 0, 5, 3)], pads=pads)
    f, _ = check_weird(pcb)
    c = _cats(f)
    dangles = [x for x in f if x['category'] == 'dangling-end']
    results.append(("dangling spur end flagged", c['dangling-end'] == 1))
    results.append(("dangle reported at the free end (5, 3)",
                    len(dangles) == 1 and abs(dangles[0]['x'] - 5) < 1e-6
                    and abs(dangles[0]['y'] - 3) < 1e-6))
    results.append(("spur root (T-junction) NOT flagged as dangling",
                    all((x['x'], x['y']) != (5.0, 0.0) for x in dangles)))

    # 2. Fanout stub ending on a pad (via F.Cu stub + B.Cu run): clean.
    pads = [_pad(0, 0, num='1'), _pad(3, 0, layers=('B.Cu',), num='2', ref='U2')]
    pcb = _pcb([_seg(0, 0, 1, 0), _seg(1, 0, 3, 0, layer='B.Cu')],
               vias=[_via(1, 0)], pads=pads)
    f, _ = check_weird(pcb)
    results.append(("fanout stub ending on pad/via NOT flagged", len(f) == 0))

    # 3. Soft joint: two collinear tracks whose endpoints stop 0.05mm short of
    #    each other -- caps overlap ((w1+w2)/2 = 0.2 > 0.05 > min gap 0.01).
    pads = [_pad(0, 0, num='1'), _pad(10, 0, num='2', ref='U2')]
    pcb = _pcb([_seg(0, 0, 5, 0), _seg(5.05, 0, 10, 0)], pads=pads)
    # gap 0.05 < default tolerance 0.1: detection is exercised at tolerance=0,
    # and the default-filter behavior is asserted right after.
    f, _ = check_weird(pcb, tolerance=0)
    c = _cats(f)
    results.append(("soft joint flagged", c['soft-joint'] == 1))
    results.append(("soft-joint ends not double-reported as dangling",
                    c['dangling-end'] == 0))

    # 4. Duplicate segment (stacked copper).
    pads = [_pad(0, 0, num='1'), _pad(10, 0, num='2', ref='U2')]
    pcb = _pcb([_seg(0, 0, 10, 0), _seg(0, 0, 10, 0)], pads=pads)
    f, _ = check_weird(pcb)
    c = _cats(f)
    results.append(("duplicate segment flagged as stacked-copper",
                    c['stacked-copper'] == 1))
    results.append(("each duplicate is individually removable",
                    c['removable-segment'] == 2))

    # 5. Square loop: 4 edges between two pads; one edge is a redundant cycle.
    pads = [_pad(0, 0, num='1'), _pad(10, 0, num='2', ref='U2')]
    pcb = _pcb([_seg(0, 0, 10, 0), _seg(10, 0, 10, 10),
                _seg(10, 10, 0, 10), _seg(0, 10, 0, 0)], pads=pads)
    f, _ = check_weird(pcb)
    c = _cats(f)
    results.append(("square loop flagged as redundant-cycle",
                    c['redundant-cycle'] == 1))
    results.append(("every loop edge individually removable (superset)",
                    c['removable-segment'] == 4))
    results.append(("loop has no dangling ends", c['dangling-end'] == 0))

    # 6. Clean two-pad net: one segment pad to pad; nothing weird.
    pads = [_pad(0, 0, num='1'), _pad(10, 0, num='2', ref='U2')]
    pcb = _pcb([_seg(0, 0, 10, 0)], pads=pads)
    f, _ = check_weird(pcb)
    results.append(("clean two-pad net has no findings", len(f) == 0))

    # 7. Half-segment tail past a mid-body via anchor (#347 class): the trunk
    #    is load-bearing THROUGH the via at (6, 0), but 4mm of copper past it
    #    dangles.
    pads = [_pad(0, 0, num='1'), _pad(6, 5, layers=('B.Cu',), num='2', ref='U2')]
    pcb = _pcb([_seg(0, 0, 10, 0), _seg(6, 0, 6, 5, layer='B.Cu')],
               vias=[_via(6, 0)], pads=pads)
    f, _ = check_weird(pcb)
    c = _cats(f)
    dangles = [x for x in f if x['category'] == 'dangling-end']
    results.append(("half-segment tail flagged as dangling-end",
                    c['dangling-end'] == 1))
    results.append(("tail reported past the body anchor with its length",
                    len(dangles) == 1
                    and 'body anchor' in dangles[0]['detail']
                    and '4.000mm' in dangles[0]['detail']
                    and abs(dangles[0]['x'] - 10) < 1e-6))
    results.append(("half-dangle case has no other findings", len(f) == 1))

    # 8. Floating via far from any copper.
    pads = [_pad(0, 0, num='1'), _pad(10, 0, num='2', ref='U2')]
    pcb = _pcb([_seg(0, 0, 10, 0)], vias=[_via(20, 20)], pads=pads)
    f, _ = check_weird(pcb)
    c = _cats(f)
    results.append(("floating via flagged as unsupported-via",
                    c['unsupported-via'] == 1))

    # 9. Coincident same-net vias (stacked copper, via variant).
    pads = [_pad(0, 0, num='1'), _pad(10, 0, num='2', ref='U2')]
    pcb = _pcb([_seg(0, 0, 10, 0)],
               vias=[_via(10, 0), _via(10.005, 0)], pads=pads)
    f, _ = check_weird(pcb)
    c = _cats(f)
    results.append(("coincident vias flagged as stacked-copper",
                    c['stacked-copper'] == 1))

    # Soft joints carry size=None deliberately (check_weird.py, the note in
    # _check_soft_joints: a SMALLER gap is still fragile, so filtering by size
    # would invert severity, and on <=0.1mm routing the whole category vanished
    # at the default). So the default 0.1mm tolerance must NOT hide the 0.05mm
    # joint of case 3. This assertion previously ran on the coincident-VIAS
    # board above -- which has no soft joint at all -- so it passed vacuously
    # while claiming the opposite of the shipped behavior.
    soft_pcb = _pcb([_seg(0, 0, 5, 0), _seg(5.05, 0, 10, 0)],
                    pads=[_pad(0, 0, num='1'), _pad(10, 0, num='2', ref='U2')])
    f_tol, _ = check_weird(soft_pcb)
    results.append(("0.05mm soft joint survives the default 0.1mm tolerance",
                    len([x for x in f_tol if x['category'] == 'soft-joint']) == 1))

    # Orphan island: two joined segments + via, nowhere near any pad of the
    # net (pads at 0/10, island at 50) -> flagged with its total length; a
    # sub-tolerance island is hidden by the default 0.1mm filter.
    pcb = _pcb([_seg(0, 0, 10, 0),
                _seg(50, 5, 52, 5), _seg(52, 5, 52, 7, layer='B.Cu')],
               vias=[_via(52, 5)],
               pads=[_pad(0, 0, num='1'), _pad(10, 0, num='2', ref='U2')])
    f, _ = check_weird(pcb)
    isl = [x for x in f if x['category'] == 'orphan-island']
    results.append(("orphan pad-less island flagged",
                    len(isl) == 1 and '4.00mm' in isl[0]['detail']))
    pcb2 = _pcb([_seg(0, 0, 10, 0), _seg(50, 5, 50.05, 5)],
                pads=[_pad(0, 0, num='1'), _pad(10, 0, num='2', ref='U2')])
    f2, _ = check_weird(pcb2)
    results.append(("sub-tolerance orphan island hidden by default",
                    not [x for x in f2 if x['category'] == 'orphan-island']))

    # 10. Narrow pad joint (#416): a 0.2mm track whose free cap overlaps a
    #     1.0x1.0mm rect pad only at the CORNER. The floor is the board's
    #     thinnest track (0.2), so erosion by 0.1 parts the cap from the pad:
    #     a sub-floor web. Flagged, and NOT dropped by the default 0.1mm
    #     tolerance (size=None is deliberate -- for a web, thinner is worse).
    pads = [_rect_pad(0, 0, 1.0, 1.0, num='1'),
            _rect_pad(10, 0, 1.0, 1.0, num='2', ref='U2')]
    pcb = _pcb([_seg(0.55, 0.55, 2.0, 2.0)], pads=pads)
    f, _ = check_weird(pcb)
    necks = [x for x in f if x['category'] == 'narrow-pad-joint']
    results.append(("corner-graze terminal cap flagged as narrow-pad-joint",
                    len(necks) == 1))
    results.append(("narrow-pad-joint reported at the cap (0.55, 0.55)",
                    len(necks) == 1 and abs(necks[0]['x'] - 0.55) < 1e-6
                    and abs(necks[0]['y'] - 0.55) < 1e-6))
    results.append(("narrow-pad-joint survives the default 0.1mm tolerance",
                    len(necks) == 1 and necks[0]['size'] is None))
    #     Negative control, same pad and same track: a cap landing in the pad
    #     BODY joins through full-width copper and is NOT flagged. Without it
    #     the case above would also pass on a check that flagged everything.
    pcb = _pcb([_seg(0.0, 0.0, 2.0, 2.0)], pads=pads)
    f, _ = check_weird(pcb)
    results.append(("cap landing in the pad body NOT flagged",
                    not [x for x in f if x['category'] == 'narrow-pad-joint']))

    # 11. The reporter, which is what #696 actually broke: a finding whose
    #     category is missing from CATEGORIES counted toward the headline and
    #     the exit code but printed nothing, so the board was blocked by a
    #     defect that was never named. Both halves are pinned here.
    emitted = _emitted_categories()
    results.append(("the source emits categories at all (guard not vacuous)",
                    len(emitted) >= 8))
    results.append(("every emitted category is registered in CATEGORIES",
                    bool(emitted) and emitted <= set(CATEGORIES)))

    stray = [{'category': 'brand-new-category', 'net': NAME, 'layer': 'F.Cu',
              'x': 1.0, 'y': 2.0, 'detail': 'a category nobody registered',
              'size': None}]
    buf = io.StringIO()
    with redirect_stdout(buf):
        print_report(stray, [])
    out = buf.getvalue()
    results.append(("print_report names a category missing from CATEGORIES",
                    'brand-new-category: 1' in out
                    and 'a category nobody registered' in out))
    counts = [int(m) for m in re.findall('^  [^ ]+: ([0-9]+)$', out, re.M)]
    head = re.search('FOUND ([0-9]+) WEIRD THINGS', out)
    results.append(("the headline count equals the sum of printed counts",
                    head is not None and bool(counts)
                    and int(head.group(1)) == sum(counts) == 1))

    passed = 0
    for name, ok in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
        passed += bool(ok)
    print(f"\n{passed}/{len(results)} check_weird tests passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
