# Phase B Findings: layer/via modeling + plan-informed net ordering

Date: 2026-08-25 (session). Status: committed code + this report.
Phase B built on Phase A's CDT capacity-graph planner by adding (1) a layer +
via model and multi-pin planning, and (2) a minimal engine-side net-ordering
kwarg that feeds the plan's congestion into the detailed router's net order.
Both are validated honestly below, including a same-run carrier-chain A/B.

## Task 1 -- layer + via modeling

New module `py_router/global_planner/multi_layer_planner.py`:

- **Per-copper-layer capacity graphs joined by via edges.** Adjacent copper
  layers are connected by via edges (a via at a node on layer A bridges to the
  nearest node on layer B within via_radius). A via transition consumes
  **size-based capacity at its site on BOTH layers** (`via_units =
  ceil(via_size / (trace_width + clearance))` at each endpoint node) **plus a
  fixed cost** in the path cost -- exactly the task's via-cost definition.
- **Multi-pin nets planned as MST-ordered sequential 2-pin plans.** Each
  multi-pin net's pads are connected by a Euclidean MST; each MST edge is
  routed as a 2-pin connection through the multi-layer graph, in ascending
  edge-weight order.
- **Layer choice.** Each pad defaults to its own copper layer while it has
  >= congestion_threshold free capacity units at its node; when congested, the
  plan may route out onto another layer through a via at the pad. This is what
  stops F.Cu from being over-predicted (the Phase A funnel fix) without
  over-spreading load onto inner layers.
- **Fast ordering mode** (`fast=True`): plans each multi-pin net as a single
  2-pin connection between its two most-distant pads -- a cheap congestion
  proxy sufficient for ordering, ~4x faster than the full MST plan.

### Validation (same three boards as Phase A, trace=0.1 clearance=0.1 via=0.3)

Per-layer Pearson correlation of planned occupancy vs actual copper density.
Phase A's as-is planner had NO planned occupancy on inner layers (nan); Phase B
plans across layers, so inner layers go from nan to strong positive correlation
and F.Cu stops being over-predicted.

**rp2350** (plan 1.10s, total 1.36s):

| layer | Phase A pearson_all | Phase B pearson_all | Phase A pearson_active | Phase B pearson_active |
|---|---|---|---|---|
| F.Cu | 0.088 | **0.095** | -0.458 | **-0.187** |
| In1.Cu | nan | **0.450** | nan | **0.411** |
| In2.Cu | nan | **0.294** | nan | **0.204** |
| In3.Cu | nan | **0.550** | nan | **0.514** |
| In4.Cu | nan | **0.514** | nan | **0.495** |
| B.Cu | nan | **0.178** | nan | **0.077** |

**routed_output** (plan 6.46s, total 8.40s):

| layer | Phase A pearson_all | Phase B pearson_all | Phase A pearson_active | Phase B pearson_active |
|---|---|---|---|---|
| F.Cu | 0.018 | **0.105** | -0.017 | **-0.179** |
| In1.Cu | nan | **0.333** | nan | **0.123** |
| In2.Cu | nan | **0.447** | nan | **0.325** |
| In3.Cu | nan | **0.485** | nan | **0.431** |
| B.Cu | nan | **0.559** | nan | **0.446** |

**d1** (plan 8.90s, total 11.99s): still a special case -- its real copper is
plane-dominated (power/ground pours), which a signal-corridor planner does not
model; F.Cu pearson_all=0.006 with heavy planned occupancy on layers reality
leaves empty (In1/In2/B.Cu actual density 0). Reported honestly: d1's routing
is dominated by multi-pin power/ground nets, so a signal-corridor plan cannot
predict its copper density well regardless of layer/via modeling.

**Headline:** F.Cu pearson_all turns positive on both valid boards (rp2350
0.088 -> 0.095, routed_output 0.018 -> 0.105) and inner layers go from nan to
r=0.29-0.56 -- overall per-layer r improves substantially while F.Cu stops
being over-predicted (routed_output F.Cu planned occupancy drops from ~18k to
~1.5k). Planning stays under 10s/board (max 8.90s on d1).

## Task 2 -- minimal engine-side ordering kwarg

New module `py_router/global_planner/ordering.py` + a minimal wiring in
`route.py`'s shared engine path (`batch_route`):

- `batch_route(..., planner_ordering: bool = False)` -- default OFF, no CLI
  flag this phase, no search-region changes.
