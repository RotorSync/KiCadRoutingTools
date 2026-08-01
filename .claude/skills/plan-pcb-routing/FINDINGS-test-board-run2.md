# test-board run 2 — where the skill and the tooling failed

Second end-to-end run of `plan-pcb-routing` on `edgehero/test-board` (RP2350A QFN-60
breakout, 51 × 21 mm, 2 layers, 42 footprints, ~50 `HW-TB-*` requirements), from a
pristine board, under the skill as written and with run 1's six fixes in place.

Run 1's findings are in [`FINDINGS-test-board-run.md`](FINDINGS-test-board-run.md).
This document records only what run 2 found. Every number below was measured on
this board in this run; where a claim is a single command's output, the command is
given so it can be re-run.

**The run-1 fixes all held.** Each is confirmed working in §D, including two that
fired exactly as designed and one whose stated behaviour turns out not to be the
behaviour the situation needed.

---

## A. Skill findings

### A1. Step 0c's acceptance rule is necessary but not sufficient — and it says it is

> *"**Acceptance rule — apply it, do not skip it.** Read the `JSON_SUMMARY:` line
> from 0c. If `crossings_after > crossings_before` or `hpwl_after > hpwl_before`,
> **discard the result and route from the original board.**"*

Applied literally, that rule **accepts a placement that breaks two HARD
requirements**. Measured, first placement iteration of this run:

| | before | after | rule says |
|---|---|---|---|
| `crossings` | 85 | 60 | improved → accept |
| `hpwl` | 602.19 | 595.70 | improved → accept |
| `overlap_area` | 0.0184 | 0.0 | improved |
| **HW-TB-COMP06** U3.8 → nearest 100 nF | **2.04 mm** | **9.57 mm** | *not read* |
| **`check_floorplan --intent`** | PASS | **exit 4, `zone_containment`** | *not read* |

Both regressions are real: COMP06 is HARD and the intent violation was Y1's
courtyard 1.40 mm outside its declared `crystal` block, which also lengthens the
XTAL legs against HW-TB-PCB22's ≤10 mm.

The skill *does* list `check_floorplan` in the Step 9 lever table and in the
trigger table. But Step 0c's own acceptance rule — the one written in bold, with
"do not skip it" attached — names only the two optimizer metrics, and it is the
rule a reader applies at the moment they have the `JSON_SUMMARY` in front of them.

**Fix:** make the Step 0c acceptance rule a conjunction — crossings and hpwl must
improve **and** `check_floorplan --intent` must still pass **and** any repo-local
requirement gate must still pass. Two numbers produced by the optimizer cannot
adjudicate a requirement the optimizer has no term for.

### A2. Rotation is invisible to every check the skill prescribes

The COMP06 regression above came from `place_optimize` **rotating U3 by 180°**.
U3's footprint origin never moved. So:

- a position diff of the two boards shows **nothing** — U3 is not in the moved list;
- the delta render the skill tells you to `Read` shows **no arrow** for U3;
- `parts_moved` counts it, but 14 other parts moved too;
- the requirement it broke is about a **pin**, and a 180° rotation puts pin 8 at the
  opposite corner of the package.

