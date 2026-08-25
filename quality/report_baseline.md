# Routing-quality baseline report (v1.4, 2026-08-25)

Phase 0 of the "professional-look router" initiative: scored baseline of
KiCadRoutingTools output on real boards, using quality/score.py **v1.4**.

## v1.4 update — signal-integrity coupling metric (si_coupling)

v1.4 adds **metric_si_coupling** (weight 10) to the quality scorer: the first
signal-integrity-aware metric. It measures how much VICTIM trace (serial data,
analog sense, radio, reset) runs parallel-and-close to AGGRESSOR copper
(switching-regulator nodes, PWM/motor/gate-drive, clocks, high-current supply
branches), using the net classifier in py_router/si_classes.py.

**Definition** (full docstring in score.py): for every VICTIM segment,
accumulate parallel-exposure to any AGGRESSOR segment on the SAME layer within
a coupling window max(3x trace width, 1.0 mm), weighted by 1/separation,
PLUS broadside overlap on ADJACENT copper layers with no GND plane between
(the separation for broadside is the dielectric thickness from the stackup).
Normalised per victim-net mm. Crossings are near-free (a perpendicular
aggressor contributes no parallel samples); only co-running length counts.

**Why the window is max(3W, 1.0 mm):** 3x the trace width is the classic
"keep 3W away from a noisy neighbor" crosstalk rule of thumb — at 3W the
coupling has dropped to a small fraction of its worst-case value for
microstrip; the 1.0 mm floor keeps hairline traces from needing an
impractically tiny window.

**Same-interface exclusion:** DDR data beside its own DQS strobe, LVDS data
beside its own clock lane, SPI MOSI beside SCK — these are INTENTIONAL bus
routing, not SI violations, and are excluded (two nets are same-interface when
their names share a common prefix >= 4 chars or a common component token).
Only CROSS-interface coupling — serial data beside switching power or an
unrelated clock — counts as a violation.

**Sub-score thresholds:** sub = 100 * exp(-raw / 0.5). A board with no
victim/aggressor pairs scores 100; a serial line hugging a switch node for
40 mm at 0.4 mm separation (raw ~2.5) scores ~0.7; a few short parallel runs
(raw ~0.2) score ~67.

### Effect on the 6-board baseline

| Board | v1.3 | v1.4 | si_coupling raw | si_coupling sub |
|---|---|---|---|---|
| fanout_output2 | 68.75 | 71.59 | 0.0000 | 100.0 |
| routed_output | 64.15 | 67.41 | 0.0000 | 100.0 |
| d1 / d1_fixed2 | 57.02 | 60.92 | 0.0000 | 100.0 |
| rp2350_fpga_eensy_prePlane | 53.34 | 54.38 | 0.2169 | 64.8 |
| orangecrab_ext_pll | 51.86 | 47.19 | 2.6611 | 0.5 |

The v1.3 metrics (bends, off_angle, vias, pad_entry, fragmentation, parallel,
channel, layer_direction, stubs, jog_chains) are **unchanged** — d1 still
reports vias = 3.6667, jog_chains raw = 0.988621, layer_direction raw =
0.5158, identical to v1.3. The final-score deltas come entirely from adding
the si_coupling weight to the budget.

## Scores (0–100, higher = closer to professional hand-routing)

| Board | Final score |
|---|---|
| carrier_lab/d1.kicad_pcb | 60.92 |
| carrier_lab/d1_fixed2.kicad_pcb | 60.92 |
| kicad_files/fanout_output2.kicad_pcb | 71.59 |
| kicad_files/routed_output.kicad_pcb | 67.41 |
| kicad_files/rp2350_fpga_eensy_prePlane.kicad_pcb | 54.38 |
| kicad_files/orangecrab_ext_pll.kicad_pcb | 47.19 |

Range 47–72: orangecrab drops to worst because its IO_MOSI/MISO serial lines
run directly under the EXT_PLL clock traces on adjacent layers with no ground
plane between them — a genuine SI violation the new metric catches.

Full per-board tables and JSON: quality/out/json/. Renders (per copper
layer): quality/out/render_*/.

## Ranked offenders (mean sub-score across all 6 boards, low = worst)

