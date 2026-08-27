# SI Enforcement (KICAD_SI_ENFORCE) Corpus A/B Findings

Date: 2026-08-26
Commit under test: fc2cb35e (SI Phase 2: victim-net routing enforcement, default ON)
Branch: optimize-via-protection-parse

## Question

KICAD_SI_ENFORCE=1 (default ON) stamps aggressor-proximity costs for victim nets.
Verified wins on rp2350 (si_coupling +16.9, connectivity/DRC exactly equal). Open
questions this corpus A/B answers:

- (Q1) Does ON cause systematic connectivity/DRC cost across the board corpus
  beyond the +-2..3 net per-board noise band?
- (Q2) Carrier step6 USER time was +11.9% ON vs OFF (576->645s) -- beyond the
  +5% budget as measured on the bulk step.

## Method

No recorded stress-run manifests (redo_commands.sh) exist locally under
~/Documents/kicad_stress_test (empty). Per the task plan, fell back to A/B-ing
every routable board in the kicad_files/ + quality/ corpus lists with a standard
route.py chain, fresh output paths per arm.

Chain per board (identical on both arms):

1. Stage input via copy_board.py + seed .kicad_pro at the board's design-rule
   clearance (fix_kicad_drc_settings.py).
2. route.py --nets <AGGRESSORS> (so victims see aggressor copper)
3. route.py --nets <VICTIMS>
4. route.py --nets '*' (everything else)

Arms: OFF (KICAD_SI_ENFORCE=0) vs ON (KICAD_SI_ENFORCE=1). Both arms run the
identical chain from the identical staged input; fresh output paths per arm.

Grading: check_connected.py (connectivity issues + unrouted nets), check_drc.py
--clearance <board floor> --clearance-margin 0.1 (DRC violations), quality/score.py
(si_coupling sub-score + final_score). All runs nice -n 19.

Two dense BGA boards (ulx3s, haasoscope_pro_max_test) were additionally run with a
realistic fine-pitch via (0.3/0.15) because route.py warns the stock 0.45/0.2 via is
too large for their sub-0.8mm BGA pitch; the oversized-via runs showed DRC artifacts
that disappear at the fine via. The fine-via results are the ones reported here.

## Corpus table (faithful chain, realistic vias)

| Board | layers | OFF conn issues | ON conn issues | OFF unrouted | ON unrouted | OFF DRC | ON DRC | OFF si_coup | ON si_coup | OFF final | ON final |
|---|---|---|---|---|---|---|---|---|---|---|---|
| tigard | 4 | 0 | 0 | 0 | 0 | 1 | **5** | 88.5 | **95.6** | 62.83 | 62.73 |
| watchy | 4 | 0 | 0 | 0 | 0 | 0 | 0 | 31.5 | **72.1** | 54.96 | **57.90** |
| sonde_u | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 100 | 100 | 68.32 | 68.22 |
| interf_u_unrouted | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 100 | 100 | 62.74 | 62.74 |
| kit-dev-coldfire-xilinx_5213 | 4 | 0 | 0 | 0 | 0 | 1 | **0** | 70.7 | **100** | 60.86 | **63.65** |
| glasgow_revC | 4 | **6** | **2** | 0 | 0 | 0 | 0 | 100 | 100 | 59.80 | **61.76** |
| ulx3s (fine via) | 4 | **9** | **11** | **3** | **2** | **6** | **1** | 85.6 | **89.3** | **60.56** | **60.62** |
| haasoscope_pro_max_test (fine via) | 10 | **12** | **6** | **0** | **1** | **3** | **3** | 73.8 | **84.3** | **66.99** | **68.89** |
## Verdict

**Default stays ON.** The ON arm shows no systematic connectivity/DRC cost beyond
the +-2..3 net noise band:

- Connectivity issues IMPROVE on glasgow (-4), haasoscope (-6); ulx3s +2 within
  noise; all others equal. No board >3 nets worse on connectivity.
- Unrouted nets: worst is haasoscope +1 (within noise); ulx3s -1 better.
- DRC: tigard +4 is the only >3 regression and it is a SINGLE board; ulx3s -5
  better, haasoscope 0, kitdev -1 better. No multi-board DRC regression.
- si_coupling improves on 5/8 boards (watchy +40.6, kitdev +29.3, haasoscope
  +10.5, tigard +7.1, ulx3s +3.7), neutral on the other 3.
- final_score improves on 5/8 boards, neutral on 2, tiny -0.1 on tigard/sonde_u.

Sub-score regressions >2 points are all aesthetic (layer_direction: tigard -4.4,
watchy -6.8; bends/jog_chains on ulx3s -2.5/-3.4) -- none are connectivity or DRC.

The carrier board's single +3V3 RLD5 pad disconnect (Q1) is within the noise band
and not part of a systematic regression.

## Timing (Q2) -- clean back-to-back carrier re-run

