# Writer-gap fixes: gate results (2026-09-01)

Scope: the two hw12 glasgow DRC violations from hw_sweep_findings.md finding 5
(#468 doctrine: a writer that can create overlap must exact-check vs foreign
copper and DECLINE). The fixes themselves live in the working-tree diff to
`py_router/pcb_modification.py` (rounded-rect pad model for the via nudge's
shortfall estimate) and `py_router/single_ended_routing.py`
(`same_net_inprogress_vias` threading into the #189 in-pad unblock placer);
regression tests `tests/test_nudge_circle_pad_graze.py` and
`tests/test_same_net_unblock_via_drill.py`.

NOTE on concurrency: two agent sessions worked this task in the same checkout.
The other session ran stash-window isolation controls (`git stash push
py_router/... && route ... && git stash pop`), which silently swaps the engine
under anything launched mid-window. Every gate below was therefore run from a
dedicated worktree pinned to HEAD (a09f3606) + the fix diff
(`git stash show -p` applied), with `grid_router.so` 0.27.0 copied in — immune
to the stash cycling. One earlier carrier-chain launch raced a stash window and
was discarded.

## Gate 1 — hw12 glasgow chain (fixed replay, carrier_lab/hw_sweep_fixed/)

- DRC: **2 -> 0** (both named violations gone: Pad:/FLAGC<->Via:/CLKREF and the
  /~{ALERT} same-net drill pair). NO DRC VIOLATIONS FOUND.
- Connectivity: 3 issues + 1 unrouted vs orig 2 + 1 (**+1**, different net sets
  entirely — generic outcome drift, within the documented ±2-3/board replay
  spread; both arms share the C33-area issue and QB6).
- Per-board fixed-vs-orig (conn/unr/drc): interf_u, kitdev, sonde_u, tigard,
  watchy identical 0/0/0; haasoscope 3/0/0 vs 2/0/0; ulx3s **6/0/1 vs 2/0/0**
  (see residuals). Orig-arm caveat: the 8/31 sweep ran pre-baf7c3e6, so the
  delta bundles the oracle-finalize commit with the fixes; the other session's
  isolation controls (/tmp/ulx3s_iso*) attribute that.

## Gate 2 — carrier chain (ab_chain_v2.sh from the fixed worktree,
outputs preserved at carrier_lab/vinprot_fix/carrier/)

- **The VIN_PROT drill-hole is GONE** (the short_hunt_findings follow-up item).
- **ALL NETS FULLY CONNECTED** (invariant holds; improvement gate verdict
  "accept", 3 disconnected pads left vs 1059 before reconciliation ran).
- DRC: 2 (short-hunt baseline was 1):
  1. +3V3 same-net soft joint In2.Cu at (99.2,25.5) — the #722 family; came and
     went across prior runs, position drifts; pre-existing class.
  2. Via:GNDA<->Via:GNDA drill pair (138.10,81.70)/(137.90,81.80) — NEW
     residual, see below.

## Residuals — same #468 family, DIFFERENT writers (log-attributed)

The two fixed writers held: **zero UNBLOCK events fired in the whole carrier
step-6 log**, so neither residual is a failure of the shipped fixes.

1. **Stub layer switch** (ulx3s SDRAM_D15, fixed-arm n board, vias
   (143.70,86.50)/(143.68,86.60)): `switch_boxed_stub_near` /
   `apply_stub_layer_switch` validate the switch via against FOREIGN copper
   (`via_barrel_clear_of_foreign_copper`) but never against same-net drill
   holes. Log line: "Stub layer switch: SDRAM_D15 stub at (143.70,86.50)
   In1.Cu -> In2.Cu".
2. **Ordinary tap-edge via conversion** (carrier GNDA pair): two separately
   routed MST tap edges of the same multipoint net each dropped a layer-change
   via ~0.22mm apart (log: repeated 0.80mm edge pad 33 -> pad 29,
   target (137.90,81.84)). The A* grid does not price same-net drill holes —
   same-net copper is deliberately not an obstacle, but KiCad's hole-to-hole
   floor is net-independent.

Both need the same doctrine treatment as the shipped fixes; neither was in this
task's scope (the task named the two glasgow writers).

## Suite (fixed worktree)

- `tests/run_all.py --fast`: 322 passed, 4 failed, 131 skipped
  (carrier_lab/vinprot_fix/suite_fast.log). All 4 failures
  (test_connection_width_grading, test_exact_clusters, test_plane_score,
  test_si_classes) reproduce IDENTICALLY on a clean-HEAD control worktree —
  pre-existing branch reds, not caused by the fixes.
- test_703 passes with the regenerated literals (uncommitted diff).
- Both new regression tests pass, including their fail-on-pre-fix legs;
  test_gnd_return_via_clear 9/9, test_rescue_escape_short_guard 5/5.

## Verdict for the hw-default promotion question

The two named writer gaps are fixed and gated: glasgow hw12 grades DRC 0 and
the carrier VIN_PROT item clears. What now stands between hw 1.2-1.5 and the
default is (a) the ulx3s attribution the other session is measuring, and
(b) the two remaining same-net-drill writers above, which deeper search will
keep tripping (the hw12 arm trips them more often, same as finding 5 said).
