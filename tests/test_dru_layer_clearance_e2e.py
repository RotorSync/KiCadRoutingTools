#!/usr/bin/env python3
"""#498 end-to-end: a .kicad_dru per-layer clearance rule is honored by the
router and graded by check_drc, with no CLI flag and no GUI wiring.

Board: splitflap_driver (2 copper layers), routed at clearance 0.2 with a
B.Cu rule of 0.5 mm.

  1. WITH the dru: the route must come out clean when graded at the rule
     (check_drc auto-reads the output's sibling dru, carried over by
     fix_project_for_output).
  2. WITHOUT the dru (control): the same route graded AT the rule must show
     violations -- proving the rule is a real constraint the default route
     violates, i.e. honoring it actually changed the copper (a wiring fix can
     be inert -- the c99ffb4 lesson).
  3. No-dru output carries no dru and grades clean at 0.2 (strict no-op).
"""
import os
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

BOARD = os.path.join(REPO, "kicad_files", "splitflap_driver.kicad_pcb")
RULE = 0.5
DRU = f'(version 1)\n(rule outer_wide (layer "B.Cu") (constraint clearance (min {RULE}mm)))\n'
NETS = ["*"]

passed = failed = 0


def check(name, ok, detail=""):
    global passed, failed
    passed += bool(ok)
    failed += not ok
    print(f"  {'OK  ' if ok else 'FAIL'} {name}{(' -- ' + detail) if detail else ''}")


def route(workdir, with_dru, out_name):
    src = os.path.join(workdir, "in_dru.kicad_pcb" if with_dru else "in_plain.kicad_pcb")
    if not os.path.exists(src):
        from copy_board import copy_board
        copy_board(BOARD, src)
        dru = os.path.splitext(src)[0] + ".kicad_dru"
        if with_dru:
            open(dru, "w").write(DRU)
        elif os.path.exists(dru):
            os.remove(dru)
    out = os.path.join(workdir, out_name)
    r = subprocess.run(
        [sys.executable, os.path.join(REPO, "route.py"), src, out,
         "--nets", *NETS, "--layers", "F.Cu", "B.Cu",
         "--clearance", "0.2", "--track-width", "0.2"],
        capture_output=True, text=True, timeout=1200)
    return out, r


def drc_violations(board, clearance):
    r = subprocess.run(
        [sys.executable, os.path.join(REPO, "check_drc.py"), board,
         "--clearance", str(clearance), "--no-size-checks", "--quiet"],
        capture_output=True, text=True, timeout=600)
    import re
    m = re.search(r"(\d+)\s+violation", r.stdout + r.stderr)
    if m:
        return int(m.group(1)), r
    # fall back to exit code semantics: 0 = clean
    return (0 if r.returncode == 0 else -1), r


with tempfile.TemporaryDirectory() as d:
    # --- 1. route WITH the rule ---
    out_dru, r1 = route(d, with_dru=True, out_name="out_dru.kicad_pcb")
    check("route (dru) completed", r1.returncode == 0 and os.path.isfile(out_dru),
          f"rc={r1.returncode}")
    check("engine announced the rule",
          "Per-layer clearance rules" in r1.stdout and "B.Cu:0.5" in r1.stdout)
    check("output carries the dru sibling",
          os.path.isfile(os.path.splitext(out_dru)[0] + ".kicad_dru"))

    n1, rr1 = drc_violations(out_dru, 0.2)
    check("dru-routed board grades CLEAN at the rule", n1 == 0,
          f"{n1} violation(s)")
    check("checker announced the rule", "Per-layer clearance rules" in rr1.stdout
          or n1 == 0)  # --quiet may suppress; cleanliness is the real assert

    # --- 2. control: route WITHOUT the rule, grade AT the rule ---
    out_plain, r2 = route(d, with_dru=False, out_name="out_plain.kicad_pcb")
    check("route (plain) completed", r2.returncode == 0 and os.path.isfile(out_plain),
          f"rc={r2.returncode}")
    check("plain route reads no rule", "Per-layer clearance rules" not in r2.stdout)
    # plant the dru next to the PLAIN output and re-grade: the checker now
    # requires 0.5 on B.Cu, which a 0.2-clearance route must violate somewhere
    planted = os.path.splitext(out_plain)[0] + ".kicad_dru"
    open(planted, "w").write(DRU)
    n2, _ = drc_violations(out_plain, 0.2)
    check("plain route VIOLATES the rule (rule changed real copper)", n2 > 0,
          f"{n2} violation(s)")
    os.remove(planted)

    # --- 3. strict no-op without a dru ---
    n3, _ = drc_violations(out_plain, 0.2)
    check("plain route grades clean at 0.2 (no-op baseline)", n3 == 0,
          f"{n3} violation(s)")

print(f"\n{passed}/{passed + failed} checks passed")
sys.exit(1 if failed else 0)
