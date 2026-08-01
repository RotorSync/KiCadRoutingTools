# What the test-board run found about `plan-pcb-routing`

A full 20-iteration placement+routing convergence on `edgehero/test-board`
(RP2350A breakout, 51×21 mm, 2 layers, 42 footprints, ~50 requirements), driven
by the rewritten skill. Board delivered at `blocking = 11`; PR edgehero/test-board#23.

The board is the lesser deliverable. This is the other one.

---

## A. Where the skill was WRONG

**A1. "Use the printed flags as-is" is wrong on any board with a spec.**
Step 4 tells you to take `list_nets.py --design-rules` output as authoritative.
On this board that output actively contradicts three HARD requirements:

| `--design-rules` says | the spec says |
|---|---|
| `--via-drill 0.25` | HW-TB-PCB08: **0.3 mm**, HARD |
| *"drop `--track-width` to the fab floor 0.1 … BELOW the board's own rule, like the human originals"* | HW-TB-PCB07: 0.15 HARD, and explicitly *"shall not be routed to"* 0.10 |
| `check_drc.py --clearance 0.1` | classes are 0.16 / 0.45; grading at 0.1 hides real violations |

The skill's own headline routing advice — *"Route signals at the FAB floor by
default … thinner is monotonically better on both axes"* — is **how the previous
run produced 267 segments at 0.127 mm**. It is good advice for a corpus board with
an aspirational netclass and no requirements document. It is wrong for a board
whose spec sets a floor above the fab's. The skill states the rule without the
precondition.

**Fix:** make the spec outrank `--design-rules`. Add a rule of the form *"if the
board has a requirements document, its floors win; `--design-rules` reports fab
capability, not permission."*

**A2. The `kicad` groups claim is corpus-specific and misleading.**
Step 9.2: *"`kicad` groups exist on **0 of 27** in-repo boards. If it fires, trust
it and stop looking"* and *"Groups exist → 20 iterations per group."*

This board has **8** `kicad:` groups — a Zener-generated board carries one per
module (Mcu, CoreReg, Usb, Buttons, Flash, Power, Xtal, Led). Following the rule
gives 8 × 20 = **160 iterations** for a 42-part board, and the groups are
functional modules sharing one congested centre, so iterating per-group would
route a fraction and report success on that fraction — the exact hazard the skill
warns about elsewhere for `route.py --group`.

**Fix:** the per-group budget needs a size/decomposability test, not just "groups
exist". And the 0-of-27 figure should be labelled as KRT's own corpus.

**A3. The unplaced-board detection signal does not fire.**
Step 0: *"unplaced (the tools exit 3) → **NO** — out of scope; report and stop."*
On a genuinely unplaced board (42 footprints at `pcb layout` defaults, no outline,
`board_bounds: None`), `place_optimize.py --suggest-locks` exited **0** and gave
advice. Had I followed the decision table by its stated signal, I would have
stopped and reported the board as placeable when it was not — or, worse, treated
exit 0 as "placed" and refined a pile of parts.

---

## B. Where the skill was SILENT, and it cost the most

**B1. `--fab-overrides` is not mentioned anywhere. It is the single most
important flag on a spec'd board.**

Without it, with `--track-width 0.16 --via-size 0.6 --via-drill 0.3` all correctly
passed, `route.py` emitted **155 of 785 segments at 0.127 mm** and **7 vias at
0.25/0.15**. `--fab-tier standard` *silently auto-escalates to `advanced`* when it
wants a finer escape; supplying an overrides file is what disables escalation.
Adding one file took `undersized` from **169 → 0**.

This is the whole 141/141-vias failure class, and the skill gives no way to find
the flag. I found it by grepping `--help`.

It has a trap of its own worth documenting in the same breath: its `clearance`
key **replaces** the per-class clearance map rather than flooring it. Pinning it
to the spec minimum (0.15) rather than the board's Default class (0.16) is why
XTAL_12M's 0.45 mm clearance was not honoured — restoring 0.45 afterwards produced
126 violations.

**B2. No step says to copy the `.kicad_dru` sibling.**
The skill is emphatic about never `cp`-ing a board without its `.kicad_pro`
(rightly). Sibling lookup for rules is `splitext(board)[0] + ".kicad_dru"`, per
board stem, so every intermediate board needs it too. Nothing says so.

**B3. Placement tools write no `.kicad_pro` sibling at all.**
`place_optimize.py` writes only the `.kicad_pcb`. Every downstream step then reads
no project and resolves its floor from the stock netclass — the precise failure the
`copy_board.py` warning exists to prevent, arriving through a door the skill does
not guard.

**B4. Nothing warns that `--track-width` is advisory.**
`route.py` silently substitutes the fab floor when the requested width does not
fit. Measured: `--track-width 0.8` on the R7→J1 half that never touches the QFN →
**0.127 mm**, `failed_single` empty, no warning. And `--power-nets-widths` is
ignored entirely for 2-pad nets (it only reaches multipoint power trees) — so
`--power-nets USB_DP … --power-nets-widths 0.8` produced 0.16 mm.

Between them these root-cause the previous run's *"USB_DP had no 0.8 mm segment"*
as a **tool defect, not operator error**. A skill that tells you to pass a width
must tell you to verify the width landed.

**B5. The DRC writeback's blast radius is understated.**
The skill mentions the `.kicad_pro` floor carryover. What actually happens: all
six net classes are flattened to one row (0.15/0.15/0.6/0.25) — GND_REF and PWR's
0.4 mm track, USB_FS_DIFF's 0.8 mm and XTAL_12M's 0.45 mm clearance all gone — and
`via_drill` goes to **0.25, below the board's HARD 0.3**. A later step reads that
back as nominal.

