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
---

## Re-score at tuned defaults (R0.8/C0.1)

Date: 2026-08-28
Commit under test: 975180c0 (SI enforcement: tune default radius/cost to 0.8/0.1)
Branch: optimize-via-protection-parse

The corpus verdict above (dd7b047e) was measured at the OLD defaults (RADIUS 1.5 /
COST 0.44). The production defaults are now R0.8/C0.1 (975180c0). This section
re-scores the SAME 8-board corpus at the CURRENT defaults to confirm the verdict
still holds.

### Method

Identical board set and chain shape as the original: the faithful chain
(aggressors -> victims -> rest) replayed from the original si_corpus_ab logs
(CMD lines parsed verbatim, only output paths fresh). Arms: OFF
(KICAD_SI_ENFORCE=0) vs ON (KICAD_SI_ENFORCE=1, current defaults, no knob
overrides). One chain at a time, free -g >= 8 before each step, all nice -n 19.
Grading identical to the original: check_connected.py + check_drc.py
--clearance <floor> --clearance-margin 0.1 + quality/score.py.

Replay fidelity verified: sonde_u and interf_u reproduce the original table
exactly (si_coup 100/100, final 62.74/62.74 etc.), and the OFF arms of the
remaining boards reproduce their original OFF numbers exactly (tigard DRC 1,
kitdev DRC 1, glasgow conn 6, ulx3s conn 9/unrouted 3/DRC 6, haasoscope conn
12/DRC 3). The ulx3s ON arm was re-run twice more (fresh full chain from the
staged input) and is deterministic: DRC 19, conn 6, si_coup 81.0, final 59.26
every time.

### Corpus table (re-score at R0.8/C0.1)

| Board | layers | OFF conn | ON conn | OFF unrout | ON unrout | OFF DRC | ON DRC | OFF si_coup | ON si_coup | OFF final | ON final |
|---|---|---|---|---|---|---|---|---|---|---|---|
| tigard | 4 | 0 | 0 | 0 | 0 | 1 | 1 | 88.5 | **92.9** | 62.83 | **63.22** |
| watchy | 4 | 0 | 0 | 0 | 0 | 0 | 0 | 31.5 | 31.4 | 54.96 | 54.11 |
| sonde_u | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 100 | 100 | 68.32 | 68.32 |
| interf_u | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 100 | 100 | 62.74 | 62.74 |
| kit-dev-coldfire-xilinx_5213 | 4 | 0 | 0 | 0 | 0 | 1 | **0** | 70.7 | **98.1** | 60.86 | **63.20** |
| glasgow_revC | 4 | 6 | 6 | 0 | 0 | 0 | 0 | 100 | 100 | 59.80 | 59.78 |
| ulx3s (fine via) | 4 | **9** | **6** | **3** | **0** | **6** | **19** | 85.6 | **81.0** | **60.56** | **59.26** |
| haasoscope_pro_max_test (fine via) | 10 | **12** | **9** | **0** | **3** | **3** | **0** | 73.8 | **73.3** | **66.99** | **67.23** |

### Verdict at tuned defaults

**Default stays ON, but the si_coupling win is much smaller than at the old
defaults and one board (ulx3s) shows a real DRC regression.**

Connectivity/DRC cost vs the same rules as the original:

- Connectivity issues: ulx3s -3 better, haasoscope -3 better, glasgow equal (6/6),
  all others equal. No board >3 nets worse on connectivity.
- Unrouted nets: haasoscope +3 is the only regression and it is a SINGLE board
  (within the +-2..3 noise band); ulx3s -3 better.
- DRC: ulx3s +13 (6 -> 19) is a REAL regression on a single board -- the SDRAM_A1
  copper is steered into SDRAM_D4's channel on In1.Cu by the enforcement field.
  tigard +0 (was +4 at old defaults -- the old DRC regression is GONE at R0.8/C0.1),
  kitdev -1 better, haasoscope -3 better, all others equal.
- si_coupling: improves on only 2/8 boards (kitdev +27.4, tigard +4.4), NEUTRAL on
  4 (sonde_u, interf_u, glasgow, haasoscope ~-0.4), and REGRESSES on watchy (-0.1)
  and ulx3s (-4.6). The old table's headline wins -- watchy +40.6, haasoscope +10.5,
  ulx3s +3.7 -- are all GONE at R0.8/C0.1.
