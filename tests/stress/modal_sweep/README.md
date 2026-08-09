# Modal parameter sweep

Fan a routing-parameter sweep across the stress corpus on rented cores, so
"what does this knob do across 150 boards?" takes ~an hour instead of a day.

No LLM is involved. Every recorded board leaves a `redo_commands.sh` manifest
that replays its whole chain deterministically, so a sweep is pure CPU.

## Why one task per (arm, board)

The wall-clock floor of the whole sweep is **the single longest board**. An arm
run as one task is ~9 CPU-hours *serial*, so 100 of those in parallel still take
9 hours. Fanning out to board granularity makes the floor a board (~50 min on
sets 1-10, `daisho`) instead of an arm.

Tasks are dispatched **longest-first** from recorded timings. Round-robin order
lets a 50-minute board start near the end and add 50 minutes to the tail.

## Measured sizing (sets 1-10, 150 boards)

| | |
|---|---|
| One arm | ~8.6 CPU-hours (12.7 CPU-h of LLM tool time x 0.68 replay ratio) |
| 100 arms | ~864 CPU-hours, 15,000 tasks |
| Floor | ~50 min (`daisho`) |
| Cores to reach the floor | ~1,730 |
| 500 cores | ~1.7 h |
| Cost | ~$17 spot / ~$39 on-demand at $0.02-0.045 per vCPU-hour |

Cost is not the constraint; **burst concurrency quota is**. 1,700 concurrent
containers is above the default limit on every platform — plan on 500-1,000
for the first run and raise the limit if the tail matters.

## Memory: two tiers, and why not one

Containers are 4 GB, with a 12 GB tier for boards whose recorded peak exceeds
3.5 GB (7 boards in the reference wave).

