# Lane A — Adaptive Fruitless Cap (Rate-of-Improvement Rule) — Findings

Date: 2026-08-28
Branch: optimize-via-protection-parse
Commit under test: HEAD + this change set (crate 0.25.0)
Board: carrier step-6 bulk route (route.py), input = /tmp/si_tune2_tuned2/routed_d2.kicad_pcb
(the d2 board from the tune2 chain, exactly what ab_chain_v2.sh produces at step 5).

## What was done

C2 (c2_ordering_findings.md) found the fixed-fraction fruitless cap (fruitless_pct)
and the hard CEILING=600000 both fragile: CEILING saved -26s on carrier but cost
+12s/+5.1M iters on kitdev via premature rip-up-ladder fallback. Its PRONG-2
recommendation: replace the fixed-fraction check with a RATE-OF-IMPROVEMENT rule —
a search keeps earning iteration tranches while best_h is still improving at a
sufficient rate per tranche; a wall-contouring search whose best_h has stalled for
N consecutive tranches stops earning and falls back to the rip-up ladder.

This change set implements that rule in the Rust search core:

- **Rust** (rust_router/src/router.rs, crate 0.25.0): `rate_window` / `rate_cells` /
  `rate_pct` parameters on `route_multi` / `route_with_frontier` (defaults 0/0.0/0.0
  = disabled, byte-identical to prior behavior). Once `rate_window` tranches have
  been granted, extension is denied unless best_h has dropped by at least
  max(`rate_cells` grid cells, `rate_pct`% of initial_h) over that window.
  Calibration-only trace: `GRID_ROUTER_TRACE=1` emits one line per tranche
  grant/denial to stderr (never affects search state).
- **Python** (py_router/env_knobs.py, single_ended_routing.py): env knobs
  KICAD_DYNAMIC_ITERATIONS_RATE_WINDOW / _RATE_CELLS / _RATE_PCT, default OFF,
  wired into _dynamic_iterations.
- **Tests**: 3 Rust unit tests (spares genuine detour, stops fruitless creep,
  determinism) — all pass.

## Calibration method

GRID_ROUTER_TRACE on both boards captured every tranche grant/denial with best_h,
initial_h, and tranche count. Per-search trajectories were analyzed to find the
distinguishing signal between carrier's grinders (the searches CEILING=600000 cut)
and genuine long detours.

## The decisive data

Carrier's grinders (the 4-tranche searches CEILING cut) decay best_h at ~1.6%/tranche
(45.8% -> 42.3% -> 41.1% of initial_h over tranches 1-3, then plunge to 2.9% at
tranche 4 = completion). Kitdev's genuine completers decay at ~7-15%/tranche early.
But a synthetic genuine detour (serpentine maze) decays at ~4%/tranche — BETWEEN the
two. The distributions overlap; no rate threshold separates them cleanly.

## A/B table (carrier step-6, KICAD_SI_ADAPTIVE=0 pinned to isolate lane B)

| Config | user s | net iters | failed_multipoint | open_single | rate denials |
|---|---|---|---|---|---|
| OFF (baseline) | 459.30 | 32,435,776 | IPAD_DM | [] | — |
| ON w3 p5 (grant-point) | ~460 | 32,435,776 | IPAD_DM | [] | 1 (no effect) |
| ON w2 p2 (mid-tranche) | ~460 | **30,816,112** | RTL_RXN, RTL_TXP, UART0_TX, IPAD_DM, RTL_1V0 | **RTL_RXN** | 10 |

(All arms KICAD_SI_ADAPTIVE=0; the w3 p5 timing was load-inflated in one run, the
iteration counts are the robust metric.)

### Reading the table

- **The grant-point rule is inert on carrier.** At w3 p5 it fires once on a search
  that was already at 5.4% of initial_h (nearly complete); the rip-up reconnects it
  and the outcome is byte-identical to OFF (32,435,776 iters, IPAD_DM only).
- **The mid-tranche rule CAN reproduce CEILING's iteration savings** (w2 p2:
  30,816,112 iters — exactly C2's cap2 arm) **but ships RTL_RXN broken**
  (open_single). The rip-up ladder cannot recover the cut searches. This is the
  kitdev failure mode, now on carrier: cutting genuine completers mid-flight breaks
  nets.
- **There is no threshold that both saves iterations AND preserves connectivity.**
  At a safe threshold the rule is inert; at an effective threshold it breaks nets.

## Why the rule cannot work

A search that rides many tranches either (a) completes — its best_h keeps dropping
and eventually plunges to the target — or (b) stalls — best_h stops dropping and the
existing #529 grace mechanism denies it. The "slow creep that neither completes nor
stalls" pattern the rate rule targets does not occur on carrier or kitdev. The
searches C2's CEILING cut are type (a): genuine completers whose slow phase is only
3 tranches long. By the time a rate window fills (3 tranches), they have already
plunged — so a grant-point check never sees the slow phase, and a mid-tranche check
that does see it also catches genuine detours with similar decay rates.

## Verdict

**Fragile — knobs stay default OFF.** The rate-of-improvement rule is a correct
implementation of the task's spec but cannot separate carrier's grinders from
genuine long detours: their best_h decay profiles overlap, and cutting the grinders
mid-flight breaks nets the rip-up ladder cannot recover (RTL_RXN on carrier). This
extends C2's verdict: no per-search best_h signal predicts whether cutting a slow
search is safe, because the cost of the rip-up fallback is board-dependent.

The code stays in the tree (env-knob gated, default OFF) so the hypothesis remains
A/B-able, but it is NOT defaulted ON.

## Gates

- Gate 1 (carrier A/B): FAIL — no iteration/time win at a connectivity-safe
  threshold; the effective threshold breaks RTL_RXN.
- Gate 2 (kitdev A/B): N/A — the rule is inert on kitdev (identical iterations,
  within +2% trivially), but there is no win to claim.
- Gate 3 (third board): N/A — no positive result to confirm directionally.
- Gate 4 (twice-run determinism): PASS — OFF and ON configs each reproduce exactly
  (32,435,776 and 33,493,851 iters across two runs; the delta is lane B's
  SI_ADAPTIVE default-ON change in the shared working tree, not this change set).
- Gate 5 (full suite): PASS — 276 passed / 4 failed / 110 skipped, exact baseline
  parity (the 4 failures reproduce identically on clean HEAD).
- Gate 6 (self-containment): PASS — crate 0.25.0 triple aligned; default path
  byte-identical to 0.24.0 (verified: OFF with SI_ADAPTIVE=0 = 32,435,776 iters,
  failed: 1).

## Files changed

- rust_router/src/router.rs — rate_window/rate_cells/rate_pct in GridSearch +
  route_multi / route_with_frontier signatures + GRID_ROUTER_TRACE hook + unit tests.
- rust_router/Cargo.toml / Cargo.lock / README.md, VERSION, metadata.json — crate
  0.25.0 bump documenting the rate rule.
- py_router/env_knobs.py — DYNAMIC_ITERATIONS_RATE_WINDOW/CELLS/PCT knobs (default OFF).
- py_router/single_ended_routing.py — rate kwargs wiring into _dynamic_iterations.
- carrier_lab/adaptive_cap_findings.md — this file.
- carrier_lab/analyze_trace_any.py — trace analyzer kept as a calibration tool.

## Coordination note

Lane B's uncommitted SI_ADAPTIVE change (py_router/si_enforce.py, default ON) landed
in the shared working tree mid-session and contaminated early A/B runs (32.4M ->
33.5M iters). All final A/B arms pin KICAD_SI_ADAPTIVE=0 to isolate this change set.
