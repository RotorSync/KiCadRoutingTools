# Phase A Findings: CDT global-routing planner validation

Date: 2026-08-24 (session). Status: complete -- committed code + this report.
The point of Phase A was to measure, honestly, whether a capacity-graph planner
predicts real routed copper. It does -- on inner layers, when the layer is right
-- and it does not on F.Cu, because the single-layer model has no via/layer
choice. Both halves are reported below.

## Module name note

The task asked for a module directory `py_router/global_plan/`, but
`py_router/global_plan.py` already exists and is imported by route.py,
single_ended_routing.py, repair_planes.py, routing_context.py, etc. Creating a
`global_plan/` package shadows that module and broke 49 tests (caught by the full
gate). The new module is therefore named **`py_router/global_planner/`** -- same
content, collision-free name. This is the one deliberate deviation from the task's
directory name, forced by an existing-module collision.

## Environment choices

- scipy.spatial.Delaunay IS importable in /home/austin/eda/.venv (scipy 1.18.0,
  numpy 2.5.2). Used for the triangulation. A pure-python fallback
  (_simple_triangulation) exists and is unit-tested on a tiny fixture.
- Obstacles = pads (as corner points, radius 0, so triangle edges land in the
  real channels between pads), keepouts (tracks-blocked only), board edge
  (bounds corners + interpolated edge points).
- Capacity = floor(gap_width / (trace_width + clearance)), gap = distance
  between the two obstacle points minus their radii (0 for corners/edge).
- Clearances: d1 0.1 (pro), routed_output 0.09 (pro), rp2350 none recorded ->
  used dominant actual track width 0.09 + clearance 0.1 (noted as an assumption).

## Validation boards

| board | copper layers | 2-pin nets | 2-pin nets WITH actual copper | verdict |
|---|---|---|---|---|
| carrier_lab/d1.kicad_pcb | F,B (+2 empty inner) | 113 | **0** | INVALID for this validation |
| kicad_files/rp2350_fpga_eensy_prePlane.kicad_pcb | F,In1..In4,B | 29 | 29 | valid |
| kicad_files/routed_output.kicad_pcb | F,In1..In3,B (+6 empty) | 211 | 209 | valid |

**d1 is invalid**: all of its real routed copper belongs to multi-pin nets; none
of its 113 two-pin nets has any copper. A 2-pin-only planner cannot be validated
against it -- the planner predicts congestion for nets reality never routed. This
is itself a finding: d1's routing is dominated by multi-pin (power/ground/bus)
nets, so Phase B must plan multi-pin nets too, not just 2-pin.

## (a) Correlation: planned occupancy vs actual copper density

As-is planner (each net routed on its own chosen shared layer):

| board | layer | pearson_all | pearson_active | spearman_active |
|---|---|---|---|---|
| rp2350 | F.Cu | 0.088 | -0.458 | -0.643 |
| routed_output | F.Cu | 0.018 | -0.017 | -0.117 |

The as-is planner routes nearly every net on F.Cu (most pads are F.Cu; no via
model), so F.Cu is over-predicted and inner layers are empty in the plan while
reality spreads copper across them. Correlation on F.Cu is weak-to-negative.

Layer-matched diagnostic (route each net on its ACTUAL-copper layer -- ground
truth used only to isolate layer-assignment error from graph error):

| board | layer | pearson_all | pearson_active | spearman_active |
|---|---|---|---|---|
| rp2350 | In1.Cu | 0.423 | 0.124 | 0.125 |
| rp2350 | In2.Cu | 0.400 | 0.051 | -0.064 |
| rp2350 | B.Cu | 0.203 | -0.141 | -0.346 |
| routed_output | In1.Cu | 0.533 | 0.354 | 0.423 |
| routed_output | In2.Cu | 0.468 | 0.214 | 0.422 |
| routed_output | In3.Cu | 0.481 | 0.266 | 0.248 |
| routed_output | B.Cu | 0.582 | 0.337 | 0.460 |

**Conclusion**: when the layer is right, the capacity graph predicts congestion
well (r=0.4-0.6 on inner layers). The dominant error is layer assignment, not
the graph. Phase B must add a via/layer model.

## (b) Top-5 predicted-congested regions vs actual

For every board, the top-5 predicted-congested nodes (highest occupancy/capacity)
show **zero or near-zero actual density**:

- d1 F.Cu: all 5 predicted-congested nodes have actual=0 (no 2-pin copper exists).
- rp2350 F.Cu: nodes at (149.4,103.3) occ=2/1, (149.5,103.2) occ=2/2,
  (149.8,101.4) occ=2/2, (149.5,94.0) occ=1/1, (146.9,119.4) occ=1/1 --
  actual densities 0,0,1,0,1.
- routed_output F.Cu: nodes at (217.0,98.9), (173.8,99.7), (173.0,101.0),
  (174.2,97.8), (173.4,101.0) all occ=3/1 -- actual densities all 0.

Meanwhile the highest actual-density nodes have planned occupancy ~0 (e.g.
routed_output F.Cu node (209.3,96.0) actual=26 planned=10; rp2350 F.Cu node
(141.5,106.6) actual=12 planned=0). The planner's predicted hot spots are not
where the real copper is dense -- because the real copper is spread across
layers and the planner's F.Cu funnel is wrong.

## (c) Runtime

| board | parse+graphs | plan | validate | TOTAL |
|---|---|---|---|---|
| d1 | 3.08s | 1.17s | 0.02s | **4.27s** |
| rp2350 | 0.20s | 0.03s | 0.03s | **0.26s** |
| routed_output | 1.95s | 4.04s | 0.03s | **6.02s** |

All well under the 10s/board budget.

## Where the prediction is wrong (honest list)

1. **Layer assignment** -- the single-layer planner funnels nets to F.Cu; reality
   uses vias and inner layers. This is the #1 error and the #1 Phase B item.
2. **2-pin-only scope** -- d1 is entirely multi-pin copper; a 2-pin planner cannot
   predict it at all.
3. **Cross-layer nets skipped** -- nets whose pads are on different layers (11 of
   29 on rp2350) need a via and are not planned in Phase A.
4. **F.Cu over-prediction** -- even where the graph is right, the funnel makes F.Cu
   look congested when it is not.

## Full test gate

`tests/run_all.py --fast`: **273 passed / 5 failed / 110 skipped**.

- My changes add two passing tests (test_global_plan_capacity.py,
  test_global_plan_planner.py) and break nothing.
- The 4 known env failures are present: test_connection_width_grading,
  test_exact_clusters, test_plane_score, test_run8_locked_contact.
- The 5th failure, **test_keep_input_copper**, is NOT from my changes: it fails in
  `beautify.py` -> `clearance_field.py` (`TypeError: cannot unpack non-iterable
  NoneType`), code modified by the concurrent beautification session
  (`py_router/beautify.py` +815 lines, `py_router/cleanup_pipeline.py` modified).
  I did not touch those files (out of scope per the task's fences).

## What this means for Phase B

The capacity-graph/congestion model is sound (inner-layer r=0.4-0.6). Phase B
should carry corridors into the real router's ordering, search windows, layer
choice and bundling -- and must add a via/layer model and multi-pin planning.
See phase_b_design.md for the full design and verification gates.
