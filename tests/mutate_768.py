#!/usr/bin/env python3
"""The #768/#769 mutation battery, shipped so its numbers can be re-derived.

`tests/test_768_cap_clearance_ceiling.py` records what each arm kills. A count
is only checkable if the exact source edit is written down -- two reviewers of
the #746 branch reconstructed its rows from their names and both got the wrong
answer, because a plausible-looking reconstruction of one row was semantically
inert. So the edits live here, as data, next to the numbers they produced.

Every row carries an EXPECTATION. Some mutations are deliberately inert, and an
inert row recorded as an expected survivor is a finding, while an inert row
quietly deleted is a hole. `writeback-spends-the-flag-not-the-priced-value` is
the one here, and its reasoning is written beside it.

FOUR targets, which is one more than any previous copy: the change spans the
model (`legality.py`), the engine (`fanout_clearance.py`), the CLI main
(`place_fanout_clearance.py`) and the GUI call site (`fanout_gui.py`). That
spread is the point -- CLAUDE.md's CLI/GUI rule says a fix to one front is not
automatically a fix to the other, and rows 20-21 are what hold the GUI to it.

This is the FOURTH copy of this runner (`mutate_730.py`, `mutate_750.py`,
`mutate_756.py`, `mutate_761.py`). It is not refactored into a shared one
deliberately: that would rewrite four shipped batteries whose recorded counts
are the evidence for four merged reviews, which is a change to make on its own,
not inside a fix.

NOT named `test_*.py`, so `tests/run_all.py` does not collect it: it REWRITES
the engine in place. One writer per tree -- do not run it while a suite, an A/B
replay or a review is reading the same checkout. The file refuses to start on a
dirty target, because restoring would write the COMMITTED text back over
uncommitted work.

    python3 tests/mutate_768.py
    python3 tests/mutate_768.py --row cap-is-assignment-not-min

A row is KILLED by a FAILURE **or an ERROR**: several of these mutations make an
arm raise rather than fail (inverting the `ceiling is not None` guard reaches
`min(v, None)`), and a battery that counted only failures would call that a
survivor.

An anchor that does not match EXACTLY ONCE is reported as BROKEN rather than
skipped -- a battery that silently applies nothing reports every row as a
survivor, which reads as a catastrophic test failure and is really a stale
anchor.

Python `str.replace(old, new, 1)`, never `sed`: commit `bb8f4477` records two
rows of `mutate_761` left a `SyntaxError` behind because `sed` ate an unescaped
metacharacter, and a battery that cannot start reports nothing at all.
"""
from __future__ import annotations

import argparse
import io
import os
import subprocess
import sys

_TESTS = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_TESTS)

LEG = os.path.join(_ROOT, 'py_placer', 'placement', 'legality.py')
FC = os.path.join(_ROOT, 'py_placer', 'placement', 'fanout_clearance.py')
CLI = os.path.join(_ROOT, 'py_placer', 'place_fanout_clearance.py')
GUI = os.path.join(_ROOT, 'kicad_routing_plugin', 'fanout_gui.py')
MAN = os.path.join(_ROOT, 'tests', 'stress', 'manifest_to_plan.py')
TARGETS = {'leg': LEG, 'fc': FC, 'cli': CLI, 'gui': GUI, 'man': MAN}

T768 = os.path.join(_TESTS, 'test_768_cap_clearance_ceiling.py')
T697 = os.path.join(_TESTS, 'test_697_placement_pad_clearance.py')
T725 = os.path.join(_TESTS, 'test_725_fanout_clearance_pad_floors.py')

# The disclosure block, quoted once because three rows touch it.
_DISCLOSE = """                    if _capped:
                        notes.append(
                            'net classes capped at the %gmm --clearance '
                            'ceiling: %s' % (ceiling, ', '.join(
                                '%g -> %g (%d net%s)'
                                % (_v, ceiling, _c, '' if _c == 1 else 's')
                                for _v, _c in sorted(_capped.items()))))
"""

_HOLE_KW = """                hole_clearance=board_constraint(args.input_file,
                                                'min_hole_clearance'),
"""

_GUI_CEILING = """                netclass_ceiling=(
                    fanout_config.get('clearance', defaults.BGA_CLEARANCE)
                    if fanout_config.get('fix_drc_settings', True) else None),
"""

