#!/usr/bin/env python3
"""A refuted vector source, kept as its measurement.

Run 7 endorsed a third vector source for reconstruct: cluster the STRETCHED
AIRWIRES of small nets, on the theory that a translate stretches them all by
the damage vector while a healthy board's long airwires point in unrelated
directions.

Implemented and measured, it fails both ways at once: healthy boards fire, and
on a board translated by a known vector the top cluster is not that vector.

The repo's own A/B doctrine says a rejected term keeps its rows, marked
rejected with its measured numbers, so it stays a change detector instead of
becoming folklore someone re-proposes. Same here: the function stays in the
tree, is NOT wired into the driver, and these numbers are pinned.

Run: python3 -X utf8 tests/test_run8_airwire_refuted.py
"""
import glob
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault('KRT_NO_BANNER', '1')

from kicad_parser import parse_kicad_pcb                       # noqa: E402
from placement import reconstruct                              # noqa: E402
import pose_score                                              # noqa: E402

FAILURES = []


def check(name, cond, detail=''):
    print(f'  {"PASS" if cond else "FAIL"}  {name}'
          + (f'\n        {detail}' if not cond and detail else ''))
    if not cond:
        FAILURES.append(name)


def vectors_for(path, clearance=0.1):
    pcb = parse_kicad_pcb(path)
    st = pose_score.make_state(pcb, path, clearance=clearance,
                               board_edge_clearance=0.2, grid_step=0.1)
    return reconstruct.airwire_cluster_vectors(st)


def main():
    print('it is not wired into the driver')
    drv = open(os.path.join(ROOT, 'place_reconstruct.py'),
               encoding='utf-8').read()
    check('the driver does not call it', 'airwire_cluster_vectors' not in drv)
    doc = reconstruct.airwire_cluster_vectors.__doc__ or ''
    check('the function says REFUTED in its first line',
          doc.strip().startswith('REFUTED'), doc[:80])
    check('...and carries the numbers that refuted it',
          '6 of 33' in doc and '60mm' in doc)

    print('why: healthy boards fire')
    firing = []
    for f in sorted(glob.glob(os.path.join(ROOT, 'kicad_files', '*.kicad_pcb'))):
        if vectors_for(f):
            firing.append(os.path.basename(f))
    check('a healthy board firing at all disqualifies it as a source',
          len(firing) >= 3,
          f'{len(firing)} of 33 fire: {firing[:6]}')

    print('and the vector it finds on real damage is not the damage')
    dmg = os.path.join(ROOT, 'wk', 'run7', 'glasgow_revC',
                       'perturbed.kicad_pcb')
    if os.path.isfile(dmg):
        got = vectors_for(dmg, 0.09)
        check('it does produce a confident cluster', bool(got), str(got[:1]))
        if got:
            vx, vy = got[0]['v']
            # The recorded truth for that board is (4.5, -2.4).
            miss = ((vx - 4.5) ** 2 + (vy + 2.4) ** 2) ** 0.5
            check('...and it misses the true vector by a wide margin',
                  miss > 5.0, f'top cluster {(vx, vy)}, miss {miss:.1f}mm')
    else:
        print('  SKIP  recorded damaged board not present; the measured '
              'result is in the docstring')

    print()
    if FAILURES:
        print(f'FAIL: {len(FAILURES)} check(s): {", ".join(FAILURES)}')
        return 1
    print('OK')
    return 0


if __name__ == '__main__':
    sys.exit(main())
