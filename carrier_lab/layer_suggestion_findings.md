# Layer-suggestion seam (Phase B 3.3) -- A/B findings

Date: 2026-08-31
Branch: optimize-via-protection-parse
Commit under test: ebd91bcc (HEAD at run time; both arms on the SAME commit)
Board: helisync-carrier step-6 bulk route (route.py), input = carrier_lab/ab2_base/routed_d2.kicad_pcb
(the d2 board from the ab2 chain, exactly what ab_chain_v2.sh produces at step 5).

## What was done

Implemented the layer-suggestion seam from planning_lab/phase_b_design.md section 3.3:
feed the plan's preferred layer into the detailed router as a SOFT per-net layer-cost
bias. The Phase B multi-layer planner assigns each net a preferred layer (its planned
path's majority layer); the router taxes every OTHER layer by a multiplier
(KICAD_LAYER_SUGGEST_TAX, default 1.3 = 30% more expensive to leave the suggested
layer), so a net vias out only when its corridor on the suggested layer is genuinely
exhausted.

Design constraints honored (the ordering experiments' failure mode):
  * NEVER a hard constraint -- nothing forbidden, nothing cheaper.
  * NEVER an ordering change -- net order untouched.
  * Soft by construction: the router still defects for real obstacle-cost reasons.

Mechanism: per-net layer_costs multipliers via a replace() clone of the routing
config, consumed by GridRouter.layer_costs -- the exact #589 plan_layer_config /
#658 power_layer_config pattern. No Rust change, no search-region change.

Files:
  - py_router/global_planner/layer_suggestion.py -- new module (planner_layer_prefs,
    apply_layer_suggestion)
  - py_router/env_knobs.py -- KICAD_LAYER_SUGGEST / _TAX / _DEBUG (default OFF)
  - py_router/route.py -- pref-map computation (top-level only, final_reconcile gate)
  - py_router/single_ended_loop.py -- per-net application in the SE loop
  - py_router/single_ended_routing.py -- per-net application in route_multipoint_taps
  - tests/test_layer_suggestion.py -- 7 unit tests

## Gate 1 -- connectivity (ABSOLUTE)

| Arm | check_connected | Disconnected nets |
|---|---|---|
| OFF | MUX_A0P open (HEAD-level, see note) | MUX_A0P |
| ON #1 | **IPAD_DM open (NEW)** | IPAD_DM |
| ON #2 (repro) | **IPAD_DP + MUX_A0N open (NEW)** | IPAD_DP, MUX_A0N |

**FAIL.** The bias perturbs the obstacle landscape enough to strand nets that OFF
connects. The failing net is NOT deterministic (IPAD_DM vs IPAD_DP+MUX_A0N) -- the
classic order-perturbation signature seen in every prior ordering experiment. This is
the exact failure mode the task warned about.

Note: OFF itself has MUX_A0P open on current HEAD (the historical ab2_base was ALL
CONNECTED on an older commit; the A* speedup commits 2706f0e2/203385d3 changed
routing slightly). This is a HEAD-level behavior, NOT caused by this change -- OFF has
zero layer-suggest activity (verified: 0 'Layer suggestion' lines in the OFF log).

## Gate 2 -- vias/net and layer_direction (THE POINT)

Carrier (step-6 bulk):

| Metric | OFF | ON #1 | ON #2 | Verdict |
|---|---|---|---|---|
| vias total | 1292 | 1312 | 1326 | **UP** |
| vias/net (raw) | 4.85 | 4.91 | 4.98 | **UP** |
| vias sub-score | 19.8 | 19.4 | 19.0 | **DOWN** |
| layer_direction sub | 61.8 | 61.0 | 60.7 | **DOWN** |
| final score | 59.95 | 60.28 | 60.09 | ~equal |
| thrash vias (same-layer-pad nets spanning >1 layer) | 897 | 903 | -- | **UP** |

**FAIL.** The bias does NOT reduce vias -- it consistently INCREASES them. The plan's
layer choices are overwhelmingly F.Cu (242/269 = 90% on the pre-fidelity planner;
261/269 = 97% after the co-session's fidelity term landed), so taxing non-F.Cu layers
just makes the router fight itself to reach the B.Cu/inner layers it genuinely needs
for escape. The thrash-via count (the target of this seam) goes UP, not down.

## Gate 3 -- other sub-scores and DRC

Carrier DRC @0.1: OFF = 2 violations (+3V3 soft joint, VIN_PROT via drill);
ON = 0 violations. **DRC better ON** (a side effect of different paths, not a
reliable benefit -- same pattern as the C2 findings).

kitdev DRC: OFF = 1 violation, ON = 1 violation (equal).
ulx3s DRC: OFF = 0, ON = 0 (equal).

No other sub-score regresses >2 on any board (largest: carrier pad_entry +3.0 ON#1,
ulx3s pad_entry +4.8 ON -- both IMPROVEMENTS; the regressions are all in vias and
layer_direction).

## Gate 4 -- timing (honest)

| Board | OFF user s | ON user s | OFF net-time sum s | ON net-time sum s |
|---|---|---|---|---|
| carrier | 361.62 | 462.84 (+28%) | 174.3 | 232.6 (+33%) |
| kitdev | 178.35 | 181.61 (+2%) | 49.5 | 50.8 (+3%) |
| ulx3s | 600.52 | 601.75 (+0.2%) | 75.2 | 165.2 (+120%) |

**FAIL on carrier.** The bias makes the router work harder (more iterations searching
around taxed layers). Carrier iterations: OFF 129.7M vs ON 139.8M (+7.7%). The
slowdown is real (net-time sum, load-independent), not just wall-clock noise.

## Corpus A/B (gate: judge on >=3 boards, paired and directional)

| Board | OFF vias/net | ON vias/net | OFF layer_dir sub | ON layer_dir sub | OFF final | ON final |
|---|---|---|---|---|---|---|
| carrier | 4.85 | 4.91-4.98 | 61.8 | 60.7-61.0 | 59.95 | 60.09-60.28 |
| kitdev | 2.42 | 2.44 | 60.1 | 59.3 | 63.59 | 63.39 |
| ulx3s | 2.73 | 2.83 | 72.0 | 72.2 | 58.95 | 60.74 |

**vias/net goes UP on all three boards** (the primary gate). layer_direction is DOWN
on carrier+kitdev, ~flat on ulx3s. Connectivity is broken on carrier (the absolute
gate). ulx3s connectivity is MIXED (ON fixes 6 of OFF's 8 disconnected nets but
introduces SW3 + 4 unrouted) -- a wash, not a win.

## Verdict

**Fragile -- knob stays default OFF.**

The layer-suggestion seam, implemented exactly as designed (soft per-net layer-cost
tax, no ordering change, no hard constraint), FAILS every primary gate:
  1. Connectivity breaks on carrier (IPAD_DM / IPAD_DP+MUX_A0N) -- reproducible.
  2. vias/net goes UP on all three boards, never down.
  3. layer_direction goes DOWN on carrier+kitdev.
  4. Timing regresses on carrier (+28% user, +33% core).

Root cause: the plan's layer choices are NOT trustworthy enough to bias the router.
The co-session's phase_b_findings.md measured this directly -- per-net majority-layer
agreement with reality is 1-16% once the trivial F.Cu majority is removed, and the
plan keeps ~93% of nets on F.Cu regardless of reality. On this board the router
genuinely needs B.Cu/inner layers for escape; taxing them makes routing worse, not
better. The fidelity term added by ebd91bcc makes the plan EVEN more F.Cu-heavy
(261/269), so this negative would only be worse with the current planner.

The code stays in the tree (env-knob gated, default OFF) so the hypothesis remains
A/B-able, but it is NOT defaulted ON.

## What would make this work (for a future attempt)

The seam itself is sound (soft per-net layer costs, proven pattern). The problem is
the SOURCE of the preferred layer. A trustworthy source would need:
  * Layer choice learned from routed boards (the co-session's proposed fix), not
    from a congestion-only planner that funnels to F.Cu.
  * Explicit plane/pour modeling -- the carrier's B.Cu dominance is plane-driven.
  * Geometric via placement at real pad-escape points.


## Ground-truth control arm + response curve (added after co-session heads-up)

Heads-up from the planner-validation session: the plan reproduces the right overall
layer SPREAD but gets ~2/3 of INDIVIDUAL per-net assignments wrong (rp2350: 31%
per-net agreement, 6.26 predicted vs 0.97 actual vias). To separate "layer bias as
a mechanism does not help" from "the mechanism works but the plan's picks are
bad", I added a CONTROL arm that biases the router toward the GROUND-TRUTH layer
assignment (the layer each net actually routed on in ab2_base/routed_routed.kicad_pcb)
via KICAD_LAYER_SUGGEST_SOURCE=ground_truth + KICAD_LAYER_SUGGEST_GT=<routed board>.

### Response curve (carrier step-6 bulk, all arms on the same commit)

| Arm | Source | Tax | vias | vias/net | connectivity | final |
|---|---|---|---|---|---|---|
| OFF | -- | -- | 1292 | 4.85 | MUX_A0P (HEAD-level) | 59.95 |
| ON #1 | plan | 1.3 | 1312 | 4.91 | IPAD_DM (NEW) | 60.28 |
| ON #2 | plan | 1.3 | 1326 | 4.98 | IPAD_DP+MUX_A0N (NEW) | 60.09 |
| GT | ground_truth | 1.3 | 1305 | 4.89 | IPAD_DP+RTL_XO (NEW) | 59.64 |
| GT tax1.1 | ground_truth | 1.1 | **1283** | **4.82** | MUX_A0P only (OK) | **60.41** |
| GT tax2.0 | ground_truth | 2.0 | 1338 | 5.01 | RTL_XI+RTL_XO (NEW) | 60.09 |
| PLAN tax1.1 | plan | 1.1 | 1296 | 4.88 | MUX_A0N+MUX_A0P (NEW) | 60.65 |

### What the curve says

1. **The mechanism CAN help, but only with perfect layer info AND very weak bias.**
   The ground-truth arm at tax 1.1 is the ONLY arm that reduces vias below OFF
   (4.82 vs 4.85, -0.6%), holds connectivity (only the HEAD-level MUX_A0P), and
   improves final score (60.41 vs 59.95) and DRC (1 vs 2 violations).
2. **The benefit is marginal and fragile.** -0.6% vias at tax 1.1; at tax 1.3 the
   same ground-truth source already goes UP (4.89) and breaks connectivity. There is
   no robust operating point.
3. **The plan's picks negate even the weak-bias benefit.** The plan arm at tax 1.1
   gives vias 4.88 (above OFF) and breaks connectivity (MUX_A0N NEW). The plan's
   per-net layer choices are bad enough that biasing toward them never helps.
4. **Timing regresses even on the winning arm.** GT tax1.1: user 427.44s / net-sum
   197.8s vs OFF 361.62s / 174.3s (+18% / +13%). The marginal vias win costs real
   time.

### Verdict (updated)

**Fragile -- knob stays default OFF.** The seam is proven worth keeping as a HARNESS
(the per-net preferred-layer map + soft per-layer cost bias + source/strength knobs
let us test ANY layer oracle later), but neither the plan source nor even the
GROUND-TRUTH source delivers a robust win. The verdict is NOT "fix the planner"
-- it is "layer bias as a mechanism does not help enough to justify its cost".
The plan's layer picks are bad (co-session: 31% per-net agreement), but even perfect
picks only buy -0.6% vias at a fragile weak-bias operating point.

## Gates

- Full suite: tests/run_all.py --fast = **321 passed / 3 failed / 131 skipped** --
  exact baseline parity (the same 3 failures reproduce on clean HEAD:
  test_connection_width_grading, test_exact_clusters, test_plane_score).
- New unit tests: tests/test_layer_suggestion.py = **8 passed / 0 failed**.
- No git push.