**B6. No guidance on ordering a per-net width requirement.**
Getting XTAL to 0 vias (HW-TB-PCB22) required routing it **on one layer, first,
before the general pass**, so it claimed a via-free path. The skill has this
pattern — Step 2b, for impedance nets — but frames it as impedance-only. It is the
general answer to any per-net geometric constraint.

---

## C. Where the SCORE was wrong (`scripts/board_score.py`)

**C1. A skipped length group counts as a failure.** `score_length` does
`failures += 1` on the `'no track path between the pad pair'` skip. Every
0-copper board scores `length = 1` per group forever, double-counting what
`unrouted` already measures. **1 of the delivered board's `blocking = 11` is
spurious; the honest figure is 10.**

**C2. `undersized` checks global floors, not per-net required widths.** It reported
**0** while 47 segments breached HW-TB-PCB25's ≥0.4 mm. The previous run's
"USB_DP had no 0.8 mm segment" would *still* not be caught. Caught by the `spec`
verifier lens, not by the score.

**C3. `broken` counts nets, not pads.** A net with 23 stranded pads and one with 5
score identically — so the score ranked iteration 15 *worse* (11 vs 9) than
iteration 12 when it had taken GND's disconnected components 10 → 6 and stranded
pads 23 → 5. I delivered against the score on measurement.

**C4. The lexicographic model treats incomparable defects as equal.** A pad-to-pad
**short** and a courtyard 1.4 mm outside a zone I drew myself are one unit each.
So are a manufacturing defect and an intent breach.

**C5. It cannot distinguish a mandatory chain stage from an optimisation.**
Pouring GND *must* happen; it adds vias, so `quality` degrades and `blocking` may
rise. Applying the accept rule literally would reject the pour.

**C6. `--length-groups` documents inline JSON and wants a file path.** The `--help`
string shows `{"group": {"nets": [...]}}`; the code does `os.path.isfile`.

---

## D. The lenses — which earned their cost

Nine lenses, five subagents. **`intent`, `legality`, `coverage` PASS;
`connectivity`, `drc`, `spec`, `routing-feedback` FAIL.**

- **`routing-feedback` was the most valuable and I would not have caught it.** It
  found that two ledger entries carried *another board's* score file (I passed
  `--score wk/score_iter05.json` when ledgering iteration 9), so iteration 10's
  headline delta was measured against the wrong baseline. It also caught that
  iteration 17's "distinct lever" produced byte-identical quality to iteration 15,
  so stop condition 3 rested on **two** levers, not three — and that I applied the
  accept rule unevenly (rejected 47→47 at iter 1, accepted 18→18 at iter 6). All
  correct. Corrections are in `convergence.json`.
- **`drc` did real adversarial work**, decomposing overlap arithmetic
  (0.500 = 0.300 clearance + 0.200 half-width) to separate 4 benign castellated
  endpoints from 1 genuinely off-board via.
- **`spec` found C2**, the score's own false negative.
- **`intent`/`legality` were near-noise** — they restated `check_floorplan`'s JSON.
  Worth folding into one lens.

The `evidence=` requirement did its job: every FAIL carried a file+pointer, and
that is why I could act on them immediately.

---

## E. Smaller things

- `render_placement --json` is a **bare flag** printing a `JSON_SUMMARY:`-prefixed
  line into stdout among progress text — not a path, and not clean JSON. The
  trigger table's "add `--json`" reads like `board_score.py --json <path>`.
- With `--focus`, `render_placement -o X.png` treats `X.png` as a **directory**.
- The skill's Step 3 pitch calculation reported **0.05 mm** for U1 — it picked up
  the thermal-via array, not the 0.4 mm perimeter pitch. Right verdict, wrong
  reason; on a different part that arithmetic would mislead.
- `kicad-cli` was absent, so the oracle recheck never ran. The skill should say
  what that costs (zone-aware grading) rather than letting a one-line note pass.
- HW-TB-PCB14, this board's only custom DRC rule, is **netclass-scoped**, so
  `kicad_dru.read_board_layer_clearances` returns `{}` with *"skipped, KiCad will
  still enforce it"*. Neither the router nor `check_drc` enforces it. With no
  kicad-cli, **nothing in the run graded it** — and the score cannot list it in
  `ungraded` because it never knew about it.

---

## F. What worked, and should not be lost

- **Step 0a (spec locks before the lock advisor) was correct and load-bearing.**
  The advisor found **4** refs; the spec fixes **20**. Its own output says the
  lexical rules "miss house libraries entirely" — on this board they missed every
  decap, the regulator cluster and U1.
- **`must_lock` in the intent caught a real regression**: at 2 mm the quench walked
  Y1 1.40 mm past its zone. Prose would not have.
- **The score caught what the router's self-report never would** — a pad-to-pad
  short in the *seed*, before any copper existed.
- **The lever ladder pointed correctly.** Failures radiating from U1's pad ring in
  all four directions read as an escape problem, and `qfn_fanout` took `blocking`
  18 → 8 with `failed_single` empty.
- **Stop conditions 3 and 4 both fired with measurements**, which is what stopped
  this run from grinding 20 iterations at an unsatisfiable requirement.

---

## G. What I would change first

1. Document `--fab-overrides` in Step 4, with the clearance-replacement caveat. **(B1)**
2. Make the spec outrank `--design-rules`. **(A1)**
3. Fix `score_length`'s skip-as-failure. **(C1)**
4. Give `undersized` per-net required widths, not just floors. **(C2)**
5. Make `broken` count pads. **(C3)**
6. Add "verify the width landed" after any width-bearing route step. **(B4)**
7. Replace "groups exist → 20 per group" with a decomposability test. **(A2)**
