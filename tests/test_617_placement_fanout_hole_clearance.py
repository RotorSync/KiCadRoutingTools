#!/usr/bin/env python3
"""Issue #617, engine 3/3: placement/fanout_clearance must read
min_hole_clearance in its keep-out geometry -- and not in its via mover.

The decoupling-cap repair (#130) grew its NPTH keep-out rects to a flat
`NPTH_TO_TRACK_CLEARANCE` (0.20) and validated its via-mover connectors at a
flat `max(clearance, NPTH_TO_TRACK_CLEARANCE)`. `check_drc` grades
copper-to-hole at the BOARD's `min_hole_clearance` (check_drc.py:2390), so on a
board declaring 0.25 this pass parked cap pads inside a band its own checker
then flagged. #617 changes ONE of the two sites:

  CHANGED    `_Repair`'s NPTH keep-out rects -- the cost model the whole search
             descends. Raising it makes the pass SEE the shortfall and MOVE the
             cap out of the band; every other cap it was already repairing is
             still repaired.
  UNCHANGED  `nudge_vias_for_unresolved`'s connector gate -- the mover searches
             a 0.6 mm budget for a spot where the relocated via AND its
             connector back to the stub both validate. Raising the connector's
             hole floor does not find a better spot, it makes the whole search
             return "no clear spot" and the #130 pad-via graze the pass exists
             to fix persists. Kept below as a CHANGE DETECTOR with its numbers.

Case 1 runs the REAL repair pass end to end on a REAL in-repo board
(`kicad_files/orangecrab_ext_pll.kicad_pcb`) staged into a temp dir beside a
project that DECLARES `min_hole_clearance: 0.25`, with an NPTH mounting hole
placed 0.22 mm off the pad copper of a cap that is otherwise CLEAN -- so the
hole is the only reason to move it. Both arms are graded on the same board.

    python3 tests/test_617_placement_fanout_hole_clearance.py
"""
import json
import math
import os
import shutil
import sys
import tempfile
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'py_router'))   # #522
sys.path.insert(0, os.path.join(ROOT, 'py_placer'))   # placement split
sys.path.insert(0, os.path.join(ROOT, 'py_tools'))    # #522
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import obstacle_map
from kicad_parser import BoardInfo, Pad, Segment, Via, parse_kicad_pcb
from single_ended_routing import _seg_foreign_hole_dist
from placement.fanout_clearance import (_Repair, nudge_vias_for_unresolved,
                                        repair_fanout_clearance)
from synth import make_pad, make_pcb

BOARD = os.path.join(ROOT, 'kicad_files', 'orangecrab_ext_pll.kicad_pcb')
CLEAR = 0.1
DECLARED = 0.25
BAND = 0.22                 # copper-to-hole-WALL gap under test
HOLE_DRILL = 1.0


def _declaring_project(d, **rules):
    with open(os.path.join(d, 'b.kicad_pro'), 'w', encoding='utf-8') as f:
        json.dump({'board': {'design_settings': {'rules': rules}}}, f)


def _stage_real_board(tmp, name, **rules):
    """A COPY of the in-repo board plus a sibling project declaring `rules`."""
    d = os.path.join(tmp, name)
    os.makedirs(d, exist_ok=True)
    dst = os.path.join(d, 'b.kicad_pcb')
    shutil.copyfile(BOARD, dst)
    _declaring_project(d, **rules)
    return dst


def _stub_board(tmp, name, **rules):
    """A bare board file plus a sibling project declaring `rules` (case 2 uses
    synthetic PCBData; only the project has to exist on disk)."""
    d = os.path.join(tmp, name)
    os.makedirs(d, exist_ok=True)
    pcb = os.path.join(d, 'b.kicad_pcb')
    with open(pcb, 'w', encoding='utf-8') as f:
        f.write('(kicad_pcb (version 20240108))\n')
    _declaring_project(d, **rules)
    return pcb


def _seed_state(path):
    return _Repair(parse_kicad_pcb(path), path, CLEAR, 0.1, 0.55, 1.0, 2.0,
                   0.3, 'C', set())


