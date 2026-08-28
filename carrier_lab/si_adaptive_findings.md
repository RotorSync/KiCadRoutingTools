# Adaptive SI Enforcement Radius (KICAD_SI_ADAPTIVE) Findings

Date: 2026-08-28
Branch: optimize-via-protection-parse

## Question

The fixed radius/cost knobs (R0.8/C0.1) were tuned on the carrier board, whose
victims sit >2mm from aggressor copper. The corpus probe (si_corpus_findings.md
"Middle-point probe") proved the knob is board-dependent: dense boards whose
victims hug aggressors (watchy, haasoscope) need a WIDE band to recover their
si_coupling win, while sparse boards (carrier, ulx3s) need a NARROW band to
avoid wasted steering and DRC/connectivity regressions. No single fixed
radius/cost pair dominates. This lane implements per-net ADAPTIVE enforcement
radius derived from the measured victim-aggressor distance distribution.

## Design

At stamp time the only victim geometry available is the victim net's PADS (the
chain routes aggressors first, then victims, so a victim has no own copper
yet). For each victim net we measure the distance from its pads to the nearest
AGGRESSOR copper on the pad's own layer(s), and size the enforcement band from
that distribution:

    frac_close = fraction of the net's pads within the metric's own 1.0mm
                 coupling window of aggressor copper
    radius     = clamp(0.5 + 1.0 * min(1, frac_close / 0.30), 0.5, 1.5)
    cost       = base_cost * (0.5 + 0.5 * radius / 0.8)

A net whose pads already hug aggressors (frac_close high) gets a wide band
(real steering); a net whose pads sit far away gets a small band (no wasted
steering, no timing cost).

Own-pad exemption: a victim must ALWAYS be able to reach its own pads, even
when aggressor copper hugs them (watchy mid probe: the +3V3 U4 pad sits 0.01mm
from +3V3 aggressor copper and the wide band blocked its approach -> conn 1).
Enforcement cells within a small radius of the victim's own pads are dropped.

KICAD_SI_ADAPTIVE=0 restores the fixed R0.8/C0.1 behaviour exactly (verified
bit-identical field output).

## Measured pad-distance signal per board

| Board | pad median (mm) | frac pads <=1.0mm | adaptive radius mean |
|---|---|---|---|
| watchy | 1.01 | 47% | 1.00 |
| haasoscope | 2.08 | 18% | 1.01 |
| ulx3s | 2.80 | 13% | 0.81 |
| tigard | 3.96 | 19% | 0.88 |
| kitdev | 25.8 | 0% | 0.50 |
| glasgow | 9.4 | 0% | 0.50 |
| carrier | 4.0 | 7% | 0.50 |

## Gate results (complete)

All gates run on the faithful chain (aggressors -> victims -> rest) replayed
from the original si_corpus_ab logs, fresh output paths under
carrier_lab/si_corpus_adaptive/. OFF/ON arms are the existing deterministic
rescore runs. Grading: check_connected + check_drc --clearance <floor>
--clearance-margin 0.1 + quality/score.py. All nice -n 19.

### Adaptive arm per board (vs OFF and fixed-ON R0.8/C0.1)

| Board | OFF conn/DRC | ON conn/DRC | AD conn/DRC | OFF si | ON si | AD si |
|---|---|---|---|---|---|---|
| tigard | 0/1 | 0/1 | 0/1 | 88.5 | 92.9 | 88.5 |
| watchy | 0/0 | 0/0 | 0/0 | 31.5 | 31.4 | **25.3** |
| sonde_u | 0/0 | 0/0 | 0/0 | 100 | 100 | 100 |
| interf_u | 0/0 | 0/0 | 0/0 | 100 | 100 | 100 |
| kitdev | 0/1 | 0/0 | 0/1 | 70.7 | 98.1 | **98.3** |
| glasgow | 6/0 | 6/0 | 6/0 | 100 | 100 | 100 |
| ulx3s | 9/6 | 6/19 | 9/2 | 85.6 | 81.0 | **82.2** |
| haasoscope | 12/3 | 9/0 | 7/1 | 73.8 | 73.3 | **70.1** |

### Gate-by-gate verdict

**Gate 1 (ulx3s conn<=6 / DRC<=6 / unrouted=0): FAIL.** No configuration
achieves the triple on ulx3s. The fixed R0.8/C0.1 arm hits conn=6/unrouted=0
but DRC=19 (SDRAM_A1 steered into SDRAM_D4). The adaptive arm classifies ulx3s
as SPARSE (median 2.8mm >= 2.5) so every net gets the R=0.5 floor, which
behaves like OFF: conn=9/unrouted=3/DRC=2. A forced-radius sweep (R=0.6:
conn9/unrout0/DRC2; R=0.7: conn6/unrout3/DRC11; R=1.0/C0.2 mid: conn10/unrout0/
DRC6; R=1.5/C0.44 old: conn11/unrout2/DRC1) shows every radius trades one of
the three terms for another -- no point satisfies all three.

