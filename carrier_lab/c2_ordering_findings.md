# C2 — Phase-3 Span-First Ordering + Fruitless-Iteration Cap — Findings

Date: 2026-08-27
Branch: optimize-via-protection-parse
Commit under test: 9e41f0c6 (C1 pre-window) + this change set
Board: carrier step-6 bulk route (route.py), input = /tmp/si_tune2_tuned2/routed_d2.kicad_pcb
(the d2 board from the tune2 chain, exactly what ab_chain_v2.sh produces at step 5).

## What was done

C2 from bulk_profile_findings.md: Phase-3 tap routing's A* search is ~250 s of the
bulk step, dominated by the power/ground trunks (GNDA, +3V3, GND, TC8_N, VBUS,
TC7_AP = 57% of Phase-3 time) that route LAST into a full board and burn 4M+
iterations each. The findings proposed two python-side levers before any Rust change:

1. **Span-first Phase-3 ordering** (KICAD_PHASE3_SPAN_FIRST=1): composite score =
   boxed-in-risk + alpha*log(1+span) + beta*log(1+pads), promoting long-span /
   high-pad-count multipoint nets earlier so the power trunks route while the board
   is emptier. Default OFF.
2. **Fruitless-iteration cap** (KICAD_DYNAMIC_ITERATIONS_CEILING + KICAD_DYNAMIC_
   ITERATIONS_FRUITLESS_PCT): bound the #529 dynamic-iteration extension so a search
   contouring a wall falls back to the rip-up ladder sooner instead of riding to the
   1e7 ceiling. Default OFF (ceiling stays 1e7, fruitless 0.0).

Both are env-knob gated (no CLI flag), so the A/B harness toggles them without a
crate bump. The fruitless cap required a Rust change (the check lives in the search
core); the ordering is python-only.

## A/B table (carrier board, step6 = route.py bulk)

| Arm | Knobs | user s | iter_sum | net time s | connected | DRC @0.1 | score |
|---|---|---|---|---|---|---|---|
| base2 | none | 454.95 | 32,435,776 | 265.01 | ALL CONNECTED | OK (16 warn) | 58.33 |
| cap2 | CEILING=600000 | 431.53 | 30,816,112 | 232.70 | ALL CONNECTED | OK (17 warn) | 58.35 |
| fp8 | FRUITLESS=0.8 | 451.86 | 32,435,776 | 263.17 | ALL CONNECTED | OK (16 warn) | 58.33 |
| combo2 | CEILING+FRUITLESS | 428.98 | 30,816,112 | 228.69 | ALL CONNECTED | OK (17 warn) | 58.35 |
| head | SPAN_FIRST=1 | 563.03 | 39,778,305 | 396.14 | **FAIL: CC2_PD** | OK (26 warn) | 58.34 |

(combo = first run, 426.46 s; combo2 = confirmation run, 428.98 s — consistent.)

### Reading the table

- **The CEILING is the effective lever.** cap2 cuts 1.62M iterations and ~32 s of
  net time vs base2; combo2 matches cap2's iteration count exactly (30,816,112).
- **The fruitless cap alone is nearly inert on carrier.** fp8's iteration sum is
  IDENTICAL to base2 (32,435,776) and its net time is only ~2 s lower. The fruitless
  check only fires when a search would extend past its first tranche without
  approaching — which the carrier main run rarely does.
- **Span-first ordering is actively harmful on carrier.** head burns +7.3M iterations
  and +131 s of net time vs base2, AND fails check_connected.

## Gate 1 — connectivity (order-perturbation hazard)

The head arm (span-first ordering) FAILS check_connected:

    Disconnected components: 2
    Disconnected pads:
      (52.30, 107.95) on F.Cu [J2]

This is CC2_PD — the EXACT same signature as the rustcore parallel-routing failure
documented in rustcore_findings.md (which also disconnected CC2_PD at (52.30,107.95)
F.Cu [J2]). Reordering Phase-3 nets changes the obstacle landscape for interacting
nets; a different path for an earlier net blocks a later one. This is a hard gate:
**span-first ordering must NOT default ON.** The combo/combo2 arms did NOT use
span-first (their Phase-3 order line reads "(boxed-in risk first)", identical to
baseline) — the -27 s win is from the CEILING alone.

