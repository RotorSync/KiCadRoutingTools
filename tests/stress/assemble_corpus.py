#!/usr/bin/env python3
"""Assemble a corpus set from candidates/curation.json:
  - copy each source .kicad_pcb into sources/github_setN/<slug>.kicad_pcb
  - try to fetch the sibling .kicad_pro from the repo (best effort)
  - write manifest_setN.json (pre-existing entries preserved + new)
  - regenerate fetch_setN.py and prep_setN.sh with a full MAP array
Run with plain python3 (the prep step itself uses KiCad's python separately).

`set` in curation.json is the set's NAME, so any set works -- "set26", and the
extreme sets "set3monster" too, not just an integer.

Re-running is idempotent: a pre-existing manifest entry survives unless the new
curation re-adds the same `short_name`. (This used to be "keep entries WITHOUT a
short_name", which was really a stand-in for set4's one hand-written board and
would have silently dropped every entry in an already-populated set.)"""
import json, os, shutil, subprocess
from pathlib import Path

STRESS = Path(os.environ.get("STRESS_DIR", str(Path.home() / "Documents/kicad_stress_test")))
CAND = STRESS / "sources/candidates"
# This script's own directory IS tests/stress -- never a hardcoded home dir.
STRESS_TESTS = Path(__file__).resolve().parent
cur = json.load(open(CAND / "curation.json"))

TIER_PKG = {"easy": "2-layer,MCU", "medium": "4-layer,USB,MCU", "hard": "BGA,FPGA/SoC,high-speed",
            "monster": "BGA/SoC,high-speed,high-layer"}

# What a set IS, carried into the generated fetch_setN.py docstring. Without this
# the generator overwrites any hand-written description every time it re-runs.
SET_BLURB = {
    "set3monster": (
        "set3monster is the \"extreme / intractable\" monster batch: boards whose\n"
        "size or stackup puts a single route step near (or past) the RUNBOOK 3h/command\n"
        "cap. lora_cubesat_cm (485 nets / 6 layers) was moved here out of set11 for\n"
        "exactly that reason. The rest are 8-14 copper layer Antmicro boards -- the\n"
        "first >8-layer boards in the corpus, admitted once validate_candidate.py's\n"
        "upper layer bound was removed in favour of the `monster` tier."
    ),
}

def fetch_pro(raw_url, dest):
    if not raw_url or not raw_url.endswith(".kicad_pcb"): return False
    pro_url = raw_url[:-len(".kicad_pcb")] + ".kicad_pro"
    r = subprocess.run(["curl", "-sL", "--fail", pro_url, "-o", str(dest)], capture_output=True)
    if r.returncode == 0 and dest.exists() and dest.stat().st_size > 20:
        return True
    if dest.exists(): dest.unlink()
    return False

by_set = {}
for b in cur: by_set.setdefault(str(b["set"]), []).append(b)

