#!/usr/bin/env python3
"""fence_audit: a ground-truth leak is content, not a filename (run-7 D4).

The hole this closes: the perturbation's INPUT board (the human placement with
its copper stripped) sat in every run-7 work dir. Nothing named it as truth, so
nothing fenced it -- and its footprint poses ARE the human placement.

The four behaviours pinned here are the ones that make the audit usable rather
than merely loud:

  1. a renamed copy of the control is caught (the point);
  2. a work dir with only the perturbed board is clean (no false alarm);
  3. a board PRODUCED BY THE RUN that reaches truth is NOT a leak -- one run-7
     board recovered bit-exactly (d1 = 0.000000) and every downstream board it
     wrote matches the control. Calling that a breach would make the audit fire
     loudest on the experiment's best result;
  4. a board PRESENT AT CREATION that matches truth still is a leak, even
     after the same run.

3 and 4 are the same content and opposite verdicts, so the creation manifest --
not a threshold -- is what separates them.

Run: python3 -X utf8 tests/test_run8_fence_audit.py
"""
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

AUDIT = os.path.join(ROOT, 'tests', 'stress', 'fence_audit.py')
BOARD = os.path.join(ROOT, 'kicad_files', 'splitflap_driver.kicad_pcb')

CLEAN, LEAK = 0, 4


def run_audit(control, workdir, mode):
    proc = subprocess.run(
        [sys.executable, '-X', 'utf8', AUDIT, '--control', control,
         '--workdir', workdir, '--mode', mode],
        capture_output=True, text=True, encoding='utf-8', errors='replace')
    return proc.returncode, (proc.stdout or '') + (proc.stderr or '')


def make_case(tmp):
    """control + a perturbed board whose poses differ from it."""
    from placement.perturb import perturb

    control = os.path.join(tmp, 'perturbed.control.kicad_pcb')
    shutil.copy(BOARD, control)
    perturbed = os.path.join(tmp, 'perturbed.kicad_pcb')
    perturb(control, perturbed, kind='translate', seed=91, dose_mm=8.0)
    return control, perturbed


def main():
    failures = []

    def check(name, cond, detail=''):
        print(f'  {"PASS" if cond else "FAIL"}  {name}'
              + (f'\n        {detail}' if not cond and detail else ''))
        if not cond:
            failures.append(name)

    with tempfile.TemporaryDirectory() as tmp:
        wd = os.path.join(tmp, 'wk')
        os.makedirs(wd)
        control, perturbed = make_case(wd)

        # 1. clean: the work dir holds the perturbed board and the DECLARED
        #    control only. The control is excluded by name on purpose -- the
        #    audit hunts undeclared carriers.
        code, out = run_audit(control, wd, 'audit')
        check('perturbed-only work dir is CLEAN', code == CLEAN, out)

        # 2. the leak, under a name nobody would fence
        leaked = os.path.join(wd, 'stripped.kicad_pcb')
        shutil.copy(control, leaked)
        code, out = run_audit(control, wd, 'audit')
        check('a renamed control copy is a LEAK', code == LEAK, out)
        check('the leak names the file', 'stripped.kicad_pcb' in out, out)

        os.remove(leaked)

        # 3. create -> manifest -> a board written LATER that reaches truth
        code, out = run_audit(control, wd, 'create')
        check('create mode on a clean dir exits 0', code == CLEAN, out)
        check('create writes the manifest',
              os.path.isfile(os.path.join(wd, '.fence-manifest.json')), out)

        recovered = os.path.join(wd, 'r4_final.kicad_pcb')
        shutil.copy(control, recovered)          # a perfect reconstruction
        code, out = run_audit(control, wd, 'audit')
        check('a board produced BY the run that reaches truth is not a leak',
              code == CLEAN, out)
        check('...and is reported as recovery, not silence',
              'r4_final.kicad_pcb' in out and 'produced by the run' in out, out)

        # 4. same content, present at creation -> still a leak
        os.remove(recovered)
        os.remove(os.path.join(wd, '.fence-manifest.json'))
        preexisting = os.path.join(wd, 'board_orig.kicad_pcb')
        shutil.copy(control, preexisting)
        code, out = run_audit(control, wd, 'create')
        check('create mode CATCHES a pre-existing carrier', code == LEAK, out)

    print()
    if failures:
        print(f'FAIL: {len(failures)} check(s): {", ".join(failures)}')
        return 1
    print('OK')
    return 0


if __name__ == '__main__':
    sys.exit(main())
