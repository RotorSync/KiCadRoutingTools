# Bulk Route Step — Time Attribution Findings

Date: 2026-08-27
Branch: optimize-via-protection-parse
Commit under test: 975180c0 (SI enforcement tuned to R0.8/C0.1 — the current production default)
Board: carrier_lab/in.kicad_pcb, step-6 bulk route (route.py), input = the d2 board from the
tune2 chain (`/tmp/si_tune2_tuned2/routed_d2.kicad_pcb`), which is what ab_chain_v2.sh produces
at step 5 under the current production SI config.

## Purpose

Nobody had a time-attribution map for the carrier bulk route step (~8:50 wall on a quiet
machine). This document is that map: where the ~515 s of user time goes, split by phase, by
Python-vs-Rust-FFI, and by per-net difficulty. **Findings only — no engine changes were made.**

## Method

Two full runs of the exact step-6 invocation under `py-spy record --native` (wall-clock
sampling at 100 Hz, captures Rust frames), plus per-line wall-clock timestamps on every log
line (tslog.py). Run A = flamegraph format (70,064 samples), Run B = raw format (73,144
samples) for call-tree aggregation. Both runs used fresh output paths and the identical
input board.

**Load conditions (honest note):** the machine was NOT quiet during these runs — the user's
interactive desktop apps (voice typer ~36% of one core, Bambu Studio / OpenSCAD renders at
~99% of a core intermittently, browsers) kept load at 2–4. Profiling measures *relative*
time attribution, which is robust to this; the absolute wall times are inflated and are NOT
comparable to the quiet-machine baseline. The step-4 cross-check (profile shares vs the
router's own per-phase log times from a second run) confirms the attribution is stable.

### Overhead factor

| run | user s | wall s | vs quiet baseline |
|---|---|---|---|
| quiet baseline (tune2, no profiler) | 513 | 515 | — |
| Run A (py-spy flamegraph + desktop load) | 595 | 725 | user ×1.16, wall ×1.41 |
| Run B (py-spy raw + desktop load) | 615 | 760 | user ×1.20, wall ×1.48 |

py-spy adds ~16–20% to *user* time (sampling pauses); the wall inflation is mostly the
desktop load. All percentages below are profile shares (relative), which are the robust
measure.

### Determinism cross-check

Both runs produced IDENTICAL iteration counts (32,435,776 total) and near-identical per-net
times (GNDA 55.7 vs 55.4 s, TC8_N 30.6 vs 30.6 s). The router is deterministic; the small
per-net deltas are desktop-load noise.

## 1. Phase-level wall-time attribution (Run B, main run)

| phase | wall s | % of run |
|---|---|---|
| **Phase 3 tap routing** (801 tap edges, incl rip-up ladders + reroutes + seam re-asks) | **396.3** | **52.2%** |
| Octolinear smoothing (#536) | 128.6 | 16.9% |
| Graze prune/nudge passes (silent window) | 55.1 | 7.3% |
| Net obstacle cache precompute | 50.8 | 6.7% |
| Reconcile sub-runs (3 nested batch_route calls on failed nets) | 66.0 | 8.7% |
| Main writeback (3.5 MB board) | 40.3 | 5.3% |
| Rescue pass (fine-parameter) | 4.6 | 0.6% |
| Cycle prune + strict collapse + width neck | 7.4 | 1.0% |
| Beautify | 5.8 | 0.8% |

Run A agrees within ~2% on every phase (Phase 3: 362.6 s, smoothing window ~126 s, graze
window ~56 s). The single-ended loop is negligible (~2 s — most nets are multipoint).

**The bulk step is dominated by Phase-3 tap routing (52%) and the post-route cleanup
(smoothing + graze ≈ 24%).**

## 2. Python vs Rust FFI boundary

Overall self time by category (Run B, 73,144 samples):

| category | samples | % of all |
|---|---|---|
| **Rust FFI** (grid_router.so) | 28,458 | **38.9%** |
| **numpy/libm C** (called from Python) | 23,136 | **31.6%** |
| Python | 16,307 | 22.3% |
| libc | 5,243 | 7.2% |

### Rust FFI breakdown (self time)

| Rust function | % of Rust | % of all |
|---|---|---|
| BlockedBitmap.test (obstacle bitmap query) | 38.2% | 15.7% |
| GridObstacleMap.is_blocked | 12.7% | 5.2% |
| GridObstacleMap.segment_blocked | 11.0% | 4.5% |
| route_with_frontier (the A* search entry) | 8.8% | 3.6% |
| identify_blocking_obstacles | 5.5% | 2.3% |
| set_layer_proximity_batch (SI stamping) | 4.8% | 2.0% |
| BinaryHeap.pop (A* open list) | 3.4% | 1.4% |
| is_via_blocked / NodeStore.ensure_index / BlockedBitmap.set / clone_fresh / add_blocked_*_batch | ~6% | ~2.5% |

**~74% of Rust FFI time is inside `_probe_route_with_frontier_once`** (the A* search call in
the tap-routing path): `route_with_frontier` + its internal `BlockedBitmap.test` /
`is_blocked` / `segment_blocked` queries during neighbor expansion. This is genuinely
Rust-internal A* work, NOT Python orchestration — the boundary is clean.

### numpy/libm breakdown (self time)

`hypotf64` (9.8% of numpy), `DOUBLE_subtract`/`multiply`/`add` (~18%), `hypot` (2.5%), plus
LONG/DOUBLE ufunc loops and reductions (~10%). These are the clearance-distance kernels:
`_seg_foreign_pad_dist` (~4.9% cumulative), `_seg_foreign_seg_dist` (~1.9%), `_custom_pad_min_dist`
(~2.0%), and the batched `_clears_batch` (~17.2% cumulative).

## 3. Function-level cumulative attribution (call tree)

| function | cumulative % | where it runs |
|---|---|---|
| route_multipoint_taps → _route_connection_at_margin → direct → _probe_route_with_frontier → _route_with_via_unblock → _route_main_connection | **34.5%** | Phase 3 tap routing |
| run_phase3_tap_routing (loop) | 22.5% | Phase 3 |
| try_phase3_ripup (rip-up ladder) | 22.5% | Phase 3 |
| _reroute_phase3_ripped_nets (rip victims reroute) | 21.7% | Phase 3 |
| run_post_route_cleanup (whole pipeline) | 28.7% | cleanup |
| smooth_octolinear_chains (#536) | **17.7%** | cleanup |
| seam_reask_one_net (#444) | 13.5% | Phase 3 |
| fragility_on_copper_change / refresh (plane fragility) | 9.8% | Phase 3 + cleanup |
| _clears_batch / clears() (clearance checks) | 17.2% / 21.1% | everywhere |
| rescue_failed_nets (#331/#371) | 3.2% | rescue |
| _stamp_si_enforcement / stamp_victim_si_field (SI) | 2.4% | obstacle build |

Note these overlap (ripup ⊂ taps; reroute ⊂ ripup; smoothing ⊂ cleanup). The non-overlapping
top-level story is the phase table in §1.

## 4. Per-net time distribution (Phase-3 "Net total time")

146 nets logged; sum = ~377 s of Phase-3 net time.

| net | time s | iterations |
|---|---|---|
| GNDA | 55.4 | 4,126,034 |
| +3V3 | 42.0 | 3,432,003 |
| GND | 37.0 | 3,834,705 |
| TC8_N | 30.6 | 324,977 |
| VBUS | 28.8 | 938,092 |
| TC7_AP | 21.9 | 600,981 |
| +5V | 13.8 | 371,016 |
| SDA1 / VBULK / RTL_1V0 / VOUT_PD / VIN_PROT / RTL_XI / TC4_AP ... | 3–7 each | — |

**Top-10 nets = ~66% of Phase-3 net time** (249 of 377 s). The long tail (136 nets) = ~34%.
The top-6 nets alone (GNDA, +3V3, GND, TC8_N, VBUS, TC7_AP) = ~216 s = **57% of Phase-3
time**. These are the power/ground trunks and the hardest analog nets — they burn iterations
(4M+ each) because they route last against a full board.

## Cross-check (step 4)

Profile cumulative shares vs router's own per-phase log times agree:
- Smoothing: profile 17.7% vs wall-share 16.9% ✓
- Cleanup pipeline: profile 28.7% vs wall-share (smoothing + graze + cycle/neck + beautify ≈ 26%) ✓
- Phase-3 taps: profile union (taps + ripup + reroute + seam ≈ overlapping) vs wall-share 52.2% ✓
- Per-net times stable across both runs; iteration counts identical ✓
- The router's own "Net total time" sum (~377 s) ≈ the Phase-3 wall window (~396 s), confirming Phase-3 net time is nearly all of the phase.
The attribution is trustworthy despite the desktop load.

## Top-5 optimization candidates (ranked by ceiling)

Ceilings assume the phase's wall time could be cut to ~0 — a bound, not a promise.

### C1 — Octolinear smoothing is pure numpy clearance-checking (~129 s, python-only)
`smooth_octolinear_chains` = 17.7% of samples; its leaves are ~60% numpy/libm math
(hypotf64 + DOUBLE ufuncs) plus `_custom_pad_min_dist` and `_seg_foreign_seg_dist_batch`.
It calls `clears()` per candidate span against the full foreign-copper arrays.
**Ceiling: ~120 s.** Python-only: batch the per-span clearance checks into one vectorized
pass (`_clears_batch` already exists for same-net/layer/width legs — smoothing's spans are
exactly that shape), or pre-window the foreign arrays once per net instead of per span.
No Rust change needed.

### C2 — Phase-3 tap routing's A* search cost (~250 s of the phase is Rust A*)
`route_multipoint_taps` = 34.5%; ~85% of its time is Rust FFI (`BlockedBitmap.test` +
`is_blocked` + `segment_blocked` + `route_with_frontier` + `BinaryHeap.pop`). This is the
A* search itself expanding nodes against the obstacle bitmap.
**Ceiling: ~250 s.** **Needs Rust.** The bitmap query (`BlockedBitmap.test`) is already
inlined in Rust; the win would be algorithmic — better ordering so hard nets route before
the board fills (the top-6 nets burn 4M iterations each routing last), or a cheaper
heuristic/expansion for the long power trunks. This is the single biggest lever but the
heaviest to change.

### C3 — Graze prune/nudge passes (~55 s, python-only)
The silent window between orphan-drop and cycle-prune is the graze prune + nudge passes,
which call `_seg_foreign_pad_dist`/`_seg_foreign_seg_dist` per segment.
**Ceiling: ~50 s.** Python-only: these passes re-scan foreign copper per segment; batching
the distance checks per net/layer (like `_clears_batch`) or caching the foreign arrays'
windowed subsets would cut most of it.

### C4 — Net obstacle cache precompute (~51 s, python-only)
`build_incremental_obstacles` = 5.7% cumulative; the precompute builds per-net obstacle
stamps before routing.
**Ceiling: ~45 s.** Python-only: the cache is built once for all unrouted nets; parallelizing
it across cores (it's embarrassingly parallel per net) or deferring it until a net is first
routed would cut most of it.

### C5 — Reconcile sub-runs re-do the whole pipeline (~66 s, python-only)
Three nested `batch_route` calls re-parse the board, rebuild base maps + caches, and re-run
cleanup for a handful of failed nets.
**Ceiling: ~60 s.** Python-only: the sub-runs re-parse and re-build from scratch; reusing
the parent's parsed PCBData / base map / cache across sub-runs (they already share config)
would cut most of it.

### Honest flags
- **C1, C3, C4, C5 are python-only** — cheap to try, no crate bump / binary redistribution.
- **C2 is the biggest ceiling but needs Rust** — a crate version bump, `build_router.py --from-source`, and re-publishing prebuilt binaries per CLAUDE.md. Surface that cost early.
- The top-6 nets' iteration burn (C2's target) is also addressable python-side by ordering: route power/ground trunks earlier in Phase-3 order so they don't fight a full board — worth testing before any Rust change.
- The single-ended loop is negligible (~2 s); do not optimize it.
- The writeback (~40 s) and parse (~2 s) are not worth chasing.
- SI enforcement (`_stamp_si_enforcement`/`stamp_victim_si_field`) = only ~2.4% cumulative — NOT a meaningful lever despite being the recent timing concern; the tuned R0.8/C0.1 already brought it within budget.
- plane_fragility refresh = ~9.8% cumulative — it fires on every copper commit; worth a look but secondary to C1/C2.
- The top-6 nets' iteration burn is the real Phase-3 story: GNDA alone burns 4M iterations (55 s). If ordering can't fix it, a Rust-side cap on fruitless iteration for power trunks is the lever.

## Files

- Drivers: `carrier_lab/si_phase2/run_bulk_profile.sh`, `carrier_lab/si_phase2/tslog.py`
- Analyzers: `carrier_lab/si_phase2/analyze_tslog.py`, `analyze_nettimes.py`,
  `analyze_cprofile.py`, `aggregate_pyspy.py`
- Raw data (not committed): `/tmp/bulk_prof_a/` (flamegraph), `/tmp/bulk_prof_b/` (raw)