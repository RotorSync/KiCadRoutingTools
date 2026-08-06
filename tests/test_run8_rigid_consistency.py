#!/usr/bin/env python3
"""A rigid restore cannot make two differently-moved parts touch (E7).

The second wrong-basin counter-gate, and the strongest, because it needs no
ground truth -- only the before and after boards.

The argument: parts displaced by the SAME vector keep their relative geometry
exactly, so they cannot newly touch each other. A restored part CAN come to
rest against a part that never moved; that is the damage being undone. So a new
contact pair between two parts that BOTH moved, by vectors differing by more
than a tolerance, is impossible in a rigid restore and routine in a search that
shoved neighbours aside to make room.

Measured, from a from-scratch implementation of the rule:

    a wrong-basin run: damaged -> accepted    43 new pairs, 12 INCONSISTENT
    the same damage  -> TRUTH control         13 new pairs,  0 inconsistent
                                              (all 13 mover-vs-static)
    three legitimate recoveries               0 inconsistent
    33 corpus boards against themselves       0

One board of five real runs fires, and it is the one known to be in the wrong
basin.

Run: python3 -X utf8 tests/test_run8_rigid_consistency.py
"""
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'py_router'))  # #522/py_placer layout
sys.path.insert(0, os.path.join(ROOT, 'py_placer'))  # #522/py_placer layout
sys.path.insert(0, os.path.join(ROOT, 'py_tools'))  # #522/py_placer layout
os.environ.setdefault('KRT_NO_BANNER', '1')

from check_rigid_consistency import analyse, DEFAULT_TOL_MM   # noqa: E402
from kicad_parser import parse_kicad_pcb                      # noqa: E402
from placement.writer import write_placed_output              # noqa: E402

BOARD = os.path.join(ROOT, 'kicad_files', 'splitflap_driver.kicad_pcb')
FAILURES = []


def check(name, cond, detail=''):
    print(f'  {"PASS" if cond else "FAIL"}  {name}'
          + (f'\n        {detail}' if not cond and detail else ''))
    if not cond:
        FAILURES.append(name)


def shifted(src, dst, moves):
    """Write `src` with {ref: (dx, dy)} applied."""
    pcb = parse_kicad_pcb(src)
    placements = []
    for ref, (dx, dy) in moves.items():
        fp = pcb.footprints[ref]
        placements.append({'reference': ref, 'new_x': fp.x + dx,
                           'new_y': fp.y + dy,
                           'new_rotation': fp.rotation or 0.0})
    write_placed_output(src, dst, placements)
    return dst


def main():
    print('a board against itself is trivially consistent')
    r = analyse(BOARD, BOARD, clearance=0.1)
    check('no parts moved', r['parts_moved'] == 0)
    check('no new contact pairs', r['new_pairs'] == 0)
    check('nothing flagged', not r['inconsistent'])

    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, 'a.kicad_pcb')
        shutil.copy(BOARD, src)
        pcb = parse_kicad_pcb(src)
        refs = sorted(pcb.footprints)

        print('a whole block moved by ONE vector stays consistent')
        v = (3.0, 2.0)
        block = {r_: v for r_ in refs[:6]}
        rigid = shifted(src, os.path.join(tmp, 'rigid.kicad_pcb'), block)
        rr = analyse(src, rigid, clearance=0.1)
        check('the block registers as moved', rr['parts_moved'] >= 6,
              str(rr['parts_moved']))
        check('no pair between same-vector movers is flagged',
              not rr['inconsistent'],
              str(rr['inconsistent'])[:300])

        print('two parts driven together by DIFFERENT vectors are flagged')
        # Put two parts on top of each other, from opposite directions, so
        # both are movers with a large displacement spread.
        a_ref, b_ref = refs[0], refs[1]
        fa, fb = pcb.footprints[a_ref], pcb.footprints[b_ref]
        mid_x, mid_y = (fa.x + fb.x) / 2.0, (fa.y + fb.y) / 2.0
        collide = {a_ref: (mid_x - fa.x, mid_y - fa.y),
                   b_ref: (mid_x - fb.x, mid_y - fb.y)}
        bad = shifted(src, os.path.join(tmp, 'bad.kicad_pcb'), collide)
        br = analyse(src, bad, clearance=0.1)
        hit = [f for f in br['inconsistent']
               if {f['a'], f['b']} == {a_ref, b_ref}]
        check('the pair the two movers created is flagged', bool(hit),
              f"inconsistent={[(f['a'], f['b']) for f in br['inconsistent']]}")
        if hit:
            check('the report names both displacement vectors',
                  hit[0]['disp_a'] != hit[0]['disp_b'])
            check('...and how far apart they are',
                  hit[0]['spread_mm'] > DEFAULT_TOL_MM, str(hit[0]['spread_mm']))

        print('a mover coming to rest against a STATIC part is not flagged')
        # Move one part onto a neighbour that stays put: this is what undoing
        # damage looks like, and flagging it would make the gate useless.
        onto = {a_ref: (fb.x - fa.x, fb.y - fa.y)}
        one = shifted(src, os.path.join(tmp, 'one.kicad_pcb'), onto)
        orr = analyse(src, one, clearance=0.1)
        check('a new contact appeared', orr['new_pairs'] > 0,
              str(orr['new_pairs']))
        check('it is counted as mover-vs-static, not inconsistent',
              orr['new_pairs_mover_vs_static'] > 0 and not orr['inconsistent'],
              f"static={orr['new_pairs_mover_vs_static']} "
              f"flagged={len(orr['inconsistent'])}")

    print('recorded runs, when the work dir is present (wk/ is gitignored)')
    wrong = (os.path.join(ROOT, 'wk', 'run7', 'glasgow_revC',
                          'perturbed.kicad_pcb'),
             os.path.join(ROOT, 'wk', 'run7', 'glasgow_revC',
                          'rL_repair.kicad_pcb'),
             os.path.join(ROOT, 'wk', 'run7', 'glasgow_revC',
                          'perturbed.control.kicad_pcb'))
    if all(os.path.isfile(p) for p in wrong):
        bad_r = analyse(wrong[0], wrong[1], clearance=0.09)
        good_r = analyse(wrong[0], wrong[2], clearance=0.09)
        check('the wrong-basin placement is flagged',
              len(bad_r['inconsistent']) >= 10,
              str(len(bad_r['inconsistent'])))
        check('the TRUTH restore is not',
              not good_r['inconsistent'], str(good_r['inconsistent'])[:200])
        check('and truth\'s new contacts are all mover-vs-static',
              good_r['new_pairs'] == good_r['new_pairs_mover_vs_static'],
              f"{good_r['new_pairs']} vs {good_r['new_pairs_mover_vs_static']}")
    else:
        print('  SKIP  recorded boards not present; measured values are in '
              'the docstring')

    print()
    if FAILURES:
        print(f'FAIL: {len(FAILURES)} check(s): {", ".join(FAILURES)}')
        return 1
    print('OK')
    return 0


if __name__ == '__main__':
    sys.exit(main())
