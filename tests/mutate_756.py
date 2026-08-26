#!/usr/bin/env python3
"""The #756 mutation battery, shipped so its numbers can be re-derived.

`tests/test_756_fanout_clearance_drill_floors.py` records what each arm kills.
A count is only checkable if the exact source edit is written down -- two
reviewers of the #746 branch reconstructed its rows from their names and both
got the wrong answer, because a plausible-looking reconstruction of one row was
semantically inert. So the edits live here, as data, next to the numbers they
produced.

Every row carries an EXPECTATION. Some mutations are deliberately inert, and an
inert row recorded as an expected survivor is a finding, while an inert row
quietly deleted is a hole. A row whose verdict does not match its expectation
is reported as WRONG.

TWO targets, like `mutate_730.py`'s: the via-nudge resolver in
`placement/fanout_clearance.py` and the sibling drill floor in
`bga_fanout/__init__.py`. Rows are graded by more than one test file -- the
staleness guards live in `test_730`/`test_737`/`test_750` and the rest in
`test_756` -- and a row is KILLED if ANY of its named tests exits non-zero.

THIS IS THE FOURTH COPY OF THIS RUNNER (`mutate_730.py`, `mutate_750.py`,
`mutate_761.py`). Deliberately not refactored into a shared one: that would
rewrite three shipped batteries whose recorded counts are the evidence for
three merged reviews, which is a change to make on its own and not inside a
fix.

NOT named `test_*.py`, so `tests/run_all.py` does not collect it: it REWRITES
the engine in place. One writer per tree -- do not run it while a suite, an A/B
replay or a review is reading the same checkout. The file refuses to start on a
dirty engine, because restoring would write the COMMITTED text back over
uncommitted work.

    python3 tests/mutate_756.py
    python3 tests/mutate_756.py --row resolver-drops-the-fab-wrap

A row is KILLED by a FAILURE **or an ERROR**: dropping a term makes some arms
raise rather than fail, and a battery that counted only failures would call
that a survivor.

An anchor that does not match EXACTLY ONCE is reported as BROKEN rather than
skipped -- a battery that silently applies nothing reports every row as a
survivor, which reads as a catastrophic test failure and is really a stale
anchor.

Edits are applied with `str.replace(old, new, 1)`, never `sed`. Commit bb8f4477
on the #761 branch records two rows left as a SyntaxError by unescaped `sed`
metacharacters -- a battery that cannot start reports nothing at all, which is
only safe because someone runs it.
"""
from __future__ import annotations

import argparse
import io
import os
import subprocess
import sys

_TESTS = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_TESTS)

FC = os.path.join(_ROOT, 'py_placer', 'placement', 'fanout_clearance.py')
BGA = os.path.join(_ROOT, 'py_router', 'bga_fanout', '__init__.py')
TARGETS = {'fc': FC, 'bga': BGA}

T756 = os.path.join(_TESTS, 'test_756_fanout_clearance_drill_floors.py')
T750 = os.path.join(_TESTS, 'test_750_fanout_clearance_via_drill.py')
T737 = os.path.join(_TESTS, 'test_737_fanout_clearance_via_hole.py')
T730 = os.path.join(_TESTS, 'test_730_fanout_clearance_npth_local_clearance.py')
T370 = os.path.join(_TESTS, 'test_370_tierb_fixes.py')

# The resolver's return, quoted once so several rows can anchor on parts of it.
_RET = ("    return max(declared, fab_via), max(declared, fab_pad), "
        "declared, src")

