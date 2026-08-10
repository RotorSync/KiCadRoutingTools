#!/usr/bin/env python3
"""Modal app: fan a routing-parameter sweep across the stress corpus.

    modal run tests/stress/modal_sweep/modal_app.py \
        --arms tests/stress/modal_sweep/arms.example.json \
        --sets set1,set2,set3,set4,set5,set6,set7,set8,set9,set10

ONE TASK = ONE (arm, board). That granularity is the whole point: an arm run as
a single task is ~9 CPU-hours SERIAL, so 100 of those in parallel still take
9 hours. Fanning out to board level makes the wall-clock floor the single
longest BOARD (tens of minutes) instead.

Measured on sets 1-10 (see sweep_lib.load_cost_table for provenance):
  one arm ~8.6 CPU-h  ->  100 arms ~864 CPU-h / 15,000 tasks
  500 concurrent cores ~1.7 h   |   ~1,730 cores reaches the floor

Two container tiers, assigned from recorded peak_footprint_mb: 4 GB standard,
12 GB for the handful of boards that spike (one hit 10 GB). Sizing off
peak_rss_mb instead would under-provision those by up to 5x -- RSS
under-reports (issue #419).

Prerequisites and known-unverified bits are in README.md. Notably this file has
NOT been executed against Modal's API from this repo -- pin `modal` per the
README and expect to adjust image-builder method names if you are on an older
client.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import modal

# Recorded manifests bake ABSOLUTE tool paths. Rather than assume one prefix, we
# detect each manifest's own and remap it (sweep_lib.recorded_repo_prefix), so
# manifests recorded from a worktree or another machine replay unchanged.
REPO = "/opt/kicad-routing-tools"
CORPUS = "/corpus"          # read-only volume: runs_setN/ manifests + boards
RESULTS = "/results"        # writable volume: one JSON per (arm, board)

app = modal.App("kicad-routing-sweep")

corpus_vol = modal.Volume.from_name("kicad-corpus", create_if_missing=True)
results_vol = modal.Volume.from_name("kicad-sweep-results", create_if_missing=True)

_here = Path(__file__).resolve().parent
_repo_root = _here.parent.parent.parent


def _image_source() -> tuple:
    """(dir_to_ship, provenance) -- a CLEAN checkout of HEAD by default.

    Shipping the working tree makes the baseline arm whatever happens to be
    uncommitted locally. That is not hypothetical: the first green smoke run had
    `baseline` and `tp2` produce byte-identical results (8.05s, 100%, 0 DRC)
    because another session's uncommitted TRACK_PROXIMITY_COST=2.0 was baked in
    as "pristine", so both arms ran at 2.0. A sweep would have compared 100 arms
    against an unknown baseline and reported "no effect".

    `git archive HEAD` pins the image to a commit, which also means a sweep is
    reproducible and you can keep editing locally while it runs.

    KICAD_SWEEP_DIRTY=1 ships the working tree instead -- for deliberately
    sweeping an uncommitted engine change. It warns, and the commit recorded in
    every result row is suffixed `+dirty` so the provenance is never silent.
    """
    import subprocess as _sp
    import tempfile

    def _git(*a):
        return _sp.run(["git", "-C", str(_repo_root), *a],
                       capture_output=True, text=True).stdout.strip()

    sha = _git("rev-parse", "--short", "HEAD") or "unknown"
    if os.environ.get("KICAD_SWEEP_DIRTY") == "1":
        dirty = _git("status", "--porcelain")
        print(f"!! KICAD_SWEEP_DIRTY=1: shipping the WORKING TREE, not {sha}."
              f" Uncommitted files:\n{dirty or '   (none -- tree is clean)'}")
        return str(_repo_root), f"{sha}+dirty"
    tmp = Path(tempfile.mkdtemp(prefix="kicad-sweep-src-"))
    rc = _sp.run(["git", "-C", str(_repo_root), "archive", "--format=tar", "HEAD",
                  "-o", str(tmp / "src.tar")]).returncode
    if rc != 0:
        raise SystemExit("git archive HEAD failed -- cannot build a reproducible image")
    _sp.run(["tar", "-xf", str(tmp / "src.tar"), "-C", str(tmp)], check=True)
    (tmp / "src.tar").unlink()
    print(f"image source: clean checkout of {sha}")
    return str(tmp), sha


_src_dir, GIT_SHA = _image_source()

image = (
    modal.Image.debian_slim(python_version="3.12")
    # numpy/scipy/shapely are the ENTIRE runtime dependency set: check_drc.py and
    # the router are pure python + the compiled grid_router. No KiCad needed --
    # replay+grade never calls pcbnew or kicad-cli. (Cost: ab_replay_grade's
    # optional kicad-cli cross-check degrades to None. Cross-check the winning
    # arm locally, where KiCad already lives -- that check caught #487.)
    .pip_install("numpy>=1.21.0", "scipy>=1.7.0", "shapely>=1.8.0")
    .apt_install("curl")
    .add_local_dir(_src_dir, REPO, copy=True, ignore=[
        "**/.git/**", "**/__pycache__/**", "**/target/**", "**/.claude/worktrees/**",
    ])
    .run_commands(
        # NO Rust toolchain: the v0.20.2 release publishes grid_router-linux-x86_64.so
        # built from crate 0.20.1, so build_router.py downloads the prebuilt and keeps
        # it (verified: the released macos-arm64 asset reports __version__ 0.20.1,
        # matching Cargo.toml). That took a rustup + build-essential layer and a ~10 min
        # cargo build off the cold image.
        #
        # This FAILS LOUDLY at image-build time if a future crate bump ships without
        # publishing binaries -- which is the behaviour you want. To unblock, either
        # publish the assets (a python-only release republishes current crate binaries;
        # see CLAUDE.md) or re-add:
        #   .apt_install("build-essential")
        #   .run_commands("curl -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal")
        # and append `|| (. $HOME/.cargo/env && python3 build_router.py --from-source)`.
        f"cd {REPO} && python3 build_router.py",
        # Pristine copy for per-task restore: containers are REUSED, so without
        # this a previous arm's routing_defaults patch leaks into the next task.
        f"cp {REPO}/py_router/routing_defaults.py {REPO}/py_router/routing_defaults.py.orig",
        # Prove the extension actually imports in a FRESH interpreter before 15,000
        # tasks depend on it. build_router verifies in a subprocess for the same
        # reason: an in-process re-import of a compiled extension reports the
        # PREVIOUSLY loaded library, so it can mask a bad install.
        f"cd {REPO} && python3 -c \"import sys; sys.path.insert(0,'rust_router'); "
        f"import grid_router; print('grid_router', grid_router.__version__)\"",
    )
)

with image.imports():
    sys.path.insert(0, f"{REPO}/tests/stress/modal_sweep")
    import sweep_lib


def _replay_one(task: dict) -> dict:
    """Replay + grade ONE board under ONE arm. Runs inside the container."""
    t0 = time.time()
    arm, board, set_name = task["arm"], task["board"], task["set"]

    # 0. Restore a pristine routing_defaults.py. Modal REUSES containers across
    #    tasks and the image's repo copy is a writable overlay, so a previous
    #    arm's patch would otherwise persist -- a `baseline` task landing in a
    #    container that already ran `tp2` would silently route with tp2's costs
    #    and the two arms would look identical. Failing loudly if the pristine
    #    copy is missing beats sweeping with contaminated defaults.
    pristine = Path(REPO) / "py_router" / "routing_defaults.py.orig"
    if not pristine.exists():
        raise RuntimeError("routing_defaults.py.orig missing -- image built without it; "
                           "arms would contaminate each other in a reused container")
    shutil.copy(pristine, Path(REPO) / "py_router" / "routing_defaults.py")

    # 1. Stage the board at its RECORDED absolute path. Manifests bake the input
    #    board absolutely (<stress>/boards_unrouted_<set>/<board>.kicad_pcb) while
    #    intermediates are relative, and ab_replay_grade's own --remap src:dst
    #    assumes --set IS the recorded run dir. Staging anywhere else breaks both:
    #    the first tool exits rc=1 on a missing input, which is what the smoke test
    #    caught. Reconstructing the recorded path makes every baked path resolve
    #    natively, so only the REPO prefix ever needs remapping.
    manifest_src = Path(CORPUS) / f"runs_{set_name}" / board / "redo_commands.sh"
    recorded_stress = sweep_lib.recorded_corpus_prefix(manifest_src.read_text())
    if not recorded_stress:
        raise RuntimeError(f"{board}: no '# cwd=' line -- cannot place the corpus")
    root = Path(recorded_stress)
    set_dir = root / f"runs_{set_name}"
    set_dir.mkdir(parents=True, exist_ok=True)
    dst = set_dir / board
    if dst.exists():
        shutil.rmtree(dst)            # reused container: never inherit a prior wave
    shutil.copytree(Path(CORPUS) / f"runs_{set_name}" / board, dst)
    # Board dirs are read-only inputs -> symlink rather than copy.
    for d in (f"boards_unrouted_{set_name}", f"boards_{set_name}"):
        srcd, lnk = Path(CORPUS) / d, root / d
        if srcd.exists() and not lnk.exists():
            lnk.symlink_to(srcd)
    # set_dir holds exactly this ONE board, so ab_replay_grade replays only it --
    # and emits the SAME summary.json schema as a full-set wave, so `--compare`
    # still works on the aggregate and no grading logic is duplicated here.
    stage = set_dir
    work = Path(f"/tmp/{arm}__{set_name}__{board}")

    # 2. Apply this arm's parameters.
    spec = task["arm_spec"]
    env = dict(os.environ)
    env.update({k: str(v) for k, v in (spec.get("env") or {}).items()})
    patched = {}
    if spec.get("defaults"):
        patched = sweep_lib.patch_routing_defaults(Path(REPO), spec["defaults"])
    manifest_path = dst / "redo_commands.sh"
    text = manifest_path.read_text()
    if spec.get("manifest_sed"):
        text = sweep_lib.patch_manifest(text, spec["manifest_sed"])
        manifest_path.write_text(text)

    # 3. Remap the recorded repo prefix onto this container's repo.
    extra = []
    prefix = sweep_lib.recorded_repo_prefix(text)
    if prefix and prefix.rstrip("/") != REPO:
        extra = ["--extra-remap", f"{prefix.rstrip('/')}/:{REPO}/"]

    out_dir = work / "wave"
    cmd = [sys.executable, f"{REPO}/tests/stress/ab_replay_grade.py",
           "--set", str(stage), "--out", str(out_dir),
           "--label", arm, "--jobs", "1"] + extra
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env,
                          timeout=task.get("timeout_s", 4 * 3600))

    # 4. Harvest the single summary row.
    row, summary_file = {}, out_dir / "summary.json"
    if summary_file.exists():
        try:
            rows = json.loads(summary_file.read_text())
            if rows:
                row = rows[0]
        except Exception as e:
            row = {"parse_error": str(e)[:200]}
    row.update({
        "arm": arm, "set": set_name, "board": board, "git": GIT_SHA,
        "rc": proc.returncode,
        "sweep_wall_s": round(time.time() - t0, 1),
        "patched_defaults": {k: v[1] for k, v in patched.items()},
        "env_overrides": spec.get("env") or {},
    })
    if not summary_file.exists():
        # A board that produced no summary at all is a real finding, not noise --
        # keep enough context to reproduce it rather than dropping the row.
        row["stderr_tail"] = (proc.stderr or "")[-2000:]
        row["chain_complete"] = False
    if not row.get("chain_complete"):
        # A summary can exist and STILL report a broken chain (a tool exited
        # non-zero). The engine's error lives in the per-board _replay.log inside
        # the ephemeral container, so carry its tail out -- without it the row
        # says only "chain_complete: false" and the failure is undiagnosable,
        # which is exactly where the first smoke run left us.
        log = out_dir / board / "_replay.log"
        if log.exists():
            row["replay_log_tail"] = log.read_text(errors="replace")[-3000:]

    dest = Path(RESULTS) / arm
    dest.mkdir(parents=True, exist_ok=True)
    (dest / f"{set_name}__{board}.json").write_text(json.dumps(row, indent=1))
    results_vol.commit()
    return row


@app.function(image=image, cpu=1.0, memory=4096,
              timeout=4 * 3600, retries=modal.Retries(max_retries=1),
              volumes={CORPUS: corpus_vol, RESULTS: results_vol})
def replay_standard(task: dict) -> dict:
    return _replay_one(task)


@app.function(image=image, cpu=1.0, memory=12288,
              timeout=6 * 3600, retries=modal.Retries(max_retries=1),
              volumes={CORPUS: corpus_vol, RESULTS: results_vol})
def replay_big(task: dict) -> dict:
    """Same work, 12 GB. For boards whose recorded peak footprint exceeds
    ~3.5 GB -- four boards in the reference wave, one at 10 GB."""
    return _replay_one(task)


@app.local_entrypoint()
def main(arms: str, sets: str = "set1,set2,set3,set4,set5,set6,set7,set8,set9,set10",
         stress_dir: str = "", out: str = "", dry_run: bool = False,
         boards: str = "", limit: int = 0):
    """boards: comma-separated board names to restrict to (smoke tests).
    limit:  keep only the N CHEAPEST boards -- a smoke test wants fast feedback,
            and the default longest-first order would otherwise hand you the
            50-minute monster first."""
    stress = Path(stress_dir or os.path.expanduser("~/Documents/kicad_stress_test"))
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import sweep_lib as sl

    arm_specs = json.loads(Path(arms).read_text())
    set_list = [s.strip() for s in sets.split(",") if s.strip()]
    board_list = sl.discover_boards(stress, set_list)
    costs = sl.load_cost_table(stress)

    if boards:
        want = {b.strip() for b in boards.split(",") if b.strip()}
        board_list = [(s, b) for s, b in board_list if b in want]
        missing = want - {b for _, b in board_list}
        if missing:
            raise SystemExit(f"not found in sets {set_list}: {sorted(missing)}")
    if limit:
        board_list = sorted(board_list,
                            key=lambda sb: (costs.get(sb[1]) or {}).get("seconds", 1e9))[:limit]

    tasks = sl.build_tasks(arm_specs, board_list, costs)
    boards = board_list  # keep the downstream name

    est_h = sum(t["est_seconds"] for t in tasks) / 3600
    floor_m = max((t["est_seconds"] for t in tasks), default=0) / 60
    big = [t for t in tasks if t["memory_mb"] > 4096]
    print(f"{len(arm_specs)} arms x {len(boards)} boards = {len(tasks)} tasks")
    print(f"  estimated {est_h:,.0f} CPU-hours; floor (longest board) {floor_m:.0f} min")
    print(f"  {len(big)} tasks routed to the 12 GB tier")
    unknown = sum(1 for t in tasks if t["board"] not in costs)
    if unknown:
        print(f"  NOTE {unknown} tasks have no recorded cost -- ordered mid-pack, "
              f"so a hidden monster could extend the tail")
    if dry_run:
        for t in tasks[:10]:
            print(f"   {t['est_seconds']/60:6.1f}m {t['memory_mb']:>6}MB {t['arm']:20} {t['board']}")
        return

    started = time.time()
    std = [t for t in tasks if t["memory_mb"] <= 4096]
    results = []

    # .map() BLOCKS until its tier drains, so calling the two tiers in sequence
    # would hold every 12 GB board back until the 4 GB ones finished (or vice
    # versa) -- and the 12 GB tier holds the longest boards, i.e. exactly the
    # ones that must start first. Drain both concurrently instead.
    import threading
    lock = threading.Lock()

    def drain(fn, items):
        if not items:
            return
        for r in fn.map(items, order_outputs=False):
            with lock:
                results.append(r)

    threads = [threading.Thread(target=drain, args=(replay_big, big)),
               threading.Thread(target=drain, args=(replay_standard, std))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    summary = sl.summarize(results)
    summary["wall_clock_s"] = round(time.time() - started, 1)
    out_path = Path(out or (stress / f"sweep_{int(started)}.json"))
    out_path.write_text(json.dumps({"summary": summary, "rows": results}, indent=1))
    print(f"\nwall {summary['wall_clock_s']/3600:.2f} h -> {out_path}")
    print(f"{'arm':24} {'boards':>6} {'ok':>4} {'mean_compl%':>11} {'DRC':>6} {'CPU-h':>7}")
    for a in summary["arms"]:
        print(f"{a['arm']:24} {a['boards']:6} {a['chain_complete']:4} "
              f"{str(a['mean_completion_pct']):>11} {str(a['total_drc_real']):>6} {a['cpu_hours']:7.2f}")
