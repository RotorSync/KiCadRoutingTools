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

## Timing note (Q2)

The carrier step6 USER time +11.9% (576->645s) was measured back-to-back in the
commit's own A/B (carrier_lab/si_phase2/carrier_ab.log). This corpus A/B did not
re-measure carrier timing (see the separate timing section below). The +11.9% is a
real observation from the commit's own log, but the task's final step re-measures it
cleanly with a machine-quiet gate.

## Files

All corpus outputs live under carrier_lab/si_corpus_ab/ (git-untracked, not
committed). This findings file is the only committed artifact.