# Short Hunt Findings (post-merge emitted-shorts investigation)

Date: 2026-08-29
Branch: optimize-via-protection-parse (HEAD b0ad0244 + fixes)
Context: post-merge validation (carrier_lab/postmerge_validation.md) found the
router EMITTING SHORTS on glasgow_revC and carrier. Drift in routing outcomes
is acceptable; emitted shorts are never acceptable. This documents the two
root causes, the fixes, and the ulx3s convergence diagnosis.

## TARGET 1 — glasgow_revC: /CLKREF <-> /FLAGB short (6 DRC violations)

### Symptom
/CLKREF's F.Cu diagonal (77.90,92.90)-(79.70,94.70) crossed by /FLAGB copper
near U30's BGA: 5 segment-segment overlaps + 1 crossing at (79.412,94.412).

### Root cause — a LATENT BUG, not landscape drift
The /FLAGB copper was written by the **#666 bare-ball fanout-rescue escape**
in net_rescue.py. That rung calls `generate_bga_fanout`, whose conflict model
covers balls/teeth/passives but **NOT this run's already-routed tracks** — at
rescue time the board is ROUTED, and the escape copper was extended into
pcb_data UNCHECKED. A rescue that can create overlap has a missing clearance
check, regardless of landscape.

Upstream already fixed this exact bug in c3725b31 ("routing: fix the
ship-a-short rescue escape") on origin/bus622-take2, but that commit depends on
bus_terminal.py (a #622 file absent from our branch). We ported the guard
self-contained into net_rescue.py.

### Fix (py_router/net_rescue.py)
Added `_rescue_escape_clear(pcb_data, segs, vias, config, net_id)` — an exact
foreign-copper check (crossing-aware segment-segment distance, pad distance,
via barrel distance) applied to every emitted escape seg and via BEFORE commit.
A would-short escape is DECLINED like any other no-escape outcome (a failed
rescue is acceptable output; a short is not).

Measured on the re-run: 10 escapes declined (U30.K5, U30.G11, U30.G10, U30.G8,
U30.F4, U30.J5, U30.E5, U30.B4 x2, U30.J7), including the exact U30.K5 escape
that caused the short. /FLAGB now carries ZERO copper — an honest open replaces
the shipped short.

### Gate result (glasgow chain re-run)
- DRC: 6 violations -> **0** (NO DRC VIOLATIONS FOUND)
- Connectivity: 6 issues (was 6) — conn NOT worse (4 connectivity + 2 unrouted
  vs 6 connectivity; /FLAGB went from partially-connected to fully open)
- Final score: 61.14 -> 60.25 (delta -0.89, within +-2)
- si_coupling: 94.5 unchanged

### Regression test
tests/test_rescue_escape_short_guard.py — pins the crossing-aware guard:
escape seg crossing a foreign diagonal declined, clear seg passes, escape via
overlapping foreign via declined, escape seg grazing foreign pad declined,
own-net copper exempt.

## TARGET 2 — carrier: TRD1_P short (Via:GND <-> Seg:TRD1_P) + VIN_PROT drill-hole

### Symptom (post-merge carrier, 4 DRC violations)
1. Via:GND <-> Seg:TRD1_P on F.Cu (2 violations, 1 in contact) — a GND return
   via at (72.514,56.048) landed 0.0085mm from TRD1_P's F.Cu segment.
2. Via:VIN_PROT <-> Via:VIN_PROT drill-hole (0.2mm apart, need 0.4).
3. +3V3 same-net soft joint (In2.Cu).

### Root cause — a LATENT GAP exposed by #764/#766 path drift
The TRD1_P short is NOT an upstream #764/#766 bug — those commits are
diagnostics/length-matching only (verified by reading them). It is a latent gap
in **route_diff's companion-GND return via placer** (`_create_gnd_vias` in
diff_pair_routing.py): each GND via position is computed as pure geometry
(perpendicular offset from the P/N centerline at every layer change) and was
NEVER checked against foreign copper. At route_diff time the board is ROUTED,
so an unchecked return via ships a short. The post-merge TRD1 path shift moved
TRD1_P's F.Cu segment under the GND via site — tie-break drift exposing the gap.

### Fix (py_router/diff_pair_routing.py)
Added `_gnd_via_clear(pcb_data, x, y, config, gnd_net_id, extra_segments)` —
an exact foreign-copper check for each GND return via site: foreign tracks on
F.Cu/B.Cu (the barrel spans both), foreign pads (any copper layer), foreign via
barrels (copper + drill rules). Own-net GND copper is exempt. Crucially it also
checks `extra_segments` — the pair's OWN new P/N segments being committed in
the same call — because the GND via is placed AFTER them and they are not yet
in pcb_data (this was the second iteration of the fix; the first only checked
pre-existing copper and missed the carrier short).

A would-short GND via is DECLINED (skipped), like any other no-placement
outcome.

Measured on the re-run: "GND return via at (72.514,56.048) declined (would
short foreign copper)" — the exact carrier short via is gone.

