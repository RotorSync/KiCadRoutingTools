#!/usr/bin/env python3
"""#549 B-2 end-to-end: pour via placement prefers daylight around a protected
net's committed corridor, with no loss of tap success.

Board: splitflap_driver. Chain: route /OUT_A_PHASE_A with --protect-nets (so
the protection persists to the .kicad_pro -- the AUTO source), then pour GND
on B.Cu twice: AUTO seeds vs --corridor-nets none.

Asserts: the auto run announces the seeds; the count of new vias INSIDE the
protected net's corridor band is <= the none-run's; both runs tap the same
number of pads (soft preference must not cost coverage). Deliberately NEVER
asserts zero-in-corridor -- the preference is soft by contract.
"""
import os
import re
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, 'rust_router'))

BOARD = os.path.join(REPO, "kicad_files", "splitflap_driver.kicad_pcb")

passed = failed = 0


def check(name, ok, detail=""):
    global passed, failed
    passed += bool(ok)
    failed += not ok
    print(f"  {'OK  ' if ok else 'FAIL'} {name}{(' -- ' + detail) if detail else ''}")


def _run(argv, timeout=1200):
    return subprocess.run([sys.executable, '-X', 'utf8'] + argv,
                          capture_output=True, text=True, encoding='utf-8',
                          errors='replace', cwd=REPO, timeout=timeout)


def corridor_vias(board, base_board, margin=0.5):
    """New vias (not in base_board) within `margin` of the protected net's
    committed segments."""
    from kicad_parser import parse_kicad_pcb
    from plane_corridor_ghosts import _pt_seg_dist_sq
    base = parse_kicad_pcb(base_board)
    pcb = parse_kicad_pcb(board)
    old = {(round(v.x, 3), round(v.y, 3)) for v in base.vias}
    prot = {nid for nid, n in pcb.nets.items() if n.name == '/OUT_A_PHASE_A'}
    segs = [s for s in pcb.segments if s.net_id in prot]
    n = 0
    for v in pcb.vias:
        if (round(v.x, 3), round(v.y, 3)) in old or v.net_id in prot:
            continue
        thr = v.size / 2 + margin
        if any(_pt_seg_dist_sq(v.x, v.y, s.start_x, s.start_y,
                               s.end_x, s.end_y) < (thr + s.width / 2) ** 2
               for s in segs):
            n += 1
    return n


with tempfile.TemporaryDirectory() as td:
    import shutil
    staged = os.path.join(td, 'b.kicad_pcb')
    shutil.copyfile(BOARD, staged)
    routed = os.path.join(td, 'r.kicad_pcb')
    r0 = _run(['route.py', staged, routed, '--nets', '/OUT_A_PHASE_A',
               '--protect-nets', '/OUT_A_PHASE_A'])
    check("setup route + protect", r0.returncode == 0, r0.stdout[-500:])

    outs = {}
    for label, extra in (('auto', []), ('none', ['--corridor-nets', 'none'])):
        out = os.path.join(td, f'p_{label}.kicad_pcb')
        # --allow-bare-pads: this fixture deliberately pours a PARTIAL board
        # (only the protected net is routed); the run-6 A5 pour gate would
        # otherwise refuse, which is its job on a real chain.
        r = _run(['route_planes.py', routed, out, '--nets', 'GND',
                  '--plane-layers', 'B.Cu', '--allow-bare-pads'] + extra)
        outs[label] = (out, r)
        check(f"pour ({label}) completed", r.returncode == 0 and os.path.isfile(out),
              f"rc={r.returncode}\n{r.stdout[-500:]}")

    check("auto run announced the seeds",
          'Corridor seeds (#549)' in outs['auto'][1].stdout)
    check("none run stayed silent",
          'Corridor seeds (#549)' not in outs['none'][1].stdout)

    def taps(r):
        m = re.search(r'GND: (\d+)/(\d+) pads connected', r.stdout)
        return (int(m.group(1)), int(m.group(2))) if m else None

    n_auto = corridor_vias(outs['auto'][0], routed)
    n_none = corridor_vias(outs['none'][0], routed)
    check(f"in-corridor vias: auto ({n_auto}) <= none ({n_none})",
          n_auto <= n_none)

    t_auto, t_none = taps(outs['auto'][1]), taps(outs['none'][1])
    check(f"same pad coverage (auto {t_auto} vs none {t_none})",
          t_auto is not None and t_auto == t_none)

print(f"\n{passed}/{passed + failed} checks passed")
sys.exit(1 if failed else 0)
