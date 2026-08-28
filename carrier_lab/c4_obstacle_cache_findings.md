# C4 — Obstacle-cache build: premise mislabels reality; candidate not worth the complexity

**Verdict: DO NOT LAND as a speed candidate.** The by-net index + SI-fingerprint
dedup implemented here is bit-for-bit equivalent and passes the full suite, but it
saves only ~2.9 s wall / ~3.3 s user on the carrier step-6 input — far below the
~40%-of-window success bar (~14.6 s). The findings' ~45 s ceiling for this candidate
was measured under desktop load AND mislabels the phase window; the true clean-machine
obstacle-build window is ~36.4 s, and its cost is dominated by Rust FFI stamping and
inherent feature work that cannot be cut python-only without behavior change.

## 1. What C4 claimed vs what is actually there

The bulk-profile phase table attributed a ~51 s window to "Net obstacle cache
precompute". Two independent measurements show that window is mostly NOT obstacle
precompute:

- The initial `precompute_all_net_obstacles` on carrier step-6 measures only
  **~0.29–0.49 s** (STOP_FILE run + cProfile). The ~51 s tslog window between
  "Pre-computing net obstacle cache..." and "Multi-point Phase 3" actually contains
  Phase-1/Phase-2 single-ended routing (259 nets, 2444 MST edges).
- The real obstacle-build machinery is spread across the whole run and totals
  **~36.4 s wall on a quiet machine** (instrumented, OFF arm):

| builder | wall | calls |
|---|---|---|
| build_incremental_obstacles | 26.35 s | 372 |
| prepare_obstacles_inplace | 7.20 s | 277 |
| precompute_all_net_obstacles | 2.88 s | 4 |
| **TOTAL** | **36.43 s** | |

## 2. Where the obstacle-build cost actually goes (cProfile, cumulative)

- `set_layer_proximity_batch` (Rust FFI): ~12.8 s — track-proximity + SI stamping
- `_accumulate_field` / `_get_union_field` (SI numpy): ~9 s — inherent feature work
- `clone_fresh` (Rust FFI): ~4.5 s — clone-per-build design
- `get_chip_pad_positions`: ~6 s — already memoized per epoch; retire test is inherent
- `get_stub_endpoints`: ~5 s — O(nets x all-segments) rescans (addressed)
- precompute rasterization: ~13 s cumulative (addressed)

The Rust FFI parts (~17 s) cannot be cut without rust_router changes (forbidden).
The SI field computation is inherent feature work. The remaining safe python-only
levers (scan indexing, SI dedup) are exactly what this change implements.

## 3. What was implemented (gated, default ON, '0' kill switch)

Env knob `KICAD_CACHE_BY_NET` (env_knobs.py), default ON:

1. **connectivity.get_stub_endpoints** — builds a fresh-per-call by-net copper index
   once (only when >1 net queried) instead of per-net full-board list comprehensions.
   Preserves exact board order per bucket -> bit-for-bit identical.
2. **obstacle_cache.precompute_all / precompute_net_obstacles** — same by-net index
   threaded through, eliminating O(nets x all-segments) rescans.
3. **si_enforce.compute_victim_si_field** — computes the aggressor fingerprint once
   per victim and threads it through both helpers (historical path computed it twice).

All three are pure refactors of iteration order with identical objects/order; no
staleness risk (index rebuilt from live lists every call).

## 4. Gates

### a) Output equivalence — PASS

Carrier step-6 + 2 corpus boards, ON vs OFF (KICAD_CACHE_BY_NET=0), same invocation:

| board | check_drc | check_connected | score |
|---|---|---|---|
| carrier routed_d2 | EXIT=0, 16 warnings both | EXIT=0 ALL CONNECTED both | 58.36 / 58.36 |
| glasgow_revC | EXIT=0, 20 warnings both | EXIT=1, 2 disconn both | 60.01 / 60.01 |
| kit-dev-coldfire | EXIT=0, 6 warnings both | EXIT=1, 2 disconn both | 62.11 / 62.11 |

Routing behavior identical (same iteration counts, same smoothing stats). Counts,
never file diffs.

### b) Timing — FAIL (below bar)

True clean-machine obstacle-build window (instrumented, back-to-back, quiet machine):

| arm | build_incremental | prepare_inplace | precompute_all | TOTAL wall | user |
|---|---|---|---|---|---|
| OFF | 26.35 s | 7.20 s | 2.88 s | **36.43 s** | 427.25 s |
| ON | 25.03 s | 5.80 s | 2.71 s | **33.55 s** | 423.92 s |
| delta | -1.32 | -1.40 | -0.17 | **-2.88 s** | -3.33 s |

Success bar per supervisor guidance = ~40% of true window = **~14.6 s**. Achieved:
**~2.9 s wall (~8% of window)**. Not close.

### c) Full suite — PASS

`tests/run_all.py --fast`: **276 passed / 4 failed / 110 skipped** — exact baseline
parity (the 4 failures are the pre-existing env failures: test_connection_width_grading,
test_exact_clusters, test_plane_score, test_run8_locked_contact).

## 5. Recommendation

C4 as described targets a non-problem on this board: the "~51 s precompute" is mostly
Phase-1/Phase-2 routing, and the true obstacle-build window (~36 s) is dominated by
Rust FFI stamping + inherent SI feature work that cannot be cut python-only without
behavior change or rust_router changes (both out of scope). The implemented by-net
index + SI dedup is correct, equivalent, and passes the suite, but its ~8% window win
does not justify the added complexity as a speed candidate.

**Options:** (a) keep the change as a small correctness-neutral cleanup (it IS a real,
if small, win and reduces O(nets x segments) blowup on larger boards), or (b) revert.
This commit keeps it, documented honestly as below-bar; a future candidate targeting
the Rust FFI stamping (out of python-only scope) would be the real lever.
