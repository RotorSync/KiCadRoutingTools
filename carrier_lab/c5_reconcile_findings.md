# C5 — Reconcile sub-run reuse: window measured, reuse unsound AND below bar

**Verdict: DO NOT LAND as a speed candidate.** The reconcile sub-runs' setup cost
(parse + base map + net cache + fragility) is only ~6 s of a ~35.9 s clean-machine
window, and the parent's in-memory state is NOT reusable for the sub-run's scope by
construction. Even eliminating ALL setup would save ~6 s — far below the ~14 s bar
(40% of the true window). The window is dominated by inherent A* probing of
boxed-in nets across three laps.

## 1. True window (clean machine, MEASURE-FIRST)

Two full step-6 runs on the carrier input (/tmp/si_tune2_tuned2/routed_d2.kicad_pcb),
back-to-back, quiet machine (load ~1.2-2.0, free >= 11G, no route.py), nice -n 19,
tslog.py per-line wall timestamps:

| run | user s | wall s | reconcile start | last progress-loop | reconcile window |
|-----|--------|--------|------------------|--------------------|------------------|
| A | 425.14 | 7:07 | [387.90] | [423.82] | **35.92 s** |
| B | 424.72 | 7:07 | [387.87] | [423.71] | **35.84 s** |

Both runs agree within ±0.1 s; user time matches C4's clean baseline (427.25 s)
within noise, confirming comparability. The bulk-profile findings' ~66 s reconcile
window was measured under desktop load (load 2-4) and is ~1.8x inflated.

**True window = ~35.9 s wall (~35 s user, single-threaded). Success bar = ~14 s.**

The window is THREE nested batch_route laps (the #348 end-of-run reconcile +
#572 lap loop): lap1 retries 6 nets (12.7 s), lap2 retries 3 (11.3 s), lap3 retries
3 (11.3 s). Each lap re-parses the just-written output file and rebuilds everything.

## 2. Where the window actually goes (per-lap setup vs routing)

| cost | lap1 | lap2 | lap3 | total |
|------|------|------|------|-------|
| parse (Loading...) | 0.31 | 0.31 | 0.86 | ~1.5 |
| base obstacle map | 0.65 | 0.17 | 0.15 | ~1.0 |
| net obstacle cache (268 nets) | 1.11 | 0.71 | 0.67 | ~2.5 |
| plane fragility field | 0.54 | 0.54 | 0.54 | ~1.6 |
| **setup subtotal** | | | | **~6.6 s** |
| A* routing + rescue + cleanup + writeback | | | | **~29 s** |

The dominant cost is inherent: +3V3 and VIN_PROT are boxed_in_static (their pads
surrounded by foreign copper), so each lap's A* probes blocked cells (415k-iteration
probes, "backward stuck" ladders) against a full board, then rescue + cleanup +
writeback run per lap. None of that is avoidable by reusing parent state.

## 3. Why parent-state reuse is UNSOUND here (read the code paths)

The findings' premise — "the sub-runs re-parse and re-build from scratch; reusing
the parent's parsed PCBData / base map / cache would cut most of it" — fails on
three independent grounds:

### a) Parent pcb_data is STALE at reconcile time in CLI mode
The in-run plane finalize (#562) runs BEFORE the final reconciliation (order swap,
route.py:3959). Its three legs all mutate the FILE, not pcb_data:
- engine leg: repair_planes(pcb_data=_live9) routes in-memory but writes via
  _write_output from TEXT content (repair_planes.py:3100, 3243) — pcb_data is not
  the write source;
- cleanup leg: clean_plane_copper(output_file, ...) edits the file only
  (route.py:4284);
- oracle leg: oracle_reconnect writes welded copper DIRECTLY to output_file via
  string manipulation (kicad_oracle.py:2736), and CLI mode does NOT mirror it into
  pcb_data (route.py:4463-4466 only stashes _reaudit9; the mirroring block at
  4467+ is _gui9-only). The comment at route.py:4487-4489 says it outright:
  "the CLI reconcile re-parses the oracle-edited file and gets this for free."

So handing the sub-run the parent's pcb_data would route against a board MISSING
the oracle-welded copper — a different board than the file, forking the outcome.
The GUI branch already passes pcb_data (with snap_pcb_data_to_iu_grid) because on
that front the oracle copper IS mirrored into pcb_data; the CLI front has no such
mirror and cannot reuse without adding one (a behavior change, not a refactor).

### b) Parent base obstacle map is wrong for the sub-run's scope
build_base_obstacle_map EXCLUDES every net in nets_to_route from the base map
(obstacle_map.py:210 segments, :280 vias, :312 pads all `continue` on
nets_to_route_set). The main run's base map therefore treats all 259 routed nets'
copper as NON-obstacles (they are added per-net to the working map as they route).
The reconcile sub-run routes a SUBSET of those same nets against a board where
every other net's copper IS a foreign obstacle — so the parent's base map is
semantically wrong for the sub-run scope by construction.

### c) Parent net obstacle cache is per-net OWN obstacles, not foreign maps
precompute_all_net_obstacles builds each net's OWN obstacle stamp (its own copper
as obstacles for other nets to avoid). The sub-run needs exactly that for its
targets, but the parent's cache entries for the reconcile targets were built at
main-run start and updated per-route; they do not represent "this net's copper as
seen by a fresh full-board build" and cannot be transplanted without rebuilding.

### d) Between laps the board changes
Each lap writes new copper to output_file; lap N+1 must route against lap N's
board. Even a hypothetical "reuse within one lap" cannot span laps.

## 4. Even perfect setup elimination is below bar

The maximum soundly-eliminable cost is the ~6.6 s of setup across all three laps —
and most of that is NOT soundly eliminable (see §3). Even taking the optimistic
~6 s as a ceiling, it is ~17% of the window, far below the 40% bar (~14 s). The
window is dominated by inherent A* work on boxed-in nets that no state reuse can
avoid.

## 5. What WOULD move this window (honest flags)

- The three laps re-probe the SAME boxed-in nets (+3V3/VIN_PROT) with escalating
  rip authority each lap; a python-only cap on fruitless re-probing of a net whose
  verdict was already boxed_in_static in a prior lap would cut real time — but it
  changes behavior (the lap loop exists because "the NEXT attempt can win"), so it
  needs its own A/B, not a reuse refactor.
- The oracle leg (29.5 s of the pre-reconcile finalize) is a separate window; not
  C5's target.
- Rust-side A* cost on boxed-in probes is out of python-only scope.

## Files

- Measurement logs (not committed): /tmp/c5_measure/step6.log, /tmp/c5_measure2/step6.log
- Driver: carrier_lab/si_phase2/run_bulk_profile.sh (plain mode)
- Analyzer: /tmp/c5_lap.py (per-lap marker extraction)

## Decision

C5 as described targets a non-problem on this board: the true clean-machine window
is ~35.9 s, its setup portion is only ~6 s, and the parent's in-memory state is not
reusable for the sub-run's scope by construction (stale pcb_data in CLI mode,
wrong-scope base map, per-net-not-foreign cache). No code change was made; this
findings file is the deliverable.