### Gate result (carrier chain re-run)
- DRC: 4 violations -> **1** (only the VIN_PROT drill-hole remains; gate is <=1)
- Connectivity: **ALL NETS FULLY CONNECTED** (invariant holds)
- RLD5 pad on +3V3: connected (invariant holds)
- The +3V3 soft joint and both TRD1_P shorts are gone.

### The remaining VIN_PROT drill-hole — documented, not fixed here
Via:VIN_PROT at (12.20,73.80) and (12.20,74.00) are 0.2mm apart (drill 0.15
each; hole-to-hole needs 0.15+0.25=0.4). Both are same-net VIN_PROT vias from
the step-6 bulk route.py power-net routing, connected by a 1.2mm In1.Cu
segment. This is a SAME-NET via pair — not a short — and it is pre-existing
post-merge drift (present in the original post-merge carrier too; pre-merge had
only ONE via there). It is a real fab concern but out of scope for this hunt's
"never write a short" mandate; it is documented here for a follow-up on the
step-6 power-net via placer's same-net drill-hole spacing.

### Regression test
tests/test_gnd_return_via_clear.py — pins the GND return via guard: via over
foreign F.Cu segment rejected, clear site passes, own-net GND exempt, foreign
via/pad within clearance rejected, and the measured carrier case (own new P/N
segment via extra_segments rejected).

## TARGET 3 — ulx3s: 4 boxed_in_static opens (CONVERGENCE regression)

### Diagnosis (findings only; no fix attempted)
The four nets (GP26, SDRAM_D12, USER_PROGRAMN, SDRAM_D3) are all
`boxed_in_static`: the rip-up ladder rips 3-4 nets and still fails because the
start/target pads are boxed in by STATIC obstacles (neighboring pads +
clearance), not by congestion. The forward cell always reads "ok, 0/8 neighbors
blocked" yet no route is found — the pads are geometrically sealed.

Pre-merge these nets carried copper (GP26: 22 segs/3 vias; SDRAM_D12: 7 segs;
USER_PROGRAMN: 25 segs/4 vias); post-merge they carry ZERO. The pads are
identical pre/post — the difference is the step-a aggressor routing shifted the
obstacle field (450 -> 300 segs), boxing these BGA pads in at grid 0.1 /
clearance 0.09 / track 0.2.

This is a CONVERGENCE regression, not a correctness bug: no short is emitted,
the nets just fail to route. The rip-up ladder cannot free them because there
is nothing rippable to free — the walls are neighboring pads + clearance, which
are static by definition. A finer grid or smaller clearance/track would help,
but that is a routing-quality knob change, not a correctness fix. No fix
attempted per the task's "only if obvious and cheap" guidance.

## Summary

| Target | Root cause | Fix | Gate |
|---|---|---|---|
| glasgow /CLKREF<->/FLAGB | #666 rescue escape wrote copper unchecked into routed terrain | _rescue_escape_clear guard in net_rescue.py | DRC 6->0, conn not worse |
| carrier TRD1_P | GND return via placed by pure geometry, never checked vs foreign copper | _gnd_via_clear guard in diff_pair_routing.py | DRC 4->1, ALL CONNECTED, RLD5 ok |
| ulx3s boxed_in_static | convergence regression (static pad walls), not correctness | none (findings only) | n/a |

Both fixes follow the #468 doctrine: a writer that can create overlap must
exact-check its emitted copper against foreign copper and DECLINE rather than
ship a short.