Size from **`peak_footprint_mb`, never `peak_rss_mb`**. RSS under-reports by up
to 5x (mimalloc-retained + IOAccelerator-tagged pages, issue #419). Across the
same 150 boards:

| metric | median | p90 | p99 | max | over 4 GB |
|---|---|---|---|---|---|
| `peak_rss_mb` | 447 MB | 931 MB | 1,845 MB | 2,025 MB | 0 |
| `peak_footprint_mb` | 612 MB | 1,858 MB | 4,689 MB | **10,240 MB** | 4 |

Sizing off RSS says "2 GB is plenty" and OOMs the heavy boards.

Boards with a long recorded runtime but *no* recorded memory (LLM-sourced rows
carry no peak) also get the big tier — losing an 8-hour board to an OOM is far
more expensive than over-provisioning a few containers.

`peak_footprint_mb` is darwin-only, so a Linux sweep records RSS only. The
reference numbers above came from macOS and are the conservative ones to size
from.

## Three ways to express an arm

See `arms.example.json`. Always include a **baseline arm with no overrides** —
without an anchor, corpus or commit drift reads as "every arm regressed".

| mechanism | for | how |
|---|---|---|
| `env` | `KICAD_*` knobs the engine already reads | passed to the subprocess |
| `defaults` | `routing_defaults.py` module constants | the file is patched in the container's own repo copy |
| `manifest_sed` | parameters that are CLI **flags** | regex over `redo_commands.sh` |

`defaults` patching is what the documented local A/B recipe does with
`git stash`. Doing it per container needs no shared git state — which is exactly
what lets 100 arms run **concurrently** rather than sequentially, the constraint
called out in `ab_replay_grade.py`'s docstring.

An unknown constant name raises rather than being ignored: a typo that silently
tests nothing would look like "the parameter has no effect".

Anchor `manifest_sed` patterns tightly — a loose one rewrites more of the chain
than you intended.

## Start here: the 2-board smoke test

Do this BEFORE the full sweep. It costs a few cents and exercises every moving
part — image build, corpus volume, the absolute-path remap, parameter patching,
grading, and result harvesting. The remap in particular has never been exercised
inside a container.

```bash
pip install modal && modal setup          # interactive browser login, once

# ~37 files / a few MB -- just two of the cheapest boards (~0.2 min each)
python3 tests/stress/modal_sweep/upload_corpus.py \
    --sets set9 --boards stm32_rfm95_lora,cc1101_rf_module --dry-run
python3 tests/stress/modal_sweep/upload_corpus.py \
    --sets set9 --boards stm32_rfm95_lora,cc1101_rf_module

# plan only, spends nothing
modal run tests/stress/modal_sweep/modal_app.py \
    --arms tests/stress/modal_sweep/arms.example.json \
    --sets set9 --boards stm32_rfm95_lora,cc1101_rf_module --dry-run

# 2 boards x 2 arms = 4 tasks
python3 -c "import json;json.dump([{'name':'baseline'},{'name':'tp2','defaults':{'TRACK_PROXIMITY_COST':2.0}}],open('/tmp/arms_smoke.json','w'))"
modal run tests/stress/modal_sweep/modal_app.py \
    --arms /tmp/arms_smoke.json --sets set9 --boards stm32_rfm95_lora,cc1101_rf_module
```

The first run pays a cold image build (a few minutes); later runs reuse it.

**What to check in the output — a green exit is not enough:**

1. `chain_complete` is true for all 4 rows. False means the replay broke, most
   likely the repo-path remap.
2. `patched_defaults` on the `tp2` rows shows `TRACK_PROXIMITY_COST: '2.0'`. If
   it is empty the arm tested nothing and every arm would have looked identical.
3. The two arms differ *somewhere* (completion, DRC, or `total_seconds`). Two
   byte-identical arms usually mean the parameter never reached the engine.
4. `total_seconds` is in the right ballpark (~10-30 s here). Wildly higher means
   the container is undersized and swapping.

Only then scale up.

## The full sweep

```bash
python3 tests/stress/modal_sweep/upload_corpus.py --sets set1,...,set10   # ~2.2 GB, one-off
modal run tests/stress/modal_sweep/modal_app.py \
    --arms tests/stress/modal_sweep/arms.example.json --dry-run
modal run tests/stress/modal_sweep/modal_app.py \
    --arms tests/stress/modal_sweep/arms.example.json
```

`--limit N` keeps the N *cheapest* boards (the default order is longest-first,
which is right for throughput but wrong for a quick look).

Output: a per-arm table (boards, chain-complete, mean completion %, total real
DRC, CPU-hours) plus every raw row, written to `sweep_<ts>.json`. Each
`(arm, board)` row uses the **same schema `ab_replay_grade.py` emits**, because
each task literally calls it on a one-board set dir — so no grading logic is
duplicated, and `ab_replay_grade.py --compare` still works on the results.

## Verified vs not

Tested here, against real repo files and the real corpus:

- cost table, board discovery (150/150 across sets 1-10), longest-first order
- `routing_defaults.py` patching: values land, the patched module imports, inline
  comments survive, no line drift, unknown names rejected
- recorded-repo-prefix detection from a real manifest
- all three files byte-compile

**Not verified — no Modal account or Linux box was available:**

- **The Modal API calls.** Image-builder method names have moved across client
  versions (`add_local_dir(..., copy=True)` vs the older `copy_local_dir`). Pin
  `modal` and expect to adjust if you are on an older client.
- **The Linux `grid_router` build.** Resolved as of v0.20.2 (2026-08-09): that
  release publishes `grid_router-linux-x86_64.so` built from crate 0.20.1, so the
  image just runs `build_router.py` and keeps the prebuilt — no Rust toolchain,
  ~10 min off the cold build. Verified on the macos-arm64 asset (`__version__`
  reports 0.20.1, matching `Cargo.toml`); the *linux* asset has not been imported
  on a linux box yet, which the smoke test covers. The image deliberately fails
  at build time if a future crate bump ships without binaries — see the comment
  in `modal_app.py` for the two-line fallback.
- **An end-to-end replay in a container.** Manifests bake absolute tool paths;
  `recorded_repo_prefix` detects each manifest's own prefix and remaps it, and
  that logic is tested — but the remap has not been exercised inside a container.
  Run one board, one arm before launching 15,000 tasks.

## Deliberately out of scope

- **No KiCad in the image.** Replay and grading are pure python
  (`check_drc.py` imports numpy/scipy/shapely only), so KiCad buys nothing here
  and costs a large image. The price is `ab_replay_grade`'s optional
  `kicad-cli` cross-check degrading to `None`. Cross-check the *winning* arm
  locally, where KiCad already lives — that check caught #487, where
  `clearance: warning` blinded the grader.
- **Building new corpus sets.** Still needs `pcbnew` for prep; a local one-time
  step, unrelated to sweeping.

## Screen before you sweep

100 arms x 150 boards is ~864 CPU-hours. Running all 100 arms against a ~30-board
subset costs ~10 minutes and tells you which ~20 arms deserve the full corpus.
Same answer about the winners, roughly a fifth of the compute, first results in
minutes.
