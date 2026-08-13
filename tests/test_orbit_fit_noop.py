#!/usr/bin/env python3
"""`reconstruct.fit_family_orbits` must be SILENT on healthy boards.

Same shape, and for the same reason, as `tests/test_run8_airwire_refuted.py`:
a detector that fires on ordinary layout is not a detector. The airwire source
was refuted at 6 of 33, and an un-guarded orbit fit is a related failure -- a
near-collinear family of resistors fits an enormous circle exactly as a long
airwire fits a damage vector.

The value of this file is not the 0; it is the MATRIX. Each guard's
contribution is pinned, so relaxing one shows up here as a number moving rather
than being rediscovered on a board somebody shipped. Two of these rows exist
because the investigation that proposed the fitter got them wrong in the
direction that flattered it: it reported the un-guarded rate as 6 of 33 (it is
28) and claimed the guarded rate stayed 0 with `min_inliers` at 4 and 3 (it is
2 and 7). `min_inliers` is a load-bearing guard, not a formality.

Run:  python3 tests/test_orbit_fit_noop.py
"""
import contextlib
import io
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'py_router'))  # #522/py_placer layout
sys.path.insert(0, os.path.join(ROOT, 'py_placer'))  # #522/py_placer layout
sys.path.insert(0, os.path.join(ROOT, 'py_tools'))  # #522/py_placer layout

import pose_score                                              # noqa: E402
from kicad_parser import parse_kicad_pcb                       # noqa: E402
from placement import reconstruct as R                         # noqa: E402

#: Number of boards `_corpus_boards()` returns. Pinned, because every count in
#: MATRIX is a claim about THIS set; if a board is added to or removed from the
#: repo, re-measure the rows rather than nudging this number.
CORPUS_N = 22

#: (label, min_inliers, guard overrides) -> boards of the corpus that fire.
#: Ordered so each row adds ONE guard to the row above it. Counts are measured
#: over the TRACKED corpus (see `_corpus_boards`); the rows below the occupancy
#: guard are unchanged from the ones measured over a polluted kicad_files/,
#: which is the useful part -- the extra artifact boards were all discarded by
#: occupancy anyway, so the guard ladder's conclusion never depended on them.
MATRIX = [
    ('no guards at all', 3,
     dict(ORBIT_MAX_RADIUS_FRAC=1e9, ORBIT_M_RANGE=range(2, 25),
          ORBIT_MIN_SEATS_PER_CLASS=1, ORBIT_MIN_OCCUPANCY=0.0), 17),
    ('+ scale', 3,
     dict(ORBIT_M_RANGE=range(2, 25), ORBIT_MIN_SEATS_PER_CLASS=1,
          ORBIT_MIN_OCCUPANCY=0.0), 17),
    ('+ curvature (m >= 3)', 3,
     dict(ORBIT_MIN_SEATS_PER_CLASS=1, ORBIT_MIN_OCCUPANCY=0.0), 17),
    ('+ over-determination (>= 3 distinct SEATS/class)', 3,
     dict(ORBIT_MIN_OCCUPANCY=0.0), 11),
    ('+ occupancy (>= 0.60 of slots filled)', 3, {}, 7),
    ('+ min_inliers 4', 4, {}, 2),
    ('+ min_inliers 5  [SHIPPED]', 5, {}, 0),
]


def _corpus_boards():
    """The boards the REPO SHIPS -- not whatever happens to sit in kicad_files/.

    That directory accumulates routing OUTPUTS (`*_routed`, `*_fanout`,
    `*_plane`, `*_connected`), which .gitignore:49-51 covers precisely because
    they are per-machine junk. Globbing it made this corpus a property of whose
    checkout ran the test: 22 boards on a clean clone, 29 on a used one, and
    >= 30 on the machine that pinned the MATRIX counts below -- so the numbers
    could only ever be right in one working copy. Every expected count in this
    file is a claim about a FIXED set of boards, so the set has to be fixed
    too, and git is the only thing that knows which files those are.

    Returns [] when git cannot answer; main() then SKIPS rather than grading
    against a set it cannot identify.
    """
    try:
        p = subprocess.run(['git', 'ls-files', '-z', 'kicad_files/*.kicad_pcb'],
                           cwd=ROOT, capture_output=True, text=True, timeout=60)
        if p.returncode == 0:
            return sorted(os.path.join(ROOT, n)
                          for n in p.stdout.split('\0') if n)
    except Exception:                                            # noqa: BLE001
        pass
    return []


