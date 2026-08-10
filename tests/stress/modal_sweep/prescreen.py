#!/usr/bin/env python3
"""Local dose-range prescreen: the free stage before any cloud spend.

    python3 tests/stress/modal_sweep/prescreen.py HEURISTIC_WEIGHT 1.5 2.0 2.5 3.0
    python3 tests/stress/modal_sweep/prescreen.py --env KICAD_PROXIMITY_SUM 0 1 softcap

Runs TWO cheap discriminative boards (from board_value.json's screen list,
override with --boards) locally at each candidate value of ONE knob, and
prints verdict / iterations / wall per value. Purpose: find the range where
a knob makes any sense AT ALL -- a value that blows up quality or cost on
two boards in minutes should never reach the cloud screen, and the
survivors tell you which 2-3 values deserve arms. The funnel:

  prescreen (2 boards, local, $0)  ->  screen (12 boards, ~$0.02/arm)
  ->  full corpus (~$0.7/arm)      ->  holdout (untouched sets)

Values are numbers or strings (RIPUP_ABANDON_METRIC total-pads ...); with
--env the knob is exported instead of patched into routing_defaults.
Boards replay through the same ab_replay_grade.do_board as every other
stage, so numbers are comparable up the funnel (modulo local-vs-cloud
python-version micro-divergence -- fine for range-finding). The working
tree is patched per value and restored afterwards; do not run concurrently
with other local routing work.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO / "tests" / "stress"))


def pick_boards(stress: Path, n=2):
    bv = json.loads((HERE / "board_value.json").read_text())
    costs = {s["board"]: s for s in bv.get("table", [])}
    screen = sorted((b for b in bv.get("screen", []) if b in costs),
                    key=lambda b: costs[b]["cost_h"])
    return screen[:n]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("knob")
    ap.add_argument("values", nargs="+")
    ap.add_argument("--env", action="store_true",
                    help="knob is a KICAD_* env var, not a routing_defaults constant")
    ap.add_argument("--boards", default="",
                    help="comma-separated; default: 2 cheapest screen boards")
    ap.add_argument("--stress-dir",
                    default=os.path.expanduser("~/Documents/kicad_stress_test"))
    a = ap.parse_args()

    import ab_replay_grade as abg
    import sweep_lib as sl
    stress = Path(a.stress_dir)
    boards = ([b.strip() for b in a.boards.split(",") if b.strip()]
              or pick_boards(stress))
    locs = {b: s for s, b in sl.discover_boards(
        stress, [f"set{i}" for i in range(1, 26)]) if b in set(boards)}
    missing = set(boards) - set(locs)
    if missing:
        raise SystemExit(f"boards not found in corpus: {sorted(missing)}")

    def parse(v):
        try:
            return int(v)
        except ValueError:
            try:
                return float(v)
            except ValueError:
                return v

    dirty = subprocess.run(["git", "-C", str(REPO), "diff", "--name-only",
                            "--", "py_router/routing_defaults.py"],
                           capture_output=True, text=True).stdout.strip()
    if dirty and not a.env:
        raise SystemExit("routing_defaults.py is dirty -- restore before prescreening")

    print(f"prescreen {a.knob} on {boards}")
    rows = []
    out_root = Path(os.environ.get("TMPDIR", "/tmp")) / "kicad_prescreen"
    for v in ["<default>"] + a.values:
        val = None if v == "<default>" else parse(v)
        env = dict(os.environ)
        if val is not None:
            if a.env:
                env[a.knob] = str(val)
            else:
                sl.patch_routing_defaults(REPO, {a.knob: val})
        os.environ.update(env)   # do_board children inherit via os.environ
        try:
            tag = str(val if val is not None else "default").replace("/", "_")
            out = out_root / f"{a.knob}_{tag}"
            out.mkdir(parents=True, exist_ok=True)
            verd = iters = wall = 0
            ok = True
            for b in boards:
                r = abg.do_board(stress / f"runs_{locs[b]}", out, f"pre:{tag}", b)
                if not r.get("chain_complete"):
                    ok = False
                    continue
                verd += (r.get("nets_incomplete") or 0) + (r.get("conn") or 0)
                wall += r.get("total_seconds") or 0
            rows.append((v, verd if ok else None, wall, ok))
        finally:
            if val is not None:
                if a.env:
                    os.environ.pop(a.knob, None)
                else:
                    subprocess.run(["git", "-C", str(REPO), "checkout", "--",
                                    "py_router/routing_defaults.py"], check=True)
    print(f"\n{'value':<12}{'verdict':>8}{'wall_s':>8}")
    best = min((r[1] for r in rows if r[1] is not None), default=0)
    for v, verd, wall, ok in rows:
        note = "" if ok else "  CHAIN BROKEN"
        flag = "" if verd is None or verd <= best + 3 else "  <- outside sensible range?"
        print(f"{str(v):<12}{str(verd):>8}{wall:>8.0f}{note}{flag}")
    print("\nPick 2-3 values from the sensible range for the cloud screen.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
