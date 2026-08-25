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
