#!/usr/bin/env python3
"""Regression test: the post-route cleanup same-net via drill-hole merge must
drop the merged-away via from the WRITE-LIST (results[].new_vias), not just
from pcb_data -- otherwise the writer re-emits it and the DRC violation ships.

Root case: a rip-restore / partial-restore can leave two same-net vias within
hole-to-hole of each other (one from the pre-rip route, one from the escape
stub / partial piece). The per-net via map's hole-to-hole block never fires
between same-net vias, and the via nudge only moves sub-cap grazes, so the
pair ships as a real fab DRC hit (KiCad's hole-to-hole is net-independent).
Measured on ulx3s hw12 SDRAM_D15: vias at (143.70,86.50) and (143.68,86.60),
0.102mm apart vs the 0.35mm needed (0.15/2 + 0.15/2 + 0.2).

The cleanup pipeline's merge pass (cleanup_pipeline.py, after the via nudge)
calls merge_close_same_net_vias with the REAL Via objects from
results[].new_vias (the same objects add_route_to_pcb_data appended to
pcb_data.vias), then rebinds each result's new_vias to the kept subset so
board == write model. The pre-fix version passed DICT COPIES, so the merge's
all_new_vias[:] = kept only mutated the local copy list and the dropped via
stayed in results[].new_vias -- the writer emitted it anyway.

    python3 tests/test_cleanup_same_net_via_merge.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'py_router'))  # #522
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'py_tools'))  # #522

from kicad_parser import Via
from pcb_modification import merge_close_same_net_vias

# Measured ulx3s hw12 SDRAM_D15 pair (writer-gap findings 2026-09-01).
V1 = (143.70, 86.50)
V2 = (143.68, 86.60)
DRILL = 0.15
SIZE = 0.3
HOLE_TO_HOLE = 0.2
NET = 42


class _Seg:
    def __init__(self, sx, sy, ex, ey, net_id=NET):
        self.start_x, self.start_y = sx, sy
        self.end_x, self.end_y = ex, ey
        self.net_id = net_id


class _PCB:
    def __init__(self, vias, segments):
        self.vias = vias
        self.segments = segments
        self.pads_by_net = {}
        self.board_info = type('BI', (), {'copper_layers': ['F.Cu', 'B.Cu']})()
        self.nets = {NET: type('N', (), {'name': 'SDRAM_D15'})()}


def _via(x, y):
    return Via(x=x, y=y, size=SIZE, drill=DRILL,
               layers=['F.Cu', 'B.Cu'], net_id=NET)


def run():
    fails = []

    def check(name, cond):
        print(('  PASS  ' if cond else '  FAIL  ') + name)
        if not cond:
            fails.append(name)

    # The cleanup pipeline's exact call shape: results[].new_vias holds REAL
    # Via objects (add_route_to_pcb_data appended them), and the merge is
    # followed by a rebind of each result's new_vias to the kept subset.
    v1 = _via(*V1)
    v2 = _via(*V2)
    seg = _Seg(V2[0], V2[1], V2[0], 87.00)  # segment ends on the dropped via
    pcb = _PCB([v1, v2], [seg])
    results = [{'new_vias': [v1], 'new_segments': []},
               {'new_vias': [v2], 'new_segments': [seg]}]

    all_v = [v for r in results for v in (r.get('new_vias') or [])]
    all_s = [s for r in results for s in (r.get('new_segments') or [])]
    try:
        merged = merge_close_same_net_vias(all_v, all_s, pcb, HOLE_TO_HOLE,
                                           verbose=False)
    except TypeError:
        # Pre-fix code reads nv['x'] etc. and cannot take real Via objects --
        # that is the bug this test pins (the fix duck-types via access).
        check("merge accepts real Via objects (pre-fix code crashes)", False)
        merged = 0

    check("merge fires on the measured pair", merged == 1)
    check("pcb_data dropped the merged-away via",
          len(pcb.vias) == 1 and abs(pcb.vias[0].x - V1[0]) < 1e-4)
    check("pcb_data segment re-anchored to survivor",
          abs(seg.start_x - V1[0]) < 1e-4 and abs(seg.start_y - V1[1]) < 1e-4)

    # The write-list rebind (the fix): each result's new_vias must drop the
    # merged-away via so the writer does not re-emit it.
    kept_ids = {id(v) for v in all_v}
    for r in results:
        nv = r.get('new_vias') or []
        if any(id(v) not in kept_ids for v in nv):
            r['new_vias'] = [v for v in nv if id(v) in kept_ids]
    emitted = [v for r in results for v in (r.get('new_vias') or [])]
    check("write-list emits only the survivor",
          len(emitted) == 1 and abs(emitted[0].x - V1[0]) < 1e-4)
    check("no result still references the dropped via",
          all(id(v) != id(v2) for r in results for v in (r.get('new_vias') or [])))

    # A far same-net via must NOT be merged (no over-merging): 5mm apart.
    v3 = _via(*V1)
    v4 = _via(V1[0] + 5.0, V1[1])
    pcb2 = _PCB([v3, v4], [])
    try:
        n2 = merge_close_same_net_vias([v3, v4], [], pcb2, HOLE_TO_HOLE,
                                       verbose=False)
    except TypeError:
        n2 = -1
    check("far same-net vias are not merged", n2 == 0 and len(pcb2.vias) == 2)

    print('\n' + '=' * 60)
    print(f'  {len(fails) == 0 and "ALL PASS" or f"{len(fails)} FAILED"}')
    print('=' * 60)
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(run())