for s in sorted(by_set):
    src_dir = STRESS / f"sources/github_{s}"
    src_dir.mkdir(parents=True, exist_ok=True)
    manifest = []
    # Preserve any board already curated into this set that we are NOT re-adding.
    incoming = {b["name"] for b in by_set[s]}
    prev = STRESS_TESTS / f"manifest_{s}.json"
    if prev.exists():
        manifest.extend([e for e in json.load(open(prev))
                         if e.get("short_name") not in incoming])
    mapping = []  # (short_name, filename_fragment) for prep MAP
    pros = 0
    for b in sorted(by_set[s], key=lambda x: (x["tier"], x["name"])):
        slug = b["slug"]
        fn = f"{slug}.kicad_pcb"
        dst = src_dir / fn
        src_path = b["src"] if os.path.isabs(b["src"]) else str(CAND / b["src"])
        shutil.copy(src_path, dst)
        got_pro = fetch_pro(b["raw_url"], src_dir / f"{slug}.kicad_pro")
        pros += 1 if got_pro else 0
        manifest.append({
            "set": s, "repo": b["repo"], "path": b["path"], "branch": b.get("branch", "main"),
            "file": fn, "raw_url": b["raw_url"], "github_url": b["github_url"],
            "size_kb": dst.stat().st_size // 1024, "layers_est": b["layers"],
            "footprints": b["footprints"], "routable_nets": b["routable_nets"],
            "max_pads": b.get("max_pads"), "kicad_version": b.get("kicad_version"),
            "has_kicad_pro": got_pro,
            "tier": b["tier"], "packages": TIER_PKG.get(b["tier"], "?"),
            "lane": b.get("lane", ""),
            "license": "see repo", "short_name": b["name"], "note": b["note"] or b["repo"],
        })
        mapping.append((b["name"], slug))
    json.dump(manifest, open(STRESS_TESTS / f"manifest_{s}.json", "w"), indent=2)
    # regenerate prep_setN.sh with the MAP. The MAP covers the WHOLE manifest, not
    # just this run's additions -- fetch_setN.py downloads every manifest entry, so
    # a MAP of only the new boards would leave the pre-existing ones un-prepped on
    # a from-scratch rebuild.
    known = {n for n, _ in mapping}
    for e in manifest:
        sn = e.get("short_name")
        if sn and sn not in known:
            mapping.append((sn, Path(e["file"]).stem))
    mapping.sort()
    map_lines = "\n".join(f'  "{n}|{frag}"' for n, frag in mapping)
    prep = f"""#!/bin/bash
# Normalize+strip every {s} board (one pcbnew process each, segfault-safe).
# Reuses prep_set2.py (generic: <src> <routed_dst> <stripped_dst>).
#   stripped, unrouted boards -> boards_unrouted_{s}/
#   normalized routed reference + .kicad_pro -> boards_{s}/
# Auto-generated by assemble_corpus.py. Sources are modern KiCad (v6+).
set -u
SELF="$(cd "$(dirname "${{BASH_SOURCE[0]}}")" && pwd)"
STRESS="${{STRESS_DIR:-$HOME/Documents/kicad_stress_test}}"
SRC="$STRESS/sources/github_{s}"
KPY="${{KICAD_PYTHON:-/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/Current/bin/python3}}"
PREP="$SELF/prep_set2.py"
mkdir -p "$STRESS/boards_unrouted_{s}" "$STRESS/boards_{s}"

# short-name | source-filename-fragment (unique within github_{s}/)
MAP=(
{map_lines}
)

for entry in "${{MAP[@]}}"; do
  IFS='|' read -r name frag <<< "$entry"
  srcfile=$(find "$SRC" -maxdepth 1 -name '*.kicad_pcb' -name "*${{frag}}*" | head -1)
  if [ -z "$srcfile" ]; then echo "MISS $name (frag '$frag')"; continue; fi
  echo "== $name <- $(basename "$srcfile")"
  "$KPY" "$PREP" "$srcfile" "$STRESS/boards_{s}/$name.kicad_pcb" \\
         "$STRESS/boards_unrouted_{s}/$name.kicad_pcb" 2>/dev/null || echo "  FAIL $name"
  profile="${{srcfile%.kicad_pcb}}.kicad_pro"
  [ -f "$profile" ] && cp "$profile" "$STRESS/boards_{s}/$name.kicad_pro"
done
echo "Done. stripped -> boards_unrouted_{s}/ ; routed reference -> boards_{s}/"
"""
    (STRESS_TESTS / f"prep_{s}.sh").write_text(prep)
    os.chmod(STRESS_TESTS / f"prep_{s}.sh", 0o755)

    # regenerate fetch_setN.py -- pure boilerplate over manifest_setN.json, but
    # without it the set cannot be re-downloaded from a clean checkout.
    blurb = SET_BLURB.get(s, "")
    fetch = f'''#!/usr/bin/env python3
"""Fetch {s} .kicad_pcb sources listed in manifest_{s}.json (raw download).
{(chr(10) + blurb + chr(10)) if blurb else ""}
Downloads each board (and its sibling .kicad_pro, which carries the DRC floor)
into $STRESS_DIR/sources/github_{s}/. After fetching, run `bash prep_{s}.sh`
(needs KiCad's bundled python / pcbnew) to produce boards_{s}/ (routed
reference) + boards_unrouted_{s}/ (stripped).

  python3 fetch_{s}.py
  bash prep_{s}.sh

Auto-generated by assemble_corpus.py.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
STRESS = Path(os.environ.get("STRESS_DIR", str(Path.home() / "Documents/kicad_stress_test")))
MANIFEST = HERE / "manifest_{s}.json"


def main():
    boards = json.loads(MANIFEST.read_text())
    out_dir = STRESS / "sources/github_{s}"
    out_dir.mkdir(parents=True, exist_ok=True)
    ok = 0
    for b in boards:
        dest = out_dir / Path(b["file"]).name
        r = subprocess.run(["curl", "-sL", "--fail", b["raw_url"], "-o", str(dest)],
                           capture_output=True)
        if r.returncode != 0 or not dest.exists() or dest.stat().st_size == 0:
            print(f"  FAIL {{b['repo']}}  <- {{b['raw_url']}}")
            continue
        # sibling .kicad_pro: never drop it (see CLAUDE.md #441 -- a board without
        # its project file resolves its DRC floor from the STOCK netclass).
        pro_url = b["raw_url"][: -len(".kicad_pcb")] + ".kicad_pro"
        subprocess.run(["curl", "-sL", "--fail", pro_url, "-o",
                        str(dest.with_suffix(".kicad_pro"))], capture_output=True)
        ok += 1
        print(f"  OK  {{b['repo']:42}} {{dest.stat().st_size // 1024}}KB  [{{b.get('tier','?')}}]")
    print(f"\\n{{ok}}/{{len(boards)}} {s} sources -> {{out_dir}}")
    return 0 if ok == len(boards) else 1


if __name__ == "__main__":
    sys.exit(main())
'''
    (STRESS_TESTS / f"fetch_{s}.py").write_text(fetch)
    os.chmod(STRESS_TESTS / f"fetch_{s}.py", 0o755)
    n_new = len(by_set[s])
    print(f"{s}: {n_new} new boards -> github_{s}/ ; sibling .kicad_pro fetched: "
          f"{pros}/{n_new} ; manifest={len(manifest)} entries ; MAP={len(mapping)} ; "
          f"prep_{s}.sh + fetch_{s}.py regenerated")
print("done")
