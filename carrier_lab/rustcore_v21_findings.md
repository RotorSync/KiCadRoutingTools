# Rust-core parallel routing v2.1 -- MULTI-CONNECTION NEGOTIATED ROUTING -- A/B verification findings

Date: carrier board A/B chain (`carrier_lab/ab_chain_v2.sh`), input `carrier_lab/in.kicad_pcb`.
Branch: current HEAD (c068a8b6) + v2.1 changes. Version triple bumped to 0.23.0.

## Status: DO NOT MERGE -- two verification gates FAILED

The multi-connection negotiated pre-pass (`route_tree` in Rust, `negotiated_routing.py`
in Python) was implemented per spec: each unresolved multipoint net routes its ENTIRE tap
set as ONE COHERENT UNIT inside one FFI call against a shared frozen cost map (base +
present-congestion + history), parallelism moved to NET level (rayon over whole nets per
iteration), per-net result a pure function of (taps + frozen map). It passes its Rust unit
tests (including 1-thread vs N-thread determinism), passes the full test gate at baseline
parity, and is deterministic at system level.

However, on the carrier board A/B it FAILS two required gates:

1. **Timing** -- head arm is SLOWER than baseline in both wall clock and user time.
2. **check_connected** -- head arm leaves a pad disconnected that baseline connects.

Per instructions, findings are recorded here and nothing was committed.

---

## A/B table (carrier board, step6 = route.py bulk)

| Metric | Baseline (kwarg OFF) | Head #1 (kwarg ON) | Head #2 (kwarg ON, determinism) | Gate |
|---|---|---|---|---|
| step6 user time | 522.75 s | 1079.51 s | 1059.62 s | FAIL (slower) |
| step6 wall clock | 8:45.39 | 13:23.83 | 13:05.69 | FAIL (slower) |
| Routing-core "Net total time" sum (Phase 3) | 245.54 s | 244.94 s | -- | ~equal (no savings) |
| Negotiated pre-pass time | -- | 218.10 s | 216.93 s | overhead |
| Negotiated pre-pass activity | -- | 4 iters, 40 edges, 141/150 nets | identical (4 iters, 40 edges, 141/150) | -- |
| check_connected | ALL NETS FULLY CONNECTED | **1 disconnected pad** (MUX_A0N / CAC2 @ (82.78,99.50)) | identical (MUX_A0N / CAC2) | FAIL |
| check_drc @0.25mm | 0 violations (35 same-net warnings) | 0 violations (29 same-net warnings) | identical (29 warnings) | PASS |
| quality/score.py final score | 57.91 | 59.16 | 59.20 | PASS (within 1.0) |
| Determinism (head#1 vs head#2) | -- | identical counts + pre-pass activity | identical counts + pre-pass activity | PASS |

Reference floor from today's official bench: baseline-arm core ~264s within a step6 of
~8:49-11:33 depending on conditions. This A/B is same-run, so the verdict is judged ONLY
within it: base step6 8:45 vs head step6 13:23 -- a ~53% wall regression.

---

## Failure 1 -- timing regression

The negotiated pre-pass resolves 141/150 multipoint nets in ~218 s, but the sequential
Phase-3 loop that follows does essentially the SAME total routing work as baseline
(Net-total-time sum 244.94 s vs 245.54 s). The pre-pass removed only ~21 nets from the
main sequential loop (139 -> 118 nets), and those were the CHEAP nets -- the remaining
hard nets dominate the time identically. Net result: head step6 wall is 13:23 vs base
8:45 -- a ~53% regression, opposite of the required speedup.

Why the pre-pass saves nothing:
- The sequential Phase-3 loop already routes nets efficiently in boxed-in-risk order with
  rip-up/retry ladders. The parallel pre-pass's committed copper does not make the hard
  nets faster; it just re-routes the easy ones in a different order.
- The pre-pass's rayon threads consume ~12x user time per wall second (218 s wall shows up
  as ~550 s of user time), inflating step6 user time from 522 s to 1079 s.
- The reconcile sub-runs at end-of-run each re-run their own pre-pass (3 extra pre-passes:
  3.85 s + 0.21 s + 0.18 s), adding further overhead.

## Failure 2 -- connectivity regression

Head leaves MUX_A0N (net 344) disconnected: CAC2 pad 2 at (82.78, 99.50) on F.Cu has no
copper path (Segments: 0, Vias: 1, Pads: 2, Disconnected components: 2). Baseline connects
every net. The pre-pass's parallel-order commits change which corridors are taken; the
committed copper of OTHER nets walls off MUX_A0N's sequential routing, and the sequential
fallback cannot recover the pad. This is the same connectivity-regression class as v1
(CC2_PD) and v2 (GND/RTL_XO).

