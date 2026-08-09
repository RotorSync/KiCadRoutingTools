#!/usr/bin/env python3
"""Populate the `kicad-corpus` Modal volume with everything a replay needs.

    python3 tests/stress/modal_sweep/upload_corpus.py --sets set1,set2,...,set10

What goes up, and why only this:
  runs_<set>/<board>/redo_commands.sh   the replay manifest -- the actual input
  runs_<set>/<board>/<inputs>           board files the manifest reads
  boards_unrouted_<set>/, boards_<set>/ the unrouted input + routed reference

What does NOT go up: prior wave outputs, logs, transcripts, intermediate
.kicad_pcb files from the recorded run. A run dir is mostly those, so filtering
cuts the upload by roughly an order of magnitude. Run with --dry-run first to
see the size.

This is a one-time cost; re-run it only when the corpus changes (a new set).
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

# Files inside a run dir that a replay actually consumes. The manifest is the
# entry point; the rest are inputs it references.
KEEP_SUFFIXES = (".kicad_pcb", ".kicad_pro", ".kicad_dru", ".kicad_prl", ".json", ".sh")
SKIP_NAMES = {"transcript.jsonl", "agent_narrative.md", "FINDINGS.md"}
SKIP_DIR_PARTS = {"wave", "__pycache__"}


def collect(stress: Path, sets: list) -> list:
    """[(local_path, remote_relpath)] to upload.

    Run dirs are resolved with sweep_lib.run_dirs_for, NOT `runs_<set>`: sets 1-5
    live in `runs_set1_llm0801` etc. Hardcoding the plain name here uploaded only
    75 of 150 manifests -- and a manifest is the replay's actual input, so half
    the sweep would have failed with "board not found" after the corpus was
    already up.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from sweep_lib import run_dirs_for

    out = []
    seen_boards = set()
    for s in sets:
        for run_dir in run_dirs_for(stress, s):
            for board_dir in sorted(p for p in run_dir.iterdir() if p.is_dir()):
                if not (board_dir / "redo_commands.sh").exists():
                    continue
                if board_dir.name in seen_boards:
                    continue
                seen_boards.add(board_dir.name)
                for f in board_dir.rglob("*"):
                    if not f.is_file():
                        continue
                    if SKIP_DIR_PARTS & set(f.relative_to(board_dir).parts[:-1]):
                        continue
                    if f.name in SKIP_NAMES or f.suffix not in KEEP_SUFFIXES:
                        continue
                    out.append((f, str(f.relative_to(stress))))
        for d in (stress / f"boards_unrouted_{s}", stress / f"boards_{s}"):
            if d.is_dir():
                for f in sorted(d.iterdir()):
                    if f.is_file() and f.suffix in KEEP_SUFFIXES:
                        out.append((f, str(f.relative_to(stress))))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sets", default=",".join(f"set{i}" for i in range(1, 11)))
    ap.add_argument("--stress-dir", default=os.path.expanduser("~/Documents/kicad_stress_test"))
    ap.add_argument("--volume", default="kicad-corpus")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    stress = Path(a.stress_dir)
    sets = [s.strip() for s in a.sets.split(",") if s.strip()]
    files = collect(stress, sets)
    total = sum(f.stat().st_size for f, _ in files)
    print(f"{len(files)} files, {total/1e9:.2f} GB across {len(sets)} sets")
    if a.dry_run:
        for f, rel in files[:15]:
            print(f"   {f.stat().st_size/1e6:8.2f} MB  {rel}")
        return 0

    try:
        import modal
    except ImportError:
        print("pip install modal", file=sys.stderr)
        return 1
    vol = modal.Volume.from_name(a.volume, create_if_missing=True)
    # batch_upload is transactional: it either lands the whole set or nothing,
    # so a half-populated volume can't silently produce "board not found" tasks.
    with vol.batch_upload(force=True) as batch:
        for f, rel in files:
            batch.put_file(str(f), f"/{rel}")
    print(f"uploaded -> volume {a.volume}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
