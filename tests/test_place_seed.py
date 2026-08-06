#!/usr/bin/env python3
"""place_seed: an unplaced pile + an intent must become a placement that
grades clean against that same intent, deterministically per seed.

The pile fixture is synthesized (every part stacked at the board center, the
test_431 trick) and the intent is emitted OFF the real placed board, so the
seeder is asked for an arrangement that provably exists."""
import json
import os
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (REPO,):
    if p not in sys.path:
        sys.path.insert(0, p)
        sys.path.insert(0, os.path.join(p, 'py_router'))  # placement split
        sys.path.insert(0, os.path.join(p, 'py_tools'))  # placement split
        sys.path.insert(0, os.path.join(p, 'py_placer'))  # placement split

BOARD = os.path.join(REPO, "kicad_files", "splitflap_driver.kicad_pcb")

passed = failed = 0


def check(name, ok, detail=""):
    global passed, failed
    passed += bool(ok)
    failed += not ok
    print(f"  {'OK  ' if ok else 'FAIL'} {name}{(' -- ' + detail) if detail else ''}")


def run(argv, hashseed="0", timeout=1800):
    env = dict(os.environ, PYTHONHASHSEED=hashseed,
               PYTHONIOENCODING='utf-8')
    return subprocess.run([sys.executable, '-X', 'utf8'] + argv,
                          capture_output=True, text=True, encoding='utf-8',
                          errors='replace', cwd=REPO, env=env,
                          timeout=timeout)


def summary(r):
    line = next(l for l in r.stdout.splitlines()
                if l.startswith('JSON_SUMMARY:'))
    return json.loads(line.split(':', 1)[1])


if not os.path.isfile(BOARD):
    print("SKIP: fixture missing")
    sys.exit(0)

from kicad_parser import parse_kicad_pcb
from placement.floorplan import emit_intent
from placement.writer import write_placed_output

with tempfile.TemporaryDirectory() as d:
    pcb = parse_kicad_pcb(BOARD)
    n_parts = sum(1 for fp in pcb.footprints.values() if fp.pads)
    b = pcb.board_info.board_bounds
    cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
    pile = os.path.join(d, 'pile.kicad_pcb')
    write_placed_output(BOARD, pile, [
        {'reference': ref, 'new_x': cx, 'new_y': cy, 'new_rotation': 0}
        for ref, fp in sorted(pcb.footprints.items()) if fp.pads])
    intent_path = os.path.join(d, 'intent.json')
    with open(intent_path, 'w', encoding='utf-8') as f:
        json.dump(emit_intent(pcb, BOARD), f)

    seed_py = os.path.join(REPO, 'py_placer', 'place_seed.py')
    out_a = os.path.join(d, 'a.kicad_pcb')
    r = run([seed_py, pile, out_a, '--intent', intent_path])
    check("seed run exits 0", r.returncode == 0,
          (r.stdout + r.stderr)[-500:])
    s = summary(r)
    check("all parts placed", s['placed'] == n_parts,
          f"{s['placed']}/{n_parts}")
    check("no unseated parts", s['unseated'] == 0)
    check("grade has zero errors", s['grade_errors'] == 0)

    # deterministic per seed, across PYTHONHASHSEED
    out_b = os.path.join(d, 'b.kicad_pcb')
    r = run([seed_py, pile, out_b, '--intent', intent_path], hashseed="12345")
    check("re-run exits 0", r.returncode == 0)
    check("same seed is byte-identical across hash seeds",
          open(out_a, 'rb').read() == open(out_b, 'rb').read())

    out_c = os.path.join(d, 'c.kicad_pcb')
    r = run([seed_py, pile, out_c, '--intent', intent_path, '--seed', '3'])
    check("--seed 3 exits 0/4", r.returncode in (0, 4), f"rc={r.returncode}")
    check("--seed 3 differs",
          open(out_a, 'rb').read() != open(out_c, 'rb').read())

    # a placed board is refused without --force
    r = run([seed_py, BOARD, os.path.join(d, 'x.kicad_pcb'),
             '--intent', intent_path])
    check("placed board refused with exit 3", r.returncode == 3,
          f"rc={r.returncode}")
    check("refusal names place_portfolio as the explore path",
          'place_portfolio' in (r.stdout + r.stderr))

    # the emitted seed is a valid input for the portfolio (composition)
    from placement.placement_state import assess_placement
    st = assess_placement(parse_kicad_pcb(out_a), out_a)
    check("seeded board assesses as PLACED", not st.unplaced,
          '; '.join(st.reasons))

    # sibling carry (d310ab3 regression): the seeder is the FIRST step that
    # touches the board, so a dropped .kicad_pro propagates a stock-netclass
    # floor through the whole chain (#441 class). Give the pile siblings and
    # the output must get them too.
    with open(os.path.join(d, 'pile.kicad_pro'), 'w', encoding='utf-8') as f:
        json.dump({'net_settings': {'classes': [
            {'name': 'Default', 'clearance': 0.15}]}}, f)
    with open(os.path.join(d, 'pile.kicad_dru'), 'w', encoding='utf-8') as f:
        f.write('(version 1)\n')
    out_s = os.path.join(d, 's.kicad_pcb')
    r = run([seed_py, pile, out_s, '--intent', intent_path])
    check("sibling run exits 0", r.returncode == 0)
    check("seed output carries the .kicad_pro sibling",
          os.path.isfile(os.path.join(d, 's.kicad_pro')))
    check("seed output carries the .kicad_dru sibling",
          os.path.isfile(os.path.join(d, 's.kicad_dru')))

print(f"\n{passed}/{passed + failed} checks passed")
sys.exit(1 if failed else 0)
