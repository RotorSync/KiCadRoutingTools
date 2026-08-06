#!/usr/bin/env python3
"""Relaxing a MANUFACTURING floor is a claim, and it must be said out loud.

The DRC writeback lowers a project's rules to the floors the router actually
used. For CLEARANCE that is a measured, documented decision: stock netclass
clearances are aspirational, and keeping them manufactures phantom violations
on correctly-routed copper.

`min_track_width`, `min_via_diameter`, `min_via_annular_width` and the drill
floors are a different kind of number. They say what the fab can make. Run 7
shipped two boards whose projects had been rewritten from 0.2 -> 0.0889 and
0.5 -> 0.25, with 5629 of 5933 segments and 371 of 712 vias below the board's
ORIGINAL declared floors -- and check_drc, board_score and KiCad's own DRC all
read clean, because every one of them grades against the rewritten project.

The relaxation stays (removing it re-manufactures the phantom DRC it exists to
prevent). What changes is that it can no longer happen quietly.

Run: python3 -X utf8 tests/test_run8_fab_floor_disclosure.py
"""
import io
import json
import os
import shutil
import sys
import tempfile
from contextlib import redirect_stdout

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import fix_kicad_drc_settings as fx  # noqa: E402

FAILURES = []


def check(name, cond, detail=''):
    print(f'  {"PASS" if cond else "FAIL"}  {name}'
          + (f'\n        {detail}' if not cond and detail else ''))
    if not cond:
        FAILURES.append(name)


def project(rules):
    return {'board': {'design_settings': {'rules': dict(rules)}}}


def main():
    print('what counts as a fab floor')
    keys = {k for k, _ in fx.FAB_FLOOR_KEYS}
    check('track width, via diameter and annular ring are fab floors',
          {'min_track_width', 'min_via_diameter',
           'min_via_annular_width'} <= keys)
    check('clearance is NOT a fab floor (it is a grading decision)',
          'min_clearance' not in keys)

    print('disclosure fires on a relaxation')
    before = {'min_track_width': 0.2, 'min_via_diameter': 0.5}
    after = project({'min_track_width': 0.0889, 'min_via_diameter': 0.25})
    lines = fx._fab_floor_disclosure('/does/not/exist.kicad_pcb', before, after)
    text = '\n'.join(lines)
    check('a relaxed floor is reported', bool(lines), text)
    check('it names the old and the new value',
          '0.2' in text and '0.0889' in text, text)
    check('it says every checker will now read this copper as clean',
          'read clean' in text, text)
    check('it survives an unreadable board (best-effort census)',
          'track width' in text, text)

    print('and stays silent when nothing was relaxed')
    same = project({'min_track_width': 0.127, 'min_via_diameter': 0.45})
    check('an unchanged floor says nothing',
          not fx._fab_floor_disclosure('/nope.kicad_pcb',
                                       {'min_track_width': 0.127,
                                        'min_via_diameter': 0.45}, same))
    tighter = project({'min_track_width': 0.3})
    check('a TIGHTENED floor is not a relaxation',
          not fx._fab_floor_disclosure('/nope.kicad_pcb',
                                       {'min_track_width': 0.2}, tighter))
    check('a floor the input never declared is not a relaxation',
          not fx._fab_floor_disclosure('/nope.kicad_pcb', {},
                                       project({'min_track_width': 0.0889})))

    print('end to end, with a real board')
    board = os.path.join(ROOT, 'kicad_files', 'routed_output.kicad_pcb')
    if not os.path.isfile(board):
        check('a routed corpus board is available', False, board)
    else:
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, 'out.kicad_pcb')
            shutil.copy(board, out)
            # A project declaring floors the routed copper cannot meet.
            with open(os.path.join(tmp, 'out.kicad_pro'), 'w',
                      encoding='utf-8') as fh:
                json.dump(project({'min_track_width': 0.5,
                                   'min_via_diameter': 2.0}), fh)
            buf = io.StringIO()
            with redirect_stdout(buf):
                fx.fix_project_for_output(out, clearance=0.2, verbose=False)
            out_text = buf.getvalue()
            check('the writeback discloses the relaxation it just made',
                  'FAB FLOOR RELAXED' in out_text, out_text[-400:])
            check('...with a count of the objects below the original floor',
                  'below the ORIGINAL' in out_text, out_text[-400:])

    print()
    if FAILURES:
        print(f'FAIL: {len(FAILURES)} check(s): {", ".join(FAILURES)}')
        return 1
    print('OK')
    return 0


if __name__ == '__main__':
    sys.exit(main())