## What passed

- Rust unit tests: `determinism_1_vs_n_threads`, `tree_grows_coherently`,
  `congestion_cost_changes_tree` -- all pass.
- `build_router.py --from-source` succeeds; module imports at 0.23.0 with
  `NetTreeRequest` + `route_tree` exposed.
- Full test gate at baseline parity: 276 passed / 4 known env failures / 110 skipped.
- System determinism: head#1 vs head#2 identical checker counts + identical pre-pass
  activity (4 iters, 40 edges, 141/150 nets, same over-subscribed cell counts).
- Small-board smoke test (esp_prog): pre-pass resolves 11/11 nets, full connectivity,
  0 DRC -- the approach works on uncongested boards.
- check_drc: head has ZERO violations (29 same-net warnings vs base's 35 -- same-net
  warnings are permitted by KiCad DRC and are not failures).
- quality/score.py within 1.0 on all arms (57.91 / 59.16 / 59.20).

## Root cause analysis

The fundamental tension is unchanged from v2:

- **Within-net sequential dependency**: Phase-3 tap edges within a net MUST be routed
  sequentially -- each edge launches from ALL prior edges' copper via
  `get_all_segment_tap_points`. v2.1 fixes this by routing each net's whole tap set as one
  coherent unit inside one Rust call (the `route_tree` core change), which WORKS and is
  deterministic.
- **Cross-net parallel negotiation**: PathFinder requires all nets' connections to be
  routable in parallel against a shared cost map with NO hard commits until convergence.
  But committing clean copper immediately (greedy, per-edge or per-net) blocks later nets'
  sequential routing -> slower sequential phase3 + connectivity loss.

v2.1's net-level granularity fixes the WITHIN-net fragmentation (v2's failure), but does
not fix the CROSS-net problem: the parallel pre-pass's committed copper still changes the
obstacle landscape for the remaining sequential nets, and the pre-pass overhead (~218 s)
exceeds any sequential savings (~0 s). The claim-counting on expanded capsule footprints
(which fixed v2's DRC violations -- head has ZERO violations vs v2's ~85) is correct but
conservative, so few edges resolve per iteration and the pre-pass grinds.

## Files changed (uncommitted)

- `rust_router/src/negotiated.rs` -- new rayon parallel multi-connection negotiated core:
  `NetTreeRequest` pyclass + `route_tree` pyfunction + `route_one_tree` (whole-net tree
  routing against a frozen clone) + determinism unit tests.
- `rust_router/src/obstacle_map.rs` -- Rust-only `remove_blocked_cells_plain` /
  `remove_blocked_vias_plain`.
- `rust_router/src/lib.rs` -- register negotiated module.
- `rust_router/Cargo.toml` -- version 0.23.0, rayon dep.
- `rust_router/README.md` -- version history entry.
- `/VERSION`, `metadata.json` -- version triple to 0.23.0.
- `py_router/negotiated_routing.py` -- new orchestrator (per-edge commit within coherent
  trees, expanded-capsule claim counting, fail-fast full-search cap, drop-hopeless-nets).
- `py_router/route.py`, `py_router/phase3_routing.py`, `py_router/env_knobs.py` --
  `negotiated_congestion` kwarg + KICAD_NEGOTIATED_CONGESTION env knob.

## Co-session fence

An SI-enforcement session is concurrently working python-side in the batch_route
config/cost-stamping path. At findings time, `git status` shows only this session's
changes; `py_router/si_classes.py`, `quality/`, `py_router/pcb_modification.py`, and
`py_router/beautify_jog.py` are untouched. This session's wiring lives in its own new
module (`py_router/negotiated_routing.py`) plus the three integration touchpoints
(`route.py`, `phase3_routing.py`, `env_knobs.py`) -- if the SI session later edits those
three, reconcile the `negotiated_congestion` kwarg + KICAD_NEGOTIATED_CONGESTION knob
against its changes before any merge.

## Recommendation

Do not merge as-is. The net-level granularity fix is sound and worth keeping as machinery,
but the negotiated pre-pass does not deliver speedup on this board because (1) the parallel
pre-pass's committed copper does not reduce the hard nets' sequential time, and (2) it adds
~218 s of overhead while changing routing order enough to cause a real connectivity
regression (MUX_A0N). Any follow-up should either find a way for the parallel pre-pass to
resolve the HARD nets (not just the easy ones), or accept that sequential Phase-3's
boxed-in-risk ordering with rip-up is already near-optimal for this board class and target
boards where multipoint nets are genuinely independent.
