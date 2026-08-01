# test-board run 2 — the evidence behind the run-2 findings

Second end-to-end convergence on `edgehero/test-board` (RP2350A QFN-60 breakout,
51 × 21 mm, 2 layers, 42 footprints, ~50 `HW-TB-*` requirements), from a pristine
board, under `plan-pcb-routing` as written and with run 1's six fixes in place.

Kept here rather than in test-board: it is evidence about **this** repo's skill and
tooling. Findings: [`../../FINDINGS-test-board-run2.md`](../../FINDINGS-test-board-run2.md).

| file | what |
|---|---|
| `convergence.json` | the 11-iteration ledger, with the stop condition and the HARD clauses left unmet |
| `convergence.gif` | seed → placement → XTAL → USB → QSPI → bulk → GND pour → repair |
| `score_FINAL.json` | the delivered board's score |
| `kicad_drc_filled.json` | `kicad-cli pcb drc` on the delivered board **with the zone filled** — the number KRT's own checkers cannot produce |

## Read the two DRC numbers together, not separately

```
check_drc.py final.kicad_pcb            ->   2 violations
kicad-cli pcb drc (zone filled)         -> 167 violations
```

Both are correct. 161 of the 167 are the board's two `.kicad_dru` rules —
`HW-TB-PCB14` (152) and `HW-TB-PCB23` (9) — and both are **netclass-scoped**, which
`read_board_layer_clearances` skips by design (it reads only layer-scoped rules) with
a note saying KiCad will still enforce them. On this board those two rules are the
entire reason the `.kicad_dru` exists, so the board's two hardest requirements are
exactly the ones KRT cannot grade. See FINDINGS §B7/§B12.

## Three numbers worth keeping

- **48 → 15 unconnected items**, same file, zone fill the only difference. `route_planes`
  writes the pour unfilled, so every KiCad-side check reads the plane as empty. 15 is
  exactly what `check_connected` reports — the tools agree, but only after a fill nothing
  in the chain performs. This produced a **wrong `VERDICT=FAIL` from the `connectivity`
  verifier lens**, since withdrawn.
- **0.200 mm available against 0.45 mm demanded** on the RP2350A QFN-60's 0.4 mm pin
  pitch. HW-TB-PCB23 read as a pad-to-pad netclass clearance stops the crystal leaving
  its own MCU pin: 1/3 nets routed at 0.45, 3/3 at 0.16.
- **67.72 mm of copper for a 7.13 mm span** (`QSPI_SD3`), reported as *routed*, against a
  ≤15 mm HARD limit. Nothing in `route.py` constrains length and nothing in
  `board_score.py` grades a maximum, so the requirement is both unroutable-to and
  ungradeable.

## The board is not finished, and is not presented as finished

`blocking = 23` honest (39 as reported, minus 16 grading artefacts — FINDINGS §B7/§B8).
Stop condition **2**. Five HARD clauses remain unmet; `convergence.json#/stop_condition`
itemises them with the measurement for each, and two of them are findings about the
requirement rather than about the router.