- final_score: improves on tigard (+0.39), kitdev (+2.34), haasoscope (+0.24);
  regresses on watchy (-0.85), ulx3s (-1.30); neutral elsewhere.

The si_coupling win the corpus keeps at R0.8/C0.1 vs the old table's numbers:

| Board | si_coup delta old (R1.5/C0.44) | si_coup delta new (R0.8/C0.1) |
|---|---|---|
| tigard | +7.1 | +4.4 |
| watchy | +40.6 | -0.1 |
| sonde_u | +0.0 | +0.0 |
| interf_u | +0.0 | +0.0 |
| kitdev | +29.3 | +27.4 |
| glasgow | +0.0 | +0.0 |
| ulx3s | +3.7 | -4.6 |
| haasoscope | +10.5 | -0.4 |

The corpus keeps only ~half of its old si_coupling win (sum of deltas: old +91.2,
new +26.7), concentrated in kitdev (+27.4) and tigard (+4.4). The watchy and
haasoscope wins that dominated the old verdict do not survive the tuned band.

### Interpretation

The R0.8/C0.1 tuning was validated on the CARRIER board (its victims sit >2mm from
aggressors, so the smaller band still catches them). The corpus boards are denser:
their victims sit closer to aggressors, so the smaller band + lighter cost steer
less, and on ulx3s the remaining steering actively hurts (SDRAM_A1 pushed into
SDRAM_D4). The verdict gate -- no systematic connectivity/DRC cost beyond noise --
still PASSES (the only >3 DRC regression is ulx3s, a single board; connectivity is
equal-or-better everywhere; haasoscope +3 unrouted is within noise). But the
si_coupling benefit that justified default-ON is materially weaker at the tuned
knobs: only kitdev keeps a large win.

Recommendation: keep default ON (the gate passes and kitdev/tigard still win), but
note that the corpus-level si_coupling benefit at R0.8/C0.1 is roughly a third of
what the old defaults delivered, and ulx3s shows enforcement can still cause a
single-board DRC regression at the tuned band.

### Files

Re-score outputs under carrier_lab/si_corpus_rescore/ (git-untracked). Drivers:
carrier_lab/si_corpus_rescore.py (replay chain per board) and
carrier_lab/si_corpus_grade.py (grade both arms), both committed with this section.
---

## Middle-point probe (R1.0/C0.2) -- does a middle point dominate?

Date: 2026-08-28
Branch: optimize-via-protection-parse

The tuned-defaults re-score (above) left an open question: R0.8/C0.1 fixed the
carrier timing and the old DRC regressions, but cut the corpus si_coupling win to
~1/3 and let ulx3s DRC regress 6->19 (SDRAM_A1 steered into SDRAM_D4). The old
defaults (R1.5/C0.44) delivered strong SI wins but cost carrier +29.5% time,
tigard +4 DRC, and a carrier +3V3 RLD5 pad disconnect. Is there a middle point
that dominates both? Probed R1.0/C0.2 on exactly four gates.

### Method

Same faithful chain (aggressors -> victims -> rest) replayed from the original
si_corpus_ab logs, fresh output paths under carrier_lab/si_corpus_mid/. The mid
arm sets KICAD_SI_ENFORCE=1 KICAD_SI_ENFORCE_RADIUS=1.0 KICAD_SI_ENFORCE_COST=0.2.
OFF/ON arms are the existing deterministic rescore runs (ulx3s ON re-verified
deterministic x3 in the rescore section; ulx3s mid re-run fresh here and
deterministic: identical si_coup 78.897, DRC 6, conn 10 across two fresh chains).
Carrier timing: back-to-back OFF vs mid on the full 6-step chain
(carrier_lab/si_phase2/run_carrier_mid.sh), fresh output paths, machine-quiet
gate met at start (load 1.42; a constant ~44% CPU voice-typer process runs for
both arms -- same-run deltas only). Grading identical to the rescore:
check_connected + check_drc --clearance <floor> --clearance-margin 0.1 +
quality/score.py.

### Probe table (R1.0/C0.2)