def _add_mounting_hole(pcb, hole):
    """Attach an NPTH mounting hole to a DETERMINISTICALLY chosen non-cap,
    non-resistor footprint (sorted by reference, not dict order)."""
    host = sorted(r for r in pcb.footprints if not r.startswith(('C', 'R')))[0]
    pcb.footprints[host].pads.append(Pad(
        component_ref=host, pad_number='H9', global_x=hole[0],
        global_y=hole[1], local_x=0.0, local_y=0.0, size_x=HOLE_DRILL,
        size_y=HOLE_DRILL, shape='circle', layers=['*.Cu', '*.Mask'],
        net_id=0, net_name='', drill=HOLE_DRILL, pad_type='np_thru_hole'))
    return host


def _wall_gap(rects, hole):
    """Smallest hole-wall-to-pad-copper gap over a cap's pad rects."""
    best = math.inf
    for r in rects:
        dx = max(r[0] - hole[0], 0.0, hole[0] - r[2])
        dy = max(r[1] - hole[1], 0.0, hole[1] - r[3])
        best = min(best, math.hypot(dx, dy) - HOLE_DRILL / 2.0)
    return best


def run():
    fails = []

    def check(name, cond):
        print(('PASS' if cond else 'FAIL') + f': {name}')
        if not cond:
            fails.append(name)

    with tempfile.TemporaryDirectory() as tmp:
        obstacle_map._HOLE_CLR_CACHE.clear()
        _cap_repair(check, tmp)
        _via_mover_stays_flat(check, tmp)

    print()
    if fails:
        print(f'{len(fails)} FAILURE(S): {fails}')
        return 1
    print('all checks passed')
    return 0


# === CHANGED: _Repair's NPTH keep-out, graded through the REAL repair pass ===
def _cap_repair(check, tmp):
    print(f'CHANGED site: _Repair NPTH keep-out -- {os.path.basename(BOARD)}, '
          f'graded through repair_fanout_clearance')
    silent = _stage_real_board(tmp, 'real_silent')

    # Pick, deterministically, a movable near-BGA cap that is ALREADY clean of
    # foreign copper, so the mounting hole below is the ONLY reason to move it.
    seed = _seed_state(silent)
    clean = sorted(r for r, c in seed.caps.items()
                   if seed.graze_penalty(r, c, c.x, c.y, c.rot) <= 0.0)
    cap_ref = clean[0]
    rect = min(seed.caps[cap_ref].pad_rects(), key=lambda r: r[0])
    hole = (rect[0] - (HOLE_DRILL / 2.0 + BAND), (rect[1] + rect[3]) / 2.0)
    print(f'        {len(seed.caps)} movable near-BGA caps, {len(clean)} of '
          f'them already clean -> using {cap_ref}')
    print(f'        {cap_ref} pad edge x={rect[0]:.4f}; NPTH drill '
          f'{HOLE_DRILL} at ({hole[0]:.4f}, {hole[1]:.4f}) -> {BAND}mm of wall '
          f'gap (legal at the flat 0.20 floor, 0.03 inside a declared 0.25)')

    got = {}
    for label, rules in (('nothing declared', {}),
                         ('declared 0.25', {'min_hole_clearance': DECLARED})):
        path = _stage_real_board(tmp, label.replace(' ', '_'), **rules)
        pcb = parse_kicad_pcb(path)
        _add_mounting_hole(pcb, hole)
        res = repair_fanout_clearance(pcb, path, clearance=CLEAR,
                                      cap_prefix='C', max_passes=30)
        moved = {m['reference']: m for m in res['placements']}
        cap = _Repair(pcb, path, CLEAR, 0.1, 0.55, 1.0, 2.0, 0.3, 'C',
                      set()).caps[cap_ref]
        if cap_ref in moved:
            m = moved[cap_ref]
            rects = cap.pad_rects(m['new_x'], m['new_y'], m['new_rotation'])
            where = f"MOVED to ({m['new_x']:.4f}, {m['new_y']:.4f})"
        else:
            rects, where = cap.pad_rects(), 'NOT moved'
        gap = _wall_gap(rects, hole)
        got[label] = (gap, len(res['unresolved']), len(res['placements']))
        print(f'        {label}: {cap_ref} {where}; pad-to-hole-wall gap '
              f'{gap:.4f}mm; {len(res["placements"])} cap(s) moved, '
              f'{len(res["unresolved"])} unresolved')

    check(f'the base leaves {cap_ref} inside the declared band '
          f'(gap {got["nothing declared"][0]:.4f} < {DECLARED})',
          got['nothing declared'][0] < DECLARED - 1e-6)
    check('the declared 0.25 makes the pass MOVE it out of the band '
          '(the violation is removed, not just scored)',
          got['declared 0.25'][0] >= DECLARED - 1e-6)
    check('and no repair is traded away: 0 unresolved caps on BOTH arms',
          got['nothing declared'][1] == 0 and got['declared 0.25'][1] == 0)
    print()


