# Corridor Quality Findings: do planned corridors contain real routes?

Date: 2026-09-01 (session). Status: committed code + this report.
Phase B's layer model was measured and FAILED (phase_b_findings.md, commit
8e5c5318). This report measures the OTHER half of the plan the owner wants to
build on: the **corridor geometry** -- each net's planned polyline through
triangle-edge midpoints -- and asks whether it contains the copper the real
router actually lays. The build decision (corridor-restricted A* search) rests
on this number.

## Method

For each already-routed validation board (the phase_a/phase_b set) plus
carrier_lab/routed and the owner's helisync-carrier, run the Phase B
multi-layer planner (trace=0.1 clearance=0.1 via=0.3, the same config as every
prior phase), then for each net with both a plan and real copper:

- **containment_2d[W]** = fraction of actual track length (sampled every 0.2mm)
  within W mm of the planned corridor polyline projected to 2D. This isolates
  corridor GEOMETRY quality from the known-bad layer model.
- **containment_layer[W]** = same, but each track is measured against the
  corridor sub-polyline ON ITS OWN LAYER. This shows how much damage the broken
  layer picker does on top of the geometry.
- **search-space ratio** = per-net corridor tube area (2W x plan length) /
  full-per-net search area (board area x all copper layers). This is the
  optimistic bound on the search-space reduction a corridor-restricted router
  could achieve -- the speedup ceiling.

Boards: rp2350_fpga_eensy_prePlane (72 routed nets, 6 layers, 637mm2),
routed_output (218, 10 layers, 6274mm2), carrier_lab/routed (267, 4 layers,
22200mm2), helisync-carrier (302, 4 layers, 24600mm2), carrier_lab/d1_routed
(267, 4 layers, 22200mm2). d1_routed is a copy of the carrier board (same
geometry, same nets) -- it confirms the carrier numbers are stable.

## 1. Containment: how wide must a corridor be to contain reality?

### 2D containment (geometry only, layer ignored) -- median over nets

| board | W=0.25 | W=0.5 | W=1 | W=2 | W=3 | W=4 |
|---|---|---|---|---|---|---|
| rp2350 | 22% | 42% | 67% | 96% | 100% | 100% |
| routed_output | 0% | 0% | 0% | 0% | 0% | 20% |
| carrier_lab/routed | 9% | 21% | 37% | 58% | 76% | 89% |
| helisync-carrier | 13% | 26% | 45% | 72% | 89% | 100% |
| d1_routed | 9% | 19% | 35% | 59% | 76% | 87% |

### Fraction of nets reaching >=80% containment (2D)

| board | W=0.5 | W=1 | W=2 | W=3 | W=4 |
|---|---|---|---|---|---|
| rp2350 | 15% | 44% | 64% | 81% | 94% |
| routed_output | 1% | 1% | 7% | 11% | 20% |
| carrier_lab/routed | 3% | 10% | 27% | 46% | 60% |
| helisync-carrier | 5% | 18% | 43% | 60% | 70% |

**The key number:** at the design width (trace+clearance ~0.2mm, i.e. W=0.25),
the median net's corridor contains only **9-22%** of its real copper. Even at a
generous **2mm** half-width (a 4mm-wide tube), only **27-64%** of nets reach
80% containment -- and routed_output never gets there at any width. To contain
the MEDIAN net you need a **3-4mm half-width (6-8mm wide)** tube on the carrier
boards, and even at W=4 only **60-70%** of carrier nets reach 80% containment.
routed_output is not containable at any width. A corridor that must be 8mm wide
to contain reality bounds almost nothing on a board whose channels are ~0.2mm.

### Layer-matched containment (corridor sub-polyline on same layer) -- median

| board | W=0.5 | W=1 | W=2 | W=4 |
|---|---|---|---|---|
| rp2350 | 12% | 16% | 31% | 49% |
| routed_output | 0% | 0% | 0% | 0% |
| carrier_lab/routed | 11% | 20% | 34% | 49% |
| helisync-carrier | 17% | 28% | 43% | 61% |

Layer-matching roughly **halves** containment on every board -- consistent with
the known-broken layer model (phase_b_findings: per-net layer agreement is
1-16%). But the geometry is bad even before layers are considered: the 2D
numbers above are already far below what a trustworthy corridor needs.

## 2. Search-space implication: the speedup ceiling

Per-net corridor tube area / full-per-net search area (median over nets):

| board | W=0.25 | W=0.5 | W=1 | W=2 |
|---|---|---|---|---|
| rp2350 | ratio .0034 (292x) | .0068 (146x) | .0137 (73x) | .0274 (37x) |
| routed_output | .0003 (3115x) | .0006 (1558x) | .0013 (779x) | .0026 (389x) |
| carrier_lab/routed | .0004 (2476x) | .0008 (1238x) | .0016 (619x) | .0032 (310x) |
| helisync-carrier | .0003 (3160x) | .0006 (1580x) | .0013 (790x) | .0025 (395x) |

The raw area ratios are spectacular -- a corridor tube is a tiny fraction of the
board -- so IF the corridor contained reality, the search-space reduction would
be enormous (hundreds to thousands of x). **But this is exactly the trap.** The
ratio is only meaningful if the corridor contains the route; a corridor that
misses its own net's copper by >W does not bound the search, it just excludes
the answer. The honest reading is the product: containment x area-reduction.
At W=2 on helisync, median containment is 72%, so the *effective* speedup for a
typical net is ~0.72 x 395x = ~285x IF the router could be forced to stay in
the tube -- but the tube misses a quarter of the copper, so the router must be
allowed to escape, and every escape widens the effective search back toward the
full board.