# (name, target, old, new, tests, expect)
ROWS = [
    # --- the defect itself: the board read ---------------------------------
    ('resolver-never-reads-the-board', 'fc',
     """    path = getattr(pcb_data, 'source_path', "") or ""
    if not path:
        return fab_via, fab_pad, None, 'fixed default'""",
     """    path = ""
    if not path:
        return fab_via, fab_pad, None, 'fixed default'""",
     (T756,), 'KILLED'),

    # The evasion a source-only guard misses: the resolver still EXISTS, is
    # still called, and still returns a 4-tuple -- it just answers the old
    # literals. This is the row that proves test_756 owns what the three
    # staleness guards explicitly do NOT (see test_750's corrected note).
    ('resolver-hard-wired-to-the-old-literals', 'fc',
     '    from fab_tiers import fab_floor_min\n',
     "    return 0.2, 0.45, None, 'fixed default'\n"
     '    from fab_tiers import fab_floor_min\n',
     (T756, T750, T737, T730), 'KILLED'),

    # --- the raise-only wrap ------------------------------------------------
    ('resolver-drops-the-fab-wrap', 'fc',
     _RET,
     '    return declared, declared, declared, src',
     (T756,), 'KILLED'),

    ('resolver-drops-the-fab-wrap-on-the-VIA-arm-only', 'fc',
     _RET,
     '    return declared, max(declared, fab_pad), declared, src',
     (T756,), 'KILLED'),

    ('resolver-drops-the-fab-wrap-on-the-PAD-arm-only', 'fc',
     _RET,
     '    return max(declared, fab_via), declared, declared, src',
     (T756,), 'KILLED'),

    # --- the two fab keys ---------------------------------------------------
    ('pad-floor-reads-the-VIA-fab-key', 'fc',
     "    fab_via, fab_pad = fmin['hole_to_hole'], fmin['pad_hole_to_hole']",
     "    fab_via, fab_pad = fmin['hole_to_hole'], fmin['hole_to_hole']",
     (T756, T750, T737, T730), 'KILLED'),

    ('via-floor-reads-the-PAD-fab-key', 'fc',
     "    fab_via, fab_pad = fmin['hole_to_hole'], fmin['pad_hole_to_hole']",
     "    fab_via, fab_pad = fmin['pad_hole_to_hole'], fmin['pad_hole_to_hole']",
     (T756, T750), 'KILLED'),

    # --- the CWD probe guard ------------------------------------------------
    # Without the short-circuit, `read_design_rules("")` probes ".kicad_pro"
    # relative to the PROCESS CWD -- test_756 plants exactly that file.
    ('cwd-probe-guard-dropped', 'fc',
     """    if not path:
        return fab_via, fab_pad, None, 'fixed default'
""",
     '',
     (T756,), 'KILLED'),

    # --- `declared` must mean "the board said this" -------------------------
    ('fallback-passed-instead-of-None', 'fc',
     "    declared, src = board_floor(path, 'hole_to_hole', None, None)",
     "    declared, src = board_floor(path, 'hole_to_hole', None,\n"
     "                                defaults.HOLE_TO_HOLE_CLEARANCE)",
     (T756,), 'KILLED'),

    # --- the disclosure -----------------------------------------------------
    ('raised-disclosure-deleted', 'fc',
     """        print(f"  via-nudge drill floors: via-hole {H2H_VIA:g}mm, pad-hole "
              f"{H2H_PAD:g}mm (from the board's own min_hole_to_hole)")""",
     '        pass',
     (T756,), 'KILLED'),

    ('below-fab-disclosure-deleted', 'fc',
     """        print(f"  Board min_hole_to_hole {_h2h_decl:g}mm is below the "
              f"{H2H_VIA:g}mm fab hole-to-hole floor; using {H2H_VIA:g}mm.")""",
     '        pass',
     (T756,), 'KILLED'),

    ('disclosure-fires-at-the-packaged-default-too', 'fc',
     '            _h2h_decl > defaults.HOLE_TO_HOLE_CLEARANCE:',
     '            _h2h_decl >= defaults.HOLE_TO_HOLE_CLEARANCE:',
     (T756,), 'KILLED'),

    # --- the two gates the floors feed --------------------------------------
    # Kept from mutate_750's rows deliberately: #756 preserved both expression
    # spellings precisely so those rows keep applying, and a row here proves
    # the preserved spelling is still load-bearing rather than decorative.
    ('h2h-via-gate-deleted', 'fc',
     """            # net-INDEPENDENT: two holes collide whatever they carry
            if d < vdr + ovdr + H2H_VIA:
                return False""",
     '            pass',
     (T756, T750), 'KILLED'),

    ('h2h-pad-gate-deleted', 'fc',
     """                if _point_to_seg_dist(nx, ny, c1x, c1y, c2x, c2y) < \\
                        vdr + prad + H2H_PAD:
                    return False""",
     '                pass',
     (T737, T730), 'KILLED'),

    # --- the assignment's position ------------------------------------------
    # A GENUINE move back above the early return is semantically inert for
    # every caller that has work to do; recorded as an expected survivor rather
    # than omitted, so its survival is not read as a hole. What it costs is a
    # project read and a printed line on a call with nothing to do, which no
    # arm asserts and which is a transcript question, not a correctness one.
    ('assignment-moved-back-above-the-early-return', 'fc',
     [('    H2H_VIA, H2H_PAD, _h2h_decl, _h2h_src = '
       'resolve_drill_floors(pcb_data)\n', ''),
      ('    via_moves, new_segments = [], []\n',
       '    H2H_VIA, H2H_PAD, _h2h_decl, _h2h_src = '
       'resolve_drill_floors(pcb_data)\n'
       '    via_moves, new_segments = [], []\n')],
     None,
     (T756, T750), 'SURVIVED'),

    # The first draft of the row above INSERTED a second assignment instead of
    # moving the one that is there, and test_750's shape guard killed it. That
    # is the guard working, so the mutation is kept under the name it actually
    # has -- a duplicated assignment is a real hazard (two board reads, two
    # disclosure lines, and a later reader deleting "the" one).
    ('assignment-DUPLICATED-above-the-early-return', 'fc',
     '    via_moves, new_segments = [], []\n',
     '    H2H_VIA, H2H_PAD, _h2h_decl, _h2h_src = '
     'resolve_drill_floors(pcb_data)\n'
     '    via_moves, new_segments = [], []\n',
     (T756, T750), 'KILLED'),

    # --- the layer-count read -----------------------------------------------
    # Also inert TODAY, and that is exactly what test_756's change detector
    # says out loud: fab_floor_min carries 0.20/0.45 in all four cells, so no
    # layer count can move either answer. Recorded so the day that stops being
    # true, this row flips to KILLED and someone reads the note.
    ('layer-count-forced-to-the-multilayer-bucket', 'fc',
     "    fmin = fab_floor_min(len([l for l in (_cu or []) "
     "if str(l).endswith('.Cu')]))",
     '    fmin = fab_floor_min(4)',
     (T756,), 'SURVIVED'),

    # --- the bga sibling ----------------------------------------------------
    ('bga-via-arm-reverted-to-the-flat-constant', 'bga',
     """                    + _h2h - 1e-6:
                return "drill hole-to-hole vs an existing via\"""",
     """                    + HOLE_TO_HOLE_CLEARANCE - 1e-6:
                return "drill hole-to-hole vs an existing via\"""",
     (T756,), 'KILLED'),

    ('bga-pad-arm-reverted-to-the-flat-constant', 'bga',
     """            if d < vdr + prad + _h2h - 1e-6:
                return "drill hole-to-hole vs a pad drill\"""",
     """            if d < vdr + prad + HOLE_TO_HOLE_CLEARANCE - 1e-6:
                return "drill hole-to-hole vs a pad drill\"""",
     (T756, T370), 'KILLED'),

    ('bga-drops-the-fab-wrap', 'bga',
     '    _h2h = max(_h2h_decl, _h2h_fab)',
     '    _h2h = _h2h_decl',
     (T756, T370), 'KILLED'),

    ('bga-never-reads-the-board', 'bga',
     '''        getattr(pcb_data, 'source_path', "") or "", 'hole_to_hole','''
     '\n',
     "        '', 'hole_to_hole',\n",
     (T756,), 'KILLED'),

    # The tidy-up #756 explicitly refused: charging the nudger's 0.45 pad-hole
    # floor here too. It is a THIRD change with its own before/after, and the
    # arm that pins the refusal must actually fire.
    ('bga-pad-arm-adopts-the-nudgers-045', 'bga',
     '            if d < vdr + prad + _h2h - 1e-6:',
     "            if d < vdr + prad + max(_h2h, 0.45) - 1e-6:",
     (T756, T370), 'KILLED'),
]