| Board | arm | conn | unrouted | DRC | si_coup | final |
|---|---|---|---|---|---|---|
| ulx3s | off | 9 | 3 | 6 | 85.6 | 60.56 |
| ulx3s | on (R0.8/C0.1) | 6 | 0 | 19 | 81.0 | 59.26 |
| ulx3s | **mid (R1.0/C0.2)** | **10** | 0 | **6** | 78.9 | 59.95 |
| watchy | off | 0 | 0 | 0 | 31.5 | 54.96 |
| watchy | on (R0.8/C0.1) | 0 | 0 | 0 | 31.4 | 54.11 |
| watchy | **mid (R1.0/C0.2)** | **1** | 0 | 0 | **53.0** | 55.09 |
| haasoscope | off | 12 | 0 | 3 | 73.8 | 66.99 |
| haasoscope | on (R0.8/C0.1) | 9 | 3 | 0 | 73.3 | 67.23 |
| haasoscope | **mid (R1.0/C0.2)** | **8** | 1 | 2 | **87.7** | 68.74 |

Carrier timing (back-to-back, same-run):

| arm | step6 user (s) | vs OFF |
|---|---|---|
| OFF | 420.78 | -- |
| mid (R1.0/C0.2) | 450.62 | **+7.1%** |

Carrier connectivity: BOTH arms fully connected (ALL NETS FULLY CONNECTED); the
+3V3 RLD5 pad (pad1 at 40.175,73.5) is connected in both -- mid lands a direct
+3V3 segment on the pad. Carrier DRC: OFF 1 violation, mid 0.

### Verdict: NO default change -- the knob is board-dependent

The middle point does NOT dominate; it trades one set of regressions for another.

- **Probe 1 (ulx3s): FAIL.** DRC returns to ~6 and unrouted to 0, but
  connectivity regresses to conn=10 -- WORSE than both off (9) and the current
  defaults (6). The +3V3 net alone has 7 disconnected pads (U1), plus GP0,
  SDRAM_D8, SDRAM_CLK, LED7, SD_D0, WIFI_EN, SDRAM_D15, SDRAM_D14, /power/FB1.
  Deterministic across two fresh chains.
- **Probe 2 (watchy/haasoscope): PASS.** SI recovery comes back meaningfully:
  watchy si_coup +21.5 vs off (old defaults +40.6, current ~0), haasoscope +13.9
  (old +10.5, current ~0). BUT watchy gains a new connectivity issue (conn=1,
  +3V3 U4 pad at (82.68,85.31) disconnected) that neither arm has.
- **Probe 3 (carrier timing): FAIL.** step6 user +7.1% (420.8 -> 450.6s), over
  the +5% budget.
- **Probe 4 (carrier connectivity): PASS.** Fully connected; RLD5 +3V3 pad stays
  connected; DRC actually better than OFF (0 vs 1).

**Binding constraints: ulx3s connectivity (+4 vs current defaults) and carrier
timing (+7.1% > +5%).** The knob is genuinely board-dependent: R1.0/C0.2 recovers
the SI win on the dense corpus boards (watchy/haasoscope) but at the cost of
ulx3s connectivity and carrier time -- the same knob that fixes one board breaks
another. There is no single radius/cost pair that dominates.

**Recorded recommendation for a future Rust/py design: per-board adaptive
scaling.** The exposure analysis already explains the mechanism: on the carrier,
victim segments sit >2mm from aggressors (the metric's coupling window is only
1.0mm), so a small band wastes steering; on dense corpus boards victims sit close,
so a large band over-steers into DRC/connectivity trouble. A per-board radius
derived from the actual victim-aggressor distance distribution (e.g. the metric's
own coupling window vs the board's exposure profile) would let enforcement keep
the SI win without the cross-board regressions. Defaults stay at R0.8/C0.1.

### Files

Mid-arm outputs under carrier_lab/si_corpus_mid/ (git-untracked). Drivers:
carrier_lab/si_corpus_mid.py (mid faithful chain), carrier_lab/si_corpus_mid_grade.py
(grade off/on/mid), carrier_lab/si_phase2/run_carrier_mid.sh (carrier timing A/B),
all committed with this section.