# === UNCHANGED: nudge_vias_for_unresolved's connector gate ==================
class _FakeCap:
    """Minimal stand-in for _Cap: one fixed pad rect (the #370 B3 harness)."""

    def __init__(self, rects):
        self._rects = list(rects)
        self.side = 'F'
        self.x = self.y = self.rot = 0.0

    def pad_rects(self, x=None, y=None, rot=None):
        return self._rects


class _FakeSt:
    """Minimal stand-in for _Repair: one cap, permanently 'unresolved'."""

    def __init__(self, cap_rects):
        self.caps = {'C1': _FakeCap(cap_rects)}
        self.vias = []

    def graze_penalty(self, ref, cap, x, y, rot):
        return 1.0


BAR = (0.2, 0.9, 1.8, 1.1, 2)   # cap pad bar under the via: only +y escapes
NPTH = (1.0, 0.98)              # 0.42 below the via -> 0.32 of centreline room
NPTH_DRILL = 0.2


def _mover_board(path):
    bi = BoardInfo(layers={}, copper_layers=['F.Cu', 'B.Cu'],
                   board_bounds=(0.0, 0.0, 3.0, 3.0))
    pads = {0: [make_pad(net_id=0, x=NPTH[0], y=NPTH[1], ref='BUS1',
                         num='H1', size_x=NPTH_DRILL, size_y=NPTH_DRILL,
                         shape='circle', layers=['F.Mask', 'B.Mask'],
                         drill=NPTH_DRILL, pad_type='np_thru_hole')]}
    via = Via(x=1.0, y=1.4, size=0.5, drill=0.3, layers=['F.Cu', 'B.Cu'],
              net_id=3)
    stub = Segment(start_x=1.0, start_y=1.6, end_x=1.0, end_y=1.4, width=0.2,
                   layer='F.Cu', net_id=3)
    return make_pcb(board_info=bi, vias=[via], segments=[stub],
                    footprints={'C1': SimpleNamespace(layer='F.Cu', pads=[])},
                    pads_by_net=pads, source_path=path, zones=[])


def _via_mover_stays_flat(check, tmp):
    """Change detector. On the #370-B3 harness the mover's only validated spot
    puts its connector 0.22 mm off the NPTH. Raising the gate to a declared
    0.25 returns 0 moves and 0 connectors -- the offending via never relocates
    and the pad-via graze stays. Keeping the flat floor keeps the repair."""
    print('UNCHANGED site: nudge_vias_for_unresolved -- connector gate still '
          'at the flat fab floor')
    declares = _stub_board(tmp, 'stub_declares', min_hole_clearance=DECLARED)
    pcb = _mover_board(declares)
    moves, segs = nudge_vias_for_unresolved(_FakeSt([BAR]), pcb, CLEAR)
    gaps = [_seg_foreign_hole_dist(pcb, s['net_id'], s['start'][0],
                                   s['start'][1], s['end'][0], s['end'][1])
            - s['width'] / 2.0 for s in segs]
    print(f'        declared 0.25: {len(moves)} move(s), {len(segs)} '
          f'connector(s)'
          + (f'; copper-to-hole gap {gaps[0]:.4f}mm' if gaps else ''))
    check('the offending via still relocates on a DECLARING board (the #130 '
          'repair is kept)', len(moves) == 1 and len(segs) == 1)
    check(f'its connector lands in the declared band ({BAND}mm) -- the cost of '
          'keeping the repair, stated rather than hidden',
          bool(gaps) and abs(gaps[0] - BAND) < 1e-4)


if __name__ == '__main__':
    sys.exit(run())
