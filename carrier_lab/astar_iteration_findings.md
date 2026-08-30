# A* Iteration Cost — Findings & Word-Scan Speedup

Date: 2026-08-29
Branch: optimize-via-protection-parse
Commit under test: 65ce611d (HEAD before this change) -> crate 0.27.0
Board: carrier_lab/in.kicad_pcb, step-6 bulk route (route.py), input = the d2 board from the
tune2 chain (/tmp/si_tune2_tuned2/routed_d2.kicad_pcb), the same input the bulk_profile
findings used.

## Purpose

The last untried speed lever: make each A-star ITERATION cheaper, without changing the
search's decisions. Same nets, same order, same expansions, same results -- only less
time per expansion. Byte-identical routing output is both the goal and the gate.

## Phase 1 — Measurement

### py-spy native profile (carrier step-6, 58,024 samples)

| Rust function | % of all samples |
|---|---|
| **BlockedBitmap::test** | **17.70%** |
| GridObstacleMap::is_blocked | 5.85% |
| GridObstacleMap::segment_blocked | 4.72% |
| GridRouter::route_with_frontier | 4.08% |
| BinaryHeap::pop | 1.73% |
| identify_blocking_obstacles | 1.33% |
| BlockedBitmap::set | 1.25% |
| is_via_blocked | 0.77% |

A* inner loop (test + is_blocked + segment_blocked + route_with_frontier + heap) ≈ **34%
of all samples**. The obstacle query path (test + is_blocked + segment_blocked) alone is
**28.3%**.

### Targeted counters (cheap, per-move not per-cell)

Instrumented segment_blocked entry points on the carrier step-6 run:

- segment_blocked r>0 (sweep): **6.94M calls**
- segment_blocked r<=0 (plain): **2.84M calls**
- => **71% of segment_blocked calls are wide-net sweeps**

### Why the sweep dominates

Track margins for the carrier power nets (grid 0.1mm, base width 0.2mm, clearance 0.1mm):

| net width | margin (grid cells) | swept box cells/move |
|---|---|---|
| 0.2mm (base) | 0.0 | 1 |
| 0.5mm (+3V3, CM4_3V3) | 2.24 | 49 |
| 0.8mm (GNDA, GND, +5V, VBUS) | 5.0 | 121 |
| 1.2mm (VIN_PROT, VBULK, VOUT_PD) | 8.0 | 289 |

The _snap_to_lattice_reach phase correction pushes margins well above the naive
half-width (0.8mm -> 5.0, 1.2mm -> 8.0). The top iteration burners are exactly these
power nets (GNDA 4.6M iters, GND 3.8M, +3V3 3.4M, VBUS 0.94M of the 36.16M total), so
the swept-capsule check -- (2*ceil(r)+1)^2 is_blocked probes per move -- is the dominant
per-expansion cost.

## Phase 2 — Implementation

### Word-at-a-time swept-capsule scan (crate 0.27.0)

segment_blocked previously called is_blocked on EVERY cell in the swept box (each doing
two bitmap tests + a source/target hash lookup on hit). The fast path:

1. For each row in the box, load the covering words from BOTH bitmaps (dynamic +
   static) via a new BlockedBitmap::row_word_range helper.
2. Iterate only SET bits (trailing-zeros), distance-checking each against the segment
   with the source/target override applied identically to is_blocked.
3. Skip whole empty words entirely.

Correctness: byte-identical to the per-cell loop -- every cell in the box is either
skipped (no bit set => not blocked) or distance-checked with the same override, in the
same order. The fast path runs ONLY when there are no BGA zones and neither bitmap has
overflow cells; otherwise the exact per-cell loop runs unchanged.

### Equivalence verification

- 10,000 random maps/segments/radii: **0 mismatches** vs a Python reference of the
  original per-cell loop.
- Overflow fallback (cell far outside window): **0 mismatches**.
- BGA-zone fallback: **0 mismatches**.

### Review-found bugs (fixed before shipping)

A review of the fast path found TWO correctness bugs that the original fuzz and the
three-board gates did not fire on. Both are closed; the regression tests below pin them.

**Bug 1 — adjacent-row bits in straddling words (false block).** The bitmap is a flat
w*h array packed 64/word with NO per-row padding (new_words = (w*h+63)/64), so rows are
word-aligned only when width is a multiple of 64. A straddling word holds bits from the
adjacent row. The fast path decoded each set bit as cx = min_x + (cell_idx % width) and
checked only cx against [lo_x, hi_x], never that the bit's row equals cy. A set bit in
row y-1 near the window's right edge decodes to cx near max_x; a query box touching the
right edge passed the x-check and distance-tested it as though it sat at (cx, cy) — a
FALSE BLOCK (prunes a legal move). Symmetric false clear at the left edge for row y+1
bits. FIX: row_word_range now returns the row's exact bit bounds [bit_lo, bit_hi] and
each word is masked to them (BlockedBitmap::mask_word) before decoding, so every
surviving bit is in-row and in-range by construction.

