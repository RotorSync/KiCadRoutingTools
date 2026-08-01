# test-board run 1 — the evidence behind the fixes

A full 20-iteration placement+routing convergence on `edgehero/test-board`
(RP2350A breakout, 51×21 mm, 2 layers, 42 footprints, ~50 `HW-TB-*` requirements),
driven by `plan-pcb-routing`. Kept here, not in test-board: it is evidence about
**this** repo's skill and tooling, and the board it produced was deliberately
discarded (PR edgehero/test-board#23, closed unmerged).

The board reached `blocking = 11` and stopped on **budget exhaustion**. That is
the uninteresting half. The interesting half is that it found six real defects,
all since fixed on `fix/test-board-run-findings` — see
[`../../FINDINGS-test-board-run.md`](../../FINDINGS-test-board-run.md) §H for the
disposition table.

| file | what |
|---|---|
| `convergence.json` | the 20-iteration ledger: lever, score, accepted/reverted, note per iteration |
| `convergence.gif` | the delivered lineage — seed → placement → QFN fanout → signals → GND pour → plane repair |
| `score_FINAL.json` | the delivered board's score, under the OLD scorer |

**`score_FINAL.json` was produced before the scorer was fixed**, so read it with
the three known errors in mind: `broken` counts nets rather than separations,
`length = 1` is a phantom from a skipped group, and `undersized = 0` is a false
negative that missed 47 segments under a HARD ≥0.4 mm rail rule. Re-scoring the
same board with the fixed tool gives `blocking = 19` — higher, and honest.

Two numbers worth keeping, because they are what the fixes are measured against:

- **155 of 785 segments at 0.127 mm** against a 0.15 mm HARD floor, with
  `--track-width 0.16` correctly passed. Cause: `net_rescue` retrying at the fab
  floor. Now bounded by `--track-width-floor`.
- **A GND via at (124.20, 82.40)** on a board whose south edge is y=81.0 — 1.40 mm
  off the PCB. Cause: `route_planes` never threading the copper-to-edge clearance
  into the GND-via obstacle map. Now 0.
