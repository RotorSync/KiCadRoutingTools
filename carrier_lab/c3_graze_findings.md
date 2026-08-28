# C3 — Graze prune/nudge passes: weld vectorization + pad prefilter

**Status: PARTIAL — gate 1 (equivalence) PASSES, gate 2 (timing >=20s USER) FAILS on a clean machine, gate 3 (suite parity) PASSES.**

## What was done

Two exactness-preserving optimizations to the graze window (steps 4-8 of the
post-route cleanup: prune_grazing_segments → weld_redundant_grazing_detours →
nudge_grazing_octolinear → nudge_grazing_microshift → nudge_grazing_vias),
all in `py_router/pcb_modification.py`:

1. **Weld O(N²) loop → batched kernel.** `weld_redundant_grazing_detours`'s
   `clears()` scanned EVERY same-layer segment per call via a Python loop
   (`_seg_seg_min_dist`). Replaced with a single `_seg_foreign_seg_dist_batch`
   call (C1's batch kernel, which also folds foreign vias as degenerate
   segments). The separate pad/via checks are kept.

2. **Pad graze prefilter (`_pad_grazes_fast`).** The graze passes call
   `_seg_foreign_pad_dist` (exact rounded-rect kernel) per segment. Added a
   cheap conservative lower bound on the pad edge distance (point-to-segment
   distance from the pad CENTRE minus the pad's max half-extent, minus the
   per-pad clearance excess) that skips ~85-90% of near pads that provably
   cannot graze; survivors are evaluated with the exact batch kernel. Gated by
   `KICAD_GRAZE_PAD_PREFILTER` (default ON; '0' restores the plain kernel).
   Wired into prune, octolinear, and microshift grazes().

Both are bit-for-bit equivalent to the original kernels (verified below).

## Gate 1 — Output equivalence: PASSES

- **Carrier step-6** (`/tmp/si_tune2_tuned2/routed_d2.kicad_pcb`), two A/B pairs
  (ORIG = git-stashed original code vs HEAD = my change):
  - check_drc: 15 same-net warnings both arms (both pairs)
  - check_connected: 2 disconnected components both arms (both pairs)
  - quality/score.py: FINAL SCORE 58.76 both arms (both pairs)
  - total_iterations identical (7777738) both arms
- **Corpus boards** (graze window standalone, knob ON vs OFF):
  - routed_output.kicad_pcb: SAME=True (microshift fires: 4/1/3 segs)
  - rp2350_fpga_eensy_prePlane.kicad_pcb: SAME=True (microshift fires: 12/3/7 segs)
  - orangecrab_ext_pll.kicad_pcb: SAME=True (via_nudge fires: 1/1)
- **Unit-level**: `_pad_grazes_fast` vs `_seg_foreign_pad_dist` flag, 8000 random
  segments each with and without synthetic net_clearances: 0 mismatches.
  Weld batch vs single kernels: 3000 random segments, 0 mismatches.

## Gate 2 — Timing (>=20s USER): FAILS on a clean machine

Back-to-back `/usr/bin/time -v` USER-time A/B on carrier step-6 (identical
iterations both arms):

| Pair | ORIG (s) | HEAD (s) | Delta (s) |
|------|----------|----------|-----------|
| 1 (ORIG→HEAD) | 295.95 | 289.41 | **-6.54** |
| 2 (HEAD→ORIG) | 288.34 | 282.24 | **-6.10** |

Standalone graze window on the FINAL board (`routed_routed.kicad_pcb`, all 5
passes): ORIG **20.09s USER** → HEAD **10.28s USER** = **-9.81s**.

**Why the gate fails:** the graze window is only ~10-20s of a ~290s run on this
clean machine (main cleanup HEAD ~13s + reconcile sub-runs ~2s; ORIG ~24-37s).
The findings' ~55s ceiling was measured under desktop load; on a clean machine
the window is far smaller, so a 20s USER win is impossible from this
optimization alone — even eliminating the ENTIRE window saves <20s.

The change IS a real, verified saving (~6-10s USER total-run, ~9.8s standalone
graze window), but it does not meet the aggressive 20s bar under clean-machine
measurement.

## Gate 3 — Suite parity: PASSES

`tests/run_all.py --fast`: **276 passed, 4 failed, 110 skipped** — exact
baseline parity. The 4 failures (test_connection_width_grading,
test_exact_clusters, test_plane_score, test_run8_locked_contact) are the known
env failures; none reference the graze passes.

## Honest flags / what did NOT work

- **Batching/pre-windowing the pad/via/seg/hole sweeps** (C1-style per-net-group)
  does NOT beat single kernels: batch kernels compute exact geometry over ALL
  passed obstacles then mask, so they do MORE geometry work than single kernels'
  tight per-segment near masks. Measured slower even on the biggest F.Cu group.
- **Segment-level prefilter** (skip segments with no near obstacle): only ~1.2%
  of segments are skipable on a dense board — useless.
- **Seg/hole obstacle-level prefilters**: seg midpoint-circle bound is too loose
  (long segments → large circle radius → most near segs survive); holes are few
  (12 on carrier) so the near set is already tiny. Both measured slower or
  neutral.
- The pad prefilter works because pads are COMPACT (small max-extent), so the
  bounding-circle lower bound is tight (~85-90% skip rate).

## Files changed (uncommitted)

- `py_router/pcb_modification.py` — weld vectorization + `_pad_grazes_fast` +
  wiring into prune/octolinear/microshift grazes().
- `py_router/cleanup_pipeline.py` — measurement-only per-pass timing hook gated
  by `KICAD_CLEANUP_TIMING=1` (no behavior change when off).

C2-owned files (`env_knobs.py`, `phase3_routing.py`, `single_ended_routing.py`)
were NOT touched. No rust_router/ changes.

## Decision

Gate 2 fails on clean-machine measurement, so per the task rule ("COMMIT when
gates pass") this change is NOT committed. The verified exactness-preserving
optimization remains in the working tree for review; it saves ~6-10s USER with
exact output equivalence and full suite parity, and could be committed if the
timing bar is relaxed or measured under load.