## Gate 2 — second-board A/B (robustness check)

Per the "a two-board result is not a default change" rule, the combined arm
(CEILING=600000 + FRUITLESS=0.8) was run on a second corpus board: kit-dev-coldfire-
xilinx_5213 (kitdev), single-step route.py bulk (input = the chain2 off_v board,
--clearance 0.09). Both arms on a quiet machine (C3's chains finished).

| Metric | kitdev base2 (clean) | kitdev combo | Verdict |
|---|---|---|---|
| user s | 140.04 | 152.08 | **+12.04 s SLOWER** |
| iter_sum | 14,236,193 | 19,354,092 | **+5.1M MORE** |
| net time s | 35.54 | 49.61 | **+14.07 s MORE** |
| check_connected | ALL CONNECTED | ALL CONNECTED | equal |
| check_drc @0.09 | 1 violation (EXIT=1) | 0 violations (EXIT=0) | combo better |
| score | 62.65 | 62.61 | delta 0.04 |

(Note: the first kitdev baseline run at 168.91 s ran concurrently with C3's chain and
was load-inflated; the clean re-run at 140.04 s is the fair comparison.)

### Why kitdev regresses

On kitdev the combined arm burns +5.1M iterations and +14 s of net time. The
mechanism: capping dynamic extension at 600k makes searches give up earlier and fall
back to the rip-up ladder; on this board that triggers MORE rip-up/retry cycles (70
vs 52 retry events) and more reconciliation laps (+3.3V and GND both fail in the
reconciliation subset vs GND only on baseline). The run-scope total_iterations is
identical (11,598,423) — the extra burn is in the dynamic-extension tranches and the
reconciliation retries, which the per-net "Net total time" lines capture.

The DRC improvement on kitdev (1 -> 0 violations) is a side effect of the different
routing paths, not a reliable benefit.

## Verdict

**Fragile — knobs stay default OFF.**

- The combined arm helps carrier (-6%, -26 s) but hurts kitdev (+8.6%, +12 s). The
  direction is board-dependent: on carrier the cap prevents wasteful grinding on the
  power trunks routing last into a full board; on kitdev it causes premature fallback
  that triggers more rip-up/retry cycles.
- Span-first ordering fails connectivity on carrier (CC2_PD) — a hard gate.
- The fruitless cap alone is nearly inert on carrier (identical iteration count).

The code stays in the tree (env-knob gated, default OFF) so the hypothesis remains
A/B-able, but it is NOT defaulted ON.

## Files changed

- py_router/env_knobs.py — C2 knobs (PHASE3_SPAN_FIRST/ALPHA/BETA,
  DYNAMIC_ITERATIONS_CEILING, DYNAMIC_ITERATIONS_FRUITLESS_PCT), all default OFF.
- py_router/phase3_routing.py — span-first ordering path (gated).
- py_router/single_ended_routing.py — fruitless_pct wiring into _dynamic_iterations.
- rust_router/src/router.rs — fruitless_pct in GridSearch + route_multi /
  route_with_frontier signatures + unit tests.
- rust_router/Cargo.toml / Cargo.lock / README.md, VERSION, metadata.json — crate
  0.24.0 bump documenting the fruitless cap.
- carrier_lab/c2_ordering_findings.md — this file.

## Gates

- Full suite: tests/run_all.py --fast = **276 passed / 4 failed / 110 skipped** —
  exact baseline parity (the 4 failures reproduce identically on clean HEAD:
  test_connection_width_grading, test_exact_clusters, test_plane_score,
  test_run8_locked_contact).
- Rust unit tests: cargo test --release fruitless = **2 passed / 0 failed**
  (fruitless_cap_stops_earlier, fruitless_cap_deterministic).
- No git push.
