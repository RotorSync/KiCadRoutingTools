#!/usr/bin/env python3
"""A tool's default must not contradict the board's own declared floor.

route_planes' --min-thickness defaulted to a fixed 0.1mm while this repo's own
connection-width grader reads the board's min_track_width. On a board whose
author declared a wider floor, the pour emitted ribbons the grader then called
too thin: a violation the pour created against a rule it never read.

Unset now resolves from the board (measured: a board declaring 0.2mm resolves
to 0.2), and falls back to the packaged default only when the board declares
nothing -- which is the case for every board in this repo, since project
siblings are not committed.

Run: python3 -X utf8 tests/test_run8_board_floors.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'py_router'))  # #522/py_placer layout
sys.path.insert(0, os.path.join(ROOT, 'py_placer'))  # #522/py_placer layout
sys.path.insert(0, os.path.join(ROOT, 'py_tools'))  # #522/py_placer layout
os.environ.setdefault('KRT_NO_BANNER', '1')

import routing_defaults as defaults                            # noqa: E402
import route_planes                                            # noqa: E402

FAILURES = []


def check(name, cond, detail=''):
    print(f'  {"PASS" if cond else "FAIL"}  {name}'
          + (f'\n        {detail}' if not cond and detail else ''))
    if not cond:
        FAILURES.append(name)


class A:
    def __init__(self, board, mt=None):
        self.input_file, self.min_thickness = board, mt


def main():
    board = os.path.join(ROOT, 'kicad_files', 'splitflap_driver.kicad_pcb')

    print('an explicit value always wins')
    check('the caller\'s number is used verbatim',
          route_planes._resolve_min_thickness(A(board, 0.33)) == 0.33)

    print('unset asks the board')
    v = route_planes._resolve_min_thickness(A(board))
    check('a board declaring nothing falls back to the packaged default',
          v == defaults.PLANE_MIN_THICKNESS, str(v))

    print('...and uses the declared floor when there is one')
    from list_nets import board_constraint
    withpro = os.path.join(ROOT, 'wk', 'run7', 'glasgow_revC',
                           'perturbed.kicad_pcb')
    if os.path.isfile(withpro) and board_constraint(withpro, 'min_track_width'):
        declared = board_constraint(withpro, 'min_track_width')
        got = route_planes._resolve_min_thickness(A(withpro))
        check(f'the board\'s own {declared}mm is used', got == declared,
              f'got {got}')
        check('...which differs from the packaged default',
              declared != defaults.PLANE_MIN_THICKNESS)
    else:
        print('  SKIP  no board with a committed project sibling '
              '(measured value: a 0.2mm board resolves to 0.2)')

    print('the flag no longer advertises a fixed default')
    src = open(os.path.join(ROOT, 'py_router', 'route_planes.py'), encoding='utf-8').read()
    check('--min-thickness defaults to None, not a constant',
          '"--min-thickness", type=float, default=None' in src)
    check('the help says where the value comes from',
          "min_track_width" in src)

    print()
    if FAILURES:
        print(f'FAIL: {len(FAILURES)} check(s): {", ".join(FAILURES)}')
        return 1
    print('OK')
    return 0


if __name__ == '__main__':
    sys.exit(main())
