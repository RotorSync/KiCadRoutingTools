# hw12 vs hw19 promotion regrade (2026-09-01, supervisor)

Graded the FINAL (n) boards of both arms. CAVEAT that decides everything:
hw19 = carrier_lab/hw_sweep/hw19 (ORIGINAL sweep, pre-writer-fix code);
hw12 = carrier_lab/hw_sweep_head/hw12 (HEAD incl. a1492506 + bb490f78).
Different code -> NOT a clean A/B. The finisher session died (loop) before
re-running hw19/hw15 on HEAD, so the promotion is UNDECIDED, not failed.

| board | hw19 conn | hw12 conn | DRC 19->12 | vias 19->12 | jog 19->12 | score 19->12 |
|---|---|---|---|---|---|---|
| glasgow | 4 issues | 1 UNROUTED + 3 issues | 0->0 | 522->514 | .574->.501 | 61.3->60.9 |
| haasoscope | 1 unrouted + 5 | 3 issues | 0->0 | 607->569 | .339->.249 | 68.9->71.7 |
| interf_u | clean | clean | 0->0 | 196->175 | .252->.140 | 64.9->67.2 |
| kitdev | clean | clean | 1->0 | 494->462 | .248->.216 | 63.2->64.7 |
| sonde_u | clean | clean | 0->0 | 14->14 | .169->.179 | 68.6->65.9 |
| tigard | clean | clean | 0->0 | 155->124 | .422->.378 | 63.7->65.4 |
| ulx3s | 1 unrouted + 8 | 6 issues | 0->0 | 775->745 | .446->.389 | 61.2->60.7 |
| watchy | clean | clean | 0->0 | 105->94 | .518->.482 | 53.9->57.1 |

Read: vias down 7/8, jogs down 7/8, DRC never worse, haasoscope/ulx3s recover
their unrouted net -- but glasgow LOSES one (1 unrouted at hw12 vs 0). Under
the directional gate (connectivity equal-or-better on EVERY board) that is a
fail on one board; whether it is hw12's fault or the code delta needs the
same-code hw19 arm. NEXT: rerun hw19 (and hw15) on HEAD with the same staged
inputs (carrier_lab/hw_sweep_head/), then decide. Do not ship 1.2 on this table.
