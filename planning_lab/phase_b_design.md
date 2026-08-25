# Phase B Design: Integrating Corridors into the Real Router

Status: design proposal (Phase A prototype validated; see phase_a_findings.md)
Scope: how the Phase A capacity-graph planner becomes the "designer brain" that
steers the real router. This is a design document only -- no Phase B code ships
here.

## 1. What Phase A proved (the foundation)

Phase A built a standalone CDT (constrained-Delaunay-triangulation) capacity
graph per copper layer and a congestion-aware global router for 2-pin nets. The
validation on three already-routed boards produced two headline results:

1. **The capacity graph predicts congestion well when the layer is right.**
   Routing each net on its actual-copper layer (ground-truth layer assignment)
   gives strong positive correlation on inner layers:
   - routed_output: In1.Cu r=0.53, In2.Cu r=0.47, In3.Cu r=0.48, B.Cu r=0.58
   - rp2350: In1.Cu r=0.42, In2.Cu r=0.40
   The triangle-edge capacity model is a sound congestion proxy.

2. **Layer assignment is the dominant error source.** The as-is single-layer
   planner funnels nearly every net onto F.Cu (no via model), so F.Cu is
   over-predicted (negative active-node correlation) while inner layers are
   under-predicted. Phase B must model layer choice and vias.

So Phase B's job is not "make a better graph" -- it is **carrying the plan's
corridors and congestion into the detailed router's decisions**: order, search
windows, layer choice, and bundling.

## 2. Integration seams (where the plan plugs into the real router)

The real router (route.py / single_ended_routing.py / batch_route) already has
the seams Phase B needs; the plan feeds them rather than replacing them:

- **Net ordering** -- the router already orders nets (net_ordering.py, MPS
  semantics). The plan provides a *plan-informed* order: route most-contended
  corridors first so scarce lanes are claimed early (the #472 / bus-corridor
  doctrine), and peel fewest-conflict nets first where contention is low.
- **Search windows** -- the detailed router's A* already supports a bounded
  search region (corridor-restricted search). The plan's per-net corridor
  (a polyline through triangle-edge midpoints) becomes a soft window: the
  detailed search is biased toward / bounded to the corridor, shrinking the
  search space and preventing the "rescue-loop waste" the plan is meant to kill.
- **Layer suggestion** -- the plan assigns each net a preferred layer (the one
  with most free capacity at its pads). The detailed router starts its search on
  that layer and only switches (via) when the corridor is exhausted.
- **Bundle grouping** -- nets whose planned corridors share triangle edges are
  grouped into bundles; the router routes them together (shared corridor,
  matched escape), which is what makes bus routing possible.

## 3. The four Phase B mechanisms

### 3.1 Net ordering from the plan

- Compute pairwise corridor conflict = number of shared triangle edges between
  two nets' planned paths (same layer).
- Order: route the most-contended corridors first (claim scarce lanes early);
  within a contention tier, route by net_id for determinism.
- Keep the existing #472 direct-first partition: blocks are reordered
  independently, never merged across blocks.
- Gate: ordering must be deterministic (fixed net order in, fixed order out).

### 3.2 Corridor-restricted search windows

- Each net's planned path is a sequence of triangle-edge midpoints -> a corridor
  polyline with a width = trace_width + clearance (the passage's capacity pitch).
- The detailed router's A* gets a soft cost bonus for staying inside the corridor
  and a hard bound (max detour distance from the corridor) so it cannot wander.
- The corridor is a *soft reservation*: if the detailed router cannot fit inside
  it (obstacle discovered at detail resolution), it may detour, and that detour
  is recorded back into the plan's occupancy for the next net.
- Gate: a net whose corridor is clear must route inside it; a net whose corridor
  is blocked must still route (detour allowed), never fail.

### 3.3 Layer suggestion

- The plan assigns each net a preferred layer = shared copper layer with most
  free capacity at its two pads.
- The detailed router starts on that layer; it switches layers (via) only when
  the corridor on the preferred layer is exhausted or blocked.
- This directly addresses Phase A's dominant error: without layer modeling the
  plan funnels everything to F.Cu. Phase B must add a **via model** to the plan:
  - a via cost per layer transition,
  - per-layer capacity that accounts for vias consuming space,
  - cross-layer nets (pads on different layers) get an explicit via in their
    planned path instead of being skipped.
- Gate: planned layer distribution should match actual routed layer distribution
  within tolerance on the validation boards (the metric Phase A could not meet).

### 3.4 Bundle grouping of same-corridor nets

- Cluster nets by shared corridor edges (union-find on corridor overlap).
- Route each bundle as a group: shared corridor, matched escape from pads,
  consistent via placement.
- This is what makes bus routing possible and reduces per-net rescue-loop waste.
- Gate: bundled nets must not increase total copper or via count vs unbundled on
  the same board (measured A/B).

## 4. Verification gates Phase B will need

Phase B must be gated on the same honest validation discipline as Phase A, plus
the project's existing gates:

1. **Unit tests** for each mechanism in isolation (ordering determinism, window
   bounding, layer choice, bundle clustering) -- hand-computable fixtures.
2. **Correlation gate**: on the three validation boards, planned occupancy vs
   actual copper density must show positive correlation on the layers where the
   plan routes (Phase A's inner-layer result, r>0.4, must hold; F.Cu must stop
   being over-predicted once layer modeling lands).
3. **Layer-distribution gate**: planned per-layer net distribution within
   tolerance of actual per-layer distribution.
4. **Runtime gate**: full plan + detailed routing under the existing chain's
   budget; the plan itself must stay under 10s/board (Phase A already meets this).
5. **Project gates**: full test suite untouched (272 pass / 4 known env failures /
   110 skipped), GUI/CLI parity if any engine parameter is added, DRC +
   connectivity clean on routed outputs.
6. **A/B discipline**: any ordering/window/layer/bundle change goes through the
   paired A/B harness (tests/test_placement_ab.py pattern) -- judge on >=3 boards,
   paired and directional, keep the dissenting row.

## 5. Scope fences for Phase B

- Do NOT modify pcb_modification.py, single_ended_routing.py, route.py, or
  rust_router/ without explicit agreement (per CLAUDE.md). Phase B should add a
  new module that *feeds* these seams, not rewrite them.
- Keep the plan standalone and importable; integration is a thin adapter that
  reads PlanResult and configures the existing engine calls.
- The via model is the one genuinely new planner capability Phase B needs; scope
  it as its own sub-task with its own validation before wiring it into ordering.
