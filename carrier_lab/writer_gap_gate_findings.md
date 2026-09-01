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

---

# Residual-writer fixes: gate results (2026-09-01, second pass)

Scope: the two residual writers above. Fixes developed and gated from a
worktree pinned to a1492506 + grid_router.so 0.27.0 (immune to the stash
cycling; the earlier session's fixes are IN that commit). Diff:
`py_router/stub_layer_switching.py` (+53), `py_router/single_ended_routing.py`
(+116); regression tests `tests/test_stub_switch_same_net_via_drill.py` and
`tests/test_same_net_via_pair_gate.py`.

## Attribution (tripwire replays, both repros deterministic)

Both repro chains were replayed with a commit-time tripwire
(same-net h2h check + stack trace at every via commit path):

- **Carrier GNDA pair — CONFIRMED writer 2, sharpened**: both vias were
  emitted by ONE `route_multipoint_taps` run inside the Phase-3 rip-reroute
  (`phase3_routing._reroute_phase3_ripped_nets` -> `add_route_to_pcb_data`,
  stack-attributed). The pair is a single tap edge's own path diving under an
  obstacle and popping back up 2 cells later — TWO layer changes in one A*
  path. No spacing exists between a path's OWN vias: the in-progress-via
  rings and `add_same_net_via_clearance` all fire only AFTER a path is
  converted, and the Rust A* has no own-path via cooldown.
- **ulx3s SDRAM_D15 pair**: reproduced at the exact coordinates with ZERO
  tripwire hits at the `add_route_to_pcb_data` / rip-restore commit sites —
  the writer appends directly to `pcb_data.vias`, which is exactly what
  `apply_stub_layer_switch` does (finding 1's attribution stands). With the
  writer-1 fix active the pair does not form (decline happens inside the
  swap validators, before any drill).

## The fixes

1. **Writer 1 (stub layer switch)** — `pad_via_drill_conflict` in
   `stub_layer_switching.py`: prices the switch pad via's HOLE against every
   existing via and drilled pad on ANY net (coincident same-net barrel
   exempt — that is the #340/#282 reuse case). Wired into `fitting_pad_via`,
   the shared fit funnel both validators and both apply paths call, so a
   conflicted switch DECLINES per the #468 doctrine.
2. **Writer 2 (grid-route emit gate)** — `_same_net_via_drill_pairs` in
   `single_ended_routing.py`: exact-checks a conversion's emitted vias
   against each other, the net's in-progress vias and its committed barrels.
   Three gates consume it:
   - Phase-3 tap loop: blocks the offending cell (`add_blocked_via`, small-
     rung mirrored, ref-counted via `_ring_cells`) and RETRIES the edge —
     the block forces the next search to space its transitions; declines
     after 2 retries. Measured on the carrier repro: fired once, retried,
     edge routed clean.
   - Phase-1 main-edge conversion: declines the edge (the `_hard_p1`
     terminal-bridge precedent); Phase 3's island machinery re-asks it.
   - Single-ended conversion: rejects the route (the #157 short-gate
     precedent); plan probes exempt (#589).
   This is the findings' "grid-level pricing" delivered reactively and
   Python-only — no Rust change, no blanket cost on every route.

## Gates (all from the pinned worktree)

- **Carrier chain** (`ab_chain_v2.sh`, fresh /tmp prefix): DRC **2 -> 0**
  (GNDA pair gone; the #722 +3V3 soft joint did not recur this run either).
  **ALL NETS FULLY CONNECTED**; improvement gate verdict "accept". The new
  tap gate fired exactly once — on the exact measured pair
  (137.90,81.80)/(138.10,81.70), 0.224 vs 0.400mm — and the retry routed.
- **ulx3s hw12 n step** (replay of v -> n with fixes): DRC **1 -> 0**
  (SDRAM_D15 pair gone). Connectivity IDENTICAL to the pre-fix replay and
  to the original findings board: same 6 issues on the same nets (GP3,
  +2V5, GN13, SD_D0, WIFI_GPIO19, GN12-family) — zero completion cost.
- **Control**: a pre-fix replay of each chain reproduced its pair at the
  exact coordinates (deterministic repro; carrier tripwire-attributed).
- `tests/run_all.py --fast` in the final state: same 4 pre-existing branch
  reds only (test_connection_width_grading, test_exact_clusters,
  test_plane_score, test_si_classes); no new failures. All four writer-gap
  regression tests pass, including fail-on-pre-fix legs.

## Still open (same family, not measured to ship violations)

- `apply_stub_layer_switch` / `apply_bare_pad_target_via` keep their
  "no fit -> ship the nominal via anyway" fallbacks; every current caller
  validates first, so the fallback is unreachable through the switch paths,
  but a NEW unvalidated caller would bypass the decline.
- Restores (`_saved_route_collides`, `_copper_conflicts`) still exempt
  same-net copper, so a partial restore could in principle re-land a via
  within h2h of an escape-stub via kept earlier. Not observed to ship in
  either gate; the cleanup-pipeline merge pass (finished below) covers the
  results-path cases.

---

# Cleanup-pipeline same-net via merge: WIP finished (2026-09-01, third pass)

The "Still open" note above said the cleanup-pipeline merge pass (a1492506)
covers the results-path cases. That was wrong: a1492506 did NOT add a merge
pass to `cleanup_pipeline.py` -- the ~25-line WIP sat uncommitted in the
working tree. Reviewed and finished:

- **Mechanism is sound** (reuse `merge_close_same_net_vias`, the plane fronts'
  same-net drill-hole merge, after the via nudge so it sees the nudged set).
- **The WIP as written was broken**: it built DICT COPIES of the vias, so the
  merge's `all_new_vias[:] = kept` only mutated the local copy list. The merge
  DID drop the via from `pcb_data.vias` and re-anchor `pcb_data.segments`, but
  `results[].new_vias` (the write-list) still held the dropped Via object, so
  the writer re-emitted it and the DRC violation shipped anyway. Board != write
  model -- exactly the #508 divergence class.
- **Fix**: pass the REAL Via objects from `results[].new_vias` (the same
  objects `add_route_to_pcb_data` appended to `pcb_data.vias`), and after the
  merge rebind each result's `new_vias` to the kept subset. The merge now
  duck-types via access (`_via_get`: dicts for the plane fronts, Via objects
  for the cleanup pipeline) so both callers merge identically.
- **Regression test** `tests/test_cleanup_same_net_via_merge.py` pins the
  measured ulx3s hw12 SDRAM_D15 pair (143.70,86.50)/(143.68,86.60, 0.102mm vs
  0.35mm needed) and asserts the write-list drops the merged-away via. Fails
  7/7 on pre-fix code (the merge crashes on Via objects), passes with the fix.
- `tests/run_all.py --fast`: 326 passed, same 3 pre-existing branch reds
  (test_connection_width_grading, test_exact_clusters, test_plane_score) --
  confirmed identical at clean HEAD. No new failures.