**Gate 2 (watchy conn=0 + si>=+10; haasoscope conn<=9 + si>=+5): FAIL.** The
adaptive arm keeps connectivity clean (watchy conn=0, haasoscope conn=7) but
REGRESSES si_coupling vs OFF (watchy -6.3, haasoscope -3.7). The adaptive cost
scaling (C~0.113 at R=1.0) is below the SI threshold: the mid-probe proved
C=0.2 at R=1.0 recovers the SI win (watchy +21.5, haasoscope +13.9) but at the
cost of watchy conn=1 (reconciliation rips +3V3 as a blocker for ACC_INT_1).
Exemption tuning was swept exhaustively:
- all-pads exemption (R1.0/C0.2): conn=0 but si_coup=31.53 (= OFF, win gone)
- aggressor-pads exemption (R1.0/C0.2): conn=0 but si_coup=31.53 (= OFF)
- margin 0.05 (R1.0/C0.2): conn=1, si_coup=52.54 (+21.0, conn still fails)
On watchy the SI win and connectivity are MUTUALLY EXCLUSIVE: enforcement that
produces SI wins necessarily creates the +3V3 rip; exempting the pad zones
fixes conn but kills SI.

**Gate 3 (carrier step6 <=+5% / all connected / DRC<=OFF): PARTIAL.** Both
arms fully connected (EXIT=0, +3V3 RLD5 pad connected in both -- the rescue
recovers it). DRC: AD=0 <= OFF=2 (PASS). Timing: two back-to-back runs gave
+9.0% (488.6->532.6s) and +16.0% (488.3->566.4s) -- both over the +5% budget.
BUT the iteration counts are identical (7,556,298 vs 7,555,302) and the AD arm
ran during a load spike (uptime load 3.98 vs OFF's 2.77; a constant ~48% CPU
voice-typer process plus GUI tenants). The adaptive path's own overhead is
negligible (median compute 0.17s once, per-victim field ~2ms; fingerprint build
0.0001s). The +9-16% is machine load noise on a shared desktop, not adaptive
cost -- but on this machine the +5% budget cannot be cleanly demonstrated.

**Gate 4 (8-board: no board worse on conn/DRC + si aggregate >+26.7): PARTIAL.**
No board is worse on connectivity (all equal or better; haasoscope -5) or DRC
(all equal or better; ulx3s -4, haasoscope -2). But the si_coupling aggregate
is +14.2 (tigard 0, watchy -6.3, sonde_u 0, interf_u 0, kitdev +27.6, glasgow
0, ulx3s -3.4, haasoscope -3.7) -- BELOW the fixed-default's +26.7 aggregate.
The adaptive arm keeps only kitdev's large win; the watchy/haasoscope wins that
justified enforcement are lost because the adaptive cost is too weak to steer.

**Gate 5 (suite 276/4/110): PASS.** tests/run_all.py --fast gives exactly
276 passed / 4 failed / 110 skipped (the same 4 pre-existing failures as the
baseline: connection_width_grading, exact_clusters, plane_score,
run8_locked_contact -- none related to SI enforcement).

### Verdict: HONEST NEGATIVE -- adaptive does not pass the gates

The adaptive heuristic as designed FAILS gates 1, 2, and gate 4's SI aggregate.
The root cause is the same board-dependence the fixed knobs hit, now per-net:
- On ulx3s the sparse classification (median>=2.5) floors every net at R=0.5,
  which behaves like OFF and misses the fixed R0.8's conn=6 -- but the fixed
  R0.8 itself fails DRC (19). No radius fixes ulx3s.
- On watchy/haasoscope the adaptive cost scaling is too weak to steer (C~0.113
  vs the C=0.2 that produces SI wins), and raising it to C=0.2 causes a
  reconciliation rip of +3V3 that no exemption variant fixes without killing
  the SI win.

The implementation is committed as a documented, default-OFF experiment
(KICAD_SI_ADAPTIVE=0 restores the fixed R0.8/C0.1 behaviour exactly). The
measurement JSONs and drivers are committed for reproducibility. The honest
recommendation: keep the fixed R0.8/C0.1 defaults; a per-board adaptive radius
does not dominate them on this corpus.

### Files

Adaptive-arm outputs under carrier_lab/si_corpus_adaptive/ (git-untracked).
Drivers: carrier_lab/si_corpus_adaptive.py (adaptive faithful chain),
carrier_lab/si_corpus_adaptive_grade.py (grade off/on/ad),
carrier_lab/si_corpus_adaptive_force.py (forced-radius sweep), all committed
with this section. Measurement JSONs: carrier_lab/si_adaptive_meas_*.json.
The knob lives INLINE in py_router/si_enforce.py (read directly from the env,
like KICAD_SMOOTH_PREWINDOW), so env_knobs.py stays clean for concurrent lanes.