The real problem is not the area ratio; it is that **the corridor does not know
where its own net's copper goes**, so a hard bound would fail and a soft cost
would be fighting the router's own better judgment.

## 3. Failure modes: who is not contained, and why

### How many nets fail?

At W=2 (a generous 4mm tube), nets with <50% containment:

| board | nets <50% @ W=2 | nets <50% @ W=4 |
|---|---|---|
| rp2350 | 13/72 (18%) | 3/72 (4%) |
| routed_output | **187/218 (86%)** | **142/218 (65%)** |
| carrier_lab/routed | 94/267 (35%) | 33/267 (12%) |
| helisync-carrier | 88/302 (29%) | 33/302 (11%) |

This is NOT a small tail. On routed_output it is the overwhelming majority at
every width; on the carrier boards it is a third of all nets at W=2 and still
~10-12% at W=4. A "soft cost escape" cannot absorb a third of nets -- at that
point the corridor is not guiding anything.

### Why do they fail? The corridor wanders.

The dominant failure mode is **corridor wander**: the planned polyline is far
longer than the real route, so it snakes around and misses its own net's copper.
Plan-vs-actual length ratio:

| board | median plan/actual length |
|---|---|
| rp2350 | **2.41x** |
| routed_output | **4.99x** |
| carrier_lab/routed | **1.72x** |
| helisync-carrier | **1.66x** |
| d1_routed | **1.67x** |

Nets whose plan is >2x their actual length: rp2350 **69%**, routed_output
**98%**, carrier **43%**, helisync **41%**. The worst offenders are long,
multi-pin nets (CTL2, TC3_AN, RTL_XO, ADC2_nDRDY on the carrier; every
/fpga_adc/lvds_rx* pair on routed_output) whose MST-ordered sequential plan
accumulates detours -- each MST edge routed independently through a congested
graph, concatenated into one wandering polyline. The plan is not a corridor, it
is a scribble.

A few nets have zero-length plans (plan_len ~0, e.g. Net-(U29-D2+), a short
pad-to-pad hop whose two pads map to the same graph node) -- these are trivially
uncontainable but tiny.

### Is it the layer model or the geometry?

Both, and they are separable:

- **Geometry alone is already bad.** The 2D containment numbers (layer ignored)
  are far below trustworthy at any useful width. The corridor misses its own
  net's copper in the plane.
- **The layer model makes it worse.** Layer-matched containment is roughly half
  of 2D containment on every board, consistent with phase_b's finding that
  per-net layer agreement collapses to 1-16%.

So this is NOT the "good 2D corridor + broken layer picker = fixable" case. The
2D geometry itself does not contain reality. Fixing the layer picker would help,
but it would not make corridors trustworthy.

## Verdict

**NO -- corridors are not trustworthy enough to bias a real router's search.**

The numbers:

1. At design width (~0.2mm), median containment is **9-22%**. A corridor that
   contains a fifth of its own net's copper cannot bound that net's search.
2. To reach ~80-90% containment for most nets you need a **6-8mm-wide tube** on
   the carrier boards -- and routed_output is not containable at ANY width
   (median containment never exceeds ~20%, even at W=4). A corridor that must be
   8mm wide to contain reality bounds nothing.
3. The search-space ratio is enormous in principle (hundreds to thousands of x)
   but meaningless in practice: it multiplies against a containment that is
   ~10-70%, and every escape widens the effective search back toward full-board.
4. The failure is not a small tail -- it is **29-86% of nets** at W=2 depending
   on board, driven by corridor wander (median plan length is **1.7-5x** actual).
5. The geometry is bad independent of layers: even ignoring layers entirely,
   containment fails. Fixing the layer picker would not fix corridors.

What is precisely wrong:

- **The MST-ordered sequential plan concatenates detours.** Each multi-pin net's
  MST edge is routed independently through a congestion-weighted graph; the
  concatenation wanders far from any single sensible path. The plan optimizes
  congestion avoidance, not path fidelity -- so it produces long scribbles that
  happen to thread low-occupancy space, not corridors that follow where copper
  can actually go.
- **The graph's congestion cost has no fidelity term.** There is no penalty for
  deviating from a straight pad-to-pad line, so the planner freely detours to
  avoid occupancy, and those detours are exactly where real routes do NOT go.
- **No model of why real routes take their path.** Real routers take short,
  direct paths through channels; the plan takes long paths through empty space.
  The two objectives are almost orthogonal.

What would make corridors trustworthy:

1. **Add a path-fidelity term** to the planner cost: penalize deviation from the
   straight pad-to-pad line (or from a shortest-path skeleton), so planned paths
   stop wandering. This is the single highest-leverage fix -- it directly attacks
   the measured failure mode.
2. **Re-measure after that fix.** The containment metric here is cheap and now
   instrumented; re-run it before any corridor-restricted build.
3. Only then consider corridor-restricted search -- and even then, gate on
   containment >=80% for >=90% of nets at a width that still gives a meaningful
   area reduction.

Until then: do NOT build corridor-restricted routing on this planner's corridors.
The layer model already failed; this measurement shows the geometry fails too.

## Measurement scripts

- `planning_lab/measure_corridor_quality.py` -- runs the planner per board,
  computes per-net containment_2d / containment_layer across widths, writes
  `planning_lab/corridor_data.json`.
- `planning_lab/analyze_corridor_quality.py` -- tables above from the JSON.
- `planning_lab/analyze_corridor_wander.py` -- plan-vs-actual length ratios.
- `planning_lab/analyze_zero_plans.py` -- zero-length corridor census.
- `planning_lab/sanity_containment.py` -- self-containment sanity check of the
  distance metric.
