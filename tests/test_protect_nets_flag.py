#!/usr/bin/env python3
"""--protect-nets shields named nets from THIS run's rip machinery (#521).

The run-5 defect this guards: only length-matched groups and routed diff pairs
were protected, so a retry step's `--rip-existing-nets` glob happily ripped a
hand-verified single-ended net as collateral (test-board: one rail rip opened
19 pads). The flag gives the operator the same shield the matchers get:

  * matches are protected for the run (reason 'user'),
  * the protection persists to the output .kicad_pro, so LATER chain steps
    honor it without the flag,
  * naming the net EXACTLY (no glob) in a rip set still overrides -- 'user'
    protection is deliberate-target overridable, unlike 'locked'.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

BOARD = os.path.join(ROOT, 'kicad_files', 'splitflap_driver.kicad_pcb')


def _run(board, out, extra, td):
    js = os.path.join(td, f'{os.path.basename(out)}.json')
    r = subprocess.run([sys.executable, '-X', 'utf8',
                        os.path.join(ROOT, 'route.py'), board, out,
                        '--json-out', js] + extra,
                       capture_output=True, text=True, encoding='utf-8',
                       errors='replace', cwd=ROOT)
    assert r.returncode == 0, r.stdout[-2500:]
    return json.load(open(js, encoding='utf-8')), r.stdout


def _seg_count(path, net_name):
    from kicad_parser import parse_kicad_pcb
    pcb = parse_kicad_pcb(path)
    ids = {i for i, n in pcb.nets.items() if n.name == net_name}
    return sum(1 for s in pcb.segments if s.net_id in ids)


def test_protect_glob_rip_and_exact_override():
    if not os.path.isfile(BOARD):
        print("  SKIP: fixture missing")
        return
    with tempfile.TemporaryDirectory() as td:
        staged = os.path.join(td, 'b.kicad_pcb')
        shutil.copyfile(BOARD, staged)

        # 1. commit GND copper
        r1 = os.path.join(td, 'r1.kicad_pcb')
        _run(staged, r1, ['--nets', 'GND'], td)
        gnd_before = _seg_count(r1, 'GND')
        assert gnd_before > 0, "setup: GND routed no copper"

        # 2. a later step glob-rips it -- but the operator protected it
        r2 = os.path.join(td, 'r2.kicad_pcb')
        summary, out = _run(r1, r2,
                            ['--nets', '/OUT_A_PHASE_A',
                             '--rip-existing-nets', 'GN?',
                             '--protect-nets', 'GND'], td)
        assert _seg_count(r2, 'GND') == gnd_before, \
            "protected net's copper changed under a glob rip"
        skipped = summary.get('protected_skipped') or {}
        flat = {n: r for ctx in skipped.values() for n, r in ctx.items()}
        assert flat.get('GND') == 'user', \
            f"refusal must be machine-readable with reason 'user', got {skipped}"
        # 3. and it persisted: the output project carries the protection
        pro = json.load(open(os.path.join(td, 'r2.kicad_pro'), encoding='utf-8'))
        rec = (pro.get('kicad_routing_tools') or {}).get('protected_nets') or {}
        assert rec.get('GND') == 'user', f"not persisted: {rec}"
        print("  PASS: glob rip refused (reason 'user'), persisted to .kicad_pro")

        # 4. next step, WITHOUT the flag: protection comes from the project;
        #    naming GND exactly in the rip set is the deliberate override
        r3 = os.path.join(td, 'r3.kicad_pcb')
        summary3, out3 = _run(r2, r3,
                              ['--nets', '/OUT_A_PHASE_B',
                               '--rip-existing-nets', 'GND'], td)
        flat3 = {n: r
                 for ctx in (summary3.get('protected_skipped') or {}).values()
                 for n, r in ctx.items()}
        assert 'GND' not in flat3, \
            f"exact name must override 'user' protection, got {summary3.get('protected_skipped')}"
        assert 'pre-existing net(s) eligible for rip-up: GND' in out3, \
            "exact-named GND should be rip-eligible again:\n" + '\n'.join(
                l for l in out3.splitlines() if 'rip' in l.lower())[:1500]
    print("  PASS: exact name in the rip set lifts the protection")


if __name__ == '__main__':
    test_protect_glob_rip_and_exact_override()
    print("ALL PASS")
