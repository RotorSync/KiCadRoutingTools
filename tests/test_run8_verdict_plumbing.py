#!/usr/bin/env python3
"""Results that were computed and then never reached a verdict channel.

Four run-7 findings, one shape: the tool KNEW, and the knowledge died in a log
line. A count in red text is not a verdict -- an exit code, a JSON key and a
ledger warning are.

  A16  check_drc at --clearance-margin 0 flagged pairs that were exactly at
       their required distance, because a FRACTIONAL tolerance collapses to
       0.0 and `overlap > 0.0` fires on double-precision residue.
  A10  repair_planes ripped nets to clear a plane corridor, failed
       to reconnect or restore them, printed one red line -- and exited 0 with
       a summary that carried counts but no names.
  A19  kicad_drc_compare fell through to a macOS bundle path on a machine
       without kicad-cli and died naming a file the user never chose.
  A20/D5/D6  converge recorded an entry whose lever text says REJECTED as
       accepted=true; recorded a CHECKER as the lever that produced a board;
       and accepted a back-filled entry whose clock precedes its predecessor.

Run: python3 -X utf8 tests/test_run8_verdict_plumbing.py
"""
import json
import os
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'py_router'))  # #522/py_placer layout
sys.path.insert(0, os.path.join(ROOT, 'py_placer'))  # #522/py_placer layout
sys.path.insert(0, os.path.join(ROOT, 'py_tools'))  # #522/py_placer layout

FAILURES = []


def check(name, cond, detail=''):
    print(f'  {"PASS" if cond else "FAIL"}  {name}'
          + (f'\n        {detail}' if not cond and detail else ''))
    if not cond:
        FAILURES.append(name)


def test_a16_epsilon_floor():
    """The tolerance is a fraction of clearance, floored above float residue."""
    import check_drc

    check('margin 0.05 still scales with clearance',
          abs(check_drc._grade_tol(0.2, 0.05) - 0.01) < 1e-12)
    check('margin 0 does not collapse to exactly zero',
          check_drc._grade_tol(0.2, 0.0) > 0.0)
    check('the floor is far below KiCad file resolution (1nm)',
          check_drc._grade_tol(0.2, 0.0) < 1e-6)

    # The residue this exists to absorb: a pair sitting EXACTLY at its required
    # distance, measured through hypot(). Real geometry never lands here.
    import math
    required = 0.5
    dx, dy = 0.3, 0.4                       # hypot -> 0.5, with residue
    overlap = required - math.hypot(dx, dy)
    check('an exactly-at-clearance pair is not a violation at margin 0',
          not (overlap > check_drc._grade_tol(required, 0.0)),
          f'overlap={overlap!r}')
    # ...while a real sub-clearance graze still is.
    check('a real 1um graze IS a violation at margin 0',
          (required - 0.499) > check_drc._grade_tol(required, 0.0))


def test_a10_rip_casualties_reach_the_verdict():
    """The still-open casualty set has a name channel and an exit code."""
    import repair_planes as rdp

    check('module publishes the still-open casualty NAMES',
          isinstance(rdp.LAST_RIPPED_STILL_OPEN, list))
    src = open(os.path.join(ROOT, 'py_router', 'repair_planes.py'),
               encoding='utf-8').read()
    check('the summary carries ripped_still_open',
          '"ripped_still_open"' in src or "'ripped_still_open'" in src)
    check('main() returns a nonzero code when casualties ship open',
          'return 4' in src.split('JSON_SUMMARY')[-1])
    check('the entry point propagates main()\'s code',
          'sys.exit(main())' in src)


def test_a19_kicad_cli_message_is_actionable():
    from tests.stress import kicad_drc_compare as kdc

    saved = kdc.KICAD_CLI
    try:
        kdc.KICAD_CLI = os.path.join(ROOT, 'no', 'such', 'kicad-cli')
        data, err = kdc.run_kicad_drc(
            os.path.join(ROOT, 'kicad_files', 'splitflap_driver.kicad_pcb'))
        check('a missing kicad-cli returns an error, not an exception',
              data is None and err)
        check('the error names the env var to set', 'KICAD_CLI' in (err or ''),
              err)
        check('the error says the oracle was skipped',
              'UNCORROBORATED' in (err or ''), err)
    finally:
        kdc.KICAD_CLI = saved


def _record(ledger, board, **kw):
    argv = [sys.executable, '-X', 'utf8', os.path.join(ROOT, 'py_placer', 'converge.py'),
            'record', '--ledger', ledger, '--board', board,
            '--kind', kw.pop('kind', 'placement'),
            '--lever', kw.pop('lever', 'test lever')]
    if kw.pop('rejected', False):
        argv.append('--rejected')
    extra_argv = kw.pop('argv', None)
    if extra_argv:
        argv += ['--argv'] + list(extra_argv)
    proc = subprocess.run(argv, capture_output=True, text=True,
                          encoding='utf-8', errors='replace', cwd=ROOT)
    return proc.returncode, (proc.stdout or '') + (proc.stderr or '')


def test_converge_nags():
    board = os.path.join(ROOT, 'kicad_files', 'splitflap_driver.kicad_pcb')
    with tempfile.TemporaryDirectory() as tmp:
        ledger = os.path.join(tmp, 'ledger.jsonl')

        code, out = _record(ledger, board,
                            lever='rung REJECTED by the two-conjunct gate')
        check('A20: reject-text recorded as accepted warns',
              code == 0 and 'reads as a REJECTION' in out, out[-400:])

        code, out = _record(ledger, board, lever='accepted: repair lap 1')
        check('A20: an ordinary accepted entry is quiet',
              code == 0 and 'reads as a REJECTION' not in out, out[-400:])

        code, out = _record(ledger, board, lever='accepted lap', rejected=True)
        check('A20: --rejected with no rejection wording warns',
              code == 0 and 'never says so' in out, out[-400:])

        code, out = _record(ledger, board, lever='lap 2',
                            argv=[os.path.join(ROOT, 'py_router', 'check_drc.py'),
                                  board, '--clearance', '0.1'])
        check('D5: a CHECKER recorded as the lever warns',
              code == 0 and 'measures a board rather than changing one' in out,
              out[-400:])

        code, out = _record(ledger, board, lever='lap 3',
                            argv=[os.path.join(ROOT, 'py_placer', 'place_seed.py'),
                                  board, 'out.kicad_pcb', '--repair'])
        check('D5: a real transform is quiet',
              code == 0 and 'measures a board rather' not in out, out[-400:])

        # D6: an entry whose predecessor is stamped in the future is a back-fill.
        entries = [json.loads(ln) for ln in open(ledger, encoding='utf-8')]
        entries[-1]['t'] = time.time() + 3600
        with open(ledger, 'w', encoding='utf-8') as fh:
            for e in entries:
                fh.write(json.dumps(e) + '\n')
        code, out = _record(ledger, board, lever='lap 4')
        check('D6: a back-filled ledger warns about its own ordering',
              code == 0 and 'clock is BEHIND' in out, out[-400:])


def main():
    print('A16 -- check_drc epsilon floor')
    test_a16_epsilon_floor()
    print('A10 -- rip casualties reach the verdict')
    test_a10_rip_casualties_reach_the_verdict()
    print('A19 -- kicad-cli message')
    test_a19_kicad_cli_message_is_actionable()
    print('A20/D5/D6 -- converge nags')
    test_converge_nags()

    print()
    if FAILURES:
        print(f'FAIL: {len(FAILURES)} check(s): {", ".join(FAILURES)}')
        return 1
    print('OK')
    return 0


if __name__ == '__main__':
    sys.exit(main())