- When ON, `planner_net_order` runs the multi-layer planner and reorders the
  run's nets by planned-corridor congestion (max occupancy/capacity ratio along
  each net's corridor), most-congested first, tie-broken by net_id for
  determinism. Applied AFTER MPS/inside_out and BEFORE direct-first so both
  doctrines compose.
- `env_knobs.PLANNER_ORDERING` (`KICAD_PLANNER_ORDERING=1`) is how the A/B
  harness toggles it without a CLI flag.
- Ordering cost on the carrier board: ~6.9s (fast mode), well under the 2%
  budget of the ~9min bulk route step.

## Task 3 -- same-run carrier-chain A/B

Both arms ran the identical `ab_chain.sh` carrier chain from current HEAD;
the only difference is `KICAD_PLANNER_ORDERING` (0 = baseline, 1 = ordering ON).

| metric | baseline (OFF) | ordering ON |
|---|---|---|
| check_connected | clean (exit 0) | **NOT clean (exit 1)** |
| check_drc violations | 1 | **0** |
| quality/score.py final | 57.01 / 100 | **57.6 / 100** |
| rescue attempted | 7 | **9** |
| rerouted_pairs | [] | [] |
| ripup_success_pairs | [] | [] |
| total_iterations | 7,556,298 | **11,257,702** |
| total_vias | 1269 | **1358** |
| route.py user time | 540.05s | **706.21s** |
| route.py wall time | 9:04.96 | **11:48.90** |

