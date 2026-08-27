# C1 — Octolinear Smoothing: Pre-Windowed Foreign Arrays — Findings

Date: 2026-08-27
Branch: optimize-via-protection-parse
Commit under test: 4b6ccd75 (the bulk-profile findings commit) + this change
Board: carrier step-6 bulk route (route.py), input = /tmp/si_tune2_tuned2/routed_d2.kicad_pcb
(the d2 board from the tune2 chain, exactly what ab_chain_v2.sh produces at step 5).

## What was done

C1 from bulk_profile_findings.md: smooth_octolinear_chains spends ~17.7% of the
bulk step in per-span numpy clearance checks. The findings proposed two paths:
(a) batch the per-span checks into one vectorized pass, and (b) pre-window the
foreign arrays once per net instead of per span.

**Path (a) was already implemented** at commit 7d38aa55 ("Speed: per-layer clearance
distance field + batched smooth candidate scan") — the candidate loop already calls
_clears_batch (Phase 2.5 Task 2), which evaluates candidate legs in vectorized
batches. What remained was path (b): every batch-kernel call still scanned the
FULL-LAYER foreign arrays (F.Cu: ~7985 seg rows x ~1452 pads), computing full (N,M)
distance matrices and masking with a near filter at the end. The profile showed
this as _seg_foreign_seg_dist_batch (13.6s cumulative), _seg_foreign_pad_dist_batch
(27.4s), and _custom_pad_min_dist (16.6s cumulative, 21,603 calls, 112M builtins.abs).

**This change implements path (b):** pre-window the foreign arrays ONCE PER CHAIN
from the chain's bounding box (+ scan windows), and pass the subsets into the batch
kernels via an optional arrays= parameter. Every candidate leg of a chain lies
inside the chain's bbox, so an obstacle whose own bbox cannot reach within its scan
window of the chain bbox cannot reach within that window of ANY candidate leg —
dropping it is provably verdict-preserving (the subset is a superset of every
per-leg near mask). The result is bit-for-bit identical output with matrix ops
shrunk from full-layer to chain-local.

Files changed (self-contained, no rust_router/):
- py_router/clearance_batch.py — all four batch kernels accept an optional
  pre-windowed arrays= tuple (pad/seg/via/hole); None keeps the historical
  full-array scan.
- py_router/pcb_modification.py — smooth_octolinear_chains: _clears_batch
  threads the optional arrays through; new _prewindow_arrays helper computes the
  per-chain subsets; wired at the chain loop. Gated by KICAD_SMOOTH_PREWINDOW
  (default ON; '0' restores the full-array scan for A/B equivalence testing).

## Gates

### a) Output equivalence (judged by counts, never file diffs)

Carrier step-6 input (routed_d2 -> step-6 bulk output), pre-window ON vs OFF:

| metric | ON | OFF |
|---|---|---|
| smoothing spans | 1055 + 5 | 1055 + 5 |
| smoothing nets | 208 + 2 | 208 + 2 |
| check_drc | OK (16 same-net warnings) | OK (16 same-net warnings) |
| check_connected | 267 routed nets, EXIT=0 | 267 routed nets, EXIT=0 |
| quality/score.py final_score | 58.36 | 58.36 (delta 0.0) |

Corpus boards (routed_output, rp2350_fpga_eensy_prePlane, orangecrab_ext_pll):
all three show identical smoothing stats (chains/spans/saved_mm/removed/added/
reverted) and identical check_drc / check_connected counts between ON and OFF.

### b) Timing (same-run back-to-back A/B, USER time)

Sequential back-to-back runs of the exact step-6 invocation on a quiet machine
(load ~2.3, single run at a time):

| arm | user s | wall s |
|---|---|---|
| baseline (pre-window OFF) | 569.72 | 9:32.84 |
| head (pre-window ON) | 481.99 | 8:05.42 |
| **delta** | **-87.73 s** | -1:27.42 |

**User-time reduction: 87.7 s (~15.4%)** — comfortably above the C1 win threshold
of >=40 s. A concurrent A/B (both arms running at once, for reference) showed
648.18 vs 574.77 s (-73.4 s), consistent direction; the sequential numbers above are
the clean verdict.

Standalone smoothing on the routed carrier board: 39.4 s -> 10.2 s (~3.8x) with
netclass clearances; 30.6 s -> 8.1 s without.

### c) Full suite

tests/run_all.py --fast: **276 passed, 4 failed, 110 skipped** — exact baseline
parity. The 4 failures (test_connection_width_grading, test_exact_clusters,
test_plane_score, test_run8_locked_contact) reproduce identically on the clean
baseline_head worktree (pre-existing env failures: missing KiCad refill oracle,
corpus calibration, web grading) — none caused by this change.

## Notes / honest flags

- The win is concentrated on DENSE boards: the carrier's smoothing collapses 1055
  spans against ~10k segments / ~1.5k pads per layer. On small corpus boards
  (orangecrab: 742 segs) the pre-window overhead can exceed the savings (0.21 s vs
  0.06 s standalone) — the knob is default-ON because the bulk step is where it
  matters, and the equivalence is exact either way.
- Custom (polygon) pads ride along unfiltered in the pad subset (they live in a
  separate list the bbox mask cannot filter); this is a superset, so it is exact,
  and there are only ~15 on the carrier.
- The pre-window is computed once per chain (not per net) — a chain is a single
  same-layer/width run, which is exactly the granularity the candidate legs share.
- No rust_router/ changes; no crate bump; no git push.