**Bug 2 — query box extending past a window edge (false clear).** When part of a row's
query range fell outside the window (lo_x < min_x or hi_x > max_x), row_word_range
returned None and the fast path SILENTLY SKIPPED that row instead of scanning its
in-window portion — missing real blocks near the edge (false clear -> routing into a
blocked area). FIX: row_word_range now CLAMPS dx0/dx1 to [0, width-1] instead of
returning None. This is exact because the fast path only runs when overflow is empty,
so nothing outside the window can be blocked on that layer; scanning only the in-window
portion of an overhanging range is identical to the per-cell loop.

Both bugs were confirmed empirically pre-fix (right-edge: fast=True/slow=False; left-edge
at r>=3: fast=False/slow=True) and both probes agree post-fix. The strengthened fuzz
(edge-biased, non-multiple-of-64 widths) catches the buggy version with 19 mismatches and
passes the fixed version with 0.

## Gates

### (1) Byte-identical search behaviour

| board | iteration sum old | iteration sum new | match |
|---|---|---|---|
| carrier step-6 | 36,156,064 | 36,156,064 | IDENTICAL |
| kitdev bulk | 14,241,505 | 14,241,505 | IDENTICAL |
| glasgow bulk | 4,018,376 | 4,018,376 | IDENTICAL |

check_connected / check_drc counts identical on all three boards:
- carrier: 2 disconnected components / 1 DRC violation (VIA-DRILL-HOLE at same location)
- kitdev: ALL CONNECTED / 1 DRC violation
- glasgow: 2 disconnected components / 4 DRC violations

All run-scope JSON_SUMMARY fields identical (successful, failed, vias, pad_pairs,
multipoint edges, min_clearance_used).

### (2) Timing gate — carrier step-6 back-to-back USER time

ABBA order on a quiet-ish machine (ambient desktop load ~2.0-2.6: voice_typer ~55% of
one core, gnome-shell, brave -- stated honestly):

| run | .so | user s |
|---|---|---|
| A0 | old (0.25.0) | 464.31 |
| B1 | new (0.27.0) | 370.54 |
| B2 | new (0.27.0) | 371.07 |
| A3 | old (0.25.0) | 466.85 |

Old mean 465.58s, new mean 370.81s => **94.8s faster (20.4%)**. Gate was >=20s; met with
4.7x margin.

**Post-fix re-measurement** (mask + clamp added; same ABBA protocol, load ~2.2-2.9):

| run | .so | user s |
|---|---|---|
| A0 | old (0.25.0) | 464.01 |
| B1 | fixed (0.27.0) | 342.30 |
| B2 | fixed (0.27.0) | 343.30 |
| A3 | old (0.25.0) | 398.70 |

Old mean 431.36s, fixed mean 342.80s => **88.6s faster (20.5%)**. The fix costs one mask
per row (and the clamp is free); the win stands -- actually slightly better than the
pre-fix 370.81s mean, within run noise. Gate >=20s met with 4.4x margin.

### (3) Twice-run determinism

Carrier: B1 and B2 (both new .so) produced identical iteration sums (9,839,848) and
identical summaries. Kitdev: two new-.so runs identical.

### (4) Full suite parity

tests/run_all.py --fast, old .so (0.25.0, worktree at HEAD): **319 passed / 4 failed /
131 skipped** (test_connection_width_grading, test_exact_clusters, test_plane_score,
test_si_classes). New .so (0.27.0): **320 passed / 3 failed / 131 skipped**
(test_connection_width_grading, test_exact_clusters, test_plane_score). The 3 consistent
failures are the documented pre-existing ones (C2 baseline); test_si_classes is flaky
(passed with new, failed with old -- unrelated to the Rust change). No test that passed
with the old .so failed with the new one. Rust unit tests: cargo test --release =
**8 passed / 0 failed** (strengthened edge-biased fuzz + 2 regression tests pinning the
review-found bugs + overflow/BGA fallback tests + 3 existing rate-rule tests). The two
regression tests and the strengthened fuzz FAIL against the pre-fix code (proven by
running them on the buggy source: 3 failed) and PASS on the fixed code.

### (5) Version triple

Cargo.toml / Cargo.lock / VERSION / metadata.json / installed .so all at 0.27.0.

## Files changed

- rust_router/src/obstacle_map.rs -- row_word_range helper + word-scan fast path in
  segment_blocked.
- rust_router/Cargo.toml / Cargo.lock / README.md, VERSION, metadata.json -- crate
  0.27.0 bump.
- carrier_lab/astar_iteration_findings.md -- this file.

## Why this one worked where five prior attempts failed

The five prior attempts (bulk_profile -> adaptive_cap -> c2_ordering) all changed WHICH
nets route when or WHEN a search gives up -- scheduling. Every one broke connectivity or
lost time. This change touches neither: same nets, same order, same expansions, same
results -- only less time per expansion (word-at-a-time bitmap scanning instead of
per-cell probing in the swept-capsule check). Byte-identical output is both the goal and
the gate, and it held on all three boards.