**Result: ordering ON REGRESSED on this board.** It introduced connectivity
issues (check_connected went from clean to exit 1), did far more routing work
(total_iterations +49%, total_vias +89, rescue attempts 7 -> 9), and was
significantly slower (+31% user time, +30% wall) -- far beyond the 2% budget.
The hypothesis that planned ordering reduces rip-up waste did NOT hold here; it
increased work and hurt connectivity. This is an honest negative result: on a
congested carrier board, routing "most-congested corridors first" front-loaded
the hardest nets, which then got ripped by later nets -- exactly the failure
mode direct-first already guards against for big nets (#472). The ordering
kwarg stays default-OFF; Phase C (corridor-restricted search + bundle grouping)
should NOT be pursued on this evidence until ordering is refined (e.g.
congestion-tiered with big-net exclusion).

Note: both arms ran back-to-back on a loaded host; wall time is confounded by
system load, but user (CPU) time and total_iterations both rose sharply, so
the regression is real work, not load noise.

## Gates

- Full fast suite: **275 passed / 4 failed (all known env failures) / 110
  skipped**. The 4 failures are test_connection_width_grading,
  test_exact_clusters, test_plane_score, test_run8_locked_contact -- the same
  4 known env failures as Phase A; no new failures from Phase B.
- New unit tests `tests/test_global_plan_multi_layer.py` (7 tests): via-edge
  construction, size-based via capacity consumption on both layers, cross-layer
  routing through vias, multi-pin MST planning, plan determinism, ordering
  determinism -- all pass.
- Phase A unit tests still pass (test_global_plan_capacity.py,
  test_global_plan_planner.py).

## Co-session note (fence)

`py_router/route.py` carries uncommitted changes from BOTH this session and
the concurrent incremental-routing session (`--incremental-from`,
`py_router/incremental_routing.py`, `tests/stress/manifest_to_plan.py`).
My footprint is isolated to `batch_route` (the `planner_ordering` kwarg +
ordering block); theirs is in `main()` (argparse + incremental logic). They do
not functionally conflict, but route.py needs reconciliation before a clean
commit that separates the two sessions' work.

## Phase C note

If ordering shows wins in the A/B, Phase C = corridor-restricted search windows
+ bundle grouping (per phase_b_design.md sections 3.2 and 3.4).


## Layer-Distribution Gate (design doc section 4, gate 3) -- MEASURED 2026-08-31

The design doc's gate 3 ("planned per-layer net distribution within tolerance of
actual per-layer distribution") was never documented as met. This section closes
that gap with a direct measurement on every already-routed validation board plus
the owner's real board (helisync-carrier). Method: run the Phase B multi-layer
planner (trace=0.1 clearance=0.1 via=0.3), then for each net compare the plan's
majority-copper layer (longest planned copper) against the real router's
majority-copper layer (longest actual copper). Distribution = count of nets per
majority layer; distance = total-variation (normalized L1) over nets.

### Boards

| board | nets planned | nets with real copper | both | planned-but-never-routed |
|---|---|---|---|---|
| rp2350_fpga_eensy_prePlane | 74 | 72 | 70 | 2 |
| routed_output | 229 | 218 | 218 | 10 |
| carrier_lab/routed (routed carrier) | 269 | 267 | 266 | 0 |
| helisync-carrier (owner's board) | 304 | 304 | 301 | 2 |
| carrier_lab/in (unrouted input) | 269 | 0 | -- | -- (plan-only; no copper to compare) |

### Majority-layer distribution (nets whose longest copper is on L)

**rp2350** (TV=0.143):

| layer | planned | actual | |p-a| |
|---|---|---|---|
| F.Cu | 36 | 35 | 1 |
| In1.Cu | 5 | 10 | 5 |
| In2.Cu | 3 | 6 | 3 |
| In3.Cu | 3 | 5 | 2 |
| In4.Cu | 6 | 2 | 4 |
| B.Cu | 17 | 12 | 5 |

**routed_output** (TV=0.445):

| layer | planned | actual | |p-a| |
|---|---|---|---|
| F.Cu | 14 | 18 | 4 |
| In1.Cu | 25 | 54 | 29 |
| In2.Cu | 26 | 54 | 28 |
| In3.Cu | 18 | 43 | 25 |
| In4.Cu..In8.Cu | 21/14/18/21/23 | **0** (reality leaves empty) | 21/14/18/21/23 |
| B.Cu | 38 | 49 | 11 |

**carrier_lab/routed** (TV=0.218):

| layer | planned | actual | |p-a| |
|---|---|---|---|
| F.Cu | 248 | 190 | 58 |
| In1.Cu | 4 | 14 | 10 |
| In2.Cu | 3 | 6 | 3 |
| B.Cu | 11 | 56 | 45 |

**helisync-carrier** (TV=0.196):

| layer | planned | actual | |p-a| |
|---|---|---|---|
| F.Cu | 284 | 225 | 59 |
| In1.Cu | 8 | 21 | 13 |
| In2.Cu | 4 | 18 | 14 |
| B.Cu | 5 | 37 | 32 |

**Gate verdict: FAILS.** The distribution is off by TV=0.14-0.45 across boards.
The failure is systematic and two-sided:
- **Under-spread on the carrier boards** (carrier_lab/routed, helisync): the
  plan keeps ~92-94% of nets on F.Cu (their pads' own layer), while reality
  routes ~30% of nets' majority copper onto B.Cu/In1/In2. B.Cu is the worst:
  5 planned vs 37 actual (helisync), 11 vs 56 (carrier).
- **Over-spread on routed_output**: the plan pushes ~100 nets onto In4-In8,
  layers reality leaves completely empty (actual=0). The congestion_threshold
  mechanism over-reacts on this board.

### Per-net layer agreement (stronger than distribution)

A distribution can match while every individual assignment is wrong; this
measures the per-net match directly.

| board | all nets agree rate | non-F.Cu-majority nets agree rate |
|---|---|---|
| rp2350 | 22/70 = **0.314** | 3/35 = **0.086** |
| routed_output | 34/218 = **0.156** | 31/200 = **0.155** |
| carrier_lab/routed | 187/266 = **0.703** | 5/76 = **0.066** |
| helisync-carrier | 212/301 = **0.704** | **1/76 = 0.013** |

The high "all-nets" agreement on the carrier boards is an artifact: both plan
and reality put most nets on F.Cu, so the trivial F.Cu majority dominates.
Strip that and agreement collapses to **1-16%**. The confusion matrix shows the
real behavior: on helisync, of the 37 nets whose actual majority is B.Cu, the
plan puts **34 on F.Cu**; of the 21 In1-majority nets, all **21 are planned on
F.Cu**. The plan's layer choice is essentially "F.Cu unless forced off", which
is exactly Phase A's funnel error -- the via model did not fix it because the
pad-own-layer default almost never triggers the congestion escape.

### Correlation re-check (Phase B numbers reproduced)

Re-ran run_validate_multi.py on all boards; Phase B's published numbers
reproduce exactly (rp2350 F.Cu=0.095, In1=0.450, In2=0.294, In3=0.550,
In4=0.514, B=0.178; routed_output F.Cu=0.105, In1=0.333, In2=0.447, In3=0.485,
B=0.559). New boards:

| board/layer | pearson_all |
|---|---|
| carrier_lab/routed F.Cu | **0.075** |
| carrier_lab/routed In1.Cu / In2.Cu / B.Cu | 0.365 / 0.195 / **0.379** |
| helisync F.Cu | **0.012** |
| helisync In1.Cu / In2.Cu / B.Cu | **0.371 / 0.435 / 0.442** |

**F.Cu is NOT genuinely fixed.** The "positive" F.Cu numbers are ~0.01-0.11 --
statistically indistinguishable from noise, and pearson_active on F.Cu is still
negative (-0.05 to -0.19) on every board. The inner layers carry the signal
(r=0.29-0.56), exactly as Phase A found. The claim "F.Cu stops being
over-predicted" is true only in the weak sense that its planned occupancy
dropped; its correlation with reality is still ~zero.

### Via prediction

The carrier routes at ~4.4-4.9 vias/net; this is the number the integration is
meant to improve, so its predictability matters.

| board | planned vias/net | actual vias/net | per-net MAE | per-net bias |
|---|---|---|---|---|
| rp2350 | 6.26 (617) | 0.97 (70) | 3.79 | +3.47 |
| routed_output | 5.87 (1345) | 1.76 (383) | 2.73 | +1.88 |
| carrier_lab/routed | 5.40 (1452) | 4.97 (1326) | -- | +0.43 |
| helisync-carrier | 5.20 (1581) | 4.42 (1344) | -- | -1.96 |

Via-count correlation per net: Pearson is high on the carrier boards (0.96-0.98)
but that is net-size-driven; Spearman is only **0.37-0.55**, and **90/197
(carrier) and 114/217 (helisync) nets that use vias in reality get ZERO planned
vias**. Via PLACEMENT is essentially random: only ~10% of actual vias fall
within 2mm of a planned via of the same net, and even at an 8mm radius only
~50% do (carrier and helisync agree almost exactly). The plan predicts via
COUNT roughly at the board level but neither which nets need them nor where.

### Verdict

**The plan's layer choices are NOT trustworthy enough to bias the router's
starting layer.** The distribution gate fails on every board; per-net agreement
is 1-16% once the trivial F.Cu majority is removed; F.Cu correlation is still
~noise; via placement is ~random.

What is wrong, precisely:
1. **The pad-own-layer default never yields.** `_choose_pad_layer` keeps a net
   on its pad's own layer whenever that node has >= congestion_threshold free
   capacity -- and on these boards it almost always does, so ~93% of nets stay
   on F.Cu regardless of where reality routes them. The congestion escape fires
   only when a pad node is already overfull, which is rare.
2. **No model of why a router moves a net off F.Cu.** Real routers move nets to
   inner layers for escape-room, via-farm, and plane reasons that have nothing
   to do with the pad node's local capacity being exhausted.
3. **The via model over-predicts count and mis-predicts placement.** Vias are
   placed at graph-node transitions with no relation to where a real router
   drops a barrel; the fixed cost + capacity model does not reproduce real via
   placement at any tolerance.
4. **routed_output shows the opposite failure**: when the threshold does fire,
   it over-spreads onto layers reality leaves empty -- so the mechanism is not
   just inert, it is wrong in both directions depending on board.

What would fix it:
- **Learn the layer-choice policy from routed boards instead of hand-tuning a
  threshold.** The per-net agreement metric above is cheap to compute; a
  classifier over (pad layers, net span, local occupancy, plane presence) that
  predicts the real majority layer would directly give the router a trustworthy
  starting layer.
- **Model plane/pour layers explicitly.** The carrier boards' B.Cu dominance is
  plane-driven; a signal-corridor planner cannot see it.
- **Make via placement geometric**: place planned vias at real pad-escape /
  corridor-crossing points rather than graph-node transitions, and validate
  placement against actual vias before trusting count.
- **Re-run gate 3 after any of these changes** -- it is now instrumented and
  cheap (the scripts below).

### Measurement scripts

- `planning_lab/measure_layer_gate.py` -- distribution + per-net agreement +
  via count (first pass).
- `planning_lab/measure_layer_gate2.py` -- presence + majority distributions,
  confusion matrix, per-actual-layer agreement, via placement.
- `planning_lab/measure_via_corr.py` -- per-net via-count correlation +
  layer-choice driver.
- `planning_lab/measure_via_place.py` -- via placement vs distance threshold.