The skill's Step 0a is good on *which* parts to lock and why, but it frames locking
as being about **position** ("mechanical facts, not optimizer variables", "an exact
XY"). Nothing says the quench rotates, and nothing says a part whose **pin
geography** is load-bearing must be locked even when its position is free.

**Fix:** say it in Step 0a. The rule is not "lock what the spec pins to a
coordinate", it is "lock anything whose **pin positions** a requirement depends on".
On this board that added U3 (COMP06), Y1/C3/C4 (PCB22/23) and R7/R8 (PCB13/16) to
the lock set — six parts the spec gives no coordinates for at all.

Cost of those six locks, and it is worth stating plainly because it looks like a
regression: `crossings` 85 → 74 instead of 85 → 60. That is the same trade the
skill already records for decaps ("crossings 52 to 60 … the correct trade").

### A3. ~~Step 2c's exclusion does not protect the copper it routed~~ — WITHDRAWN, the claim was false

**This finding was wrong and is retracted in full.** It is left in place, struck
through, because how it was wrong is more useful than the claim was.

I reported that the bulk pass rips Step 2c copper, on the evidence that
`XIN`/`XOUT`/`XTAL_XOUT` routed 3/3 in their own pass and that `check_spec.py` then
reported `XOUT leg: NO ROUTED PATH` in the finished board. I concluded the leg had
been "left in two pieces", and wrote `lock_nets.py` to prevent it.

Both halves were wrong:

1. **The measurement was my own instrument's bug.** `check_spec.py` built its graph
   from segment *endpoints* only, so a segment ending mid-span of another — an
   ordinary T-junction, which KiCad joins and the router emits routinely — read as a
   break. `XIN`, `XOUT`, `XTAL_XOUT` and `USB_DP` each have at least one. Fixed
   (split every segment at any other endpoint lying on it; bridge every pad of the
   net as a zero-cost hub), the leg is **continuous**.
2. **The claim itself does not reproduce.** Directly tested: strip all 52 copper
   locks from the post-XTAL board, run the bulk pass with `--max-ripup 20` and the
   XTAL nets excluded, count again.

   ```
   XTAL segments BEFORE bulk pass: 32
   XTAL segments AFTER  bulk pass: 32
   ```

   Exclusion **is** sufficient. The skill was right and I was not.

`lock_nets.py` therefore solves a problem that does not exist. It is harmless and
stays in the chain (locked copper is still a reasonable belt-and-braces on a
multi-pass chain), but it is not load-bearing and its docstring's justification is
withdrawn.

**What the corrected instrument actually shows** — and this is a real finding the
broken one was concealing:

| clause | broken checker said | corrected |
|---|---|---|
| HW-TB-PCB22 XOUT leg ≤10 mm | `NO ROUTED PATH` | **18.09 mm — 81% over a HARD limit** |
| HW-TB-PCB23 leg symmetry ≤1 mm | `unmeasurable, a leg is unrouted` | **12.64 mm — 12.6× over a HARD limit** |

A "no routed path" reads as an instrument gap. An 18.09 mm leg reads as a defect.
Replacing the second with the first is the worst failure mode an instrument has, and
mine had it. **The lesson that generalises:** a hand-written checker used to grade
what the toolchain cannot has no tests and no second opinion, and this run shipped
findings resting on one for several hours. The adversarial verifier lens caught it;
nothing else did, and nothing else could have.

### A4. Nothing says to restore the net classes after a `--clearance` pass

CLAUDE.md and the skill both explain that `--clearance` is a ceiling and that the
writeback clamps every class down to what was routed. Neither says what to do when
a **multi-pass** chain has one pass that needs a tighter clearance than another
class wants to keep.

On this board the USB pass must pass `--clearance 0.15`, because
`route_diff.py:236` raises any `--diff-pair-gap` below the call's clearance up to
it and HW-TB-PCB13's gap is 0.15 against a Default class of 0.16. Measured
immediately after that pass:

```
[XTAL_12M] clearance=0.1442     <- was 0.45 (HW-TB-PCB23, HARD)
[USB_FS_DIFF] clearance=0.1442  <- was 0.16
[Default] clearance=0.1442      <- was 0.16
```

Every class, including the one the next pass depends on. Nothing in the chain
warns; the next pass just routes to a floor nobody chose.

**Fix:** the skill should say that on a spec'd board the canonical classes are
**restored** between passes, and that the restore must be re-applied after every
step, not once at the end.

### A5. The `--clearance` guidance has no advice for a per-net conflict

HW-TB-PCB23 needs XTAL copper 0.45 mm from foreign copper, but at 0.45 mm as a
class clearance the crystal **cannot leave its own MCU pin** (§C1). The resolution
is asymmetric and the skill has no vocabulary for it: route XTAL at 0.16, then make
every *later* pass keep 0.45 mm away from it, via two different
`--net-clearances` files.

`--net-clearances` is mentioned once in CLAUDE.md as an override and never in the
skill. It is the right tool for "this net's clearance to others is not the same
number as its clearance while it is being routed", which is what any 3W-style rule
needs on a fine-pitch part.

---

## B. Tooling findings

### B0. `route_diff.py` routed **nothing**, on every run, and the chain swallowed it

Under-disclosed in the first draft of this document, caught by the adversarial lens.
Every run of the chain records:

```
== 3. USB pair, connector side: coupled, 0.8mm / 0.15mm gap (HW-TB-PCB13) ==
  Diff pairs:    0/1 routed
```

`route_diff` failed on the pair it *could* name (`USB_DP`/`USB_DM`), not merely on
the one it could not (§B1). **All** USB copper on the delivered board comes from the
single-ended `route.py` rescue in step 3a. Two consequences that were being reported
the wrong way round:

- The 72.3% / 77.6% at 0.8 mm is `--power-nets-widths` on a single-ended route, not
  a coupled pair holding its width.
- The **0.15 mm gap was never router-enforced at all.** The 0.1502 mm KiCad measures
  between `USB_DP` and `USB_DM` is an uncontrolled by-product of two independent
  single-ended routes that happen to run alongside each other. Everything this
  document says about `--clearance 0.15` being required so `route_diff` does not
  floor the gap to 0.16 is *correct about the flag* and *irrelevant to this board*,
  because that code path never ran to completion.

And the chain hid it: `| grep -E 'Diff pairs|Total vias' || true` turns a failed
pass into a silent one. `|| true` was there to stop a `grep` miss killing a
`set -e` script, and it swallowed the gate as well. Two other reported failures went
past unread the same way — `KiCad-oracle recheck: 3 link(s) still unconnected` and
`GND: 49/55 pads connected to plane`.

**Fix, on both sides:** `route_diff` should exit non-zero when it routes 0 pairs
(today the JSON says so and the exit code does not), and a chain must never wrap a
routing step's output filter in `|| true` without also asserting on the tally.

### B1. `route_diff.py` cannot be told that two nets are a pair — and `/identify-diff-pairs` exists precisely to find the pairs it cannot name

```
$ route_diff.py ... --nets 'USB_DP_R' 'USB_DM_R'
Error: No differential pairs found matching the patterns!
  Differential pairs must have _P/_N, P/N, or +/- suffixes.
```

`find_differential_pairs()` (`route_diff.py:492`) is name-based only, and the
marker must be a **suffix**. `USB_DP_R` / `USB_DM_R` — the MCU side of the two
27 Ω series resistors HW-TB-PCB16 mandates — carry it mid-name. There is no
`--diff-pairs A:B` flag, no `--pair` override, nothing.

This matters more than a naming nit, because the skill's own advice is:

> *"Name-based detection misses pairs with unconventional names … run
> `/identify-diff-pairs` for datasheet-based detection by pin function."*

`/identify-diff-pairs` can *find* such a pair. Nothing can then *route* it as one.
The board's spec is explicit that this segment is a coupled pair ("the 90 Ω
geometry target applies to the two segments either side of the resistors
independently"), and it had to be routed single-ended.

**Fix:** an explicit `--diff-pair NET_P:NET_N` (repeatable) that bypasses name
detection. It is the missing half of `/identify-diff-pairs`.

### B2. `--track-width-floor` is call-scoped; `--power-nets-widths` is per-net

One pass needing 0.8 mm on `USB_DP`/`USB_DM` and 0.25 mm on `USB_DP_R`/`USB_DM_R`
cannot state both floors. Passing `--power-nets-widths 0.8 0.8 0.25 0.25` with
`--track-width-floor 0.15` produced **0.16 mm on all four** — the neck-down went
straight to the Default track width, and the floor never fired because 0.16 > 0.15.

Splitting into two calls with `--track-width-floor 0.8` and `0.25` respectively
produced the intended widths. Correct, but the flag's granularity does not match
the flag it has to work with.

**Fix:** accept a per-net form (`--track-width-floors` parallel to
`--power-nets-widths`), or document the split-the-call workaround where
`--track-width-floor` is defined.

### B3. `--net-clearances` documents an inline JSON object and opens a file

```
--net-clearances JSON   "a JSON object mapping net name -> that net's net-class clearance in mm"
```
```python
route.py:3491:  with open(args.net_clearances, encoding="utf-8") as _f:
```

Identical to run 1's B4 finding about `--length-groups`, in a second flag. Worth
sweeping every `metavar="JSON"` argument rather than fixing them one at a time.

### B4. There is no clearance floor, only a width floor

`--track-width-floor` (run 1's fix) bounds the width the neck-down and the per-net
rescue may use. Nothing bounds the **clearance**. Measured on the bulk pass before
any fab override:

```
"min_clearance_used": 0.127
```

0.127 mm is the `standard` 2-layer tier's clearance floor. HW-TB-PCB07 is
0.15/0.15 HARD and says in terms that this board "shall not be routed to" JLCPCB's
0.10 mm. So a run-1-class defect — spec-violating geometry, reported as success —
survives in the clearance axis after being closed in the width axis.

`--fab-overrides` fixes it (`min_clearance_used` → 0.15), and the skill does
document that flag well. But `--track-width-floor` and `--fab-overrides clearance`
are the same idea applied to two axes, and only one of them is discoverable from
the place you go looking.

**Fix:** either add `--clearance-floor`, or say under `--track-width-floor` that
the clearance axis is held by `--fab-overrides` and must be set too.

### B5. `(locked yes)` is position-sensitive and fails silently

`kicad_parser`'s segment and via patterns accept `(locked yes)` at exactly two
positions — directly after `(width …)` or directly after `(layer …)` — and both
are **before** `(net …)`. Written after the `uuid`, which is where it reads most
naturally and where a hand-edit or a third-party tool may well put it:

- `grep -c 'locked yes'` counts it,
- KiCad opens the board and shows the tracks locked,
- `parse_kicad_pcb` reports `locked=False` for every one of them,
- so the rip machinery — whose entire contract per CLAUDE.md is "locked copper
  makes its net never-rippable with NO override" — never sees it.

Measured: 32 segments written with the token after `uuid` → parser saw 0 locked.
Same 32 with it after `(layer …)` → parser saw 32.

**Fix:** parse `(locked yes)` positionally-freely inside the s-expression, or warn
when a segment block contains the token in a position the pattern did not match.
A silent false negative on a "never, no override" guarantee is the worst shape a
parser bug can have.

### B6. `board_score.py` cannot express a maximum length, or a leg that spans two nets

Its `length` component grades **matching** — the spread across a group of nets
against a tolerance. Three of this board's HARD clauses are not that shape:

| clause | shape | expressible? |
|---|---|---|
| HW-TB-PCB19 QSPI direct run ≤15 mm | maximum, per net | **no** |
| HW-TB-PCB22 XTAL ≤10 mm per leg | maximum, per leg | **no** |
| HW-TB-PCB23 XTAL legs matched within 1 mm | match, but a *leg* is `XOUT` + `XTAL_XOUT` in series through R2 | **no** — a group is a set of nets, and it cannot sum two of them into one arm |

Declaring `{"xtal_symmetry": {"nets": ["XIN","XOUT","XTAL_XOUT"], "tolerance_mm": 1.0}}`
looks right and measures the wrong thing: it compares three nets pairwise, when the
requirement is one two-net chain against one one-net chain.

All three had to be measured by a purpose-written checker
(`wk/check_spec.py` in the board repo, ~260 lines, shortest-path over actual
copper). That is the correct fallback and the skill's "ungraded is not passed"
discipline caught that they were unmeasured — but a `max_length_mm` key and a
`nets_in_series` arm form would let the instrument carry them.

### B7. `check_drc.py` grades the whole board at ONE clearance, not per net class

`score.json#/components/drc/graded_at` is a scalar. On a board whose classes are
0.15 (USB_FS_DIFF) and 0.16 (everything else), it graded everything at 0.16 and
produced **16 violations** — 15 `SEGMENT-SEGMENT` plus 1 `PAD-SEGMENT`, every one
of them `USB_DM ↔ USB_DP`, every one with `Overlap: 0.010mm`.

0.010 mm is exactly 0.16 − 0.15. Those 16 are the **spec-mandated pair gap**
(HW-TB-PCB13, 0.15 mm HARD) being graded against a class it does not belong to.
Re-graded at the pair's own class clearance:

```
check_drc.py final.kicad_pcb        -> FOUND 18 DRC VIOLATIONS
check_drc.py final.kicad_pcb -c 0.15 -> FOUND 2 DRC VIOLATIONS   (both castellation edge, expected)
```

The router honours per-class clearance (PR392/#439, and this run's `--net-clearances`
work depends on it). The **grader does not**, so a multi-class board is graded at
one number and the tighter class manufactures phantom violations — the exact error
CLAUDE.md warns about, arriving from inside the toolchain rather than from a
hand-picked `-c`.

**This is not cosmetic — it inverted the run's central decision.** See B8.

### B8. `blocking` ranked the worse board better, twice over

Two full chains, identical except for the order of the two constrained passes:

| | `blocking` as reported | HARD spec clauses violated | QSPI routed lengths | USB at 0.8 mm |
|---|---|---|---|---|
| qspi_first | **28** | **12** | 16.4 / 18.8 / 42.1 / **67.4** mm vs ≤15 | 0% |
| usb_first | 39 | **8** | 14.6 / 7.3 mm | 72.3% / 77.6% |

`blocking` says qspi_first is better by 11. The board is plainly worse: it violates
four more HARD clauses, its QSPI runs miss a ≤15 mm limit by up to 4.5×, and its USB
pair holds none of its mandated width.

Two independent causes, both already named above:

1. **B7** — usb_first's `drc: 18` is really `drc: 2` plus 16 artefacts of grading a
   0.15 mm class at 0.16. Correcting that alone takes usb_first from 39 to **23**,
   below qspi_first's 28 and reversing the ranking.
2. **B6** — `blocking` has no max-length component, so *none* of the QSPI length
   violations appear in it at all. A 67 mm route for a 7 mm span scores exactly the
   same as a 7 mm one.

The delivered board is usb_first, chosen on the spec evidence rather than on
`blocking`. That is the right outcome, but it required going behind the number that
the skill designates as *"the one number not produced by the thing being graded"*
and *"the only number that decides better/worse"*. On a spec'd board it is not yet
sufficient for that role.

### B9. Nothing constrains a route's LENGTH, so a max-length requirement cannot be routed to

`route.py` has `--length-match-group` (matching) and no maximum. Measured on the
qspi_first board, `QSPI_SD3` between `U3.7 (116.6, 75.9)` and `U1.55 (122.1, 71.3)`
— a 7.13 mm straight line:

```
QSPI_SD3  23 segments  total copper 67.72mm  bbox x[116.6, 142.1] y[60.9, 77.9]
```

The router went 20 mm east, past the crystal and almost to the SWD header, and came
back. It reports the net **routed**; `board_score` agrees; only a purpose-written
length check disagrees. HW-TB-PCB19's ≤15 mm is HARD, and the run had no way to ask
for it — the F.Cu-only, zero-via constraints that PCB19 *also* imposes are exactly
what forces the detour.

**Fix:** a `--max-length <mm>` (per net, or via the same JSON-file mechanism as
`--net-clearances`) that fails the net rather than accepting an arbitrarily long
path. Without it, a max-length requirement is unroutable-to and ungradeable in the
same breath.

### B10. `--rip-blocker-nets` is unusable as an iteration lever on this board

The skill prescribes it for exactly the situation this board has — plane-net pads
that cannot reach their plane because a signal trace walls the via site off (11 GND
pads: decap grounds C8/C14/C15/C17 and a CN1 castellation). Measured:

| plane repair | wall clock |
|---|---|
| plain | ~1 minute |
| `--rip-blocker-nets` | **killed at 20 minutes, still running** |

At that cost it cannot participate in a Step 9 loop at all, so the largest single
component of `blocking` on this board (`broken`) has no affordable lever. Reported
rather than worked around; the delivered chain uses the plain repair.

### B12. The pour is written UNFILLED, and every KiCad-side check then reads the board as disconnected

`route_planes.py` writes a zone **outline** with no `filled_polygon`, and prints
`Note: Open in KiCad and press 'B' to refill zones`. That note reads as cosmetic.
It is not. Measured on the delivered board, the same file, zone fill the only
difference:

| | `kicad-cli pcb drc` unconnected_items | plain `clearance` violations |
|---|---|---|
| as written by the chain | **48** (37 of them GND) | 12 |
| after `ZONE_FILLER(b).Fill(b.Zones())` | **15** | 3 |

15 is *exactly* what KRT's own `check_connected` reports (5 unrouted + 10 broken).
So the two tools agree completely — but only after the fill, and nothing in the
chain does it.

**This cost a wrong verdict inside this very run.** The `connectivity` verifier
lens, given the unfilled board's KiCad output, concluded that KiCad saw GND in 38
islands against the router's 5 and that *"the router over-credits GND"*, and
returned `VERDICT=FAIL`. It was reasoning correctly from bad evidence. The lens's
conclusion is **withdrawn**; `check_connected` was right all along.

Two consequences beyond the one bad verdict:

- `route_disconnected_planes.py`'s own **kicad-oracle recheck** runs against this
  unfilled state (it reported *"9 link(s) still unconnected per KiCad after 3
  rounds"*), so the oracle is grading a board whose plane KiCad cannot see. It does
  do a `pcbnew refill` for its island fetch, but the board it writes is unfilled
  again.
- Anyone grading a KRT board with `kicad-cli` — which CLAUDE.md positions as the
  authoritative, fill-aware oracle — gets a number that is 3× too high unless they
  know to fill first.

**Fix:** fill the zones in `route_planes.py` / `route_disconnected_planes.py`
before writing, or at minimum promote that "press B" note into a loud warning that
says KiCad-side connectivity is meaningless until it happens. The delivered chain
adds an explicit `fill_zones.py` step as its last action.

### B13. `board_score.py` records `graded_at: null` when `--clearance` IS passed

```
board_score.py board ... (no --clearance)        -> "graded_at": 0.16
board_score.py board ... --clearance 0.15        -> "graded_at": null
```

Exactly backwards: the field records the floor when it was inferred, and drops it
when it was stated. The `drc` verifier lens caught this and correctly refused to
accept the run's claimed grading floor, because the artifact did not carry it —
only a free-text `--label` did.

Combined with B7 (grading is one scalar for a multi-class board) this means a
spec'd board has **no honest single DRC number** available from `board_score`:
0.16 manufactures 16 phantom pair violations, 0.15 is looser than the Default
class and hides real ones, and whichever you choose the artifact does not say
which you chose.

### B15. `pcbnew.LoadBoard(...).Save()` silently flattens the sibling `.kicad_pro` to one net class

Filling the zones (§B12) is done with KiCad's own python, because nothing else can.
That call **rewrites the sibling project** and deletes every non-Default net class
while leaving all 15 `netclass_patterns` pointing at classes that no longer exist:

```
before fill_zones.py:  classes = [Default, GND_REF, PWR, QSPI_BUS, USB_FS_DIFF, XTAL_12M]
after:                 classes = [Default]          patterns = 15  (all now orphaned)
```

**This shipped.** The first commit of the delivered board carried the flattened
project, so KiCad resolved every net to Default 0.16 — which is precisely how the
USB pair's spec-mandated 0.15 mm gap became 12 real DRC errors. Caught by the
adversarial lens, not by any gate.

It is the same hazard CLAUDE.md documents for a bare `cp` of a board without its
`.kicad_pro` (#441), arriving through a door nothing guards: the recommended
`copy_board.py` faithfully copies a project that a *previous* step had already
destroyed.

**Fix applied:** the project restore now runs **after** the fill, and
`restore_project.py` gained a hard assertion — it previously printed the same
success line whether it had corrected both classes or found neither, because its
`done` list was simply empty. A restore that cannot distinguish "already correct"
from "the classes are gone" is not a restore.

### B16. `board_score.py` has no component for a requirement nothing models

Distinct from §B6, and sharper. `ungraded` lists components that *know* they did not
run (`impedance`, `length`). A requirement that no component models at all —
HW-TB-PCB24's plane continuity, HW-TB-PCB26's three fiducials — does not appear in
`ungraded` either. It is invisible rather than unexamined, which defeats the
"ungraded is not passed" discipline exactly where that discipline is most needed.

There is no general fix inside `board_score`; the answer is procedural, and the
`spec` verifier lens is it — the only step in the whole procedure that walks the
requirements document and asks "what measured this?" of every clause. That argues
for promoting it from a final gate to something run **early**, so the chain is built
knowing which clauses nothing will grade.

### B17. `--design-rules` still contradicts a spec'd board on four counts

Confirming run 1's C2 finding is live, and that the skill's new *"FIRST: does this
board have a requirements document?"* section is load-bearing — it is the only
thing standing between the printed suggestions and four HARD violations:

| `--design-rules` printed | the spec says |
|---|---|
| `--via-drill 0.25` | 0.3 mm, HW-TB-PCB08 HARD |
| *"drop `--track-width` to the fab floor 0.1"* | 0.15 mm HARD, and "shall not be routed to" 0.10 |
| `check_drc.py --clearance 0.1` | classes at 0.15/0.16, HW-TB-PCB07 HARD |
| `route_diff.py --track-width 0.2 --diff-pair-gap 0.2` | 0.8 mm / 0.15 mm, HW-TB-PCB13 HARD |

---

## C. Findings about the board and its spec

These belong to `edgehero/test-board`, not to this repo, and are raised there.

### C1. HW-TB-PCB23 has exactly the defect HW-TB-PCB14 has, and the same fix

PCB23 says *"≥3× trace width (≥0.45 mm), edge-to-edge, from digital and switching
nets"*. Carried as a KiCad netclass clearance it also applies **pad-to-pad**, and
then the crystal cannot leave its own MCU pin:

```
U1.21 (XIN)  nearest pad U1.22 (XOUT)     edge-to-edge 0.200mm
             nearest pad U1.20 (VCC3V3)   edge-to-edge 0.200mm
```

0.45 mm demanded, 0.200 mm available — short by 2.25×, on the vendor's own land
pattern, which HW-TB-PCB09 fixes. One routing pass, everything else identical:

| XTAL_12M class clearance | result |
|---|---|
| 0.45 | **1/3 nets routed**; XIN and XOUT both fail *at U1* |
| 0.16 | **3/3 routed, 6/6 pads, 0 vias** |

`docs/stackup-and-netclasses.md` §6 already made this exact argument for PCB14 and
moved it to `layout.kicad_dru` scoped to tracks. Done the same way here, with the
measurement recorded in the rule's own comment. **The spec wording should be
amended to say trace-to-trace** — for both clauses.

### C2. HW-TB-PCB13's 0.8 mm is unachievable on the MCU side, by 3.2×

Bisected, one net pair, everything else identical:

| requested width into U1.51/52 | result |
|---|---|
| 0.35 mm | fails |
| 0.30 mm | fails |
| **0.25 mm** | **routes** |
| 0.20 mm | routes |

U1.51/52 sit on the RP2350A QFN-60's **0.4 mm pin pitch**. No placement changes
that — it is the vendor land pattern. The connector-side segments, which have room,
hold **72.3% and 77.3% of their length at 0.8 mm** with 0 vias on F.Cu.

So PCB13 is met where the geometry allows it and is physically impossible where it
does not. As written it is unsatisfiable end-to-end; the honest form is
"0.8 mm except within the escape fan of the QFN-60, where the pin pitch governs".

### C3. USB_FS_DIFF's class clearance exceeds its own `diff_pair_gap`

`test-board.zen` gives the class `clearance = 0.16` and `diff_pair_gap = 0.15`. A
KiCad netclass clearance governs the pair's own P↔N coupling too, so a class whose
clearance exceeds its own gap makes the spec-mandated geometry a DRC violation by
construction. 0.15 is the only self-consistent value and is exactly HW-TB-PCB07's
floor.

### C4. The seed violated HW-TB-COMP06, which §8 had already diagnosed and not fixed

Three supply pins had no 100 nF within 3 mm — U1.53 (USB_OTP_VDD) at 5.15 mm,
U1.54 (QSPI_IOVDD) at 4.75 mm, U3.8 (flash VCC) at 4.79 mm — because
`make_board.py` seeds decaps **by rail** into two tidy strips, not by the pin each
serves. `docs/stackup-and-netclasses.md` §8 records this and names the fix; the fix
was never applied.

### C5. The seed's two USB series resistors were electrically backwards

R7/R8 at rot 0 put pad 1 — carrying the MCU-side net `USB_DP_R`/`USB_DM_R` — on the
**west** face, away from U1 and toward the connector, so each pair had to cross over
itself at its own series resistor. `crossings` sees this (82 vs 85 after the fix),
and the quench's own remedy is a **rotation**, which is the move A2 says must be
denied for these parts. So it has to be right in the seed.

### C6. `layout/krt/README.md`'s recipe cannot run as written

```bash
python3 -X utf8 fix_castellated.py seed.kicad_pcb \
    --footprint Castellated_1x10_P2.54mm_EdgePlated --verify seed.pre
```

`--verify PRE` asserts the patched file differs from a **pre-image** by nothing but
the property; the recipe never creates `seed.pre`, so the step dies with
`FileNotFoundError`. (Separately, the step is now a no-op: the 20 castellated pads
already carry `pad_prop_castellated` from the footprint library.)

---

## D. What the verifier lenses caught that I did not

All ten lenses ran as independent subagents. Six passed. The four that did not
earned their place, and two of them **corrected me**.

| lens | verdict | what it did |
|---|---|---|
| `intent` | PASS | confirmed the 2 skipped rules are skipped because the intent declares nothing for them, not because the checker dodged them |
| `legality` | PASS | overlap/oob bit-identical to the seed across a 14-part move |
| `delta` | PASS | independently re-argued the six extra locks as a legitimate trade rather than accepting the analogy to the decap case |
| `blocks` | PASS | verified no routing command silently acquired a `--group`, one invocation at a time, and checked the per-board budget against the internal/touching ratios (CoreReg and Power have **zero** internal nets) |
| `coverage` | PASS | partition exhaustive, symmetric difference empty |
| `connectivity` | **FAIL — withdrawn** | reasoned correctly from bad evidence: the unfilled pour (§B12). Its conclusion that `check_connected` over-credits GND is wrong; filled, the two tools agree exactly at 15 |
| `routing-feedback` | **FAIL — upheld** | caught **three** ledger defects: `parent_board` on iterations 3–4 naming a path that had also held a *rejected* board, `score_FINAL` labelled with the arm the ledger had rejected, and iteration 8 left `score: null, accepted: null`. Same lens caught a ledger error in run 1. It keeps earning its place |
| `drc` | **FAIL — upheld** | caught `graded_at: null` (§B13) and refused to accept a claimed grading floor the artifact did not carry |
| `spec` | **FAIL — upheld, and the most valuable single result of the run** | see below |
| `adversarial` | **FAIL — upheld, and it overturned a headline finding of mine** | see below |

### The adversarial lens disproved §A3 and found a defect I had shipped

It was pointed at the hand-written helpers specifically — *"these are hand-written
for this run and have no tests … if `check_spec.py`'s shortest-path measurement is
wrong, several headline findings are wrong."* It was.

- **`check_spec.py` built its graph from segment endpoints only**, so every
  T-junction read as a break. That is what produced `XOUT leg: NO ROUTED PATH`, and
  §A3's entire diagnosis rested on it. Fixed and re-measured: the leg is continuous
  at **18.09 mm** against a 10 mm HARD limit, and the legs are **12.64 mm** apart
  against a 1 mm HARD limit. §A3 is withdrawn; both violations are real and were
  being concealed as an instrument gap.
- **The delivered `.kicad_pro` had five of its six net classes deleted** (§B15), by
  the `pcbnew` save inside the zone fill. It shipped that way in the first commit.
- **`route_diff` routed 0/1 pairs on every run** and the chain's `|| true` swallowed
  it (§B0) — so this document's original account of the USB pair was wrong about
  where its copper came from.

It also flagged that `blocking = 23` is a re-grade at a clearance **looser** than the
board's own Default class, and that `graded_at: null` means the artifact never
recorded which floor was used. That is right, and it is the honest reading: 23 is
defensible only for the USB pair's own class; the board-wide number is 39, and
neither is correct for a multi-class board because `check_drc` cannot grade per
class (§B7). Both are quoted here rather than picking one.

Three of its criticisms I checked and do not accept:

- *"§C1's pad-to-pad argument"* — it agreed this one is a genuine geometric bound.
- *"the 166 KiCad errors were never read"* — they were; §B12 and the evidence
  README are built on them. They are not folded into `blocking` because
  `board_score` cannot see netclass-scoped rules at all, which is §B7's point.
- *"`chain.sh` as delivered does not reproduce the bundle"* — correct as filed, and
  now fixed: the default is `ORDER=usb_first` and the post-chain fill is a chain step.

### The `spec` lens found two HARD clauses that NOTHING in the run measured

**HW-TB-PCB24 — "GND pour unbroken beneath the USB pair and the entire QSPI bus".**
Not measured by `board_score`, not by `check_drc`, not by `check_connected`, and
not by my own purpose-written `check_spec.py`, which names the clause in its
docstring and never implements it. Independently confirmed after the lens raised it:
non-GND B.Cu copper crosses beneath both protected regions. It is a **HARD** clause
and it was silently absent from every gate.

**HW-TB-PCB26 — 3 fiducials, 1.0 mm copper / 2.0 mm mask opening.** The board has
**zero**. Confirmed: 42 footprints, none a fiducial, none matching `FID*`. Nothing
in the chain looks for a part that is *supposed to exist*, because every gate reasons
about the copper that is there rather than the parts the spec requires.

Both are the same shape and it is the shape the skill's own discipline is meant to
prevent: *ungraded is not passed*. `score.json#/ungraded` lists `impedance` and
`length` because those components know they did not run. A requirement no component
models at all does not appear in `ungraded` either — it is invisible rather than
unexamined, which is strictly worse. The `spec` lens is the only thing in the
procedure that walks the requirements document itself, and it is the only reason
these two surfaced.

### The `spec` lens also corrected §C2's strongest claim

I wrote that HW-TB-PCB14's 2.4 mm is unachievable, full stop. The lens split the
142 violations by where they sit:

- **121 are inside a pad fan-out** — adjacent 0.4 mm-pitch pins at U1's west face,
  and CC1/CC2 at the USB4105 receptacle. Those are the genuinely unsatisfiable class,
  for the same land-pattern reason as §C1.
- **21 are in open channel, worst 0.97 mm against 2.4 mm demanded.** Those are real
  routing failures and must not be absorbed into the requirement finding.

That distinction is right and mine was too coarse. The corrected claim: PCB14 is
unsatisfiable *in the fan-out annulus* and merely unmet *in open channel*, and only
the first is a finding about the requirement.

## E. The run-1 fixes, re-verified

| fix | verdict |
|---|---|
| **A1** `route_planes` threads the board edge into GND-via placement | **holds.** 0 vias outside the outline across every chain run |
| **A2/A3** writeback no longer flattens non-Default classes; `via_drill` clamps to the smallest placed via | **holds**, and is what made §A4's damage *visible* rather than total — the surviving clamp is `clearance`, which is a documented ceiling effect, not the old blanket flattening |
| **A4** `--track-width-floor` | **works exactly as specified, and that turned out not to be enough.** At floor 0.8 and 0.4 the nets `failed_single` honestly instead of silently necking to 0.16 — the run-1 defect is closed. But "fail honestly" is only useful when failing is an option; for a net that must be connected, the floor has to be set to something reachable, and then it permits everything above it. See §B2 and §B4 |
| **B1** skipped length group reports `ungraded`, not `blocking=1` | **holds.** Verified on the 0-copper seed: `length` → `ran: false`, `ungraded: [impedance, length]` |
| **B2** `--net-min-widths` | **holds, and earned its place.** It is the only reason HW-TB-PCB13's and PCB25's per-net widths appear in `blocking` at all — `undersized` reads 0 on the same board |
| **B3** `broken` counts separations, not nets | **holds** |
| **skill** "FIRST: does this board have a requirements document?" | **holds, and was load-bearing** — see §B7 |
