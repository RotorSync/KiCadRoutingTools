#!/usr/bin/env python3
"""A face that carries demand and has no lane left (E8) -- as a DELTA, not an absolute.

The third wrong-basin counter-gate, and the one whose first design failed.

The proposal was absolute: flag any escape face with zero supply at the finest
grid while carrying >= N nets, because such a face is a placement the router
cannot rescue. It reads well, and it does discriminate on the board it came
from. Calibrated against the preregistered promotion rule ("promote only if no
healthy board fires"), it fails:

    demand >= 5    6 of 33 healthy in-repo boards fire
    demand >= 7    4 of 33
    demand >= 9    2 of 33
    demand >= 11   2 of 33

and worse, on one run the HUMAN control board fires exactly the same face as
the tool's output. A starved face is often a property of the DESIGN -- a dense
part hard against an edge -- not of the placement decision under test.

Taking the delta against the board the placement was derived from removes the
design term, because both boards carry it:

    a wrong-basin placement    1 starved face on its input -> 2, NEW: 2
    three legitimate runs      unchanged, NEW: 0

So the gate is `--baseline` + `--gate`, mirroring check_assembly, and the
absolute count stays a report. The refuted absolute form is recorded here with
its numbers so the finding stays a change detector rather than folklore.

Run: python3 -X utf8 tests/test_run8_starved_face_gate.py
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'py_router'))  # #522/py_placer layout
sys.path.insert(0, os.path.join(ROOT, 'py_placer'))  # #522/py_placer layout
sys.path.insert(0, os.path.join(ROOT, 'py_tools'))  # #522/py_placer layout
os.environ.setdefault('KRT_NO_BANNER', '1')

import check_channels                                          # noqa: E402

FAILURES = []


def check(name, cond, detail=''):
    print(f'  {"PASS" if cond else "FAIL"}  {name}'
          + (f'\n        {detail}' if not cond and detail else ''))
    if not cond:
        FAILURES.append(name)


def run(args):
    proc = subprocess.run(
        [sys.executable, '-X', 'utf8',
         os.path.join(ROOT, 'py_tools', 'check_channels.py')] + args,
        capture_output=True, text=True, encoding='utf-8', errors='replace',
        cwd=ROOT)
    return proc.returncode, (proc.stdout or '') + (proc.stderr or '')


def main():
    print('the rule itself')
    led = {'U1': [{'face': 'S', 'demand_nets': 10, 'supply_finest_grid': 0},
                  {'face': 'N', 'demand_nets': 12, 'supply_finest_grid': 4},
                  {'face': 'W', 'demand_nets': 2, 'supply_finest_grid': 0}]}
    starved = check_channels._starved_faces(led, check_channels.GATE_MIN_DEMAND)
    check('a face with demand and no supply is starved',
          ('U1', 'S', 10) in starved, str(starved))
    check('a face with supply is not', not any(f == 'N' for _, f, _ in starved))
    check('a face with almost no demand is not (that is noise, not news)',
          not any(f == 'W' for _, f, _ in starved), str(starved))
    check('the demand floor is a named constant, not a literal',
          isinstance(check_channels.GATE_MIN_DEMAND, int)
          and check_channels.GATE_MIN_DEMAND >= 5)

    print('the gate needs a baseline (the absolute form was refuted)')
    board = os.path.join(ROOT, 'kicad_files', 'tigard.kicad_pcb')
    code, out = run([board, '--gate'])
    check('a --gate run with no baseline cannot fail the board',
          code == 0, out[-300:])

    code, out = run([board, '--baseline', board, '--gate'])
    check('a board against ITSELF is never a new starvation',
          code == 0 and 'NEW' in out, out[-300:])
    check('...and says so with counts', '0 NEW' in out, out[-300:])

    print('recorded runs, when the work dir is present (wk/ is gitignored)')
    wrong = os.path.join(ROOT, 'wk', 'run7', 'glasgow_revC')
    if os.path.isdir(wrong):
        code, out = run([os.path.join(wrong, 'rL_repair.kicad_pcb'),
                         '--clearance', '0.09', '--baseline',
                         os.path.join(wrong, 'perturbed.kicad_pcb'), '--gate'])
        check('the wrong-basin placement fails the gate', code == 4,
              out[-400:])
        check('it names the starved faces', out.count('NEW:') >= 2, out[-400:])
    else:
        print('  SKIP  recorded boards not present; the measured values are '
              'in the docstring')

    print()
    if FAILURES:
        print(f'FAIL: {len(FAILURES)} check(s): {", ".join(FAILURES)}')
        return 1
    print('OK')
    return 0


if __name__ == '__main__':
    sys.exit(main())