def _dirty(path):
    p = subprocess.run(['git', 'status', '--porcelain', '--', path],
                       capture_output=True, text=True, cwd=_ROOT)
    return bool(p.stdout.strip())


def run(only=None):
    rows = [r for r in ROWS if only is None or r[0] == only]
    if not rows:
        print('no row named %r' % only)
        return 1
    for path in TARGETS.values():
        if _dirty(path):
            print('REFUSING: %s has uncommitted changes. Commit or stash '
                  'first -- this battery restores by overwriting.'
                  % os.path.basename(path))
            return 2

    orig = {k: io.open(v, encoding='utf-8', newline='').read()
            for k, v in TARGETS.items()}
    results = []
    try:
        for name, tgt, old, new, tests, expect in rows:
            path = TARGETS[tgt]
            base = orig[tgt]
            edits = old if isinstance(old, list) else [(old, new)]
            # Rows are written with LF. `base` is read with newline='' so the
            # restore is byte-identical, which means a CRLF target never
            # matches a multi-line anchor -- and reports BROKEN, which reads
            # like a stale anchor and is really a line-ending mismatch. The
            # first #756 run lost THREE bga rows to exactly this
            # (bga_fanout/__init__.py is CRLF, fanout_clearance.py is LF), so
            # the anchor is translated to the target's own ending here rather
            # than every row being hand-written twice. BROKEN keeps its real
            # meaning; the restore stays byte-identical.
            if '\r\n' in base:
                edits = [(o.replace('\n', '\r\n'), n.replace('\n', '\r\n'))
                         for o, n in edits]
            counts = [base.count(o) for o, _n in edits]
            if counts != [1] * len(edits):
                results.append((name, 'BROKEN', expect,
                                'anchors matched %s times' % counts, []))
                continue
            mutated = base
            for o, nw in edits:
                mutated = mutated.replace(o, nw, 1)
            io.open(path, 'w', encoding='utf-8', newline='').write(mutated)
            killed, failed = False, []
            for t in tests:
                p = subprocess.run([sys.executable, '-X', 'utf8', t],
                                   capture_output=True, text=True,
                                   encoding='utf-8', errors='replace',
                                   timeout=1800, cwd=_ROOT)
                out = (p.stderr or '') + (p.stdout or '')
                if p.returncode:
                    killed = True
                failed += ['%s::%s' % (os.path.basename(t)[5:8],
                                       l.split('(')[0].replace('FAIL: ', '')
                                       .replace('ERROR: ', '').strip())
                           for l in out.splitlines()
                           if l.startswith(('FAIL:', 'ERROR:'))]
            io.open(path, 'w', encoding='utf-8', newline='').write(base)
            results.append((name, 'KILLED' if killed else 'SURVIVED', expect,
                            '%d' % len(failed), failed))
    finally:
        for k, v in TARGETS.items():
            io.open(v, 'w', encoding='utf-8', newline='').write(orig[k])

    w = max(len(r[0]) for r in results)
    wrong = 0
    for name, verdict, expect, cnt, failed in results:
        mark = ''
        if verdict != expect:
            mark = '   <-- WRONG, expected %s' % expect
            wrong += 1
        print('%-*s  %-9s  %-3s%s' % (w, name, verdict, cnt, mark))
        for f in failed:
            print('%s      %s' % (' ' * w, f))
    killed = sum(1 for r in results if r[1] == 'KILLED')
    survived = sum(1 for r in results if r[1] == 'SURVIVED')
    broken = sum(1 for r in results if r[1] == 'BROKEN')
    print('\n%d rows: %d killed, %d survived (%d of them expected), %d broken'
          % (len(results), killed, survived,
             sum(1 for r in results if r[1] == r[2] == 'SURVIVED'), broken))
    if wrong or broken:
        print('%d row(s) did not match their expectation' % (wrong + broken))
    return 1 if (wrong or broken) else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--row', help='run a single row by name')
    a = ap.parse_args()
    return run(a.row)


if __name__ == '__main__':
    sys.exit(main())