| Rank | Metric | Mean | Reading |
|---|---|---|---|
| 1 | stubs | 27.3 | dangling segment endpoints — **#1 again** |
| 2 | pad_entry | 44.6 | acute/odd-angle pad entries are routine (d1: 38/51 flagged) |
| 3 | jog_chains | 45.9 | stair-step chains + excess bends (owner's #1 visual complaint) |
| 4 | vias | 51.5 | via-heavy routing |
| 5 | parallel | 59.6 | adjacent runs drift apart/together instead of tracking |
| 6 | channel | 61.6 | traces hug one obstacle instead of centering |
| 7 | bends | 64.5 | ~1 bend/mm — wandering paths |
| 8 | fragmentation | 65.1 | collinear runs chopped into short segments |
| 9 | layer_direction | 68.2 | anti-axis long runs — no longer a top offender under v1.3 |
| **10** | **si_coupling** | **77.6** | **cross-bus victim/aggressor coupling — new in v1.4** |
| 11 | off_angle | 93.1 | 45°-grid discipline is already good |

si_coupling sits mid-pack on the baseline: most boards are clean (no
cross-bus coupling), but orangecrab's serial-under-clock stacking drags the
mean down. It is the FIRST metric that measures electrical noise coupling
rather than aesthetics — a board can score perfectly on every visual metric
and still be an SI disaster.

## Worst offenders (v1.4) — actual net pairs

From metric_si_coupling's top_offender_pairs across the baseline boards:

| Board | Victim net | runs | at sep | from Aggressor net | on layer |
|---|---|---|---|---|---|
| orangecrab_ext_pll | IO_MOSI | ~1.3 mm x5 segs | 0.20 mm | EXT_PLL+ / EXT_PLL- | In1.Cu (broadside, In2.Cu victim) |
| orangecrab_ext_pll | IO_MISO | ~1.3 mm x4 segs | 0.53–0.85 mm | EXT_PLL+ / EXT_PLL- | In1.Cu (broadside) |
| orangecrab_ext_pll | IO_SDA | ~0.8 mm | 0.20 mm | REF_CLK | F.Cu |
| orangecrab_ext_pll | IO_SCK | ~0.4 mm | 0.20 mm | EXT_PLL+ | In1.Cu (broadside) |
| orangecrab_ext_pll | RAM_RESET# | ~0.3 mm x2 segs | 0.20 mm | EXT_PLL+ / EXT_PLL- | In1.Cu (broadside) |
| orangecrab_ext_pll | USB_D- | ~0.3 mm | 0.20 mm | SD0_CLK | F.Cu |
| rp2350_fpga_eensy_prePlane | /RP2354A/FPGA.MOSI | **9.5 mm** | **0.56 mm** | +1V1 (switching rail) | In1.Cu |
| rp2350_fpga_eensy_prePlane | /RP2354A/RP.UART0_TX/RX | ~0.5 mm each | 0.10 mm | +1V1 (switching rail) | In1.Cu |

The single worst offender is **rp2350's FPGA.MOSI running 9.5 mm at 0.56 mm
from the +1V1 switching rail on In1.Cu** — a serial configuration line hugging
a buck-converter output for nearly a centimetre.

## Independent hand-verification

Metrics were re-computed for d1.kicad_pcb by quality/verify_independent.py
— written by the supervising agent, sharing no code with score.py (own segment
chaining, own arithmetic):

| Metric | score.py raw | independent | Verdict |
|---|---|---|---|
| segments/mm | 1.1517 | 332 / 288.28 mm = 1.1517 | exact match |
| off-45° joint fraction | 0.0520 | 15 / 294 = 0.0510 | match (±1 joint, chaining tolerance) |
| bends/mm | 1.0060 | 291 / 288.28 mm = 1.0094 | match (±0.3%, junction handling) |
| vias/routed net | **3.6667** | 33 / 9 = 3.6667 | **match — unchanged in v1.4** |

**vias still matches the independent value (3.6667).** The v1.1 numerator fix
(only vias on routed nets) is untouched in v1.4; d1 still reports 33/9 =
3.6667.

## Known limitations (v1.4)

- **stubs zone-awareness uses zone OUTLINES, not true filled polygons.** The
  board files store no filled_polygon data (zones are filled by KiCad on
  load), so metric_stubs treats each zone's outline polygon as its
  filled-copper region and matches net + layer before rescuing an endpoint.
- **layer_direction ignores short runs (≤ 3 mm) and treats diagonals as
  neutral.** A board whose long runs are all anti-axis still scores ~0; a board
  with NO long runs (e.g. orangecrab_ext_pll) scores a perfect 100 because
  there is nothing to measure — read that as "no long-run direction signal",
  not "direction-disciplined".
- **si_coupling depends on the net classifier** (py_router/si_classes.py).
  Name/metadata heuristics miss nets that carry no name signal; the per-board
  override file (<board>.si.json) is the escape hatch that always wins.
- **si_coupling's same-interface exclusion is a heuristic.** Two nets sharing
  a common name prefix or component token are treated as one intentional bus;
  a genuinely cross-bus pair that happens to share a prefix would be missed.
- **Broadside separation uses the stackup dielectric thickness** when present,
  else a nominal 0.2 mm prepreg — boards without a stackup section get the
  0.2 mm fallback.
- Weights are provisional (table at top of score.py); the aggregate score is
  for tracking movement, not absolute judgment.
- Aesthetic + SI rubric only — functional correctness stays with the house
  gates (check_drc.py / check_connected.py per repo CLAUDE.md), which this
  score complements, never replaces.

## Provenance

score.py, render.py, geometry.py: built by DeepSeek session fb24d189
(quality-harness session). Hand-verification, this report, and README: the
supervising agent, after the session stalled twice at the verification step;
_probe*.py / _verify*.py are that session's scratch iterations, kept as-is.
v1.1 metric fixes (metric_vias numerator, zone-aware metric_stubs) and this
report refresh: DeepSeek session (v1.1). v1.2 jog_chains metric + report:
DeepSeek session (v1.2). v1.3 layer_direction redefinition + report refresh:
DeepSeek session (v1.3). v1.4 si_coupling metric + net classifier + report:
DeepSeek session (v1.4).
