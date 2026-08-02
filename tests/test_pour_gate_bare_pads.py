#!/usr/bin/env python3
"""Run-6 A5 pour gate: a bare pad (>=2-pad net, zero copper) at pour time has
its escape channel consumed by the tap-via carpet -- which is not rippable
copper. Measured: five bare QFN rail pads oscillated 6-9 oracle joins across
five post-pour repair attempts and never closed. route_planes now REFUSES to
pour (exit 3) while bare pads exist, unless --allow-bare-pads.
"""
import os
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (REPO, os.path.join(REPO, 'rust_router')):
    if p not in sys.path:
        sys.path.insert(0, p)

BOARD = os.path.join(REPO, "kicad_files", "splitflap_driver.kicad_pcb")

passed = failed = 0


def check(name, ok, detail=""):
    global passed, failed
    passed += bool(ok)
    failed += not ok
    print(f"  {'OK  ' if ok else 'FAIL'} {name}{(' -- ' + detail) if detail else ''}")


if not os.path.isfile(BOARD):
    print("SKIP: fixture missing")
    sys.exit(0)

# 1. unit: bare_pad_nets on the unrouted fixture finds bare nets; hardened
#    exclusions hold (no 'unconnected-*', plane net excludable).
from kicad_parser import parse_kicad_pcb
from check_connected import bare_pad_nets

pcb = parse_kicad_pcb(BOARD)
gnd = next(i for i, n in pcb.nets.items() if n.name == 'GND')
bare = bare_pad_nets(pcb, exclude_net_ids={gnd})
check("unrouted fixture has bare nets", len(bare) > 5, f"{len(bare)}")
check("excluded plane net not in the bare set", gnd not in bare)
check("no auto no-connects in the bare set",
      not any(pcb.nets[i].name.lower().startswith('unconnected-')
              for i in bare))

with tempfile.TemporaryDirectory() as d:
    src = os.path.join(d, 'b.kicad_pcb')
    import shutil
    shutil.copyfile(BOARD, src)

    # 2. gate refuses: pouring GND on the raw board (every signal net bare)
    out = os.path.join(d, 'pour.kicad_pcb')
    r = subprocess.run([sys.executable, '-X', 'utf8',
                        os.path.join(REPO, 'route_planes.py'), src, out,
                        '--nets', 'GND', '--plane-layers', 'B.Cu'],
                       capture_output=True, text=True, timeout=600)
    check("gate refuses with exit 3", r.returncode == 3,
          f"rc={r.returncode}\n{(r.stdout + r.stderr)[-500:]}")
    check("refusal names the gate and pads", 'POUR GATE' in r.stdout
          and 'BARE (unconnected) pads' in r.stdout, r.stdout[-400:])
    check("no board written on refusal", not os.path.isfile(out))

    # 3. --allow-bare-pads proceeds
    r2 = subprocess.run([sys.executable, '-X', 'utf8',
                         os.path.join(REPO, 'route_planes.py'), src, out,
                         '--nets', 'GND', '--plane-layers', 'B.Cu',
                         '--allow-bare-pads'],
                        capture_output=True, text=True, timeout=1200)
    check("--allow-bare-pads pours", r2.returncode == 0 and os.path.isfile(out),
          f"rc={r2.returncode}\n{(r2.stdout + r2.stderr)[-500:]}")
    check("deliberate note printed", 'pouring anyway (deliberate)' in r2.stdout)

print(f"\n{passed}/{passed + failed} checks passed")
sys.exit(1 if failed else 0)
