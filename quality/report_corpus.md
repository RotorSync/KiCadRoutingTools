# Corpus-wide routing-quality report (v1.4, corpus baseline)

## v1.4 update — si_coupling at corpus scale

All 13 boards re-scored under quality/score.py **v1.4**, which adds
**metric_si_coupling** (weight 10): cross-bus victim/aggressor coupling
exposure, backed by the net classifier in py_router/si_classes.py. See
report_baseline.md for the full definition, the max(3W, 1.0 mm) window
rationale, the same-interface exclusion, and the sub-score thresholds.

Corpus-scale ranking (mean sub-score, low = worst, None-scoring degenerate
boards excluded per metric):
**stubs 39.8 › pad_entry 59.9 › vias 61.8 › fragmentation 66.1 › channel 68.2 ›
jog_chains 70.7 › parallel 73.2 › bends 76.9 › layer_direction 85.3 ›
si_coupling 89.6 › off_angle 91.0**.

si_coupling lands at #10 — most corpus boards are clean (no cross-bus
coupling), but the two real boards with genuine violations (orangecrab,
rp2350) pull it below off_angle. It is the only metric that measures
ELECTRICAL noise coupling rather than aesthetics.

Updated scores: orangecrab 47.19, rp2350 54.38, lvds 66.42, d1/d1_fixed2
60.92, routed_output 67.41, fanout_output2 71.59, fanout_output1 73.51,
fanout_starting_point 73.03, qfn_fanned_out 80.32, qfn_diffpair_escape /
qfn_interior_pads 90.39, qfn_underpad_coupling 98.23.

The v1.3 metrics are unchanged (d1 vias = 3.6667, jog_chains raw = 0.988621,
layer_direction raw = 0.5158 — identical to v1.3); final-score deltas come
from adding the si_coupling weight.

---

Original v1.1 report follows.

Broadens the v1.1 baseline (6 boards) to the whole KiCadRoutingTools corpus so
the beautification work has a wider target set.

## Scope & method

