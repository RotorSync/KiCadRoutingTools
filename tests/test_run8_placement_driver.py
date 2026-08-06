#!/usr/bin/env python3
"""The placement driver is the workflow; the skill file is its reference.

A skill document is read all at once, which is how a 4,900-line one produced
executors that skimmed the gate and improvised the ladder. The driver is the
tape head instead: one stage per invocation, and a guard that WITHHOLDS the
next stage until the evidence the previous one owed actually exists.

The refusal is the mechanism. A gate written in prose is a sentence someone
skims; a gate that will not print the next instructions cannot be skimmed past.

This test runs the driver's own --self-test (33 checks: every stage emits a
tagged block, says what comes next, stays under 80 lines, no hedging phrases,
every guard refuses without its evidence and proceeds with it) and pins the
contract the skill file promises.

Run: python3 -X utf8 tests/test_run8_placement_driver.py
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DRIVER = os.path.join(ROOT, '.claude', 'skills', 'plan-pcb-placement',
                      'scripts', 'placement_driver.py')
SKILL = os.path.join(ROOT, '.claude', 'skills', 'plan-pcb-placement',
                     'SKILL.md')
FAILURES = []


def check(name, cond, detail=''):
    print(f'  {"PASS" if cond else "FAIL"}  {name}'
          + (f'\n        {detail}' if not cond and detail else ''))
    if not cond:
        FAILURES.append(name)


def run(args):
    p = subprocess.run([sys.executable, '-X', 'utf8', DRIVER] + args,
                       capture_output=True, text=True, encoding='utf-8',
                       errors='replace', cwd=ROOT)
    return p.returncode, (p.stdout or '') + (p.stderr or '')


def main():
    check('the driver ships with the skill', os.path.isfile(DRIVER))

    code, out = run(['--self-test'])
    check('its own self-test passes', code == 0 and out.strip().endswith('OK'),
          out[-400:])

    code, out = run(['--list'])
    check('it lists its stages', code == 0 and 'P0' in out and 'P-close' in out)

    print('one stage at a time')
    code, out = run(['--stage', 'P0', '--board', 'b.kicad_pcb'])
    check('a stage emits and exits 0', code == 0)
    check('...tagged as instructions for the reader',
          out.startswith('<stage_instructions'))
    check('...naming only its own stage',
          out.count('<stage_instructions') == 1 and 'stage="P0"' in out)
    check('...and short enough to act on', len(out.splitlines()) <= 80,
          str(len(out.splitlines())))

    print('guards withhold, and say what is missing')
    code, out = run(['--stage', 'P4', '--board', 'b.kicad_pcb'])
    check('a stage whose evidence is absent refuses', code == 4, out[:200])
    check('the refusal is tagged', out.startswith('<error>'))
    check('it names what to produce', '--before' in out)

    code, out = run(['--stage', 'P3', '--board', 'b.kicad_pcb'])
    check('reconstruct refuses without the copper-free measurement', code == 4)
    check('...and gives the command that makes it', 'check_drc.py' in out)

    print('the skill file points at the driver')
    text = open(SKILL, encoding='utf-8').read()
    check('the skill tells the reader to drive, not to improvise',
          'placement_driver.py' in text and '--stage P0' in text)
    check('it explains the three tags',
          '<stage_instructions>' in text and '<subagent_prompt>' in text
          and '<error>' in text)
    check('it says a subagent prompt is NOT the reader\'s instructions',
          'Do NOT read it as your own instructions' in text)
    check('it says an error means a gate is holding',
          'not a malfunction' in text)

    print()
    if FAILURES:
        print(f'FAIL: {len(FAILURES)} check(s): {", ".join(FAILURES)}')
        return 1
    print('OK')
    return 0


if __name__ == '__main__':
    sys.exit(main())