def _states():
    out = {}
    for b in _corpus_boards():
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            pcb = parse_kicad_pcb(b)
            out[os.path.basename(b)] = pose_score.make_state(pcb, b)
    return out


def _fire(states, min_inliers, patch):
    saved = {k: getattr(R, k) for k in patch}
    for k, v in patch.items():
        setattr(R, k, v)
    try:
        out = {}
        for n, st in states.items():
            f = R.fit_family_orbits(st, min_inliers=min_inliers)
            if f:
                out[n] = f
        return out
    finally:
        for k, v in saved.items():
            setattr(R, k, v)


def main():
    states = _states()
    if not states:
        print('SKIP: the tracked corpus could not be enumerated (git not '
              'available here?), and every count below is a claim about a '
              'fixed board set -- grading against an unknown set would be '
              'worse than not grading.')
        return 0
    assert len(states) == CORPUS_N, (
        f'corpus is {len(states)} boards, expected {CORPUS_N}. The MATRIX '
        f'counts are pinned to a fixed set: if a board was added to or removed '
        f'from kicad_files/, re-measure the rows and update both.')
    print(f'corpus: {len(states)} healthy boards\n')
    bad = []
    for label, mi, patch, expect in MATRIX:
        fired = _fire(states, mi, patch)
        ok = len(fired) == expect
        print(f'  {"ok " if ok else "FAIL"} {label:<50} '
              f'{len(fired):>2} of {len(states)} fire  (expect {expect})')
        if not ok:
            bad.append((label, len(fired), expect, sorted(fired)))

    # The one that actually gates behaviour, stated separately so a reader
    # cannot miss it among the diagnostic rows.
    shipped = _fire(states, R.ORBIT_MIN_INLIERS, {})
    print(f'\n  SHIPPED CONFIGURATION: {len(shipped)} of {len(states)} '
          f'healthy boards fire')
    if shipped:
        bad.append(('shipped config is not a no-op', len(shipped), 0,
                    sorted(shipped)))
        for n, f in sorted(shipped.items()):
            print(f'     {n}: {[repr(x) for x in f]}')

    # ...and the detector must still SEE a real orbit, or a no-op is trivial.
    # A synthetic one, so this does not depend on a wk/ artifact.
    fit = R._fit_orbit_m(
        'synthetic', 0.0, 0.0, 10.0,
        [f'D{i}' for i in range(8)],
        {f'D{i}': (10.0 * __import__('math').cos(__import__('math').radians(45 * i)),
                   10.0 * __import__('math').sin(__import__('math').radians(45 * i)))
         for i in range(8)}, 5)
    if fit is None or fit.m != 8 or len(fit.inliers) != 8 or fit.free_slots:
        bad.append(('a perfect 8-fold ring was not detected', fit, 'm=8 8/8', []))
        print(f'  FAIL an exact 8-fold ring of 8 members: {fit!r}')
    else:
        print(f'  ok  an exact 8-fold ring of 8 members is detected: {fit!r}')

    # ...and a ring with ONE member removed exposes exactly one free slot,
    # which is the fact `lock_advisor` and any future consumer key on.
    gone = R._fit_orbit_m(
        'synthetic', 0.0, 0.0, 10.0,
        [f'D{i}' for i in range(7)],
        {f'D{i}': (10.0 * __import__('math').cos(__import__('math').radians(45 * i)),
                   10.0 * __import__('math').sin(__import__('math').radians(45 * i)))
         for i in range(7)}, 5)
    if gone is None or len(gone.free_slots) != 1:
        bad.append(('a ring missing one member did not expose one free slot',
                    gone, '1 free slot', []))
        print(f'  FAIL a 7-of-8 ring: {gone!r}')
    else:
        print(f'  ok  a 7-of-8 ring exposes exactly 1 free slot: {gone!r}')

    if bad:
        print('\nFAILURES:')
        for row in bad:
            print('  ', row)
        return 1
    print('\nPASS')
    return 0


if __name__ == '__main__':
    sys.exit(main())