ROWS = [
    # ---- the cap itself, in for_board's netclass admission map ----------
    ('cap-is-assignment-not-min', 'leg',
     '                    by_name = {n: min(v, ceiling) for n, v in by_name.items()}',
     '                    by_name = {n: ceiling for n, v in by_name.items()}',
     (T768,), 'KILLED'),
    ('cap-is-max-not-min', 'leg',
     '                    by_name = {n: min(v, ceiling) for n, v in by_name.items()}',
     '                    by_name = {n: max(v, ceiling) for n, v in by_name.items()}',
     (T768,), 'KILLED'),
    ('cap-removed', 'leg',
     '                    by_name = {n: min(v, ceiling) for n, v in by_name.items()}',
     '                    pass',
     (T768,), 'KILLED'),
    # Inverting the guard, NOT deleting it: deleting it leaves `ceiling` unbound
    # in the None case only by luck of the comparison order. Inverted, the
    # uncapped path takes the capped branch and reaches `min(v, None)`, which
    # is the ERROR half of the kill rule.
    ('cap-guard-inverted', 'leg',
     '                if ceiling is not None:',
     '                if ceiling is None:',
     (T768, T697), 'KILLED'),
    ('cap-not-disclosed', 'leg',
     _DISCLOSE, '',
     (T768,), 'KILLED'),
    ('ceiling-not-recorded-on-the-model', 'leg',
     '                    has_overrides=has_overrides, ceiling=ceiling)',
     '                    has_overrides=has_overrides, ceiling=None)',
     (T768,), 'KILLED'),

    # ---- resolve_pair_clearance: the two documented branches ------------
    ('omitted-ignores-the-board', 'fc',
     "            return float(declared), 'board netclass'",
     "            return float(_defaults.CLEARANCE), 'board netclass'",
     (T768,), 'KILLED'),
    ('base-ignores-the-declaration', 'fc',
     "        return min(float(declared), float(clearance)), 'cli'",
     "        return float(clearance), 'cli'",
     (T768,), 'KILLED'),
    ('zero-declaration-honoured', 'fc',
     '    if declared is not None and declared <= 0:',
     '    if False:',
     (T768,), 'KILLED'),

    # ---- the layer axis --------------------------------------------------
    ('layer-fallback-dropped', 'fc',
     "    n = len([l for l in (_cu or []) if str(l).endswith('.Cu')]) or 2",
     "    n = len([l for l in (_cu or []) if str(l).endswith('.Cu')])",
     (T768,), 'KILLED'),
    ('layer-filter-dropped', 'fc',
     "    n = len([l for l in (_cu or []) if str(l).endswith('.Cu')]) or 2",
     "    n = len(_cu or []) or 2",
     (T768,), 'KILLED'),
    # MATCH THE GRADER. check_drc does not fab-floor copper clearance, so a
    # pass that raised to it would refuse landings its own checker passes.
    ('sub-fab-clamped', 'fc',
     '    if clearance < _fab_clr - 1e-9:',
     '    clearance = max(clearance, _fab_clr)\n    if False:',
     (T768,), 'KILLED'),

    # ---- the engine's resolve + disclosure -------------------------------
    ('engine-does-not-resolve', 'fc',
     '    clearance, _clr_src = resolve_pair_clearance(pcb_file, clearance)',
     "    _clr_src = 'cli'",
     (T768,), 'KILLED'),
    ('priced-not-disclosed', 'fc',
     '    print(f"  cap pair clearance: {clearance}mm ({_clr_src})")',
     '    pass',
     (T768,), 'KILLED'),
    ('engine-drops-the-ceiling', 'fc',
     '                                             ceiling=netclass_ceiling)',
     '                                             ceiling=None)',
     (T768,), 'KILLED'),

    # ---- the CLI: one rule, every exit -----------------------------------
    ('cli-drops-the-ceiling', 'cli',
     '        netclass_ceiling=args.clearance,',
     '        netclass_ceiling=None,',
     (T768,), 'KILLED'),
    ('omitted-clamps-anyway', 'cli',
     '                clamp_nondefault_netclasses=args.clearance is not None)',
     '                clamp_nondefault_netclasses=True)',
     (T768,), 'KILLED'),
    ('hole-clearance-rides-the-ceiling', 'cli',
     _HOLE_KW, '',
     (T768,), 'KILLED'),
    # #769 proper: the clamp used to live only in the branch that moved a cap.
    ('769-copy-branch-unwritten', 'cli',
     '        print(f"Wrote {args.output_file} (unchanged copy)")\n'
     '        _write_drc_floors()\n',
     '        print(f"Wrote {args.output_file} (unchanged copy)")\n',
     (T768,), 'KILLED'),
    # EXPECTED SURVIVOR, and the reason is worth recording rather than hiding.
    # `_priced` and `args.clearance` are provably equal whenever the flag is
    # given (the ceiling is min(Default, flag), and compute_targets is
    # lower-only, so a target above the current value is a no-op either way),
    # and when the flag is OMITTED `fix_project_for_output` falls back to
    # `project_copper_clearance`, which is the same Default class `_priced`
    # resolved to. So no board in the corpus can tell them apart. `_priced` is
    # kept because it is the honest expression of "write back what you priced
    # at" -- but nothing here can prove that, and pretending otherwise with a
    # KILLED expectation would be the folklore this file exists to prevent.
    ('writeback-spends-the-flag-not-the-priced-value', 'cli',
     '                clearance=_priced,',
     '                clearance=args.clearance,',
     (T768,), 'SURVIVED'),

    # ---- the GUI half (CLAUDE.md: a CLI fix is not a GUI fix) ------------
    ('gui-ceiling-ungated', 'gui',
     "                    if fanout_config.get('fix_drc_settings', True) else None),",
     '                    if True else None),',
     (T768,), 'KILLED'),
    ('gui-no-ceiling', 'gui',
     _GUI_CEILING, '',
     (T768,), 'KILLED'),

    # ---- the recorded-manifest round trip --------------------------------
    ('manifest-drops-clearance', 'man',
     "    '--clearance': 'clearance',\n", '',
     (T768,), 'KILLED'),
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
                                   timeout=2400, cwd=_ROOT)
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