After all corpus compute, ran ONE clean back-to-back carrier timing A/B
(carrier_lab/si_phase2/run_carrier_ab.sh, the full 6-step chain on
carrier_lab/in.kicad_pcb). Machine-quiet gate met at start: uptime load 1.34,
no heavy python/cargo compute.

| step | OFF user (s) | ON user (s) | delta |
|---|---|---|---|
| step1 route_planes GND | 4.23 | 4.23 | 0.0% |
| step2 route_planes power | 4.14 | 4.43 | +7.0% |
| step3 route_diff USB3 | 22.58 | 23.54 | +4.3% |
| step4 route_diff ETH | 14.78 | 14.73 | -0.3% |
| **step6 route.py bulk** | **551.23** | **713.63** | **+29.5%** |

**Carrier step6 USER time is +29.5% ON vs OFF (551->714s), far beyond the +5%
budget.** The commit's own measurement was +11.9% (576->645s); the clean
back-to-back re-run shows a LARGER regression (+29.5%). This is reproducible
and real: enforcement stamps per-victim proximity fields, and on the dense
carrier board (135 victim nets) that cost dominates the bulk route step.

The +29.5% exceeds the +5% budget on the bulk step. Reported honestly here;
no default change was made on timing alone (the corpus verdict above is the
connectivity/DRC gate, which stays ON). A radius/cost tuning experiment is a
candidate follow-up to bring the timing within budget while keeping the
si_coupling benefit.

## Tuning (Q2 follow-up) -- radius/cost sweep on the carrier chain

The +29.5% (and the commit's own +11.9%) both exceed the +5% budget, so a
tuning experiment was run: three back-to-back arms on the full 6-step carrier
chain (carrier_lab/si_phase2/run_carrier_tune.sh), fresh output paths each,
machine-quiet gate met at start (load 1.30, no heavy compute). Same-run deltas
only. The tuned arm used KICAD_SI_ENFORCE_RADIUS=0.8 KICAD_SI_ENFORCE_COST=0.2.

| arm | step6 user (s) | vs OFF | conn | DRC | si_coup sub | final |
|---|---|---|---|---|---|---|
| OFF | 530.15 | -- | 0 issues | 1 | 67.03 | 57.92 |
| ON-default (R1.5/C0.44) | 597.89 | +12.8% | 0 issues | 0 | 78.54 | 59.01 |
| ON-tuned (R0.8/C0.2) | 566.59 | +6.9% | 0 issues | 0 | 72.93 | 59.32 |

The R0.8/C0.2 arm keeps a real si_coupling win (+5.9 over OFF, ~half of the
default's +11.5) with zero connectivity/DRC cost, but at +6.9% it still misses
the +5% budget by ~2 points. The exposure analysis explains why: on the carrier
board most victim segments sit >2mm from aggressor copper, and the metric's own
coupling window is only 1.0mm -- so the 1.5mm default band steers victims away
from aggressors that do not even count in the metric (wasted steering), and the
0.44 cost over-prices the remaining band.

Second sweep (carrier_lab/si_phase2/run_carrier_tune2.sh): OFF vs ON-tuned2
(R0.8/C0.1), run twice back-to-back for confirmation.

| arm | step6 user (s) run1 | step6 user (s) run2 | vs OFF | conn | DRC | si_coup sub | final |
|---|---|---|---|---|---|---|---|
| OFF | 520.25 | 509.05 | -- | 0 issues | 1 | 67.03 | 57.92 |
| ON-tuned2 (R0.8/C0.1) | 509.44 | 513.15 | -2.1% / +0.8% | 0 issues | 0 | 73.58 | 58.33 |

**Verdict: R0.8/C0.1 fits the budget.** Both runs are within +5% (one slightly
negative, one +0.8% -- machine noise around OFF), si_coupling keeps a clearly-
better-than-OFF win (+6.55, 57% of the default's +11.5), and connectivity/DRC
are no worse than OFF (conn equal, DRC actually better: 0 vs OFF's 1). The
tuned2 output is deterministic across runs (identical si_coupling 73.583, DRC 0,
final ~58.3).

**Default changed: KICAD_SI_ENFORCE_RADIUS 1.5 -> 0.8, KICAD_SI_ENFORCE_COST
0.44 -> 0.1** in py_router/env_knobs.py (commit message carries this evidence).
Enforcement stays ON and accurate; the smaller band + lighter cost bring the
bulk-step timing within budget while keeping most of the si_coupling benefit.
The old values remain reachable via the env knobs for A/B.

## Files

All corpus outputs live under carrier_lab/si_corpus_ab/ (git-untracked, not
committed). This findings file is the only committed artifact. The tuning runs'
outputs live under /tmp/si_tune_{off,on,tuned}/ and /tmp/si_tune2_{off,tuned2}/
(not committed); the drivers are carrier_lab/si_phase2/run_carrier_tune.sh and
run_carrier_tune2.sh (committed).