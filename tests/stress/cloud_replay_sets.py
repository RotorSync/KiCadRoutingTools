#!/usr/bin/env python3
"""Replay whole stress-test SETS in the cloud, keep the routed boards, and A/B
them against an existing baseline wave.

    python3 tests/stress/cloud_replay_sets.py --sets set10-set19

`ab_replay_grade.py` replays a set locally and grades it; `modal_sweep/` fans
(arm x board) tasks onto rented cores. This driver is the missing third thing:
a NO-PARAMETER-CHANGE replay of the current code across many sets, which keeps
the finished `.kicad_pcb`/`.kicad_pro` and then compares to a baseline. One arm,
no overrides -- the question is not "what does this knob do" but "what does the
CURRENT engine do to these 150 boards, versus the last time we looked".

Five stages, run in order by default; pick with --only:

  plan      discover boards, price the run, check preflight. SPENDS NOTHING.
  upload    push any sets the corpus volume is missing (idempotent)
  run       the cloud replay, artifact-keeping ON
  harvest   pull rows + boards into a local wave dir shaped like an
            ab_replay_grade wave, so --compare AND --regrade both work on it
  compare   per-set + aggregate A/B against the baseline wave

Two things this is careful about, both of which have burned this repo before:

1. **The kept board travels with its `.kicad_pro`.** The project carries the DRC
   floor the chain actually routed to. A bare `.kicad_pcb` re-grades against the
   stock netclass and manufactures phantom sub-floor violations on correct copper
   (#441). The container-side copy in modal_app.py takes every sibling
   (.kicad_pro/.kicad_dru/.kicad_prl) plus the manifest.

2. **A published baseline summary is not automatically a valid code datum.**
   Graders drift, and some archived waves were produced by dirty trees. This
   prints the baseline's provenance (commit + dirty flag) next to every
   comparison, refuses a dirty baseline unless you pass --allow-dirty-baseline,
   and offers --regrade-baseline to re-score the baseline's OWN kept boards with
   TODAY's grader -- which is the only way to attribute a delta to code rather
   than to grading. It snapshots the archive's summary.json first and never
   discards it.

The arm name carries the source commit, so resuming re-uses banked rows only
within one commit; changing the code forces a real re-run instead of silently
mixing two engines into one table.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
SWEEP = HERE / "modal_sweep"
sys.path.insert(0, str(SWEEP))
sys.path.insert(0, str(HERE))

DEFAULT_STRESS = Path(os.environ.get("STRESS_DIR",
                                     os.path.expanduser("~/Documents/kicad_stress_test")))
RESULTS_VOLUME = "kicad-sweep-results"
CORPUS_VOLUME = "kicad-corpus"
# Modal's published core-second price; used only to print an estimate.
USD_PER_CORE_SEC = 0.0000131


# ----------------------------------------------------------------- utilities

def sh(cmd, **kw):
    """Run a command, streaming its output. Returns the CompletedProcess."""
    print(f"  $ {' '.join(str(c) for c in cmd)}", flush=True)
    return subprocess.run([str(c) for c in cmd], **kw)


def expand_sets(spec: str) -> list:
    """'set10-set19' / 'set10,set12' / 'set10-set12,set19' -> [set10, ...]."""
    out = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            ai = int(a.strip().removeprefix("set"))
            bi = int(b.strip().removeprefix("set"))
            if bi < ai:
                raise SystemExit(f"bad range {part!r}: {b} precedes {a}")
            out += [f"set{i}" for i in range(ai, bi + 1)]
        else:
            out.append(part)
    seen, uniq = set(), []
    for s in out:                       # order-preserving dedupe
        if s not in seen:
            seen.add(s)
            uniq.append(s)
    return uniq


def git_sha(short=True) -> str:
    try:
        r = subprocess.run(["git", "-C", str(REPO), "rev-parse",
                            "--short" if short else "HEAD", "HEAD"],
                           capture_output=True, text=True)
        sha = r.stdout.strip().split("\n")[0]
    except Exception:
        return "unknown"
    return sha or "unknown"


def tree_dirty() -> list:
    """Modified TRACKED files. Untracked (`??`) does not make a run
    unreproducible, and treating it as dirty cries wolf -- a stress worker
    leaving logs in the repo root flagged a clean baseline once."""
    r = subprocess.run(["git", "-C", str(REPO), "status", "--porcelain"],
                       capture_output=True, text=True)
    return [ln for ln in r.stdout.splitlines() if ln and not ln.startswith("??")]


def wave_provenance(summary_path: Path) -> dict:
    """The git_version.json sitting beside (or one level above) a summary."""
    for cand in (summary_path.parent / "git_version.json",
                 summary_path.parent.parent / "git_version.json"):
        if cand.exists():
            try:
                return json.loads(cand.read_text())
            except Exception:
                pass
    return {}


# --------------------------------------------------------------------- plan

def discover(stress: Path, sets: list):
    import sweep_lib as sl
    boards = sl.discover_boards(stress, sets)
    costs = sl.load_cost_table(stress)
    return boards, costs


def stage_plan(args, sets, stress) -> dict:
    import sweep_lib as sl
    boards, costs = discover(stress, sets)
    if not boards:
        raise SystemExit(f"no boards found for {sets} under {stress}")

    per_set = {}
    for s, b in boards:
        per_set.setdefault(s, []).append(b)

    # Price it. LLM-sourced timings measure a whole agent session (exploration
    # and retries included) and badly overestimate a replay, so they are shown
    # separately rather than folded into one confident number.
    replay_s = llm_s = 0.0
    llm_boards, big_boards, longest = [], [], (0.0, "")
    for s, b in boards:
        e = costs.get(b, {})
        sec = e.get("seconds", 0.0) or 0.0
        if e.get("src") == "replay":
            replay_s += sec
            if sec > longest[0]:
                longest = (sec, b)
        else:
            llm_s += sec
            llm_boards.append(b)
        if (e.get("peak_mb") or 0) > 3500:
            big_boards.append(b)

    est_s = replay_s + 0.68 * llm_s      # README's measured replay:LLM ratio
    print(f"\n=== PLAN: {len(boards)} boards across {len(sets)} set(s) ===")
    for s in sets:
        print(f"  {s:9} {len(per_set.get(s, [])):3} boards")
    print(f"\n  estimated CPU time   {est_s/3600:6.1f} CPU-hours "
          f"({replay_s/3600:.1f} measured + {llm_s/3600:.1f} LLM-priced x0.68)")
    print(f"  estimated CPU cost   ${est_s * USD_PER_CORE_SEC:6.2f}  "
          f"(cores are cheap here; concurrency quota is the real limit)")
    print(f"  wall-clock floor     {longest[0]/60:6.1f} min  ({longest[1]}) "
          f"-- the longest single board, measured")
    if llm_boards:
        print(f"  NOTE {len(llm_boards)} board(s) priced from LLM sessions, which "
              f"overestimate a replay: {', '.join(sorted(llm_boards)[:6])}"
              f"{' ...' if len(llm_boards) > 6 else ''}")
    if big_boards:
        print(f"  big memory tier      {len(big_boards)}: {', '.join(sorted(set(big_boards)))}")

    # Preflight -- every failure here is cheaper to hit now than after upload.
    problems, warnings = [], []
    for s in sets:
        if not sl.run_dirs_for(stress, s):
            problems.append(f"{s}: no runs dir under {stress}")
    if not args.no_baseline:
        for s in sets:
            bp = baseline_summary(args, s)
            if not bp.exists():
                problems.append(f"{s}: baseline summary missing ({bp})")
            else:
                prov = wave_provenance(bp)
                if prov.get("dirty"):
                    msg = (f"{s}: baseline {prov.get('commit','?')[:9]} was built from a "
                           f"DIRTY tree -- its numbers are not reproducible from git")
                    (problems if not args.allow_dirty_baseline else warnings).append(msg)
    dirty = tree_dirty()
    if dirty:
        warnings.append(f"working tree has {len(dirty)} modified tracked file(s); "
                        f"the cloud image ships a CLEAN HEAD unless KICAD_SWEEP_DIRTY=1, "
                        f"so the run may not test what you have locally")
    for w in warnings:
        print(f"  WARN  {w}")
    for p in problems:
        print(f"  ERROR {p}")
    if problems:
        raise SystemExit("preflight failed; nothing was spent")

    return {"boards": boards, "per_set": per_set, "est_seconds": est_s,
            "big_boards": sorted(set(big_boards))}


def baseline_summary(args, set_name: str) -> Path:
    return Path(args.baseline).expanduser() / set_name / "summary.json"


# ------------------------------------------------------------------- upload

def corpus_has(sets: list) -> set:
    """Set names already present on the corpus volume."""
    r = subprocess.run(["modal", "volume", "ls", CORPUS_VOLUME, "/"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(f"  (could not list {CORPUS_VOLUME}: {r.stderr.strip()[:200]}); "
              f"assuming nothing is uploaded")
        return set()
    listing = r.stdout
    have = set()
    for s in sets:
        # A set is present when its RUN dir is -- boards_* alone is not enough
        # to replay. Run dirs may carry a recording suffix (runs_set3_llm0801).
        if f"runs_{s}" in listing:
            have.add(s)
    return have


def stage_upload(args, sets, stress):
    have = corpus_has(sets)
    missing = [s for s in sets if s not in have]
    print(f"\n=== UPLOAD ===\n  corpus already has: "
          f"{', '.join(sorted(have)) or '(none)'}")
    if not missing:
        print("  nothing to upload")
        return
    print(f"  uploading: {', '.join(missing)}")
    cmd = [sys.executable, str(SWEEP / "upload_corpus.py"),
           "--sets", ",".join(missing), "--stress-dir", str(stress)]
    if args.dry_run:
        cmd.append("--dry-run")
    r = sh(cmd)
    if r.returncode != 0:
        raise SystemExit(f"upload failed (rc={r.returncode})")


# ---------------------------------------------------------------------- run

def arm_name(args) -> str:
    """Arm identity = label + source commit.

    The results volume is persistent and modal_app RESUMES by skipping tasks
    that already have a row. Without the commit in the name, a second run after
    a code change would silently re-use the first run's rows and report two
    engines as one -- the exact failure the sweep README calls out ("the
    baseline was not the baseline").
    """
    return f"{args.label}_{git_sha()}"


def stage_run(args, sets, stress, plan):
    arm = arm_name(args)
    arms_file = Path(args.workdir) / "arms.replay.json"
    arms_file.parent.mkdir(parents=True, exist_ok=True)
    # One arm, no overrides: a straight replay of whatever the image ships.
    # keep_artifacts rides the ARM SPEC because that is what gets serialized into
    # the task -- a Modal container does not inherit this shell's environment, so
    # exporting the env var here would keep nothing (the smoke run proved it:
    # chain green, zero artifacts).
    arms_file.write_text(json.dumps(
        [{"name": arm, "keep_artifacts": True,
          "note": "cloud replay, no parameter overrides"}], indent=1))

    env = dict(os.environ)
    env.setdefault("KICAD_SWEEP_NAME", f"kicad-replay-{sets[0]}-{sets[-1]}")
    if plan["big_boards"]:
        env["KICAD_SWEEP_BIG_BOARDS"] = ",".join(plan["big_boards"])

    print(f"\n=== RUN ===\n  arm: {arm}\n  artifacts: ON "
          f"(final .kicad_pcb + siblings kept per board)")
    # --ignore-excludes: board_value.json's exclude list is FITTED to the arm
    # family it was measured on -- boards where competing soft-cost arms all tied
    # carry no information FOR A SWEEP. A replay is not a sweep: every board is a
    # datum, and the baseline wave graded all of them, so dropping any would
    # silently shrink the comparison (the smoke run showed it culling 5 of
    # set10's 15 before we asked for one board).
    cmd = ["modal", "run", str(SWEEP / "modal_app.py"),
           "--arms", str(arms_file), "--sets", ",".join(sets),
           "--stress-dir", str(stress), "--ignore-excludes"]
    if args.limit:
        cmd += ["--limit", str(args.limit)]
    if args.boards:
        cmd += ["--boards", args.boards]
    r = sh(cmd, env=env)
    if r.returncode != 0:
        raise SystemExit(f"cloud run failed (rc={r.returncode}); banked rows are "
                         f"still on the volume -- re-run to resume")


# ------------------------------------------------------------------ harvest

def stage_harvest(args, sets, stress) -> dict:
    """Pull this arm's rows and boards into a local ab_replay_grade-shaped wave.

    Layout (per set), which is exactly what --compare and --regrade expect:
        <out>/<set>/summary.json
        <out>/<set>/<board>/<final>.kicad_pcb   (+ .kicad_pro/.kicad_dru/...)
        <out>/<set>/<board>/redo_commands.sh
    """
    arm = arm_name(args)
    out = Path(args.out).expanduser()
    raw = out / "_raw"
    if raw.exists():
        shutil.rmtree(raw)
    raw.mkdir(parents=True, exist_ok=True)

    print(f"\n=== HARVEST ===\n  arm {arm} -> {out}")
    r = sh(["modal", "volume", "get", "--force", RESULTS_VOLUME, f"/{arm}", str(raw)])
    if r.returncode != 0:
        raise SystemExit(f"volume get failed (rc={r.returncode}) -- is the arm name right? "
                         f"`modal volume ls {RESULTS_VOLUME} /`")

    # `modal volume get` may nest under the remote dir name; find the rows.
    roots = [p for p in [raw, raw / arm] if p.exists()]
    rows_by_set, artifacts = {}, 0
    for root in roots:
        for jf in sorted(root.glob("*.json")):
            try:
                row = json.loads(jf.read_text())
            except Exception as e:
                print(f"  WARN unreadable row {jf.name}: {e}")
                continue
            row = row[0] if isinstance(row, list) else row
            s = row.get("set") or jf.name.split("__")[0]
            rows_by_set.setdefault(s, []).append(row)
        adir = root / "artifacts"
        if adir.is_dir():
            for bdir in sorted(p for p in adir.iterdir() if p.is_dir()):
                if "__" not in bdir.name:
                    continue
                s, board = bdir.name.split("__", 1)
                dest = out / s / board
                dest.mkdir(parents=True, exist_ok=True)
                for f in sorted(bdir.iterdir()):
                    if f.is_file():
                        shutil.copy2(f, dest / f.name)
                artifacts += 1

    if not rows_by_set:
        raise SystemExit(f"no result rows harvested for arm {arm}")

    total = 0
    for s, rows in sorted(rows_by_set.items()):
        rows.sort(key=lambda r: r.get("board", ""))
        sdir = out / s
        sdir.mkdir(parents=True, exist_ok=True)
        (sdir / "summary.json").write_text(json.dumps(rows, indent=2))
        # Provenance beside the summary, so compare() can print it and a later
        # reader can tell which engine produced these numbers.
        prov = {"commit": git_sha(short=False), "describe": git_sha(),
                "dirty": bool(tree_dirty()), "label": args.label,
                "arm": arm, "captured": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "source": "cloud_replay_sets.py"}
        (sdir / "git_version.json").write_text(json.dumps(prov, indent=1))
        total += len(rows)
        complete = sum(1 for r in rows if r.get("chain_complete"))
        with_art = sum(1 for r in rows if r.get("artifacts"))
        print(f"  {s:9} {len(rows):3} rows, {complete:3} chain-complete, "
              f"{with_art:3} with kept boards")

    print(f"  harvested {total} row(s), {artifacts} board artifact dir(s)")
    missing_pro = [f"{r.get('set')}/{r.get('board')}"
                   for rows in rows_by_set.values() for r in rows
                   if r.get("artifact_warning")]
    if missing_pro:
        print(f"  WARN {len(missing_pro)} board(s) flagged an artifact problem "
              f"(a .kicad_pcb without its .kicad_pro re-grades against the stock "
              f"netclass): {', '.join(missing_pro[:5])}")
    return rows_by_set


# ------------------------------------------------------------------ compare

def regrade_baseline(args, sets, stress):
    """Re-score the baseline's OWN boards with today's grader.

    Comparing a fresh run against a months-old summary conflates a code delta
    with grader drift. The baseline kept its boards, so it can simply be
    re-scored. The archive's original summary is snapshotted first and never
    thrown away.
    """
    import sweep_lib as sl
    print("\n=== REGRADE BASELINE (today's grader) ===")
    for s in sets:
        wave = Path(args.baseline).expanduser() / s
        summ = wave / "summary.json"
        if not summ.exists():
            print(f"  {s}: no baseline summary; skipped")
            continue
        snap = wave / "summary.pre_regrade.json"
        if not snap.exists():
            shutil.copy2(summ, snap)
            print(f"  {s}: snapshotted original -> {snap.name}")
        run_dirs = sl.run_dirs_for(stress, s)
        if not run_dirs:
            print(f"  {s}: no runs dir; cannot regrade (needs manifests)")
            continue
        sh([sys.executable, str(HERE / "ab_replay_grade.py"),
            "--regrade", str(wave), "--set", str(run_dirs[0])])


def stage_compare(args, sets, stress):
    out = Path(args.out).expanduser()
    print("\n=== COMPARE vs baseline ===")
    print(f"  baseline: {args.baseline}")
    if args.regrade_baseline:
        regrade_baseline(args, sets, stress)

    agg = {"sets": {}, "baseline": str(args.baseline), "new": str(out)}
    for s in sets:
        old = baseline_summary(args, s)
        new = out / s / "summary.json"
        if not new.exists():
            print(f"\n-- {s}: no new summary (not harvested?); skipped")
            continue
        if not old.exists():
            print(f"\n-- {s}: no baseline summary; skipped")
            continue
        prov = wave_provenance(old)
        print(f"\n{'='*100}\n-- {s}   baseline {prov.get('describe') or prov.get('commit','?')[:9]}"
              f"{' DIRTY' if prov.get('dirty') else ''}   "
              f"{'(re-graded today)' if args.regrade_baseline else '(as archived)'}")
        r = sh([sys.executable, str(HERE / "ab_replay_grade.py"),
                "--compare", str(old), str(new)])
        agg["sets"][s] = {"baseline_summary": str(old), "new_summary": str(new),
                          "compare_rc": r.returncode}

    # A roll-up across sets, computed from the same fields compare() grades on,
    # so a per-set win/loss does not hide in ten separate tables.
    agg["totals"] = aggregate_totals(args, sets, out)
    (out / "aggregate.json").write_text(json.dumps(agg, indent=2))
    print_aggregate(agg["totals"])
    print(f"\nwrote {out/'aggregate.json'}")


def _incompl(r):
    ni = r.get("nets_incomplete")
    return ni if ni is not None else r.get("conn")


def _drc(r):
    v = r.get("drc_real")
    return v if v is not None else r.get("drc")


def aggregate_totals(args, sets, out) -> dict:
    """Sum the graded axes over boards COMPLETE IN BOTH waves.

    Restricting to boards complete in both is what ab_replay_grade's own compare
    does: a board that fails to produce a final in one wave has no comparable
    number, and letting it contribute zero would read as an improvement.
    """
    tot = {"boards_compared": 0, "drc_old": 0, "drc_new": 0,
           "incompl_old": 0, "incompl_new": 0,
           "baseline_only_complete": [], "new_only_complete": []}
    for s in sets:
        op, np_ = baseline_summary(args, s), out / s / "summary.json"
        if not (op.exists() and np_.exists()):
            continue
        old = {r["board"]: r for r in json.loads(op.read_text())}
        new = {r["board"]: r for r in json.loads(np_.read_text())}
        for b in sorted(set(old) | set(new)):
            o, n = old.get(b), new.get(b)
            oc = bool(o and o.get("chain_complete"))
            nc = bool(n and n.get("chain_complete"))
            if oc and not nc:
                tot["baseline_only_complete"].append(f"{s}/{b}")
            elif nc and not oc:
                tot["new_only_complete"].append(f"{s}/{b}")
            if not (oc and nc):
                continue
            do, dn = _drc(o), _drc(n)
            io, inw = _incompl(o), _incompl(n)
            if None in (do, dn, io, inw):
                continue
            tot["boards_compared"] += 1
            tot["drc_old"] += do
            tot["drc_new"] += dn
            tot["incompl_old"] += io
            tot["incompl_new"] += inw
    return tot


def print_aggregate(t: dict):
    print(f"\n{'='*100}\n=== AGGREGATE over all sets ===")
    n = t["boards_compared"]
    if not n:
        print("  no boards were complete in BOTH waves -- nothing comparable")
        return
    dd = t["drc_new"] - t["drc_old"]
    di = t["incompl_new"] - t["incompl_old"]

    def arrow(d):
        return "BETTER" if d < 0 else ("worse" if d > 0 else "same")
    print(f"  boards compared (complete in both): {n}")
    print(f"  real DRC          {t['drc_old']:6} -> {t['drc_new']:6}   "
          f"{dd:+6}  {arrow(dd)}")
    print(f"  incomplete nets   {t['incompl_old']:6} -> {t['incompl_new']:6}   "
          f"{di:+6}  {arrow(di)}")
    if t["baseline_only_complete"]:
        print(f"  REGRESSED to broken chain ({len(t['baseline_only_complete'])}): "
              f"{', '.join(t['baseline_only_complete'][:8])}")
    if t["new_only_complete"]:
        print(f"  newly completing ({len(t['new_only_complete'])}): "
              f"{', '.join(t['new_only_complete'][:8])}")


# --------------------------------------------------------------------- main

STAGES = ["plan", "upload", "run", "harvest", "compare"]


def main():
    ap = argparse.ArgumentParser(
        description="Cloud-replay stress sets, keep the boards, A/B vs a baseline.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="example:\n"
               "  %(prog)s --sets set10-set19\n"
               "  %(prog)s --sets set10-set19 --only plan\n"
               "  %(prog)s --sets set10 --boards bus_pirate5 --label smoke\n")
    ap.add_argument("--sets", default="set10-set19",
                    help="ranges and/or names: set10-set19, set10,set12 (default: %(default)s)")
    ap.add_argument("--out", default="", help="local wave dir (default: <stress>/cloud_<label>_<sha>)")
    ap.add_argument("--baseline", default=str(DEFAULT_STRESS / "ab_main_0728a"),
                    help="baseline wave dir holding <set>/summary.json (default: %(default)s)")
    ap.add_argument("--label", default="replay", help="run label, part of the arm name")
    ap.add_argument("--stress-dir", default=str(DEFAULT_STRESS))
    ap.add_argument("--workdir", default="", help="scratch for the generated arms file")
    ap.add_argument("--only", default="", help=f"run one/some stages: {'|'.join(STAGES)} (comma-separated)")
    ap.add_argument("--dry-run", action="store_true",
                    help="plan only, and make `upload` report sizes without pushing")
    ap.add_argument("--limit", type=int, default=0, help="keep only the N cheapest boards (smoke)")
    ap.add_argument("--boards", default="", help="restrict to these board names (smoke)")
    ap.add_argument("--regrade-baseline", action="store_true",
                    help="re-score the baseline's own boards with TODAY's grader before "
                         "comparing (snapshots the archive's summary.json first). Use this "
                         "whenever you intend to attribute a delta to CODE.")
    ap.add_argument("--allow-dirty-baseline", action="store_true",
                    help="proceed even if the baseline wave was built from a dirty tree")
    ap.add_argument("--no-baseline", action="store_true",
                    help="skip baseline checks and the compare stage entirely")
    args = ap.parse_args()

    sets = expand_sets(args.sets)
    stress = Path(args.stress_dir).expanduser()
    if not stress.is_dir():
        raise SystemExit(f"stress dir not found: {stress}")
    if not args.out:
        args.out = str(stress / f"cloud_{args.label}_{git_sha()}")
    if not args.workdir:
        args.workdir = args.out
    stages = [s.strip() for s in args.only.split(",") if s.strip()] or list(STAGES)
    for s in stages:
        if s not in STAGES:
            raise SystemExit(f"unknown stage {s!r}; pick from {STAGES}")
    if args.no_baseline and "compare" in stages and not args.only:
        stages = [s for s in stages if s != "compare"]

    print(f"sets      : {', '.join(sets)}")
    print(f"stress    : {stress}")
    print(f"out       : {args.out}")
    print(f"stages    : {', '.join(stages)}")

    plan = stage_plan(args, sets, stress) if "plan" in stages else {"big_boards": []}
    if args.dry_run:
        print("\n--dry-run: stopping after plan; nothing was spent")
        if "upload" in stages:
            stage_upload(args, sets, stress)
        return 0
    if "upload" in stages:
        stage_upload(args, sets, stress)
    if "run" in stages:
        stage_run(args, sets, stress, plan)
    if "harvest" in stages:
        stage_harvest(args, sets, stress)
    if "compare" in stages and not args.no_baseline:
        stage_compare(args, sets, stress)
    return 0


if __name__ == "__main__":
    sys.exit(main())