- **Candidates checked:** all 27 kicad_files/*.kicad_pcb plus
  carrier_lab/d1.kicad_pcb and carrier_lab/d1_fixed2.kicad_pcb (29 total).
- **Scoreable** = board contains at least one track segment (pcb.segments > 0).
  **13 boards** qualified and were scored with
  score.py --json quality/out/corpus/<board>.json (one .json + one
  .txt per board). All exited 0 with empty stderr.
- The other 16 candidates contain **no track segments** and are skipped (see
  "Skipped boards" below) — no silent skips.

## Full score table (worst → best)

| Board | Final score | Segments | Vias | Routed nets |
|---|---|---|---|---|
| kicad_files/orangecrab_ext_pll.kicad_pcb | 47.19 | 742 | 136 | 134 |
| kicad_files/rp2350_fpga_eensy_prePlane.kicad_pcb | 54.38 | 904 | 70 | 72 |
| carrier_lab/d1.kicad_pcb | 60.92 | 332 | 39 | 9 |
| carrier_lab/d1_fixed2.kicad_pcb | 60.92 | 332 | 39 | 9 |
| kicad_files/lvds_converter_dualclk_gnd.kicad_pcb | 66.42 | 97 | 9 | 1 |
| kicad_files/routed_output.kicad_pcb | 67.41 | 1701 | 383 | 218 |
| kicad_files/fanout_output2.kicad_pcb | 71.59 | 829 | 339 | 164 |
| kicad_files/fanout_starting_point.kicad_pcb | 73.03 | 266 | 115 | 48 |
| kicad_files/fanout_output1.kicad_pcb | 73.51 | 311 | 126 | 48 |
| kicad_files/qfn_fanned_out.kicad_pcb | 80.32 | 94 | 4 | 47 |
| kicad_files/qfn_diffpair_escape.kicad_pcb | 90.39 | 3 | 0 | 3 |
| kicad_files/qfn_interior_pads.kicad_pcb | 90.39 | 3 | 0 | 3 |
| kicad_files/qfn_underpad_coupling.kicad_pcb | 98.23 | 1 | 0 | 1 |

Corpus mean final score **70.13 → ~70**, median **67.41**, range **47.19–98.23**.

## Mean sub-scores & re-derived offender ranking

Mean sub-score across all 13 scored boards (low = worst offender):

| Rank | Metric | Mean sub-score |
|---|---|---|
| **1** | **stubs** | **39.80** |
| **2** | **pad_entry** | **59.86** |
| **3** | **vias** | **61.76** |
| **4** | **fragmentation** | **66.11** |
| **5** | **channel** | **68.18** |
| **6** | **jog_chains** | **70.66** |
| **7** | **parallel** | **73.18** |
| **8** | **bends** | **76.94** |
| **9** | **layer_direction** | **85.32** |
| **10** | **si_coupling** | **89.64** |
| **11** | **off_angle** | **90.96** |

### Does stubs return to #1? **Yes.**

Under v1.2 layer_direction was the corpus #1 offender (26.07) and stubs was
#2 (39.80). Under v1.3 layer_direction drops to #9 (85.32) because clean
diagonals and short connectors are no longer penalised, so stubs returns to
#1 (39.80). v1.4 adds si_coupling at #10 (89.64) — a new signal, not a
regression of the visual priorities.

### Does the priority order hold? **stubs > pad_entry > vias now leads.**

The corpus-scale priority order is now:
**stubs > pad_entry > vias > fragmentation > channel > jog_chains > parallel > bends > layer_direction > si_coupling > off_angle**

layer_direction is no longer a top target at corpus scale; stub reduction and
pad-entry normalization are the leading actionable weaknesses, and si_coupling
is the new SI-aware signal to watch.

## si_coupling per board

| Board | raw exposure/mm | sub-score | victim nets | aggressor nets | exposed pairs |
|---|---|---|---|---|---|
| fanout_output1 / starting_point / qfn_fanned_out | 0.0000 | 100.0 | ~33 | ~1 | 0 |
| fanout_output2 / routed_output / d1 / d1_fixed2 | 0.0000 | 100.0 | many | few | 0 |
| lvds_converter_dualclk_gnd / qfn_* tiny boards | 0.0000 | 100.0 | 0–1 | 0–1 | 0 |
| rp2350_fpga_eensy_prePlane | **0.2169** | **64.8** | 14 | 6 | 9 |
| orangecrab_ext_pll | **2.6611** | **0.5** | 20 | 5 | 90 |

## Worst offenders (v1.4) — actual net pairs

From metric_si_coupling's top_offender_pairs across the whole corpus:

| Board | Victim net | runs | at sep | from Aggressor net | on layer |
|---|---|---|---|---|---|
| rp2350_fpga_eensy_prePlane | /RP2354A/FPGA.MOSI | **9.5 mm** | **0.56 mm** | +1V1 (switching rail) | In1.Cu |
| orangecrab_ext_pll | IO_MOSI | ~1.3 mm x5 segs | 0.20 mm | EXT_PLL+ / EXT_PLL- | In1.Cu (broadside) |
| orangecrab_ext_pll | IO_MISO | ~1.3 mm x4 segs | 0.53–0.85 mm | EXT_PLL+ / EXT_PLL- | In1.Cu (broadside) |
| orangecrab_ext_pll | IO_SDA | ~0.8 mm | 0.20 mm | REF_CLK | F.Cu |
| orangecrab_ext_pll | IO_SCK / RAM_RESET# / USB_D- | ~0.3–0.4 mm each | 0.20 mm | EXT_PLL± / SD0_CLK / REF_CLK | F.Cu / In1.Cu |
| rp2350_fpga_eensy_prePlane | /RP2354A/RP.UART0_TX/RX | ~0.5 mm each | 0.10 mm | +1V1 (switching rail) | In1.Cu |

The worst offender in the whole corpus is **rp2350's FPGA.MOSI running
9.5 mm at 0.56 mm from the +1V1 switching rail on In1.Cu** — a serial
configuration line hugging a buck-converter output for nearly a centimetre.
orangecrab's IO_MOSI/MISO serial lines run directly under the EXT_PLL clock
traces on adjacent layers with no ground plane between them.

## Outliers & why

- **qfn_underpad_coupling (98.23)** — degenerate: a single track segment on a
  single net, no vias, no pad entries measured, and channel is None
  (no obstacle pair to measure asymmetry against). It is a one-trace test
  board, not a routing; its near-perfect score is an artifact of tiny size.
- **qfn_diffpair_escape (90.39)** and **qfn_interior_pads (90.39)** — tiny
  escape/interior-pad test boards: only **3 segments / 3 nets** each, no vias.
  High scores reflect near-empty geometry, not professional routing.
- **qfn_fanned_out (80.32)** — **fanout-only**: 94 segments across 47 nets but
  0 vias and many dangling endpoints (fanout traces stop at the package). It is
  an intermediate fanout stage, not a completed route.
- **fanout_output1 / fanout_output2 / fanout_starting_point (73.5 / 71.6 /
  73.0)** — **fanout-only boards**: only ~48 of their ~527 nets are routed
  (fanout_output2 routes 164). They carry many dangling endpoints because
  fanout traces end at the package boundary awaiting full routing.
- **lvds_converter_dualclk_gnd (66.42)** — a **single-net board**: all 97
  segments belong to one routed net, so vias-per-net (9.0) and fragmentation
  dominate its score; layer_direction is a perfect 100 because its long runs
  are all on one disciplined axis (one differential pair + ground pour).
- **orangecrab_ext_pll (47.19)** — the worst full board: a real multi-net
  routing with heavy stubs, poor pad entries, high bends/jog_chains, AND now
  the worst si_coupling (serial lines under the PLL clock traces). Its
  layer_direction is a perfect 100 under v1.3 because it has NO run longer
  than 3 mm (max run ~2.45 mm) — there is no long-run direction signal to
  measure, which is not a claim that it is direction-disciplined.

The "real" full-routing boards (orangecrab, rp2350, routed_output, d1,
d1_fixed2) cluster at **47–67**, matching the original six-board baseline; the
fanout-only and tiny QFN boards inflate the corpus mean to ~70.

## Skipped boards (no track segments)

Checked and skipped because they contain **zero track segments** (nothing to
score; several are unrouted/placed-only or keepout-only):

| Board | Segments |
|---|---|
| kicad_files/cap_chain.kicad_pcb | 0 |
| kicad_files/esp_prog.kicad_pcb | 0 |
| kicad_files/flat_hierarchy.kicad_pcb | 0 |
| kicad_files/glasgow_revC.kicad_pcb | 0 |
| kicad_files/haasoscope_pro_max_test.kicad_pcb | 0 (4 vias, no traces) |
| kicad_files/interf_u_plane.kicad_pcb | 0 |
| kicad_files/interf_u_unrouted.kicad_pcb | 0 |
| kicad_files/interf_u_unrouted_placed.kicad_pcb | 0 |
| kicad_files/kit-dev-coldfire-xilinx_5213.kicad_pcb | 0 |
| kicad_files/lvds_converter_dualclk.kicad_pcb | 0 |
| kicad_files/qfn_csi_underpad_diff.kicad_pcb | 0 |
| kicad_files/sonde_u.kicad_pcb | 0 |
| kicad_files/splitflap_driver.kicad_pcb | 0 |
| kicad_files/tigard.kicad_pcb | 0 |
| kicad_files/ulx3s.kicad_pcb | 0 |
| kicad_files/watchy.kicad_pcb | 0 |

These are unrouted / placed-only / keepout-only inputs to the router, not
router output, so they carry no routing to score.

## Honesty notes

- No board was silently skipped: every candidate was parsed and checked for
  track segments; the skip list above is explicit.
- No crashes occurred, so no fix was applied to score.py beyond the v1.4
  si_coupling addition (which is intentional). d1 and d1_fixed2 re-verify at
  final score **60.92** with **vias = 3.6667**, identical to the v1.3 baseline;
  jog_chains values are unchanged from v1.3.
- Raw outputs: quality/out/corpus/*.json (full metric detail) and
  *.txt (human tables). The driver script used is quality/_corpus_run.sh; the
  segment probe is quality/_corpus_probe.py.
- Known limitations still apply (see quality/README.md): stubs uses zone
  *outlines* as the filled-copper proxy, parallel/channel are sampled
  heuristics, layer_direction ignores short runs and treats diagonals as
  neutral, and si_coupling depends on the net classifier's name/metadata
  heuristics (override file <board>.si.json is the escape hatch) — treat small
  deltas as noise.
