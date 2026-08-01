---
name: plan-pcb-routing
description: Analyzes a KiCad PCB file and creates a comprehensive placement-and-routing plan. Routing-only is the default and fully supported path. Detects unplaced boards and advises which parts to lock before any placement repair, can declare a floorplan intent and grade the board against it, examines components for fanout needs (BGA/QFN/QFP/PGA), identifies differential pairs, categorizes power/ground nets, and presents a step-by-step workflow with explanations. Pairs every render with the JSON key that confirms or contradicts it, reads the renders itself rather than only showing them, and classifies routing failures as floorplan-, placement- or parameter-shaped so the two halves form one loop. Never changes the board outline.
---

# Plan PCB Routing

When this skill is invoked with a KiCad PCB file, perform a comprehensive analysis and present a routing plan to the user.

## Step 0: Placement gate (usually SKIPPED — read the decision table)

Before planning any routing, decide whether the board should be **placed** or
**re-placed** at all. Most of the time the answer is no, and running placement on
a good board makes it worse.

```bash
# Is the board even placed? (report-only, writes nothing, exits 3 if not)
python3 -X utf8 place_optimize.py board.kicad_pcb --suggest-locks
```

### Decision table — when to run placement

| board state | run placement? | tool |
|---|---|---|
| **unplaced** (test it — see below) | **NO** — out of scope; report and stop | — |
| careful hand placement, routing not yet attempted | **NO** | — |
| routing already completed clean | **NO** | — |
| board already carries copper (the tools exit 3) | **NO** — placement moves footprints, not tracks | — |
| rough / imported / auto-generated placement | yes | `place_optimize.py --max-displacement 3` |
| routing FAILED and `/diagnose-routing-failures` blames **congestion / blockers** | yes | `place_route_loop.py` |
| routing FAILED and the diagnosis is **parameters** (grid, ripup budget, layer costs) | **NO** — fix the parameters | — |

`docs/placement-optimization.md`'s own measured verdict: ship the quench as a
**repair** tool for rough/generated placements, **not** as a polish pass on
careful hand placements — on a good hand placement the result was neutral at
best, and the default weights *caused 2 new routing failures*.

**Placement invalidates every downstream routed board.** Never run it mid-chain;
re-run the whole chain from the placed board.

### If the board is UNPLACED

**Do not rely on an exit code to tell you.** `--suggest-locks` exited **0** and
happily gave advice on a board with 42 footprints at their generator's default
positions and no outline at all. Test positively instead — any of these means
unplaced, regardless of what the tools return:

```python
from kicad_parser import parse_kicad_pcb
pcb = parse_kicad_pcb('board.kicad_pcb')
pcb.board_info.board_bounds is None        # no Edge.Cuts outline to place INTO
len({(round(f.x, 3), round(f.y, 3)) for f in pcb.footprints.values()}) < len(pcb.footprints) / 2
```

`check_floorplan.py --emit-intent` is the other honest probe: it exits **3** with
*"the board has no Edge.Cuts outline"*, and its `JSON_SUMMARY` carries
`state_unplaced` / `state_partially_unplaced` / `state_spread_ratio` for the
cases where an outline does exist.

This toolchain **refines** an existing placement; it does not place a board from
scratch. Report that plainly, tell the user to place the parts in KiCad, and
offer to show them the current state:

**If the repo has its own seeder** (a script that writes a starting floorplan and
the outline from the spec), that is the placement step — run it, then treat its
output as the "rough / generated placement" row of the table above. The skill
does not place a board, but it should not stop in front of a repo that does.

**Then copy the `.kicad_pro` and `.kicad_dru` onto its output yourself.** A
seeder writes a `.kicad_pcb` and, like `place_optimize.py`, usually nothing else
— and it is the FIRST thing that touches the board, so a missing sibling there
propagates through the entire chain: every later step reads no project, resolves
its floor from the stock netclass instead of the spec, and stamps that looser
floor over tighter copper. `copy_board.py` copies a board *with* its siblings,
but a seeder is not a copy, so this one is on you:

```bash
cp board.kicad_pro seed.kicad_pro && cp board.kicad_dru seed.kicad_dru
```

```bash
python3 -X utf8 render_placement.py board.kicad_pcb -o /tmp/state.png
```

Do not pass `--allow-unplaced` to "make it work". On a pile of parts every
candidate pose is illegal, so the run prints "0 parts moved" plus a legality
block that *looks like a result*.

### Step 0a: what the SPEC fixes in place — read this before the lock advisor

**Every board mates with something.** A Pico-footprint carrier drops into a
2.54 mm header; a USB receptacle has to line up with an enclosure aperture; a
mounting hole has to hit a standoff. Those positions are **mechanical facts, not
optimizer variables** — and a 3 mm nudge that improves `crossings` by 20% can
make the board physically not fit, which no routing metric will ever tell you.

**The lock advisor does not know this.** `--suggest-locks` (Step 0b) infers from
footprint names and reference prefixes; it cannot read a requirements document,
and its lexical rules "miss house libraries entirely". It is the **second** pass.
The spec is the first.

**1. List what the spec fixes, and cite the requirement next to each ref.**
Read the board's requirements/spec before touching placement. Anything with a
coordinate, a pitch, a mating standard or an enclosure feature is fixed:

| what | typical source | example |
|---|---|---|
| edge/board-to-board connectors | the mating standard | Pico castellated rows: 2.54 mm pitch, 17.78 mm apart |
| USB / barrel / RF connectors | enclosure aperture | a spec clause fixing the receptacle to a named edge |
| mounting holes | standoff pattern | keep-out + exact XY |
| castellated edges | the carrier's pad field | pad centred **on** the outline |
| test points, antennas, sensors | mechanical or RF | an antenna keepout is not negotiable |

**2. Lock every one of them, and pass the locks to EVERY placement invocation.**
Not just the first — `place_optimize`, `place_route_loop` and every retry:

```bash
python3 -X utf8 place_optimize.py board.kicad_pcb placed.kicad_pcb \
    --lock 'J*' 'H*' 'U1' --max-displacement 2
```

**3. Record them in the intent, so they are GRADED and not merely hoped for.**
A lock you forgot to re-pass is silent. A `must_lock` the grader checks is not:

```jsonc
{
  "must_lock": ["J1", "J2", "H1", "H2", "H3", "H4"],   // cite the requirement id here
  "blocks": [
    { "name": "pico-header-north",                      // and here
      "refs": ["J1"],
      "zone": {"x": 100.2, "y": 60.1, "w": 0.4, "h": 0.4} }
  ]
}
```

`edge_connectors` constrains **which edge** and `overhang_mm` — it cannot pin an
XY. Anything with an *exact* position needs a `blocks` zone a few hundred microns
wide around the spec coordinate. Then `check_floorplan --intent` **fails** the
moment an iteration walks one, which is the mechanism a spec-conformant board
needs and prose does not provide.

**4. Scope `decaps` to the caps the requirement names, and lock those too.**
The quench has no decap-proximity term, so it walks a *different* cap past the
limit every run — lock one and the next moves. Locking them one at a time is
whack-a-mole; lock the named set at once. Measured, that cost `crossings`
52 → 60, and **that is the correct trade, not a regression**: a spec-conformant
placement that routes slightly worse beats a spec-violating one that routes well.

**Do not** lock a part the spec does not fix, "to be safe". A wrong lock freezes
a part that needed to move and the failure is invisible — which is the same
reason nothing is auto-locked.

### Step 0b: what to lock — advice only

Run this **second**, after Step 0a, and read the reasons. Nothing is locked
automatically, deliberately: a wrong auto-lock silently freezes a part that
needed to move, and that failure is invisible.

```bash
python3 -X utf8 place_optimize.py board.kicad_pcb --suggest-locks \
    --suggest-locks-json /tmp/lock_advice.json
```

It reports mounting holes (structurally invisible to the airwire cost, so the
optimizer will happily slide them), parts whose body overhangs the board outline
(card edges, USB shells — the "HAT port" case), and connectors. Each finding
carries its reason and a confidence; the lexical rules (footprint name,
reference prefix) miss house libraries entirely, so treat a *quiet* result as
"nothing detected", not "nothing to lock".

### Step 0c: repair the placement, with those locks

```bash
python3 -X utf8 place_optimize.py board.kicad_pcb board_placed.kicad_pcb \
    --max-displacement 3 --length-weight 0.3 --crossing-penalty 30 \
    --halo-coef 0.15 --halo-weight 2 --edge-halo 2 \
    --ignore-nets GND VCC \
    --lock <the exact refs printed by 0b> \
    2>&1 | tee /tmp/step0_place.txt
```

`--max-displacement 3` is the measured sweet spot on both test boards; 10 mm with
strong halos destroyed a data-bus corridor (15 new failures). `--ignore-nets`
must equal the Step 5 plane-net set — a plane-routed rail's airwire is a fiction
the optimizer would otherwise chase across the board.

**Acceptance rule — apply it, do not skip it.** It is a CONJUNCTION, and all
three parts are required:

1. Read the `JSON_SUMMARY:` line from 0c. If `crossings_after > crossings_before`
   or `hpwl_after > hpwl_before`, **discard the result.**
2. `check_floorplan --intent` must still **pass**. It did not, once: the quench
   walked a crystal 1.40 mm out of its declared zone while both metrics improved.
3. Any repo-local requirement gate must still pass.

**Two numbers produced by the optimizer cannot adjudicate a requirement the
optimizer has no term for.** Measured, one accepted-by-rule-1 placement:
`crossings` 85→60 and `hpwl` 602→596, both "better" — while a decap requirement
went from 2.04 mm to **9.57 mm** because the quench **rotated** the part it served
by 180°. The footprint origin never moved, so a position diff showed nothing and
the delta render drew no arrow.

**Rotation is the trap.** Lock anything whose **pin positions** a requirement
depends on, not merely anything the spec gives a coordinate to. On one board that
added six parts the spec pins nowhere: the flash (a decap-per-pin rule), the
crystal and its load caps (leg length and symmetry), and two series resistors (a
pair's coupled geometry). Expect to pay: `crossings` 85→74 instead of 85→60. That
is the same trade the decap case records — a spec-conformant placement that routes
slightly worse beats a spec-violating one that routes well.

When routing has already failed on congestion, use the loop instead — it consumes
exactly the failed and blocker nets the router reported:

```bash
python3 -X utf8 place_route_loop.py board.kicad_pcb board_repaired.kicad_pcb \
    --route-args '--nets "*" "!GND" "!VCC" --clearance <floor> --max-ripup 10' \
    --max-displacement 3 --max-target-pins 40 --ratsnest-screen 20 \
    --lock <refs from 0b> --ignore-nets GND VCC
```

Costly: it re-routes the whole board every round. `--ratsnest-screen 20` buys
some of that back by skipping candidates whose ratsnest clearly regressed.

### Step 0d: see it before trusting it

```bash
python3 -X utf8 render_placement.py board_placed.kicad_pcb \
    --before board.kicad_pcb -o /tmp/placement_delta.png
```

Ghost rects mark seed positions, arrows show what moved, and the caption strip
carries the real metrics.

**The render is triage, not a verdict.** The verdict is the numbers —
`crossings`/`hpwl` from the `JSON_SUMMARY`, and for the loop `failures` and
`iterations`. Do **not** judge a placement by how much moved: "lots moved, looks
broken" and "barely moved, looks safe" are both wrong.

### Step 0e: declare the floorplan, so it can be checked

A placement judged only by `crossings` and `hpwl` is judged by two numbers that
are **indifferent between a sensible layout and a scattered one with the same
wirelength**. Declaring where parts belong is what makes the rest checkable.

```bash
# read a starter intent OFF the board, then edit it down
python3 -X utf8 check_floorplan.py board.kicad_pcb --emit-intent /tmp/intent.json
python3 -X utf8 check_floorplan.py board.kicad_pcb --intent /tmp/intent.json --health
```

Exit **0** clean, **4** violations, **3** the board is not in a state it can
grade. Each violation carries the measured number beside the limit it broke, so
`--json` output is quotable evidence rather than an opinion.

Worth declaring, in rough order of value: `must_lock` for the parts the lock
advisor flagged; `edge_connectors` for anything that is *meant* to overhang
(this is what stops `oob_count` reporting a card edge as a defect forever);
`keepouts` for mounting holes and antenna clearances; `decaps.max_distance_mm`;
and `blocks` with a `zone` **only where the parts really are one contiguous
area**. Schematic sheets usually are not — on ulx3s all ten sheet bounding
boxes overlap each other, so `--emit-intent` claims a zone for only 4 of 10 and
says why for the rest.

`--health` adds the routability signals: how far each block sits from the parts
it connects to, and what crosses each declared bus corridor. Advisory — they say
the floorplan will fight the router, not that it breaks your intent.

### Scope `decaps` to the caps the requirement names, then LOCK them

A spec clause like *"100nF within 3mm of every VDD pin"* names **one BOM line**,
not every capacitor. Read the MPNs off the board and exempt the rest —
bulk electrolytics, crystal loads, a regulator network — in `decaps.exempt`,
citing the line item. That is scoping the rule to what it says, not relaxing it.

Then **lock the caps it does govern**. The quench has **no decap-proximity
term**, so any decap it may move can drift past the limit for a fraction of a
millimetre of wirelength, and it is a different cap every run — measured, one
cap on the first run and two different ones on the next. Locking them one at a time is
whack-a-mole. Their proximity *is* the requirement; their exact position is not
the optimizer's to trade. Expect to pay for it: locking ten decaps there took
crossings from 52 to 60. That is the correct trade, not a regression.

### Board features that live ON the outline

Castellated edge rows, card edges and a USB shell are *meant* to cross the
boundary. Declare them in `must_lock` **and** `edge_connectors` — the second is
what stops `oob_count` reporting them as defects forever.

Three things follow that nothing will tell you:

- **`check_drc` has no castellation exemption.** A track landing on a half-hole
  is flagged `SEGMENT-BOARD-EDGE` and the tool exits non-zero. Do not call that
  board clean — say what the tool reports, then why it is benign, and check the
  flagged coordinates really are on exempt pads before claiming they are.
- **Set `pad_prop_castellated` on the pads.** KiCad has the property; without it
  the fab has nothing machine-readable saying these half-holes are deliberate,
  and KiCad's own DRC reports pad-outside-outline on every one.
- **A collinear row is not an IC.** `decap_tethers` filters those out now, but
  the reason is worth carrying: a 1×N row spanning a board edge and carrying a
  rail sits nearer to half the decaps than their real IC, and it used to capture
  them — which made a distant decap grade **clean** against the wrong part.

### Before routing a dense escape: is the channel even wide enough?

If a board fans a bus out to an edge, measure the corridor before blaming the
router: `escapes x trace pitch / channel width`. It is the difference between
*"the router failed"* and *"this was never routable"*, and a router you cannot
tell those apart on measures nothing. A spec may set its own gate — one asks for
≤75%, and the as-built channel measured 5.60mm against 2.40mm of escape, 42.9%.

### THE BOARD OUTLINE IS NOT YOURS TO CHANGE

Size, shape, cutouts, slots and mounting-hole geometry are mechanical decisions
the user owns: enclosure fit, panel rails, connector apertures.

- **Never resize a board**, and never "just widen it a little". If a board is
  genuinely too small for its parts, **say so in words with the measured number
  and stop.** That is a design decision, not a routing one.
- **Never run** `tests/stress/fix_outline_gaps.py`, `strip_routing.py` or
  `prep_set2.py`. They are corpus-normalization tools and they *do* rewrite
  `Edge.Cuts` — the only things in this repo that do.
- The intent's `envelope` is **read from the board**, never authored. A part
  outside it is a finding about the **part**.
- A part sitting inside a **cutout** is caught by `oob_count` and `oob_amount`,
  never by `oob_area` — that one is measured against the bounding-box inset and
  scores a part in a slot as `0.0`. `check_floorplan` refuses it as a budget
  key, with that reason.

This mirrors the rules you already follow for user-owned geometry: guide
corridors and keepout polygons are described in words and drawn by the user, and
the stackup is never edited directly.

### Which artifact to produce, and what to check it against

**Never read a picture on its own.** Every render is paired with a number that
either confirms or contradicts it, and the number wins. A render that looks
tidier while `crossings` went up is a worse placement that photographs well.

Full key-by-key map in
[`references/evidence-map.md`](references/evidence-map.md) — read it before
quoting any number. The headline pairings:

| after this step | produce | and CHECK it against |
|---|---|---|
| 0b lock advice | *(none)* | `JSON_SUMMARY` `unlocked_high` — re-run with your `--lock` list until it is **0**, or say which findings you are deliberately leaving free and why |
| 0c `place_optimize` | `render_placement.py placed --before seed -o delta.png` | `JSON_SUMMARY` `crossings_after` vs `crossings_before`, `hpwl_after` vs `hpwl_before`. **Both must improve or you discard the result.** The arrows show what moved; only these say whether it helped |
| 0c on a two-sided board | add `--per-side` | `overlap_area` — a per-side panel is the only place a back-side collision is visible, and `overlap_area > 0` tells you one exists before you go looking |
| chasing one bus / clock | add `--ratsnest-nets '/CLK*'` | the same crossings/hpwl pair. Use this when the default delta view is too busy to read |
| a `place_route_loop` round | `make_movie.py WORKDIR --camera auto` | per-round `failures` and `iterations` from the loop's own output. A round that moved a lot and changed neither is noise |
| a run that TRIED more than it kept | `make_film.py --from-loop-dir WORKDIR` | the same per-round numbers, for the **rejected** rounds too — the badged beats are the ones whose `failures` did not improve, and seeing where the search went is the point |
| routing failed after placement | `--summary-json <route log>` on the render | the `failed_nets` and `blockers` in that same summary — the render colours exactly those, so the picture and the diagnosis are the same data |
| board looks wrong / empty | `render_placement.py board -o state.png` | exit code. **3 means the board is unplaced or already routed** — read the message rather than reaching for an override |
| **any board you are about to call done** | `scripts/board_score.py board --intent I --json wk/score.json` | `blocking` — it must be **0**. `ungraded` lists what nothing examined; that is *unexamined*, not clean. This is the one number not produced by the thing being graded |

### Which flag, at which step — the trigger table

Producing a render at the wrong moment, or without the flag that answers the
question you actually have, is the same as not producing it. Each row is a
**trigger**: when the left column happens, run that command *then*.

| when this happens | run | because it answers |
|---|---|---|
| before authoring an intent, board has back-side parts | `render_placement --per-side` | you cannot declare zones for a side you have not seen. Pairs with `overlap_area` |
| any accepted placement change | `render_placement after --before <ledger parent_board>` | did the macro structure survive? **`--before` is the last ACCEPTED board**, not iteration N−1 — N−1 renders a delta that never existed |
| any route step failed | `render_placement board --summary-json <route log> --focus` | do the failures share one pocket (→ placement) or scatter (→ parameters)? **`--focus` emits nothing without `--summary-json`** |
| a `--group-by` decision is live | `render_placement --zoom-group <name> --group-by sheet` | which parts does this block actually pull in? |
| chasing one bus, pair or clock | `render_placement --ratsnest-nets '*USB*'` | route.py `--nets` glob syntax, exclusions included. Use it when the default delta view is too busy to read |
| every placement render | add `--ignore-nets <same as place_optimize>` | **must match** or `crossings`/`hpwl` will not reproduce the optimizer's `JSON_SUMMARY`, and you will chase a phantom disagreement |
| every placement render | add `--clearance <the board's real floor>` | halo and overlap are otherwise graded at the wrong gap |
| every render, always | add `--json` | the re-measurement channel. A tool's own report never satisfies its own gate. **It is a bare FLAG on `render_placement`**, not a path: it prints a `JSON_SUMMARY:`-prefixed line into stdout among the progress text, so grep that prefix and strip it. Only `board_score.py --json <path>` takes a file |
| with `--focus` | `-o` names a **DIRECTORY** | `render_placement --focus -o wk/x.png` writes `wk/x.png/<board>.png` and `wk/x.png/<board>_focus1.png`. Give it a directory name, and read the panel paths back out of the `panels` array |
| once, before choosing a budget | `route.py --list-groups --group-by auto` | whether the board decomposes at all. The budget is **100 per board** either way (9.2) |
| after each accepted placement | `check_floorplan --intent I --health` | will this floorplan *fight* the router? Block displacement and bus-corridor crossings |
| every Step 9 iteration | `check_floorplan --intent I --json` | the per-rule measurements the ledger records |
| every Step 9 iteration | `board_score.py --json` | the only number that decides better/worse |

**Not evidence:** `--size` and `--supersample` change how the picture looks, not
what is true — measure instead. `--ratsnest-all` is the deliberate hairball, for
showing a human, never for reading.

### LOOK at the render — you, not just the user

Renders are for **intent**; numbers are for **legality**. A render answers *"is
this the structure I meant?"* — bus corridors, block cohesion, connector
orientation, which pocket the failures sit in. It never answers *"is this
legal?"*: clearance, overlap, off-board, connectivity and DRC all come from
numbers. **Do not adjudicate clearance from pixels.**

**`Read` the PNG yourself, and say what you saw, in exactly these four cases:**

1. **Before writing an intent** — `--per-side` on any board with back-side
   parts. You cannot declare zones for a board you have not looked at.
2. **After any accepted placement change** — the delta against the board it
   actually came from. One question only: *did the macro structure survive?*
3. **When routing failed and you need to know why** —
   `--summary-json <route log> --focus`. One question: *do the failures share
   one pocket* (→ placement) *or are they scattered* (→ parameters)?
4. **When a block decision is live** — `--zoom-group`.

**Show without reading:** the movie, `--ratsnest-all` hairballs, full panel
dumps. Those are for the human. Budget **≤3 images read per turn** — pick by the
question you have, not by what is available.

### Always produce the movie — it is the only artifact that shows *how*

`place_route_loop` renders it **by default** (`--no-movie` opts out). Every other
artifact is a snapshot: the movie is the only one that shows which round moved
what, and what the router did with the room it was given. Do not make the user
ask for it.

When the chain was **not** a `place_route_loop` run — a hand chain of
`place_optimize` → `route` → `route_planes` → repair, which has no
`loop_round*.json` sidecars and so no camera — build it from the step boards you
already wrote, in order. They are cumulative, which is exactly what the animator
wants:

```bash
python3 -X utf8 make_movie.py placed.kicad_pcb r3.kicad_pcb r4.kicad_pcb r5.kicad_pcb \
    -o wk/routing.gif --size 1600 --fps 12 --chunks 30 --end-hold 12
```

`.mp4` needs `imageio` + `imageio-ffmpeg` and falls back to a sibling `.gif`
without them — ask for `.gif` directly if you know they are missing, rather than
letting the fallback surprise you. Hand it to the user with `SendUserFile`; do
not `Read` it — it is a show-without-reading artifact, and its frames would blow
the ≤3 budget for nothing.

**Pass `--camera auto` on a chain that placed anything.** A placement step
changes no copper, and the movie animates copper deltas, so without it the step
that decides everything downstream renders as a **single frame** — measured:
seed → placed, 14 parts moved, one frame. The camera used to need the loop's
sidecars; it now recovers the moves from the boards themselves when there are
none, so a hand chain gets the same animation the loop does.

**A Step 9 convergence produces one film, not N disconnected ones.** Each
iteration may render its own movie; the artifact the user wants is the whole
convergence, and the ledger already holds its frame list — **the accepted boards,
in order**:

```bash
python3 -X utf8 make_movie.py \
    wk/iter00.kicad_pcb wk/iter01.kicad_pcb wk/iter04.kicad_pcb wk/iter07.kicad_pcb \
    -o wk/convergence.gif --size 1600 --fps 12 --chunks 30 --end-hold 12
```

Feed it the **accepted** boards only. A reverted iteration spliced into *this*
sequence animates a change that was undone, which reads as the router thrashing
when it was doing the opposite.

**The attempts are a second film, not a looser cut of this one** — and they are
usually the more interesting artifact, because the accepted spine is a small
fraction of what a run actually tried (one run: 10 boards of the 45 on disk).
`make_film.py` composes the whole search in one pass: the placements animate,
every attempt is shown and then explicitly reverted with a red **TRIED** badge
so it reads as a search rather than as churn, and the diagnostic renders that
were the *input* to each decision are spliced in as cards where they were made.

```bash
# a place_route_loop run: every round it wrote, kept AND dropped
python3 -X utf8 make_film.py --from-loop-dir wk/ -o wk/film.gif --size 1200

# a converge.py run: beats captioned with the lever_argv that produced them
python3 -X utf8 make_film.py --from-ledger wk/ledger.jsonl -o wk/film.gif

# a hand chain: name the dead ends, point it at the renders
python3 -X utf8 make_film.py wk/seed.kicad_pcb wk/placed.kicad_pcb \
    'wk/r*.kicad_pcb' --reject 'r4[bcd]*' --cards-from wk/ \
    -o wk/film.gif --size 1200 --fps 8
```

`--accepted-only` gives back the convergence cut. Produce both when a run had
attempts worth seeing; produce the convergence one always.

A render can never establish: that routing will now succeed (only a re-route
shows that); that the placement improved (`crossings`/`hpwl` decide); that a
part is or is not in violation (`overlap_area`/`oob_count` decide); or anything
at a coordinate outside that panel's `view` rect.

Two things the picture cannot show you at all: `board_edge_contours` (milled
inner contours the router keeps clearance from) are **not drawn**, and a board
whose outline failed to chain renders as a clean **rectangle**. Both are visible
only in `check_floorplan`'s `outline` block.

### Verify, do not assume

- **Re-read the `JSON_SUMMARY` line you just produced.** Do not carry a number
  forward from an earlier step or from memory of what you expected.
- **VERIFY THE WIDTH LANDED.** `--track-width` and `--power-nets-widths` are
  *requests*. A wide route that will not fit is necked down, and the per-net
  rescue re-routes a failed net at the **fab floor** — both leave `failed_single`
  empty and print one easily-missed line in a long log. Measure the copper
  instead, after every width-bearing step:

  ```python
  from collections import Counter
  from kicad_parser import parse_kicad_pcb
  pcb = parse_kicad_pcb('out.kicad_pcb')
  print(Counter(round(s.width, 4) for s in pcb.segments))    # widths ACTUALLY emitted
  ```

  Pass `--track-width-floor <mm>` to make the net fail instead of going under,
  and score with `board_score.py --net-min-widths` so a per-net requirement is
  graded rather than hoped for.
- **Carry the `.kicad_dru` with the `.kicad_pro`.** Rule lookup is
  `splitext(board)[0] + ".kicad_dru"`, strictly per board stem, so every
  intermediate board needs its own. `copy_board.py` takes every sibling; a hand
  `cp` of two files does not.
- **`place_optimize.py` writes NO project sibling at all** — only the
  `.kicad_pcb`. The next step then reads no project and resolves its floor from
  the stock netclass, which is the exact failure the `copy_board.py` warning
  exists to prevent, arriving through a door nothing guards. Copy the
  `.kicad_pro` (and `.kicad_dru`) onto the placed board yourself.
- **A netclass-scoped `.kicad_dru` rule is enforced by nothing in this chain.**
  `read_board_layer_clearances` extracts only *layer*-scoped rules and skips the
  rest with a note saying KiCad will still enforce it — true in KiCad, false for
  `check_drc.py` and for the router. Without `kicad-cli` installed, such a rule
  is graded by **nobody**, and the score cannot even list it as `ungraded`
  because it never knew about it. Say so explicitly rather than letting the rule
  read as covered.
- **A tool's own report does not satisfy its own gate.** `place_optimize` says
  the placement improved; confirm it on a *different channel* by running
  `render_placement.py <the written output> --json` and checking `metrics.
  crossings` and `metrics.hpwl` reproduce it. That is what catches a writer that
  dropped something between the objective and the file.
- **A render proves nothing about connectivity or DRC.** Those come from
  `check_connected.py` and `check_drc.py`, graded at the clearance the board was
  actually routed to.
- **After ANY placement change, every downstream routed board is stale.** Re-run
  the chain from the placed board. Do not reuse a routed artifact from before it.
- **If two sources disagree, believe the JSON.** The picture is a summary of it.
- **`0 violations` and `0 rules ran` are different.** `check_floorplan` reports
  `rules_run` and `rules_skipped` precisely so you can tell them apart; quote
  both.

### Verify with independent subagents, when you can

For anything beyond a single obvious call, **fan out verifiers in ONE response**
and hand each only its slice of the round's evidence — never the raw
`.kicad_pcb`. The prompts are in
[`references/verifier-prompts.md`](references/verifier-prompts.md). Nine lenses —
six grade the **placement** (`intent`, `legality`, `delta`, `blocks`,
`routing-feedback`, `coverage`) and three grade the **routed board**
(`connectivity`, `drc`, `spec`).

**Run the `spec` lens ONCE AT THE START, before the chain is built.** It is the
only step in this procedure that walks the requirements document and asks *"what
will measure this?"* of every clause — and a clause nothing models is **invisible**,
not `ungraded`. `score.json#/ungraded` lists components that know they did not run;
a requirement no component represents never appears there at all. Two HARD clauses
were shipped unmeasured that way: a plane-continuity rule that no checker
implements, and three fiducials that were never in the netlist, so no routing step
could have noticed they were absent. Running `spec` early tells you which clauses
you must measure by hand, in time to build that into the chain instead of
discovering it after delivery.

**Then run `connectivity`, `drc` and `spec` again on every board you are about to
call done.** They are the gate, not the write-up: a `VERDICT=FAIL` means `blocking`
was not really zero, so it **re-enters the Step 9 loop** at the step named in
`route=` while budget remains (100 per board). Do not re-word a FAIL into a caveat — "complete,
with some DRC warnings" describes a board that did not pass.

Each returns exactly one machine-readable line:

```
VERDICT=PASS:lens=<lens>
VERDICT=FAIL:lens=<lens>;finding=<one line>;evidence=<path#json-pointer|path@x,y>;route=<step>
```

**A verifier that cannot fill `evidence=` has not verified anything.** The gate
is met when every lens passes or every finding is dispositioned in writing.

`VERDICT=`, **not** `RESULT=`. The GUI takes the **last** `RESULT=` line in a
reply and parses it as the plan JSON, so a verdict spelled that way would be
read as a malformed plan.

**If the Agent tool is unavailable** — the GUI's headless runs allow only
`Read,Glob,Grep,Bash,WebSearch` — run the identical lenses yourself, in the same
order, on the same inputs, tag each `mode=inline`, and **say in the report that
verification was single-agent**. A run must never look like a fan-out happened
when it did not.

### Good and bad, concretely

**Do**

- Default to **not** running placement. The measured verdict is that the quench
  is a repair tool for rough/generated placements, not a polish pass — on a good
  hand placement it was neutral at best and its default weights caused 2 new
  routing failures.
- Run the lock advisor **before** the first placement run, and read the reasons
  rather than pasting the list blind.
- Keep `--max-displacement` at ~3 mm. It is the dominant safety knob; 10 mm with
  strong halos destroyed a data-bus corridor (15 new failures).
- Pass `--ignore-nets` equal to the plane-net set, so the optimizer does not
  chase a plane-routed rail's airwire across the board.
- Show the render **and** quote the caption metrics when reporting to the user.

**Do not**

- Judge a placement by how much moved. "Lots moved, looks broken" and "barely
  moved, looks safe" are both wrong — this is the single most common misreading.
- Run placement mid-chain, between routing steps.
- Pass `--allow-unplaced` or `--allow-routed` to make an error go away. They exist
  for a human who has read the message and decided; not to unblock a script.
- Add `--group*` to a command unless the user asked to scope to a block. A
  routing run that silently acquires a scope routes a fraction of the board and
  reports success on that fraction.
- Auto-lock anything the advisor merely suggested at `low` confidence.
- Present a render as evidence that routing will now succeed. Only a re-route
  shows that.

### The inner loop — and why its verdict is not the verdict

`place_route_loop` is the router-in-the-loop form: it routes, reads the failure
diagnostics, moves only the parts implicated, re-routes, and keeps the result
**only if `(failures, iterations)` improved**. Rejected rounds revert and widen
the search. Each round costs a full re-route (minutes); `--ratsnest-screen 20`
skips the route for candidates whose ratsnest clearly regressed, buying some back.

**Its `ACCEPTED` / `REJECTED` is the ROUTER'S OWN OPINION, not a quality
verdict.** `better()` (`place_route_loop.py:358-362`) compares `failures` and
`iterations`, and `metrics_from_summary` (`:224`) reads both out of route.py's
`JSON_SUMMARY`. **No checker runs.** CLAUDE.md states the hazard directly —
*"Routers can report false success… re-verify with the authoritative,
zone/fill-aware check"* — so a round can be ACCEPTED with pads disconnected and
DRC dirty. Two consequences:

- **It is a cheap pre-filter, not a gate.** Re-score every board it hands you
  with `scripts/board_score.py` before believing it improved anything.
- **It is also only ONE `route.py` call** (`_ROUTE_PY`, `:52`), not the chain.
  Planes, plane-repair, reconnect and diff pairs never participate in its
  feedback, so a board that needs a plane repair to connect cannot converge
  inside it. That is what the **Step 9** outer loop is for.

`--rounds` (default **5**) bounds this inner loop. The 20-per-group / 20-per-board
budget in Step 9 (100 per board) bounds the outer one. They are different budgets; do not confuse
raising `--rounds` with taking another outer iteration.

**Full convergence procedure: [`references/convergence.md`](references/convergence.md).**

### Placement is CLI-only

There is no placement tab and no `place` plan action, so a placement step
**cannot** ride in a plan's `steps`. Run it on the command line *before* the
plan and hand the plan the placed board. `make_plan.py` / `manifest_to_plan.py`
**refuse** a recorded `place_optimize.py` / `place_route_loop.py` command loudly
rather than convert it.

### Do we need blocks at all, and which ones? — a procedure

**G0. The default is no blocks.** `--group-by` defaults to `none` on the
placement CLIs and `route.py --group` is unset. **Do not add `--group*` to a
plan or a command unless the user asked for it** — a routing run that silently
acquires a scope routes a fraction of the board and reports success on that
fraction.

**G1. Name the job first.** Three different jobs want three different sources:

| the job | the tool | the source that works |
|---|---|---|
| move parts together | `place_optimize` / `place_route_loop --group-by` | `decap`, and realistically only `decap` |
| scope a route or an undo | `route.py --group` + `--group-scope` | `sheet` (or `kicad` if it exists) |
| frame a picture, or a zone in an intent | `render_placement --zoom-group`, `check_floorplan` | `sheet` |

**G2. List before deciding.** Both are report-only, exit 0, write nothing:

```bash
python3 -X utf8 route.py board.kicad_pcb --list-groups --group-by auto
python3 -X utf8 render_placement.py board.kicad_pcb --list-groups --group-by sheet
```

**G3. Choose on the measured evidence, in this order.**

1. `kicad` — the designer's own `(group ...)`. Exact when present, but on **0 of
   27** in-repo boards. If it fires, trust it and stop looking.
2. `sheet` — **12 of 22** boards with sheet paths have more than one. The
   workhorse **for scoping and framing**. **Not for movement**: sheet blocks of
   16–83 parts moved on no board tried. Also not for zones — a sheet is a
   *functional* grouping, so its members scatter and its bounding box overlaps
   its neighbours' (all 10 of ulx3s's do).
3. `decap` — the only source that measurably *moves* anything. **0% internal by
   construction** (a cap bridges VCC and GND, both board-spanning), so it is
   meaningless as a routing scope.
4. `netprefix` — weakest. Enable expecting little.

**G4. Pick the scope deliberately.** `--group-scope` defaults **depend on the
operation**: `touching` when routing (routing a block's interface is the point),
`internal` with `--undo`, because a block's touching set contains GND/VCC and
undoing those strips copper board-wide (rp2350: 170 segments vs 75).

**G5. Confirm it did something.** Read `blocks` and `block_parts` from the
`JSON_SUMMARY` and the `describe()` banner. **`blocks: 0` means drop the flag**,
not "it helped". And if a round's `groups` pulled a large block in, the run moved
far more than you targeted.

- Always list first: `python3 route.py board.kicad_pcb --list-groups --group-by auto`
  (prints parts and touching/internal net counts, exits 0, routes nothing).
- Which source: `kicad` groups exist on **0 of 27** in-repo boards; `sheet` is the
  workhorse (**12 of 22**); `netprefix` is weakest; `decap` is strong but **0%
  internal by construction**.
- `--group-scope`: routing defaults to `touching` (routing a block's interface is
  the point); `--undo` defaults to `internal`, because a block's touching set
  contains GND/VCC and undoing those strips copper **board-wide** (measured on
  rp2350: 170 segments vs 75).
- For placement, `--group-by decap` is the one that measurably moves anything.
  **Sheet blocks of 16-83 parts moved on no board tried** — don't burn a run
  discovering that.
- **Hard rule:** `route.py --group` is a *scope*. A routing run that silently
  acquires one routes a fraction of the board and reports success on that
  fraction — the same class of defect as a Step 5b coverage gap.

## Step 1: Load and Analyze PCB Structure

```python
from kicad_parser import parse_kicad_pcb
pcb = parse_kicad_pcb('path/to/file.kicad_pcb')

# Basic stats
print(f'Total nets: {len(pcb.nets)}')
print(f'Total footprints: {len(pcb.footprints)}')
print(f'Existing segments: {len(pcb.segments)}')
print(f'Existing vias: {len(pcb.vias)}')
```

Report to user:
- Number of nets, components, existing routing
- Whether this is a fresh board or partially routed

## Step 2: Identify Copper Layers

Check the KiCad file directly for layer definitions:

```bash
grep -E "^\s+\([0-9]+ \".*\.Cu\"" path/to/file.kicad_pcb
```

Report to user:
- Available copper layers (F.Cu, B.Cu, In1.Cu, In2.Cu, etc.)
- Whether it's a 2-layer, 4-layer, or multi-layer board

### Stackup Check (always run this early)

Inspect the stackup now, before planning, and report the verdict **at the top of the
plan report** so problems surface before any routing work:

```python
from kicad_parser import parse_kicad_pcb
pcb = parse_kicad_pcb('path/to/file.kicad_pcb')
for layer in pcb.board_info.stackup:  # List[StackupLayer], ordered top to bottom
    print(layer.name, layer.layer_type, layer.thickness, layer.epsilon_r)
```

- No stackup section, or all dielectrics with identical thickness and ε_r ≈ 4.5, means
  KiCad's untouched default. If the board also has impedance-relevant signals (see the
  speed detection in Step 4), lead the report with a clear warning: impedance and
  time-matching calculations will not match the user's fab, and `/recommend-stackup`
  should be run before impedance-controlled routing. Take plane-layer assignments from
  its output when available.
- A 2-layer board with multiple differential pairs or planes-worth of power nets is
  itself worth flagging (no inner layers for reference planes).
- If the stackup looks deliberate, say so in one line and move on.

Report problems prominently but still produce the full plan - the user decides whether
to fix the stackup first.

## Step 3: Check for Components Needing Fanout

Identify BGA, QFN, QFP, PGA, LGA, and other array packages that benefit from escape routing:

```python
for ref, fp in pcb.footprints.items():
    name_upper = fp.footprint_name.upper()
    pad_count = len(fp.pads)

    # Check for array / fine-pitch land/no-lead packages by name. Note 'QFP'
    # already matches LQFP/TQFP/VQFP, 'QFN' matches VQFN/WQFN/HVQFN, and 'BGA'
    # matches FBGA/UFBGA/TFBGA, so only distinct families need listing.
    needs_fanout = any(k in name_upper for k in (
        'BGA',          # ball grid array
        'PGA',          # pin grid array (through-hole)
        'LGA',          # land grid array (interior lands, e.g. LGA-12) - issue #144
        'CSP', 'WLCSP', 'WLP',  # wafer-level / chip-scale = micro-BGA, sub-0.5mm
        'CGA',          # column grid array
        'QFN', 'DFN',   # quad / dual no-lead (exposed-pad)
        'QFP',          # quad flat pack
    ))

    # SMD vs through-hole FIRST -- it gates everything below (#513 item 16).
    smd_count = sum(1 for p in fp.pads if p.drill == 0)
    th_count = sum(1 for p in fp.pads if p.drill > 0)
    mostly_tht = th_count > smd_count

    # A THT part's pins are reachable on EVERY copper layer -- there is no
    # "escape" problem to solve, so fanout buys nothing regardless of pad
    # count. PLCC/DIP/ZIF SOCKETS are the trap: a PLCC-44 THT socket's
    # staggered double-ring reads as a sparse uniform grid and used to be
    # misdetected as a BGA (rc2014_82c55_ide U1 -- nets near it burned >1M
    # A* iterations each behind a phantom exclusion zone, #513 item 16).
    # Wide-pitch (>=2mm) PGAs route fine without fanout too; only reach for
    # bga_fanout on a PGA when its channels are genuinely contested.
    if mostly_tht and 'PGA' not in name_upper:
        needs_fanout = False

    # Fine-pitch arrays strand even at low pad count: trigger by PITCH + interior
    # pads, not just pad_count > 40 (issue #144: LGA-12 at 0.5mm has only 12 pads
    # but its center lands box in). Compute the min pad-to-pad spacing and whether
    # any pad is interior (not on the bounding-box edge).
    if not needs_fanout and not mostly_tht and pad_count >= 6:
        xs = sorted({round(p.local_x, 3) for p in fp.pads})
        ys = sorted({round(p.local_y, 3) for p in fp.pads})
        def _min_step(v):
            return min((b - a for a, b in zip(v, v[1:])), default=999)
        pitch = min(_min_step(xs), _min_step(ys))
        minx, maxx, miny, maxy = xs[0], xs[-1], ys[0], ys[-1]
        has_interior = any(minx < round(p.local_x, 3) < maxx and
                           miny < round(p.local_y, 3) < maxy for p in fp.pads)
        # Fine pitch (<=0.6mm) with interior pads, OR a large multi-row part
        # AT FINE PITCH. Raw pad_count > 40 alone is NOT a fanout signal: a
        # 44-pin THT socket, a 2x20 header, or a 1.27mm connector trips it
        # while gaining nothing from escape routing.
        if (pitch <= 0.6 and has_interior) or (pad_count > 40 and pitch <= 0.8):
            needs_fanout = True

    if needs_fanout:
        # Analyze pad arrangement
        xs = sorted(set(round(p.local_x, 2) for p in fp.pads))
        ys = sorted(set(round(p.local_y, 2) for p in fp.pads))
        grid_cols, grid_rows = len(xs), len(ys)
```

**Do not skip this step on a fine-pitch QFN.** It is easy to read "QFN" as
"perimeter part, ordinary routing handles it" and move on. A 0.4 mm-pitch QFN-60
does **not**: its mid-row pins are boxed in by neighbours on both sides, and the
two nets that stayed unrouted longest on one board were exactly that — mid-row
south-face pins whose escape channel their own already-routed neighbours filled.
The rule below says a perimeter at ≤0.65 mm with many pads wants fanout; a QFN-60
at 0.4 mm is squarely inside it. Fanout runs on the EMPTY board (Step 1), so
skipping it is expensive to undo — you cannot bolt it on after the bulk route.

### Does this part actually BENEFIT from fanout? (check before planning it)

A name/pad-count match is a candidate, not a decision. Fanout (escape routing)
exists to solve ONE problem: pads that cannot be reached by ordinary routing
because neighboring pads at fine pitch box them in. Before adding a fanout
step, confirm the geometry actually has that problem:

1. **Through-hole part (most pads drilled)?** → **No fanout.** Every pin is
   reachable on every layer; there is nothing to escape. This includes
   PLCC/DIP/ZIF **sockets** (a PLCC-44 THT socket's staggered pin field looks
   like a sparse grid but is just a socket, #513 item 16), headers, and DIN /
   backplane connectors. Wide-pitch (>=2mm) PGAs also normally route fine
   without fanout.
2. **Wide-pitch SMD (>=1.27mm) perimeter part?** → No fanout; plain routing
   handles it.
3. **Interior pads at fine pitch (<=0.6mm), or a perimeter at <=0.65mm with
   many pads?** → Yes, fanout genuinely helps (this is the boxed-in case).
   Dense 2-row mezzanine/card-edge connectors at 0.4mm (CM4/CM5, 200+ pads)
   DO benefit -- use `qfn_fanout.py --escape-method underpad --allow-via-in-pad`.
4. **Unsure?** The fanout tools now refuse or warn on wrong shapes
   (staggered arrays, non-arrays). Trust a refusal: if the tool says the part
   is not an array and the geometry checks above say the pins are reachable,
   plan ordinary routing instead of forcing a workaround.

### Fanout Tool Selection

| Package Type | Tool | Notes |
|--------------|------|-------|
| BGA (SMD grid) | `bga_fanout.py` | Escape routing for ball grid arrays |
| PGA (through-hole grid) | `bga_fanout.py` | Same tool works for PGA |
| LGA / WLCSP / CGA (land/chip-scale grid) | `bga_fanout.py` | Grid escape; interior lands strand without it (issue #144) |
| QFN/QFP/DFN (perimeter SMD) | `qfn_fanout.py` | Stub routing for quad/dual no-lead and flat packages |
| **AQFN / staggered multi-row no-lead** | `qfn_fanout.py` **`--escape-method underpad --allow-via-in-pad`** | Inner rows the surface fan cannot reach - see below. **Never `bga_fanout.py`** |
| DIP/SOIC (through-hole/SMD rows) | None needed | Standard routing handles these |
| PLCC (SMD J-lead or THT socket) | None needed | Perimeter part; the THT socket's pins reach every layer. Never a BGA (#513 item 16) |
| Sockets / headers / backplane connectors (THT) | None needed | All-layer reachable; pad count alone is not a fanout signal |

### When to Use Fanout for BGA/PGA/LGA

**Rule: Use fanout for any grid array (BGA/PGA/LGA/WLCSP/CGA) with more than 2 pins
depth from outside to center, OR any fine-pitch (<=0.5mm) array with interior pads
regardless of pin count** — a small LGA-12/WLCSP at 0.5mm pitch boxes its center
lands in even though it has well under 40 pads (issue #144).

**Important:** Calculate ACTUAL depth by counting pads from the edge toward center, not grid size.
Many PGA/BGA packages (especially FPGAs/CPLDs) have hollow centers with only perimeter pins populated.

To calculate actual depth:
```python
# Check middle column from top edge toward center
mid_col = xs[len(xs)//2]
depth = 0
for y in ys:  # ys sorted from edge
    if (mid_col, y) in pad_positions:
        depth += 1
    else:
        break  # Stop at first empty position
```

Examples:
- 13×13 grid, fully populated → depth = 7 → **USE FANOUT**
- 13×13 grid, hollow center (3 rows populated) → depth = 3 → **USE FANOUT**
- 10×10 grid, hollow center (2 rows populated) → depth = 2 → fanout optional
- 4×4 grid, fully populated → depth = 2 → fanout optional

Inner pins beyond depth 2 cannot escape without fanout routing through channels between outer pins.

**Escape layers (multi-layer boards):** `bga_fanout.py` defaults to `--layers F.Cu B.Cu`
only. On a 4+ layer board, pass ALL the board's copper layers, e.g.
`--layers F.Cu In1.Cu In2.Cu B.Cu` — otherwise deep balls have nowhere to escape to
and those nets are dropped from the fanout. `qfn_fanout.py` is perimeter-only and
doesn't take escape layers.

**Staggered multi-row no-lead packages (AQFN) - use via-in-pad (#500).** An
AQFN (e.g. `Nordic_AQFN-73-1EP_7x7mm_P0.5mm`, on osprey_kb / hex_gateway /
mikoto_nrf52840) puts its pads in TWO OR MORE staggered rows per side. The
surface 45-degree stub fan reaches only the outermost row, so the default
silently drops the rest. Measured on osprey_kb U1 (78 pads, 39 nets):

| command | escaped | time |
|---|---|---|
| `qfn_fanout.py` (default stub) | 26/40 | 2.4s |
| `qfn_fanout.py --escape-method underpad` | 35/40 | 2.6s |
| **`qfn_fanout.py --escape-method underpad --allow-via-in-pad`** | **39/39, DRC-clean** | **2.4s** |
| `bga_fanout.py` | 39/39 | **2967s** |

So: **for any AQFN or staggered multi-row no-lead part, plan
`qfn_fanout.py --escape-method underpad --allow-via-in-pad`.** Via-in-pad is
what reaches the innermost row; without it 5 pads drop.

Do NOT send these to `bga_fanout.py`. It models a ball grid, and a staggered
package's two offset rows project onto each axis at HALF the real pad spacing -
so its detected pitch is half the truth, its escape budget evaluates to a
NEGATIVE via size, and it grinds for ~50 minutes to reach the same answer.
`bga_fanout.py` now refuses these outright with the qfn_fanout command to use
(override: `KICAD_ALLOW_STAGGERED_BGA=1`).

Spotting one: the footprint name contains `AQFN`, or the part has far more pads
than a single peripheral ring of its size would hold (73-90 pads on a 7x7mm
body), or `bga_fanout.py` reports a pitch that is half the name's `P<pitch>mm`.

**Crowded fine-pitch QFN edge (surface fan has no room):** if a `qfn_fanout`
stub (especially a diff pair) is boxed in by a neighbour pair and a foreign
track and the surface 45° fan drops it, use `qfn_fanout.py --escape-method
underpad --via-size 0.45 --via-drill 0.25` (#164). It drops a through-via just
past each pad and escapes on an inner/back layer — straight out past the lateral
congestion instead of fanning into it (adjacent vias are staggered to clear).
Match `--via-size`/`--via-drill` to the board's fine-pitch via rule. If the
underpad run still **drops** a leg ("N dropped") because the via has no clear
room *outward* (a neighbour pad/track exactly one pitch away), add
`--allow-via-in-pad` (#161): the escape via may then sit on its own pad and
stagger *inward toward the chip*, away from the neighbour, instead of being
dropped. It still clears every other-net pad/via/track — it only gains
permission to overlap its own pad — so reach for it specifically when underpad
reports drops on a boxed-in fine-pitch pair.

**Size the escape via/track to the pitch BEFORE running fanout (issue #158).**
`bga_fanout.py` escapes one track down the channel between adjacent via columns —
at the **half-pitch**. So the via, track, and clearance must fit that half-pitch
or *every* escape grazes the neighbouring column's via by a few µm, and the fanout
still reports `failed: 0` (its success metric ignores sub-clearance grazes). The
budget, per array (measure each component's own pitch — they differ):

```
via_size + track_width + 2·clearance + margin ≤ pitch     (one escape track per channel)
via_size ≥ via_drill + 2·min_annular_ring,  track_width ≥ fab min track   (fab floors)
```

Don't just shrink the via against a *fixed* track — **solve for via AND track
together**, taking each down toward the fab floor as the pitch demands, and leave
a little margin so the result clears DRC instead of merely touching it. Read each
array's own ball pitch `P` (the min ball spacing — arrays on one board differ) and
the requested clearance `C` (Default net-class clearance from
`list_nets.py --design-rules`), plus the board's fab floors (`min_track_width`,
`min_via_diameter`/`min_via_drill`, annular ring), then:

```python
margin = 0.05                                  # slack: clear DRC, don't graze it
budget = P - 2*C - margin                       # room for one via + one track
track  = max(min(nominal_track, 0.15), min_track_width)   # keep a routable track
via    = min(nominal_via, budget - track)       # largest via that still fits
if via < via_floor:                # via fell below the floor -> thin the track to free room
    via   = via_floor
    track = max(min_track_width, budget - via)
infeasible = track < min_track_width or via < via_floor   # even fab floors won't fit
via_drill  = max(min_via_drill, via - 2*min_annular_ring)  # hold the annular ring at floor
# via_floor = max(min_via_diameter, min_via_drill + 2*min_annular_ring)
```

Pass the computed `--via-size via --via-drill via_drill --track-width track
--clearance C` to the fanout step. If `infeasible`, the pitch can't take a channel
escape even at the fab floor → switch to `--escape-method underpad` and/or add
escape layers; don't ship the graze.

**Plan params can set ANY GUI option:** in the GUI's RESULT schema, each
step's `params` may include any option shown on that step's tab or the shared
options panel, keyed by its snake_case field name (`max_iterations`,
`max_ripup`, `grid_step`, `board_edge_clearance`, `hole_to_hole_clearance`,
`via_cost`, `heuristic_weight`, `turn_cost`, `ordering_strategy`, ...).
Unknown names are ignored with a note in the plan log. Use this to carry the
same values the equivalent CLI chain would pass (e.g. `--max-iterations
1000000 --max-ripup 10 --grid-step 0.05`), so a GUI plan run matches a stress
run step for step.

**Why this heuristic matters for the GUI:** the plugin runs `/plan-pcb-routing` in
*plan-only* mode — it never executes the fanout and never runs the DRC↔smaller-via
retry loop, so it cannot discover a too-big via after the fact and shrink it. The
plan must therefore carry via/track that are **already** DRC-safe for the pitch.
Computing them here — both dimensions, with margin, clamped to the fab floor — is
what lets the single fanout the GUI runs come out clean the first time.

Worked example (keks U1, pitch 0.8, clearance 0.1, fab floor track 0.1 / via 0.45):
`budget = 0.8 − 0.2 − 0.05 = 0.55`; track 0.127 → via = min(working, 0.55−0.127) =
**0.42** (≥ floor) → DRC-clean, vs the Ø0.5 the net-class default would have used
(163 grazes). At 0.4 mm pitch the budget forces both to the floor (track 0.10, via
~0.30/0.20 advanced); if even those don't fit, go `--escape-method underpad`.
`bga_fanout.py` also warns `WARNING: escape via ... busts the half-pitch budget`
when handed infeasible params, but choose feasible ones here so it never fires.

**Always check the fanout escaped all requested balls.** `bga_fanout.py` ends with
`JSON_SUMMARY: {"component", "requested", "escaped", "failed", "unescaped_nets", ...}`.
A dropped ball is **removed from the output** and later fails signal routing as "no
rippable blockers", so it must be caught here. If `failed > 0`, retry the fanout with
more layers and/or a smaller `--clearance` (see "Escape clearance" below) before
moving on — do not start signal routing while balls are still dropped.

**If balls still drop on a dense, fully-populated array, switch to the under-pad
escape:** add `--escape-method underpad` with a small via/track for the pitch
(e.g. `--via-size 0.35 --track-width 0.12 --clearance 0.1` at 0.8 mm pitch). The
default `channel` engine confines every layer to the gaps *between* ball rows, so
a few channels over-subscribe and the deepest balls can't escape; `underpad`
routes each ball *under* the pad field on inner layers via a via-in-pad and
escapes arrays `channel` can't (e.g. a 22×22 BGA that drops ~20 balls → 0).
Caveats: it routes diff pairs as **single-ended**, and it **skips power/plane
nets** (they tap their plane), so create the planes first (or exclude power with
`--nets`). Rule of thumb: try `channel` first (keeps diff pairs); fall back to
`underpad` when `channel` can't escape a dense array.

**After every BGA/PGA fanout, run the decoupling-cap placement optimizer
(#130).** A fanout drops vias near the ball field; where a foreign-net via
lands under a decoupling cap placed at a ball, the via copper overlaps the
cap pad → a real `PAD-VIA` DRC violation at the clearance floor. The fix is
placement, so run `place_fanout_clearance.py` on the **fanned** board to
nudge those caps clear (and pull each pad toward its nearest same-net ball so
a power/GND via dropped there later shares the via). See "Step 1b" below for
the command. It's cheap, only touches caps near a BGA, and is a no-op when
nothing collides — so run it after each fanout step before moving on.

Report to user:
- List of components that may need fanout
- Package type, pad count, and grid depth for each
- Recommended fanout tool

## Step 4: Check for Differential Pairs and Power Nets

Use `list_nets.py` to detect differential pairs and power/ground nets:

```bash
python3 list_nets.py path/to/file.kicad_pcb --diff-pairs --power
```

### Read the board's design rules and pass them to the CLI

The router does NOT read the board's design rules — it falls back to a generic
`--clearance 0.25` / `--track-width` default, which is often WIDER than the
board's own rule and can box pads in so nets fail with "no rippable blockers".
Read the board's real rules and pass them explicitly:

```bash
python3 list_nets.py path/to/file.kicad_pcb --design-rules
```

**KiCad has TWO tiers of rules, and DRC only enforces one of them — this matters
for fine-pitch boards (#111/#115):**

- **Net-class values** (`clearance`, `track_width`, `via_diameter`, `via_drill`):
  these are the size new objects are *drawn at*. Of these, only **clearance** is
  a DRC-enforced minimum. `track_width` and `via_diameter`/`drill` are **not** DRC
  floors — they are just defaults, so a board can (and the human originals do) use
  a **smaller** via/track than the net-class nominal and still pass DRC.
- **Board Constraints** (`min_clearance`, `min_track_width`, `min_via_diameter`,
  `min_hole_to_hole`, `min_through_hole_diameter`): **these are the actual DRC
  floors.** `--design-rules` reads them from `design_settings.rules` and combines
  them with the JLCPCB fab minimum (backstop when a Constraint is 0/unset — e.g.
  `min_clearance` is frequently 0) into a single **manufacturing floor**.

### FIRST: does this board have a requirements document?

**If it does, the SPEC outranks everything below.** `--design-rules` reports what
the *fab* can make and what the *board file* currently declares. Neither is
permission. On a board with real requirements its suggestions actively
contradicted three HARD rules at once:

| `--design-rules` printed | the spec said |
|---|---|
| `--via-drill 0.25` | 0.3 mm, HARD |
| *"drop `--track-width` to the fab floor 0.1 … BELOW the board's own rule"* | 0.15 mm HARD, and *"shall not be routed to"* 0.10 |
| `check_drc.py --clearance 0.1` | classes at 0.16 / 0.45 — grading at 0.1 hides real violations |

So: read the requirements first, write the numbers down with their requirement
IDs, and treat the printed flags as a *starting point to be overridden*. The
"route at the fab floor" advice further down is correct **absent a requirements
document** and wrong with one — it is exactly how a previous run shipped 267
segments at 0.127 mm against a 0.15 mm HARD floor.

Two flags exist for holding a spec floor, and neither is discoverable from the
suggestions:

- **`--track-width-floor <mm>`** — the router may not go under it. `--track-width`
  is a *request*: a wide route that will not fit is necked down, and the per-net
  rescue re-routes a failed net at the **fab** floor, both reporting the net
  routed. With the floor set the net fails honestly instead. (Not to be confused
  with `route_disconnected_planes --min-track-width`, its region-join width band,
  nor `check_drc --min-track-width`, which grades.)
- **`--fab-overrides <file>`** — `key = value` lines over the `--fab-tier` floor
  (`clearance`, `track_width`, `via_diameter`, `via_drill`, `annular`,
  `board_edge`, `hole_to_hole`, `pad_hole_to_hole`). Supplying it also **forbids
  the silent `standard`→`advanced` escalation**, which is what puts 0.25/0.15
  vias on a board that asked for 0.6/0.3. Measured, one such file took the score's
  `undersized` from **169 to 0**.

  **Its `clearance` key REPLACES the per-class clearance map, it does not floor
  it.** Set it to the board's *Default class*, not the spec minimum: pinning it
  to the tighter figure is what silently dropped an `XTAL_12M` class from 0.45 mm
  to 0.15 mm, and restoring 0.45 afterwards produced 126 violations.

Use the printed flags as-is **only when the board has no spec of its own**:

- **Routing** (`route.py`, `qfn_fanout.py`, `bga_fanout.py`, `route_planes.py`):
  `--clearance` from the **Default class**, but **`--via-size`/`--via-drill`
  from the working floor**, NOT the net-class `via_diameter`. Emitting the net-class
  via everywhere is #115 — it's a max-like default, far too big for fine-pitch
  escape (e.g. a 0.4 mm QFN/BGA needs the small working via the original used).
  For `--track-width`, the net-class value is only a starting point and is *not* a
  hard minimum: on dense/congested boards route ordinary signals at the **fab
  physical floor** instead (thinner is both more complete and faster — see "Route
  signals at the FAB floor by default" in Diagnose and Retry). Keep the net-class
  width only for **current-carrying nets** (`--power-nets`).
  **Do NOT keep the net-class gap/width for impedance-controlled (diff-pair) nets** —
  the stock net class is usually wide (`diff_pair_gap` 0.25 / width 0.2 mm), and a
  fat pair is a wider bundle that gets dropped on congested boards (measured:
  `glasgow_revC` routes all 13 FPGA pairs at `--diff-pair-gap 0.1` but loses 2 at
  0.25). Per `/find-high-speed-nets`, route those at the **fab floor for gap and
  clearance (~0.1 mm)** while keeping `--impedance` for the width (the router
  computes it from the stackup and clamps it to the floor). `route_diff.py` then
  auto-updates the Default net class to those tight values (only-loosen, via
  `fix_kicad_drc_settings.py`), so the `.kicad_pro` stops advertising the wide gap.
- **Diff-pair sizing default + shrink-to-succeed.** Default `route_diff.py` to
  **`--track-width 0.1` and `--diff-pair-gap 0.1`** (the fab floor) — a thin, tight
  bundle routes on congested boards where a fat pair is dropped. If the interface
  is impedance-controlled, ALSO pass `--impedance <ohms>`: the router derives the
  width from the stackup and clamps it to the floor, so the target impedance is
  **maintained** while the geometry stays as small as it can. **When a pair fails
  or falls back** — `route_diff.py`'s `JSON_SUMMARY` lists it in `failed_diff_pairs`
  or `single_ended_diff_pairs`, or DRC shows an intra-pair / via-via graze — re-run
  the failing pairs with **smaller track width, smaller gap, AND smaller vias**
  (`--via-size`/`--via-drill` toward the fab via floor). A tighter track+gap fits a
  narrower channel, and smaller vias fit a tight pad pitch (measured: lumenpnp
  USB_D's two 0.5 mm vias collide by 0.1 mm at the connector pitch — a smaller via
  clears it). Step **toward, never below**, the fab floors, and keep `--impedance`
  so the ohms target is held as the geometry shrinks.

  **`--track-width-floor` does NOT exist on `route_diff.py`.** It is a `route.py`
  flag. So a pair whose width is a HARD requirement — a spec'd 0.8 mm USB
  geometry, say — has **no floor protection at all** in the diff-pair step: if
  the engine necks it down to fit, it does so silently and the summary still
  reports the pair routed. Measure the emitted copper after the call, and carry
  the requirement in `board_score.py --net-min-widths` so it reaches `blocking`:

  ```python
  from collections import Counter
  from kicad_parser import parse_kicad_pcb
  pcb = parse_kicad_pcb('out.kicad_pcb')
  print(Counter(round(s.width, 4) for s in pcb.segments
                if pcb.nets[s.net_id].name.startswith('USB_')))
  ```

  (The same asymmetry note applies as for `--coplanar-gap`/`--coplanar-nets`
  below: the diff engine takes some of route.py's flags and not others, and the
  ones it drops fail quietly.)
- **Escape clearance — trigger on dropped balls, not pitch (issue #122):** the
  inter-ball channel is too narrow to fit a track at the net-class clearance on
  more BGAs than just "fine-pitch" ones. Even an **0.8 mm-pitch** BGA drops balls
  at `--clearance 0.2` (the ~0.45 mm gap between 0.35 mm balls can't fit a 0.2 mm
  track at 0.2 mm clearance) — the same board escapes **all** balls at the 0.1 mm
  floor. So don't gate on pitch: gate on whether balls actually dropped.
  `bga_fanout.py` and `qfn_fanout.py` both end with a `JSON_SUMMARY: {...}` line
  giving `requested`/`escaped`/`failed`/`unescaped_nets`. **After every fanout, parse it;
  if `failed > 0` (escaped < requested), re-run the fanout with `--clearance` at
  the manufacturing floor** (never below it — the floor is the rule the human
  board passes DRC against, so tightening board-wide is manufacturable and needs
  no rule-area settings). If still short, also try the smaller **fine-pitch escape
  via** (below) and/or a narrower `--track-width` toward the floor. Do not proceed
  to signal routing with `failed > 0` unexpected — those balls are dropped from the
  output and will fail later as "no rippable blockers".
- **Also check `drc_grazes` (even when `failed == 0`).** The summary's
  `drc_grazes` (graded at the fanout `--clearance`) reports sub-clearance grazes the
  escape left in the output: `via_segment` / `pad_via` are the #130 classes (an
  escape via too close to a foreign track or pad), `segment_segment` is the #179
  class (two escape **stubs** grazing — typically the 45° fans of two adjacent pads
  of a tight-pitch diff pair, e.g. 0.4 mm-pitch QFN, clipping at the wrist),
  `total` is all DRC violations. A *successful* fanout (every ball/pad escaped) can
  still leave many of these — they're not caught by `failed`. **If any
  `drc_grazes` class > 0 and there is headroom above the fab floor, re-run the
  fanout stepping toward — never below — the floor:**
  - `via_segment` / `pad_via` (#130): smaller **`--via-size` / `--via-drill`**
    (and/or a thinner `--track-width`).
  - `segment_segment` (#179): thinner **`--width`** — the escape stubs carry the
    track width, so narrowing them widens the gap between the two converging
    diagonals. Step down toward the fab-floor track (e.g. 0.15 → 0.13 → 0.10 mm)
    until `segment_segment == 0`; all pads still escape (`failed` stays 0).
    (Measured on hackrf_one U17: 3 grazes at `--width 0.15`/`0.13`, 0 at `0.10`.)

  These grazes are typically a uniform ~1-grid-cell shortfall, so even one size step
  down usually clears them all; shrinking the via also relieves escape congestion.
  (For *via-over-pad* grazes where a decoupling cap/resistor sits on a via,
  `place_fanout_clearance.py` (Step 1b) is the better fix — it moves the part;
  smaller vias/thinner tracks help the via-over-track and stub-over-stub classes.)

  **When a SPEC pins the width floor at or above the width you are already
  using, this rung does not exist.** The ladder says "toward, never below, the
  floor" and means the *fab* floor; a requirements document can put its own
  floor higher, and then there is no next step down. Measured on one board:
  `--width 0.15` was already the spec's HARD minimum, and the spec said in terms
  that the board "shall not be routed to" the fab's 0.10. **Do not thin below a
  spec floor to clear a graze.** Reach for the fixed-width levers instead, in
  this order:

  1. **`--grid-step`, matched to the routing grid** — the cheapest and most
     likely, because the shortfall usually IS one grid cell. `qfn_fanout` and
     `bga_fanout` default to `0.1`; if the route step runs at `0.05`, the fanout
     quantised to a coarser grid than everything downstream. Measured: 7 grazes
     all at exactly `0.011 mm` overlap — one 0.1-grid rounding.
  2. **A larger `--clearance` on the fanout**, buying the gap by asking for more
     room rather than by removing copper.
  3. **`--escape-method underpad`** (± `--allow-via-in-pad`) — it removes the 45°
     wrist where two stubs converge, which is where this class of graze sits.
  4. **Fan out fewer nets** — see the no-connect trap below.

  **Grade the leftovers with `--clearance-margin`, not by re-picking `-c`.** A
  ~1-grid overlap on copper that is otherwise at the floor is the quantisation
  artifact CLAUDE.md describes; `check_drc.py --clearance-margin 0.1` filters
  exactly it. Say the raw count and the filtered count, and which you are
  standing behind.

  **`check_drc.py -c` is NOT `route.py --clearance`.** On route.py the flag is a
  **ceiling over every class**. On `check_drc` it is only the **global fallback**,
  and a netclass override still wins — the tool prints
  `Required clearance: 0.1600mm (local/netclass override; global 0.1500mm)` and
  grades at 0.16 no matter what `-c` says. Measured on one board: 7 violations at
  `-c 0.16`, the same 7 at `-c 0.15`, the same 7 at `-c 0.149`. If you expected
  a looser `-c` to clear class-driven violations, it will not; change the class,
  or use `--clearance-margin`.

  **Do not fan out no-connect nets.** `--nets "*" "!GND" "!VCC"` matches every
  single-pad `*NC_*` net on the part. They get escape stubs, no later stage
  routes them, and the result is orphan copper that grazes its neighbours —
  invisible to every tally, because `escaped 43/43, failed 0` is *true*. Measured:
  2 of one board's 7 grazes were between two no-connect nets that should never
  have been fanned. Add `"!*NC_*"` (or the part's own no-connect prefix) to the
  fanout selection, and run `check_orphan_stubs.py` after the fanout — nothing
  else looks for copper nothing owns.
- **Fine-pitch escape VIA (4+ layer):** the 0.45 mm standard via can't dog-bone /
  via-in-pad sub-~0.5 mm-pitch BGA/QFN balls. For *those parts only*, pass the
  smaller **fine-pitch escape via** that `--design-rules` prints (`fine-pitch
  escape via <d>/<drill>`, e.g. `0.30/0.15` — JLC "advanced", small extra cost)
  as `--via-size`/`--via-drill` to that part's `bga_fanout.py` / `qfn_fanout.py`,
  to `route_diff.py` when it launches from that part's escaped stubs, **and to
  `route_disconnected_planes.py`** (its per-pad repair connects the fine-pitch
  GND/power plane balls under such parts). Keep the **standard** working via for
  general `route.py` routing and the bulk `route_planes.py` pour — the advanced
  via is escape-only, not a board-wide default (issues #99/#122).
- **Non-Default classes:** route those nets separately with that class's
  `--clearance`/`--track-width` (clearance is the one per-class DRC value, so keep
  each class's nets at their own clearance rather than forcing one global value).
- **Diff pairs:** default `--track-width 0.1 --diff-pair-gap 0.1` for `route_diff.py`
  (NOT the wide net-class values), plus `--impedance` when the interface is
  impedance-controlled; shrink track/gap/via further toward the fab floor for any
  pair that fails or grazes (see "Diff-pair sizing default + shrink-to-succeed").
  **Never set `--diff-pair-gap` below the same command's `--clearance`** — KiCad
  grades the pair's P↔N coupling as a plain clearance violation, so `route_diff`
  floors the gap up to clearance (#441). Set the two equal (both at the fab floor).

**Verification (DRC/connectivity) grades at the manufacturing floor**, not the
inflated net-class clearance — that is the same rule the human original passes, so
it's the honest delta. The routing/plane/fanout steps now **record the smallest
clearance any step actually used** (route_planes/route_disconnected_planes and the
single-ended multipoint taps auto-step the fine-pitch tap clearance DOWN toward the
fab floor as the geometry demands) into the output `.kicad_pro` DRC floor and into
`JSON_SUMMARY` (`min_clearance_used`). `check_drc.py` **auto-grades at that
`.kicad_pro` clearance when `-c` is omitted**, so a bare `check_drc.py board.kicad_pcb`
already grades at the true routed floor. Passing `--clearance <floor>` still works
to TIGHTEN the grade — it is a FLOOR, `max(-c, classA, classB)`, not an
override, so a value at or below the board's netclasses changes nothing. See
Step 6 and "`check_drc.py -c` is NOT `route.py --clearance`" above.

Only fall back to tool defaults when neither net classes nor Constraints are found
(`--design-rules` then prints the JLCPCB fab floor for the board's layer count).

This will output:
- Differential pairs detected (P/N naming conventions)
- Ground nets with pad counts
- Power nets with pad counts

If differential pairs are found:
- List each P/N pair
- Note that `route_diff.py` should be used for these
- Explain that diff pairs maintain consistent spacing and length matching
- **If a pair's pads are on a BGA/PGA being fanned out, escape it with
  `bga_fanout.py` too** — pass `--diff-pairs "<patterns>" --diff-pair-gap <gap>`
  so P and N escape the array together on one layer. Don't just exclude the
  pair from fanout and hand it to `route_diff.py`: it can't launch from the
  deep balls ("no valid position at any setback"). `route_diff.py` then
  connects the escaped stubs — **but on a 4+ layer board you must pass those
  inner layers to `route_diff.py` via `--layers` too** (it defaults to F.Cu
  B.Cu, so an inner-layer escaped stub is otherwise unreachable and the pair is
  silently dropped — issue #116). Pairs not on an array package don't need fanout.

> **Tip:** Name-based detection misses pairs with unconventional names. For boards with
> high-speed ICs (PHYs, SerDes, USB, FPGA transceivers), or when detection finds suspiciously
> few pairs, run `/identify-diff-pairs` for datasheet-based detection by pin function and
> per-interface gap/impedance recommendations.

**Polarity-swap policy (#279).** `route_diff.py` can resolve a P/N polarity mismatch by
swapping the target pads' net assignments — but a swap physically cross-connects one
device's P pin to the other's N pin, and is only harmless when an endpoint can compensate.
Swaps are **denied by default**; grant them per pair with `--polarity-swap-nets <patterns>`.
Before emitting the route_diff command, classify each pair's electrical endpoints (walk
through series AC caps/resistors to the real device):

- **Allow** pairs with an FPGA/CPLD generic-I/O endpoint (pin functions are reassigned in
  gateware — look for paired `IO_LxxP/N`-style pinfunctions on Xilinx/Lattice/Altera/Gowin
  parts), and protocol-tolerant links (PCIe lanes, SerDes with polarity-invert, 1000BASE-T).
- **Deny** USB `D+/D-`, MIPI, TMDS/HDMI/DP, CAN, RS-485/422, DDR `CK/DQS`, clock/analog
  inputs to fixed-function parts, anything reaching a connector or unknown part, and any
  pair whose nets carry an asymmetric attachment (e.g. a single-sided pull-up) — it stays
  on its net and would land on the wrong physical wire. MCUs/SoCs do NOT count as
  programmable (their diff functions are fixed silicon). **When in doubt, deny** — a
  skipped pair beats a dead interface. `/identify-diff-pairs` reports a per-pair
  `polarity_swappable` verdict from datasheet pin functions for the ambiguous cases.

Pass the resulting allowlist, e.g. `--polarity-swap-nets '/fpga/IO_*'` (use `'*'` only when
every pair classifies swappable). Applied swaps are listed in `polarity_swapped_pairs` —
when they happen, the schematic sync step below applies (see "Schematic Synchronization
After Swaps"). Pairs that *wanted* a swap but were denied are listed in
`polarity_swap_denied_pairs` — surface these to the user (they either routed via the
opposite-side flip or failed honestly and may need a manual pin swap in the schematic).

**Far-apart terminal pads → single-ended follow-up (issue #121).** A "diff pair"
sometimes has pads that aren't a coupled connection — e.g. a P and an N test point
several mm apart, or a logical pair daisy-chained through spread-out parts. If the
coupled chain can't be routed, `route_diff.py` peels those far-apart pads off the
chain (routing the genuinely-coupled terminals as a pair) and lists the affected
nets under `single_ended_followup_nets` in its `JSON_SUMMARY` (and a "route them
single-ended next" block on stdout). Those pads are **not** dropped — the **Signal
Routing** step (`route.py "*" "!GND" "!VCC"`) connects them P→P / N→N along with
every other unrouted net, since they remain unrouted after the diff-pair step. So:
**do not exclude the diff-pair nets from the signal-routing step's net selection** —
that step is what finishes the peeled pads. If you scope the signal step to specific
nets instead of `"*"`, add any `single_ended_followup_nets` to it explicitly.

**CARRY THE PAIR'S WIDTH INTO EVERY STEP THAT MAY TOUCH IT.** This handoff is a
silent width leak, and on a board with a HARD pair geometry it destroys the
requirement. The peeled pads are finished by a step whose `--track-width` is the
*signal* width, so they come back thin — and every later `"*"` pass (the Step 5c
reconnect especially) can do the same to any pair segment it decides to redo.
Measured on one board: `route_diff` emitted the pair correctly at 0.8 mm, and by
the end of the chain **14 segments of it were 0.15 mm**, with `failed_diff_pairs`
empty and every step reporting success.

The fix is the same one 9.3c rule 2 gives for rips — a net returns at the
**calling** command's parameters — applied to the peel path:

```bash
# on the signal step AND the Step 5c reconnect, not just one of them
route.py ... --nets "*" "!GND" \
    --power-nets USB_DP USB_DM USB_DP_R USB_DM_R --power-nets-widths 0.8 0.8 0.8 0.8
```

`--power-nets` is not only for power: it is the per-net width channel, and it is
the only way a `"*"` pass can honour a geometry an earlier step established. Then
verify with the width counter below — `board_score --net-min-widths` will show it
as `net_widths` if you miss it, but only if you passed the file.

**ONE `--power-nets` per command, never two.** It is `nargs="*"` with no `append`
action (`route.py:2981`), so a second occurrence **replaces** the first rather
than adding to it — and the widths are positional against the net list, so the
whole first group loses its width silently. Building the flag from two shell
variables (`$PWR $USBW`) is the natural way to write this and it is wrong:
measured, the rails' 0.4 mm vanished and **248 of 248** power segments came back
at the signal width, with nothing reporting a problem. Merge the nets and the
widths into a single flag:

```bash
--power-nets VCC3V3 VBUS USB_DP USB_DM --power-nets-widths 0.4 0.4 0.8 0.8
```

**And `--power-nets-widths` is itself only a REQUEST.** Getting the flag right is
necessary and still not sufficient: a wide route that will not fit is necked down
by the same ladder as `--track-width`, and the log says so in one line among
thousands — `Wide power route blocked - routed short edge at 0.2000mm (down from
0.8000)`. Measured: the flag landed correctly (`0.8mm: USB_DP_R, USB_DP, USB_DM,
USB_DM_R` in the log) and the board still carried that pair at 0.2 and 0.15.

Two things the rest of this skill does not tell you, and both matter here:

- **`--no-power-tap-neckdown` is the actual off-switch.** It forbids the taper
  rather than asking for a width. Reach for it when a width is a HARD
  requirement and you would rather the net FAIL than come back thin — which is
  the whole point of a floor.
- **`--track-width-floor` is a single GLOBAL scalar** (`routing_config.py:167`),
  not per-net. It cannot hold a pair at 0.8 while signals run at 0.15: set it to
  0.15 for the signals and the pair may legally neck to 0.2. There is no per-net
  floor flag. So the only honest gate on a per-net width is to **measure the
  emitted copper** and carry the requirement in `board_score --net-min-widths`.

### Check for DDR/High-Speed Memory Signals

Look for DDR signal patterns in the net list that may need length matching:
- Data signals: DQ0-DQ63
- Strobes: DQS, DQM, DM
- Clocks: CLK, CK

If DDR signals detected:
- Note that `--length-match-group auto` should be used
- DQ0-7 + DQS0 form byte lane 0, DQ8-15 + DQS1 form byte lane 1, etc.

Report to user:
- List of detected differential pairs (or "none found")
- Whether `route_diff.py` is needed
- Whether DDR/length-matching is needed

### High-Speed Signal Check (delegate to /find-high-speed-nets)

Whether the plan includes GND return vias - and the `--gnd-via-distance` to use -
is the `/find-high-speed-nets` skill's job: it classifies nets into speed tiers
(datasheet lookup, rise-time estimates) and maps tiers to recommended distances.
Follow that skill's methodology here (its quick net-name/footprint scan decides
whether the deeper datasheet pass is worth it) and put the recommended distance
into the plan's GND-via step. Remember its physical floor: never set
`--gnd-via-distance` below 3 x (via_size + clearance), ~2.5 mm for standard vias.

Report to user when presenting the plan:
- If high-speed nets found: "**GND Return Vias:** This board has [tier] signals ([examples]).
  GND return vias are included in Step N with `--gnd-via-distance [X]mm`. Let me know if
  you'd like to skip this step."
- If no high-speed nets found: "**GND Return Vias:** No high-speed signals detected (only
  low-frequency I2C/UART/GPIO). GND return vias are included in the plan but are optional
  for this board. Want me to remove the step?"

`/find-high-speed-nets` ALSO reports **controlled-impedance nets** (its Step 4.5):
RF/antenna feeds (radio/PA/LNA -> SMA/U.FL/chip-antenna = **50 ohm single-ended**,
or 100 ohm if balanced), DDR SSTL, and the impedance-controlled diff interfaces.
Thread these into the plan:

- **Differential** impedance nets stay in the diff-pair step (Step 2) — just add
  `route_diff.py --impedance <ohms>`.
- **Single-ended** impedance nets (RF 50, DDR SSTL 40) get a **dedicated
  `route.py --impedance` pass placed AFTER diff pairs and BEFORE the general
  signal route** (Step 2b below). They must then be **excluded from the general
  signal route** (`"*" "!GND" "!VCC" "!RF"`) and counted in the Step 5b ledger as
  claimed by the impedance step — otherwise a later rip-up re-routes them at the
  wrong width.
- Impedance width is computed from the **stackup**: if the board has only KiCad's
  default stackup, lead the report with that warning and run `/recommend-stackup`
  first (an RF feed routed at a wrong width is electrically useless).
- For an RF/antenna feed also recommend (in words) a `User.2` keepout around the
  antenna region and `--keepout`, and route it short/direct on an outer layer.

If no controlled-impedance nets are found, omit Step 2b.

### Step 2b-i: Coplanar (CPW-over-ground) — decide this WITH the plane step (#486)

An impedance trace on an **outer layer that will also carry a GND pour** is not a
microstrip: the side ground pulls Z0 down hard, so hitting the target needs a
**narrower** trace (e.g. 0.277 mm instead of 0.376 mm for 50 Ω on 0.2 mm FR4).
Routing the microstrip width through a pour lands the trace well below target.

The router cannot detect this — at route time the pour does not exist yet. So
this is **your decision to make in the plan**, and it must be coordinated across
two steps that run at different times.

**Declare coplanar when ALL of these hold:**
1. The impedance net routes on an **outer layer** (`F.Cu` / `B.Cu`). Inner layers
   are stripline; the flag is ignored there.
2. A `route_planes` step in this plan pours **GND on that same layer** — or the
   board already has an outer-layer GND pour that will survive.
3. You can name the gap: it is the pour's zone clearance.

**If you are not pouring on the signal's own layer, do NOT pass `--coplanar-gap`.**
A coplanar declaration whose pour never arrives leaves the trace too narrow, i.e.
impedance too HIGH — the opposite error, equally wrong.

**Coordination — one number, three places:**

```bash
# choose ONE gap G (the pour's clearance; near the fab floor, e.g. 0.2)
# 1. route the impedance nets, declaring G
python3 route.py in.kicad_pcb s2b.kicad_pcb --nets "RF*" \
    --impedance 50 --coplanar-gap 0.2 --clearance 0.2

# 2. pour GND on the SAME layer with a MATCHING zone clearance
python3 route_planes.py s2b.kicad_pcb s5.kicad_pcb \
    --nets GND GND --plane-layers F.Cu B.Cu --zone-clearance 0.2

# 3. verify the declaration actually held
python3 check_impedance.py s5.kicad_pcb --coplanar-gap 0.2 --nets "RF*"
```

- `--coplanar-nets "<patterns>"` narrows the declaration to some nets in a call;
  omit it and every net in that call is treated as coplanar. Since Step 2b is
  already a dedicated impedance pass over exactly those nets, omitting it is
  usually right.
- `route_diff.py` takes `--coplanar-gap` but has **no** `--coplanar-nets` (the
  diff engine bakes one width per layer). Split interfaces into separate calls.
- The gap must be **achievable**: it is a pour clearance, so it cannot be below
  the fab floor, and near via antipads / pads the real gap will be wider. The
  Step-3 audit reports how much of each net actually achieved it.

**Report to the user** which nets you declared coplanar, the gap, and the plane
step it is tied to — this is a coupled choice they may want to override. If the
board has no outer-layer pour planned, say so explicitly and note that the
impedance nets are being routed as plain microstrip.

## Step 5: Review Power and Ground Net Strategy (delegate to /recommend-plane-mappings)

Which nets deserve planes and on which copper layers is the
`/recommend-plane-mappings` skill's job: it weighs pad counts and datasheet
current estimates, and assigns layers with SI rationale (GND adjacent to signal
layers for return paths, power planes paired against GND, split layers for
multiple rails). Follow its methodology here, seeded by the `list_nets.py --power`
output, and put the resulting net -> layer assignments into the plan's
`route_planes` steps. Nets it leaves to wide traces become `--power-nets` /
`--power-nets-widths` on the route step instead.

Report to user:
- Identified GND nets and pad counts
- Identified power nets and pad counts
- Recommended strategy (plane vs wide traces) with layer assignments

## Step 5b: Net-Coverage Reconciliation (mandatory — do not skip)

The stages partition every routable net by glob pattern, and the patterns are
**not** reconciled automatically. The failure mode this step prevents: a net is
*excluded* from one stage (`!X`) but never *claimed* by a later one, so it
silently gets zero copper and the run "completes" with it fully unrouted. This
is exactly how `GNDA` (an analog ground tied to `GND` through a single 0Ω/
ferrite) was dropped — excluded from the signal route as a "power net", yet never
added to the plane step's `--nets`, ending with 0/23 pads connected while the run
reported success.

**The invariant: every routable net (≥2 pads, not no-connect) must be claimed by
exactly one stage. A net excluded from any stage MUST be claimed by a later one.**

Before running any command, write the net-handling ledger and reconcile it
mechanically — do not eyeball it:

1. **Assign every routable net to one handler:**
   - `fanout + signal route` — ordinary signals (the `"*"` selection minus exclusions)
   - `diff-pair route` — detected pairs
   - `impedance SE route (Step 2b)` — single-ended controlled-impedance nets (RF/antenna
     50 ohm, DDR SSTL 40 ohm); excluded from the signal route, NOT poured
   - `plane / pour` — every net you exclude from the signal route with `!X` that a plane pours
   - `wide trace` — power carried *inside* the route selection via `--power-nets` (NOT excluded)

2. **Diff the pattern lists.** The set of signal-route exclusions (`!A !B …`) MUST
   equal the nets the plane step pours PLUS the single-ended impedance nets routed
   in Step 2b. A net in the symmetric difference is a plan bug — excluded from
   routing but handled by no later stage (→ unrouted), or poured/impedance-routed
   but not excluded (→ also routed as ordinary tracks, defeating it). Print and
   assert the difference is empty:
   ```python
   route_exclusions = {"GND", "+3V3", "RF"}     # the !X you will pass route.py
   plane_nets       = {"GND", "+3V3"}           # the --nets you pass route_planes.py
   impedance_se     = {"RF"}                     # nets routed in Step 2b (route.py --impedance)
   orphans = route_exclusions ^ (plane_nets | impedance_se)   # symmetric difference
   assert not orphans, f"Net-coverage gap: {sorted(orphans)} handled by no stage"
   ```
   Do not proceed until `orphans` is empty.

3. **Secondary grounds / split rails** (`AGND`, `GNDA`, `DGND`, `VREF`, or any rail
   tied to its parent through a single 0Ω resistor or ferrite bead — find the tie
   with `list_nets.py`: the part with one pad on each net). These are real,
   separate nets. Pour each as **its own local region** (Voronoi-sharing an inner
   layer with the main ground is fine) and let the single tie component join it to
   the parent. **Never** merge it into the parent plane (that shorts the split and
   defeats its purpose — a green connectivity check then hides an electrical error)
   and **never** leave it out (that leaves it unrouted). Give each its own `--nets`
   entry in the plane step, so it appears in BOTH lists in step 2 above.

### Placement steps are NOT part of this partition

`place_optimize.py` / `place_route_loop.py` move parts. They add no copper and
connect nothing, so they claim no nets and must not appear in the handler
assignment above — the ledger's assert would otherwise have to be bent around a
step that routes nothing.

Their `--ignore-nets` is a **scoring** exclusion (which nets the airwire cost
ignores), not a coverage claim. It does get one reconciliation of its own, for
the same reason the route exclusions do: a plane-routed rail's airwire is a
fiction the optimizer would otherwise chase across the board.

```python
assert set(place_ignore_nets) == plane_nets, \
    f"placement scored a plane net as an airwire: {set(place_ignore_nets) ^ plane_nets}"
```

## Step 6: Generate Routing Plan

Based on the analysis, generate a step-by-step plan. The general order is:

### Routing Order Rationale

0. **Placement (conditional -- normally SKIPPED).** Run it ONLY for a rough /
   imported / generated placement, or when routing has already FAILED and
   `/diagnose-routing-failures` blames congestion rather than parameters. See
   Step 0's decision table; the default is **do not run it**. Run the lock
   advisor first and pass its `--lock` list. A placement step claims NO nets
   (see the Step 5b carve-out) and invalidates every downstream routed board.
1. **Fanout** (if needed) - Escape routing first, while the board is empty. Exclude
   nets that planes will handle (`"*" "!GND" "!VCC"`). **After each BGA/PGA
   fanout, run `place_fanout_clearance.py`** (Step 1b) to clear decoupling-cap /
   fanout-via collisions (#130) before signal routing.
2. **Differential Pairs** - The most constrained routes claim their channels before
   anything else can block them (if present). Add `--impedance <ohms>` for the
   controlled ones (USB/Ethernet/LVDS/balanced-RF; from `/find-high-speed-nets`).
   May peel far-apart "terminal" pads (e.g. spread-out test points) off the coupled
   chain and leave them for the signal-routing step (reported as
   `single_ended_followup_nets`, issue #121).
2b. **Impedance-controlled single-ended nets** (only if `/find-high-speed-nets`
   found any - RF/antenna feeds = 50 ohm, DDR SSTL = 40 ohm). A dedicated
   `route.py --impedance <ohms>` pass, routed here - after diff pairs, before the
   bulk signal route - because they need a stackup-derived width and a short,
   direct path over a clean ground reference, so (like diff pairs) they must claim
   their channel before the bulk signals fill the area. Route an RF feed on an
   outer layer (`--layers F.Cu`); requires a real stackup (see Step 2 stackup
   check). These nets are then EXCLUDED from step 3.
2c. **ANY net with a per-net geometric requirement** — not just impedance ones.
   The step-2b shape (own pass, before the bulk route, then excluded from it) is
   the general answer whenever the spec constrains ONE net's geometry: a required
   width, a layer restriction, a **via ban**, a maximum length. Routed in the bulk
   pass those nets get whatever the router finds convenient, and no later step can
   put it back without ripping everything around them.
   Worked example — a crystal spec'd at *0.15 mm, max 0 vias per leg*: routed in
   the bulk pass it came out at 0.16 mm with **6 vias**; given its own single-layer
   pass first (`--nets XIN XOUT XTAL_XOUT --layers F.Cu --track-width 0.15`) it met
   both clauses, because a one-layer route cannot place a via at all. Constrain by
   **construction** where you can — a `--layers` with one entry is a via ban the
   router cannot violate — rather than by hoping the bulk pass agrees.

   **READ EVERY DOCUMENT THAT DERIVES FROM THE CLAUSE, not just the spec row.**
   A requirements table is rarely the whole requirement. This cost a real
   mistake, in the direction that matters: a bus whose spec row read *"Max
   direct-run length, single layer | ≤15 mm"* was pinned to one layer; the pin
   made the board hard to route (**29 broken pieces**), so the row was re-read as
   "it only bounds a length" and the pin removed. That reading was **wrong**. The
   repo's design brief said, deriving from the same clause:

   ```
   | Layer preference | L1 (top) only, one layer, direct run — HARD | HW-TB-PCB19 |
   | Via transitions  | Max 0 vias — the ≤15 mm run length assumes a direct
                        single-layer path with no layer changes  | derived from HW-TB-PCB19 |
   ```

   and the repo's own checker gated on vias for that clause. Unpinning healed
   connectivity and made the clause fail **worse** (6 violated lines instead of
   5) — a board that scores better and conforms less.

   The rule: before you decide a clause does *not* impose a constraint, check the
   **design brief, the checker, the netclass and the `.kicad_dru`** as well as the
   spec table. A constraint that is expensive to hold is not evidence that it
   isn't there — and "the score improved when I dropped it" is exactly the
   reasoning that ships a non-conformant board. If a HARD constraint really is
   unsatisfiable, that is stop condition 4: report it with the measurement, do
   not quietly relax it.

   (The genuine caution stands: a one-entry `--layers` halves the routing space,
   which on a 2-layer board is most of the board. Expect to pay for it, and say
   what it cost — but pay it when the requirement says so.)

   **A Step 2c pass is not durable. Two later steps silently undo it, and both
   report success.** Excluding the net from the bulk signal route is necessary
   and NOT sufficient — measured on one board, a crystal and a QSPI bus that
   left their own passes on F.Cu with **0 vias** ended the chain on both layers
   with **8 vias**, failing four HARD clauses, while every step printed a clean
   summary. The two doors:

   1. **`route_disconnected_planes --rip-blocker-nets` reconnects what it rips,
      IN-STEP, at ITS OWN parameters.** It does not know your `--layers`. Pass
      **`--net-layers <json>`** — `{"QSPI_SD0": ["F.Cu"], ...}` — and the ripped
      net comes back on its own layer, where it cannot take a via at all. Add
      `--track-width-floor` for a width clause. Without it a rip is a silent
      constraint reset.
   2. **The Step 5c reconnect's `--nets "*"` re-routes them again.** The template
      below excludes only the *plane* nets; on a board with per-net geometry that
      flattens every Step 2c pass in one command. **Mirror the geometry passes in
      the reconnect, in the same order, and sweep the remainder last:**

      ```bash
      route.py r7.kicad_pcb r8a.kicad_pcb --nets XIN XOUT XTAL_XOUT --layers F.Cu --track-width 0.15
      route.py r8a.kicad_pcb r8b.kicad_pcb --nets "QSPI_*" FLASH_CS --layers F.Cu --track-width 0.16
      route.py r8b.kicad_pcb r8.kicad_pcb  --nets "*" "!GND" "!XIN" "!XOUT" "!XTAL_XOUT" "!QSPI_*" "!FLASH_CS"
      ```

   **The rule, and its exact scope: a constraint with no persistence channel in
   the `.kicad_pro` must be re-stated at EVERY step that can touch the net.**
   That is **layer and width pins specifically** — `--layers`,
   `--power-nets-widths`, `--net-layers`, `--track-width-floor`. A step that
   re-routes without them resets the net to that step's defaults. Same failure as
   9.3c rule 2 (a ripped net returns at the *calling* command's parameters),
   reaching the plane repair and the reconnect as much as an explicit
   `--rip-existing-nets`.

   Do **not** over-generalise it — several constraints ARE durable and re-stating
   them is wasted effort:

   - **protected nets** (#521): matched groups and routed diff pairs are recorded
     in the sibling `.kicad_pro` and no rip glob or `--rip-blocker-nets` touches them;
   - **KiCad-`locked` copper**: never rippable, with no override at all;
   - **`net_impedance` declarations**: persisted, and recomputed identically by a
     later step from the stackup;
   - **`.kicad_dru` per-layer clearance**: auto-read by every routing step;
   - **an explicit exclusion**: a bulk pass with `"!QSPI_*"` leaves that copper
     byte-identical — the exclusion works, it just isn't sufficient on its own.

   The asymmetry is the point: those four have a home in the project file, and
   layers and widths do not.
3. **Signal Routing** - All remaining nets, **excluding the plane nets AND any
   single-ended impedance nets from step 2b** (`--nets "*" "!GND" "!VCC" "!RF"`).
   Routing the plane nets as tracks would defeat the planes step; re-routing the
   impedance nets here would drop their controlled width - both exclusions are
   mandatory. This step also finishes any diff-pair pads peeled off in step 2, so
   keep the diff-pair nets in its selection (the `"*"` covers them).
4. **Power Planes** - Create GND and VCC planes together. Stitching vias adapt
   around the routed signals; the reverse is not true - a stitching via placed
   early can block the only clean channel for a diff pair (issue #56). If signal
   tracks boxed in a power pad, add `--rip-blocker-nets` so the blockers are
   ripped and rerouted.
5. **GND Return Vias** - Add return current vias near signal vias (when GND planes
   present); folds into the planes call with `--add-gnd-vias`.
6. **Plane Repair** - Reconnect any broken plane regions
7. **Verification** - DRC and connectivity checks

### Example Plan Output Format

Present the plan to the user as a numbered list with explanations:

```
## Routing Plan for board.kicad_pcb

### Board Summary
- 2-layer board (F.Cu, B.Cu)
- 174 nets, 25 components
- Unrouted (0 existing traces)

### Components Requiring Special Handling
- **U9 (PGA120)**: 120-pin grid array - use bga_fanout.py for signals only

### Differential Pairs
- None detected

### Power/Ground Nets
- **GND**: 42 pads - use plane on B.Cu
- **VCC**: 23 pads - use plane on F.Cu (or wide traces if planes not desired)

---

## Step-by-Step Routing Commands

### Step 1: Fanout U9 (PGA120) - All Non-Plane Nets
Generates escape routing for ALL nets on the component EXCEPT those that the
planes step will handle. This ensures every signal net gets fanned out,
avoiding `--no-bga-zone` workarounds during routing.

**Important:** Use `"*" "!GND" "!VCC"` to fan out all nets except the power
plane nets. Do NOT use `"/*"` alone, as it misses nets with non-hierarchical
names like `Net-(U9-Pad1)` which would then require `--no-bga-zone` to route.

On a 4+ layer board also pass every copper layer with `--layers` (default is
F.Cu B.Cu only) so inner balls can escape — drop `--layers` only for true
2-layer boards.

python3 -X utf8 bga_fanout.py board.kicad_pcb \
    --component U9 \
    --nets "*" "!GND" "!VCC" \
    --layers F.Cu In1.Cu In2.Cu B.Cu \
    --output board_step1.kicad_pcb \
    2>&1 | tee /tmp/step1_fanout.txt

**Then check the `JSON_SUMMARY` line: if `failed > 0`, balls were dropped — retry
before continuing.** First confirm all copper layers are passed; then re-run with
`--clearance` at the manufacturing floor (e.g. `--clearance 0.1`), which fixes the
common case (an 0.8 mm-pitch BGA can't fit a track between balls at 0.2 mm). If still
short, add the fine-pitch escape via and/or a smaller `--track-width`. Only proceed
to Step 2 once `failed == 0` (or the remaining `unescaped_nets` are understood and
accepted).

### Step 1b: Optimize Decoupling-Cap Placement (run after EACH BGA fanout — issue #130)
Nudges decoupling caps near the BGA off the foreign-net fanout vias (the
`PAD-VIA` violations #130) and pulls each pad toward its nearest same-net
ball. Run it on the just-fanned board, **before** signal routing. Use the
**same `--clearance`** you gave the fanout / your DRC floor — that's the only
setting that matters (it reads each via's real size from the board).

python3 place_fanout_clearance.py board_step1.kicad_pcb board_step1b.kicad_pcb \
    --clearance 0.1

It prints `Moved N cap(s); resolved R/M ... K unresolved`. Any **unresolved**
caps had no clear spot within the displacement budget — note them for a manual
nudge; they are not auto-fixed. By default (`--cap-prefix C,R`) it moves 2-pad
**caps and resistors** near a BGA (RN-style arrays auto-excluded since only
2-copper-pad parts move); it never overlaps parts, and is a no-op when nothing
collides. Feed `board_step1b.kicad_pcb`
into the next step (if multiple BGAs are fanned in series, run this once after
each, or once after the last fanout — it considers all BGAs' vias on the board).
Verify with `check_drc.py board_step1b.kicad_pcb -c 0.1` (PAD-VIA count drops).

### Step 2b: Impedance-Controlled Single-Ended Nets (only if any were found; runs before the Step 2 signal route)
ONLY when `/find-high-speed-nets` reported single-ended controlled-impedance nets
(RF/antenna feed = 50 ohm, DDR SSTL = 40 ohm). Route them in their own
`--impedance` pass, after diff pairs and BEFORE the general signal route, so they
claim a clean, short, direct channel at the stackup-derived width. Requires a real
stackup (run `/recommend-stackup` first if the board has KiCad's default). Route an
RF feed on an outer layer over the GND plane; recommend a `User.2` keepout +
`--keepout` around any antenna region (user draws it).

python3 -X utf8 route.py board_diff.kicad_pcb board_step2b.kicad_pcb \
    --nets RF --impedance 50 --layers F.Cu \
    --clearance <floor> --no-bga-zone \
    2>&1 | tee /tmp/step2b_impedance.txt

### Step 2: Route All Signal Nets (excluding plane nets + impedance nets)
Routes all remaining unrouted nets EXCEPT the nets that get planes in the
next step - the `"!GND" "!VCC"` exclusions are mandatory here, otherwise the
power nets get routed as ordinary tracks and the planes step has nothing to
do - AND any single-ended impedance nets already routed in Step 2b
(`"!RF"`), so the bulk pass cannot re-route them off their controlled width.
Routing signals before planes means the plane stitching vias (placed
next) adapt around the signals instead of blocking them.

For boards with BGA/PGA components, use `--no-bga-zone` to allow the router
to find alternative paths through the dense pin area (even when fanout was
done, some paths may require this). Use `--max-ripup 10
--max-iterations 1000000` for difficult 2-layer boards.

python3 -X utf8 route.py board_step1.kicad_pcb board_step2.kicad_pcb \
    --nets "*" "!GND" "!VCC" \
    --no-bga-zone \
    --max-ripup 10 \
    --max-iterations 1000000 \
    2>&1 | tee /tmp/step2_routing.txt

(When Step 2b ran, add its impedance nets to the exclusions, e.g.
`--nets "*" "!GND" "!VCC" "!RF"`, and route from `board_step2b.kicad_pcb`.)

### Step 3: Create Power Planes (GND and VCC) + GND Return Vias
Creates power planes in a single call, after signal routing so the stitching
vias find spots around the finished tracks. Each net is paired with its
corresponding layer (GND→B.Cu, VCC→F.Cu). Through-hole PGA/BGA pads
automatically connect to planes on their layer; SMD pads get vias routed to
the plane. `--add-gnd-vias` also places return-current vias near the signal
vias that now exist. If signal tracks boxed in a power pad, add
`--rip-blocker-nets` to rip the blockers out of the way (they are left unrouted
and reconnected by the Step 5c route.py pass).

> **Note to user:** GND return vias improve signal integrity for high-speed
> signals. Based on the speed analysis, this board has [speed_tier] signals,
> so `--gnd-via-distance` is set to [X] mm. If this is a purely low-frequency
> board (I2C/UART/GPIO only), drop `--add-gnd-vias`. Let me know if you'd
> like that.

python3 -X utf8 route_planes.py board_step2.kicad_pcb board_step4.kicad_pcb \
    --nets GND VCC \
    --plane-layers B.Cu F.Cu \
    --add-gnd-vias --gnd-via-distance 2.0 \
    2>&1 | tee /tmp/step3_planes.txt

Adjust `--gnd-via-distance` based on the board's highest signal speed:
- Ultra-high (>1 GHz): 2.0 mm
- High (100 MHz - 1 GHz): 3.0 mm
- Medium (10 - 100 MHz): 5.0 mm
- Minimum physical limit: 3 x (via_size + clearance)

### Step 5: Repair Disconnected Plane Regions
Signal traces and GND return vias may have cut through planes. This step
reconnects any isolated copper islands AND repairs pad-level plane connections.
With `--rip-blocker-nets`, a plane-net pad that can't reach its plane (e.g. a tiny
connector GND pin blocked by a signal trace) is connected by tracing to an
adjacent same-net pad, **ripping the blocking net out of the way**. The ripped
blockers are **left UNROUTED here** — they are reconnected by the route.py pass in
Step 5c, NOT inside this step. (Re-routing them in-step is unsafe: a ripped net
that fails to re-route had its original copper restored on top of whatever had
meanwhile been routed through its freed corridor, shorting them — the restore
bypasses the obstacle map. Issue #141 reverted; `--reroute-ripped-nets` and the
plugin's "Auto-reroute ripped nets" checkbox are now deprecated no-ops.) Carry
over Step 2's clearance/via/track-width/grid and `--no-bga-zone`.

python3 -X utf8 route_disconnected_planes.py board_step4.kicad_pcb board_step5_repair.kicad_pcb \
    --clearance <floor> --via-size <V> --via-drill <D> --track-width <signal_track> --grid-step <G> \
    --rip-blocker-nets --net-layers <json> --track-width-floor <spec floor> \
    --power-nets <PWR...> --power-nets-widths <W...> [--no-bga-zone] \
    2>&1 | tee /tmp/step5_plane_repair.txt

**If it reports `Pads still unconnected` on fine-pitch (BGA/QFN ≤0.5 mm-pitch)
pads, retry the repair in this order — cheapest/safest first:**

1. **Smaller via first** — drop `--via-size`/`--via-drill` toward the **fab-floor
   / fine-pitch escape via** (e.g. `0.30/0.15`), but **never below the fab via
   floor**. A boxed fine-pitch pad usually fails because the repair *via* can't
   fit beside the ball; a smaller via fits in/near it and frees the connection.
2. **Then finer grid** — drop `--grid-step` (e.g. `0.05 → 0.025`), but **not below
   the board's minimum feature / your fab grid**. This is for the case where the
   pad connects by a *trace* to an adjacent already-connected same-net ball (the
   repair does this automatically): the trace is already thin, but at a coarse
   grid the A* can't thread the 0.65 mm-pitch BGA escape. **Measured on
   ottercast_audio: GND U1.N4 fails to route at `--grid-step 0.05` but connects
   to its neighbour ball at `0.025`** — it's a grid-resolution limit, not a width
   one (the trace runs at the thin signal track in both the A* and obstacle map).

Re-check `check_connected.py` after each retry; stop as soon as the pads connect
(finer grid is slower, so only escalate to it if the smaller via didn't do it).

### Step 5c: Reconnect the nets plane-repair left unrouted (mandatory if Step 5 ripped any)
route_disconnected_planes lists the blockers it ripped and left unrouted. Reconnect
them with a final route.py pass using the **same parameters as the Step 2 signal
route** — clearance/via/track-width/grid, `--no-bga-zone`, and the **same
`--power-nets`/`--power-nets-widths`** so a wide power net re-routes at its wide
width, not the signal default. route.py routes against the live obstacle map
(planes + repairs included) with safe rip-up/restore, so it reconnects them without
the shorts the old in-step reroute caused. This produces the canonical final board
`board_step5.kicad_pcb`. (If Step 5 reports it ripped nothing, you may skip this and
copy board_step5_repair.kicad_pcb -> board_step5.kicad_pcb **with `copy_board.py`, not
bare `cp`** — see the warning below.)

> **Never `cp` a board without its `.kicad_pro`.** A bare `cp step5_repair.kicad_pcb
> step5.kicad_pcb` copies only the board and strands the sibling `.kicad_pro`, which
> holds the DRC floor (the Default-netclass clearance/track/via the chain routed to).
> The next routing step then reads no project, resolves its floor from the STOCK
> (looser) netclass, and its writeback stamps that looser floor over tighter copper —
> so KiCad grades correct sub-floor copper as phantom clearance violations (icepi_zero:
> a dropped 0.09 floor became 0.10 → 160 phantom grazes). Use
> **`python3 copy_board.py src.kicad_pcb dst.kicad_pcb`** — it copies the board plus every
> sibling (`.kicad_pro`/`.kicad_prl`) and self-records into the redo manifest — or, if you
> must use `cp`, copy the `.kicad_pro` too. The routing scripts also WARN when an input
> board has no sibling `.kicad_pro` (#441).

python3 -X utf8 route.py board_step5_repair.kicad_pcb board_step5.kicad_pcb \
    --nets "*" "!GND" "!<other_plane_nets...>" "!<every Step 2c net>" \
    --clearance <floor> --via-size <V> --via-drill <D> --track-width <signal_track> --grid-step <G> \
    --max-ripup 10 [--no-bga-zone] \
    --power-nets <PWR...> --power-nets-widths <W...> \
    2>&1 | tee /tmp/step5c_reconnect.txt

### Step 6: Verify Results

**Score it first — one command, and it is the gate.** Everything below is the
detail behind this number; run it before anything else so you know whether you
are reviewing a finished board or an unfinished one:

```bash
python3 -X utf8 .claude/skills/plan-pcb-routing/scripts/board_score.py \
    board_step5.kicad_pcb --intent wk/floorplan.json \
    --min-track-width <spec> --min-via-diameter <spec> --min-via-drill <spec> \
    --json wk/score.json
```

`blocking == 0` (exit 0) → proceed to the review and the verifier lenses.
`blocking > 0` (exit 4) → the board is **not done**; go to **Step 9** and spend
an iteration. Do not write a summary that describes an unfinished board as
finished with caveats.

Invoke `/review-routed-board board_step5.kicad_pcb` for the full review (DRC,
connectivity, orphan stubs, length-match tolerances, GND return via coverage,
diff pair checks). If that skill is unavailable, run the raw checks — `check_drc.py`
**auto-grades at the `.kicad_pro` clearance the routing steps wrote** (the smallest
clearance any step used, including auto-stepped fine-pitch taps), NOT a hardcoded
0.25, so legitimately-tight fine-pitch escapes that are still fabbable don't read as
violations (#111/#226). A bare invocation is correct; pass `--clearance <floor>`
(from Step 4's `--design-rules` output) only to override:

python3 -X utf8 check_drc.py board_step5.kicad_pcb 2>&1 | tee /tmp/step6_drc.txt
python3 -X utf8 check_connected.py board_step5.kicad_pcb 2>&1 | tee /tmp/step6_connectivity.txt
python3 -X utf8 check_orphan_stubs.py board_step5.kicad_pcb 2>&1 | tee /tmp/step6_orphans.txt
```

**Coverage gate (mandatory — close the loop on Step 5b).** `check_connected.py`
already lists every net with ≥2 pads but no copper and no covering zone as
"Unrouted net with N pads" (it accounts for plane zones and ignores genuine
single-pad / no-connect nets). After planes + repair, **this unrouted list must
be empty** except for entries you can individually justify in writing (true
single-pad nets, deliberate no-connects). A fully-unrouted multi-pad net is a
coverage defect, NOT a shortfall to report-and-accept: it means a net fell
through the stage partition (Step 5b). For each one, go back and handle it —
route it, or add it to the plane step (a secondary ground gets its own pour
region per Step 5b) — then re-verify. Do not declare the board done while the
list has unjustified entries.

### Alternative: VCC as Wide Traces (No Plane)

If you prefer not to use a VCC plane, route VCC with wide traces instead:

```
### Step 1 (Alternative): Fanout U9 Including VCC
python3 -X utf8 bga_fanout.py board.kicad_pcb \
    --component U9 \
    --nets "*" "!GND" \
    --output board_step1.kicad_pcb

### Step 2 (Alternative): Route Signals + VCC as Wide Traces
python3 -X utf8 route.py board_step1.kicad_pcb board_step2.kicad_pcb \
    --nets "*" "!GND" \
    --power-nets VCC --power-nets-widths 0.5
```

Only GND keeps its exclusion (it still gets a plane in Step 3, now with
`--nets GND --plane-layers B.Cu` only). If VCC wasn't fanned out, add
`--no-bga-zone U9` to allow router access.

## Step 7: Check for High-Speed Signal Requirements

### Length Matching (DDR, high-speed buses)

For DDR memory or other length-matched buses, detect signals that need matching:

```python
# Common DDR signal patterns
ddr_patterns = ['DQ', 'DQS', 'DQM', 'DM', 'CLK', 'CK', 'CAS', 'RAS', 'WE', 'CS', 'ODT', 'CKE']
ddr_nets = [n.name for n in pcb.nets.values()
            if n.name and any(p in n.name.upper() for p in ddr_patterns)]
```

If DDR or length-matched signals detected, add to the plan:
- `--length-match-group auto` for automatic DDR byte lane grouping
- `--length-match-tolerance 0.1` for acceptable variance (mm)
- `--time-matching` if routes span different layers (accounts for dielectric)

### Impedance-Controlled Routing

For high-speed signals with impedance requirements:
- `--impedance 50` for 50Ω single-ended (calculates width per layer from stackup)
- `--impedance 100` with `route_diff.py` for 100Ω differential

### Bus Detection

For parallel data/address buses with clustered endpoints:
- `--bus` enables automatic bus detection and parallel routing
- Routes are attracted to neighbors, creating clean parallel traces

## Step 8: Handle Special Cases

### 2-Layer Board with Dense Components

On 2-layer boards, BGA/PGA fanout may fail for some inner pins due to
insufficient routing channels. Options:
- Accept partial fanout; router will complete remaining connections
- Skip fanout entirely; direct routing often works for through-hole PGA

**Dense 2-layer boards: treat B.Cu as a real routing layer, not a plane.**
Reserving B.Cu for a GND plane (and/or pricing it 3×) turns a congested
2-layer board into single-layer routing — neo6502's human original carries
47% of its routed length on B.Cu and pours GND *around* the routes on both
sides afterwards; our plane-first chain left 25 nets open. On a dense 2-layer
board: route signals on BOTH layers at cost 1.0 (long-haul nets cross on the
back), then pour GND last (`route_planes.py` after the signal steps — the pour
flows around existing copper). Only plane-first on 2-layer boards with light
signal content.

**Important:** If you skip fanout for a BGA/PGA component but still need to connect its
internal pads, use `--no-bga-zone <component>` to disable the automatic exclusion zone
and allow the router to enter the dense pin area:

```bash
python3 route.py board.kicad_pcb \
    --nets "*" \
    --no-bga-zone U9 \
    --output board_routed.kicad_pcb
```

Without this flag, the router auto-detects BGA/PGA zones and avoids them, which would
leave internal pads unconnected if they weren't fanned out.

### Multi-Layer Boards (4+ layers)

- Use inner layers for planes (In1.Cu for GND, In2.Cu for VCC). On a board with
  light-to-moderate routing density, **roughly half the copper layers as
  planes** works — on a 4-layer board that's In1+In2 as planes, F.Cu+B.Cu for
  signals.
- **EXCEPTION — dense boards (any BGA ≥ ~100 balls, DDR/SDRAM buses, or a
  signal step that already failed >5 nets): never plane ALL inner layers.**
  Corpus triage of the worst-connectivity boards (ulx3s, butterstick,
  orangecrab, zynq_ad9364) found this the single most damaging planning error:
  solid planes on both inner layers + 3× inner costs leaves a 2-layer board
  around the BGA, and 20–50 nets ship open while the human-routed originals
  route their long-haul nets *through* inner layers (1–2 vias each, the
  "cross-under highway"). On these boards: GND plane on ONE inner layer, and
  either keep the other inner layer a plain routing layer, or make it a SPLIT
  power plane (region pours per rail — `/recommend-plane-mappings` Step 3b) and
  keep its layer cost low (≤1.5) so signals can still cross in the gaps. On 6+
  layers, plane the middle layers and keep the layers at the BGA escape depth
  routable (human butterstick: planes In3/In4, DDR3 on In2/In5).
- **Check where the BGA fanout escapes landed before finalizing the plane
  layers** — a plane on a layer full of escape stubs forces `--rip-blocker-nets`
  to shred those escapes during tap placement (each rip risks a permanent
  casualty). Pick plane layers the escapes avoid.
- More fanout options available.

**Derive `--layer-costs` from the plane plan — penalize the plane-reserved
layers (issue #185).** The 4-layer default is **all 1.0**, so the router has no
idea which inner layers are about to become planes and freely routes signals
across them. Once you've decided the plane→layer map (via
`/recommend-plane-mappings` or the `route_planes` call you're about to make),
pass `--layer-costs` to the **signal** `route.py` step (and the later reconnect
passes) that makes each plane-reserved layer expensive, so signals prefer the
signal layers and leave the inner layers clean for the pour:
```bash
# GND plane on In1.Cu, power plane on In2.Cu -> penalize In1/In2 for signals:
route.py ... --layers F.Cu In1.Cu In2.Cu B.Cu --layer-costs 1.0 3.0 3.0 1.0
```
- **~3× is the sweet spot on boards where F/B alone can carry the signals.**
  Any value ≥2× keeps signals off the planes and doesn't hurt completion; ≥5×
  just adds vias/copper for negligible further gain. Order matches `--layers`;
  keep the real signal layers (F.Cu/B.Cu) at 1.0. **On dense boards (BGA ≥
  ~100 balls / DDR buses) where an inner layer was deliberately left
  signal-routable (see the dense-board exception above), keep that layer at
  1.0–1.5** — 3× on the only spare layer starves the long-haul nets that need
  it (ulx3s failed 72 nets at 3×; its retry at 1.5 was the correct call).
- **Why it matters — it's a cascade, not just tidiness.** Signals crossing a
  plane layer fragment the pour into islands; `route_disconnected_planes` then
  carpets the layer with island-stitching tracks. Keep signals off the plane
  layers and the planes stay whole, so the repair has almost nothing to stitch.
- **Measured on castor_pollux** (4-layer, In1=GND, In2=+3.3V/+3.3VA), full chain,
  default `1.0 1.0 1.0 1.0` vs smart `1.0 3.0 3.0 1.0`, both fully connected and
  DRC-clean:

  | | default | smart 3× |
  |---|---|---|
  | total segments | 4857 | **2966 (−39%)** |
  | signal copper on plane layers | 307 mm | **44 mm (−86%)** |
  | vias | 309 | 318 (+9) |

  The 39% segment drop is the carpet disappearing because the planes stayed whole.

This is the 4-layer analogue of the 2-layer rebalance in best-practice #8 / #178:
in both cases derive the costs from how the layers will actually be used, rather
than taking the blunt default.

### Differential Pairs Present

Insert diff pair routing after fanout but before single-ended signals:

```bash
python3 route_diff.py board.kicad_pcb \
    --nets "*LVDS*" "*USB*" \
    --diff-pair-gap 0.15 \
    --layers F.Cu In1.Cu In2.Cu B.Cu \
    --output board_diff.kicad_pcb
```

**Escape layers (multi-layer boards):** like `bga_fanout.py`, `route_diff.py`
defaults to `--layers F.Cu B.Cu` only. On a 4+ layer board you MUST pass every
copper layer — when a pair was escaped by `bga_fanout.py` onto an INNER layer,
`route_diff.py` can only launch from those escaped stubs if that inner layer is
in `--layers`. Omitting it strands the inner-layer stubs and silently drops
those pairs (you'll see a low routed-pair count, e.g. 8/40 instead of 22/40 —
issue #116). Use the same copper-layer list you passed to `bga_fanout.py`; drop
`--layers` only for true 2-layer boards.

Key options:
- `--diff-pair-gap 0.1` - Gap between P and N traces (mm)
- `--no-gnd-vias` - Disable automatic GND via placement near signal vias
- `--diff-pair-intra-match` - Match P/N lengths within each pair
- `--swappable-nets "*rx*"` - Allow target swap optimization for memory lanes

### QFN/QFP Components (Perimeter Pads)

Use `qfn_fanout.py` instead of `bga_fanout.py`:

```bash
python3 qfn_fanout.py board.kicad_pcb \
    --component U1 \
    --output board_qfn.kicad_pcb
```

Creates two-segment stubs (straight + 45° fan) for each pad. On a crowded
fine-pitch edge where the surface fan has no room, add `--escape-method underpad`
(drop a through-via past each pad) and, if a boxed-in leg still drops,
`--allow-via-in-pad` so the via can sit on its own pad and stagger inward — see
"Crowded fine-pitch QFN edge" above.

Like `bga_fanout.py`, `qfn_fanout.py` ends with a `JSON_SUMMARY` carrying
`drc_grazes` (graded at `--clearance`). **Parse it after the fanout:** if
`drc_grazes.segment_segment > 0` the 45° escape stubs of two adjacent tight-pitch
pads (often a diff pair) are grazing at the wrist — re-run with a thinner
`--width` toward the fab floor until it's 0 (issue #179; see the `drc_grazes`
bullet under Step 1). All pads keep escaping (`failed` stays 0).

### Power Net Width Options

Instead of routing power separately, use `--power-nets` with signal routing:

```bash
python3 route.py board.kicad_pcb \
    --nets "*" \
    --power-nets "GND" "VCC" "+3.3V" \
    --power-nets-widths 0.5 0.4 0.4 \
    --output board_routed.kicad_pcb
```

First matching pattern determines width. Useful when not using planes.

**Size power widths for the destination pitch, not just the current.** A
0.3–0.5 mm trunk physically cannot reach interior balls of a ≤0.8 mm-pitch
BGA (at 0.5 mm pitch only one ~0.09 mm track fits between balls; at 0.8 mm a
0.25 mm trace + 0.09 clearance is a knife-edge). The power step's automatic
tap neck-down helps at the pad, but if a rail feeds MANY interior balls
(core rails like +1V1/P1.35V/VCC_1V8), a fat-track tree through the ball
field fails outright — the human originals feed such rails with zones on
every layer plus 0.09–0.2 mm necks. For those rails prefer a plane/region
(`/recommend-plane-mappings`), or set the rail's width to what the ball
field admits (e.g. 0.15–0.2) rather than the open-field ideal.

### Target Swap Optimization (Memory Routing)

For swappable signals (e.g., memory data lanes where any DQ can connect to any):

```bash
python3 route.py board.kicad_pcb \
    --nets "*DQ*" \
    --swappable-nets "*DQ*" \
    --output board_routed.kicad_pcb
```

Uses Hungarian algorithm to find optimal assignments minimizing crossings.

### Schematic Synchronization After Swaps

When routing performs polarity swaps (P↔N) or target swaps, the schematic can get
out of sync with the PCB. Use `--schematic-dir` to automatically update:

```bash
python3 route_diff.py board.kicad_pcb \
    --nets "*LVDS*" \
    --swappable-nets "*LVDS*" \
    --schematic-dir /path/to/kicad/project \
    --output board_routed.kicad_pcb
```

This updates the `.kicad_sch` files with any pad swaps made during routing.

**Shared symbols are refused, not rewritten (#489 §3).** Pin numbers live in the
file's `lib_symbols` definition, which every instance of that `lib_id` shares. When
a second component uses the same symbol — the common case for connectors, identical
channels, and multi-channel analog — the swap is **refused** for that file with a
message naming the sharers, because applying it would silently re-pin those other
components too. The units of one multi-unit part (U2A/U2B) share the definition
legitimately and are still updated. A refused swap means board and schematic
disagree on those pins: report it and tell the user to fix it by hand (or give the
component its own uniquely-named symbol) before fabricating.

**Important:** After routing with swaps, ask the user:
> "The router performed X polarity swaps and Y target swaps. Would you like to
> update the schematic to match? If so, provide the path to your KiCad project
> directory and I'll re-run with `--schematic-dir`."

Schematic sync is **disabled by default** to avoid unexpected changes. Only enable
when the user confirms they want schematic updates.

### Guide Corridors (user-drawn preferred routes)

When specific nets keep taking bad paths (or the user wants control over where a bundle
runs), the user can draw a polyline on `User.1` in KiCad and re-route those nets with:

```bash
python3 route.py board.kicad_pcb --nets "SPI*" --guide-corridor --output board_routed.kicad_pcb
```

The route follows the line as waypoints, strictly best-effort — a guide never makes a route
fail or adds vias. See `docs/configuration.md` "Guide Corridor Options" for details.

**Scope rule: do NOT draw guide corridor geometry yourself.** Suggest *in words* where a
corridor would help ("a line on User.1 south of J3, between the mounting hole and C14") and
let the user draw it; then incorporate `--guide-corridor` into the plan.

### Keepout Zones (RF / analog exclusions)

Check the board for components that warrant routing exclusions: antennas (footprint/value
keywords ANT, ANTENNA, chip antenna parts), RF modules, and sensitive analog front-ends. If
found, recommend the user draw closed polygon(s) on `User.2` around those regions and add
`--keepout` to every routing step (`route.py`, `route_diff.py`) so tracks and vias stay out
on all copper layers. Same scope rule as guide corridors: describe where the keepout should
go; the user draws it.

### MPS Layer Swap (crossing conflicts)

When MPS ordering reports crossing conflicts (nets in Round 2+), or failures show pairs of
nets repeatedly ripping each other up, add `--mps-layer-swap` to attempt layer swaps that
eliminate same-layer crossings before routing begins.

### Vertical Track Alignment

On 4+ layer boards where through-hole components need via space, `--vertical-attraction-radius`
/ `--vertical-attraction-cost` attract tracks on different layers to stack vertically,
consolidating routing corridors.

### Plane Via Placement Options (route_planes.py)

- Multiple nets can share one plane layer (Voronoi partitioning): `--nets GND VCC --plane-layers In2.Cu In2.Cu`
- `--same-net-pad-clearance <mm>` forces plane vias outside same-net pads with that edge-to-edge clearance (default places at pad center when possible)
- `--rip-blocker-nets` rips up interfering routed nets to maximize via placement and leaves them unrouted (reconnect with a route.py pass afterward — Step 5c). `--reroute-ripped-nets` is a deprecated no-op.

### Net Ordering Strategies

| Strategy | Flag | Best For |
|----------|------|----------|
| MPS (default) | `--ordering mps` | General routing, minimizes crossings |
| Inside-Out | `--ordering inside_out` | BGA escape routing |
| Original | `--ordering original` | Manual control |

### Useful Utility Scripts

| Script | Purpose |
|--------|---------|
| `list_nets.py U1` | List all nets connected to a component |
| `list_nets.py U1 --pads` | Show pad-to-net assignments |
| `check_orphan_stubs.py` | Find traces ending without connection |

### Debug and Visualization Options

When routing fails or behaves unexpectedly:

```bash
# Verbose output with diagnostic info
python3 route.py board.kicad_pcb --nets "*" --verbose --output board_debug.kicad_pcb

# Debug geometry on User layers (visible in KiCad)
python3 route.py board.kicad_pcb --nets "*" --debug-lines --output board_debug.kicad_pcb

# Real-time visualization (requires pygame-ce)
python3 route.py board.kicad_pcb --nets "*" --visualize --output board_debug.kicad_pcb

# A* search statistics
python3 route.py board.kicad_pcb --nets "*" --stats --output board_debug.kicad_pcb
```

### Post-Routing Enhancements

```bash
# Add teardrop settings to all pads (improves manufacturability)
python3 route.py board.kicad_pcb --nets "*" --add-teardrops --output board_routed.kicad_pcb
```

### Advanced Routing Parameters

For difficult boards, consider tuning these parameters:

| Parameter | Default | Effect |
|-----------|---------|--------|
| `--max-ripup 3` | 3 | Max blocking nets to rip up and retry |
| `--max-iterations 200000` | 200000 | A* iteration limit per route |
| `--heuristic-weight 1.9` | 1.9 | >1 = faster but may miss tight routes, 1.0 = optimal |
| `--via-cost 50` | 50 | Higher = fewer vias, longer paths; lower (10-25) for BGA escape |
| `--grid-step 0.1` | 0.1 | Smaller = finer routing but slower; 0.05 for fine-pitch |

Manufacturing constraints (set to match your fab's requirements):

| Parameter | Default | Description |
|-----------|---------|-------------|
| `--clearance 0.25` | 0.25 | Track-to-track clearance (mm) |
| `--board-edge-clearance 0.5` | 0 | Min distance from board edge (mm) |
| `--hole-to-hole-clearance 0.2` | 0.2 | Min drill-to-drill spacing (mm) |

### Proximity Penalties

For dense boards, use proximity penalties to spread out routes:

```bash
python3 route.py board.kicad_pcb --nets "*" \
    --stub-proximity-radius 2.0 --stub-proximity-cost 0.2 \
    --bga-proximity-radius 7.0 --bga-proximity-cost 0.2 \
    --track-proximity-distance 2.0 --track-proximity-cost 0.1 \
    --output board_routed.kicad_pcb
```

## Important Notes

0. **Net-coverage invariant (Step 5b)** - Every routable net must be claimed by exactly one stage; a net excluded from one stage (`!X`) MUST appear in a later stage's selection. Reconcile the route-exclusion set against the plane `--nets` set before routing (symmetric difference empty), and confirm `check_connected.py`'s unrouted list is empty at the end. This is the guard against a net (e.g. a secondary ground like GNDA) being silently dropped by every stage.
1. **Always check for GND connections** - If a component has GND pads but GND isn't being fanned out, the plane vias will handle it
2. **Fanout ALL non-plane nets** - Use `--nets "*" "!GND" "!VCC"` to fan out all nets except those handled by planes. Do NOT use `"/*"` alone as it misses nets with non-hierarchical names like `Net-(U9-Pad1)`. Unconnected nets are automatically filtered out.
3. **Order matters** - Fanout, then diff pairs, then signals (always excluding plane nets with `"!GND" "!VCC"` exclusions), then planes + GND return vias, then repair. Signals route first because stitching vias can relocate around tracks, but a diff pair cannot relocate around a badly placed via
4. **Verify at the end** - Always run DRC, connectivity, and orphan stub checks
5. **Consider the analyze-power-nets skill** - For complex boards where power net identification isn't obvious, use that skill first to analyze component datasheets
6. **Consider the find-high-speed-nets skill** - For accurate GND return via distance recommendations based on actual component datasheet speeds and rise times, run `/find-high-speed-nets` before planning. The lightweight inline analysis (Step 4) uses net name patterns only.
7. **Stub layer switching is on by default** - The router automatically moves stubs to eliminate vias when beneficial; disable with `--no-stub-layer-swap`
8. **Default layer costs** - 2-layer boards default to F.Cu=1.0, B.Cu=3.0 to prefer top layer; 4+ layer boards use 1.0 for all. On **dense** 2-layer boards this 3× back-side penalty can over-bias routing onto F.Cu (top channel exhausted, B.Cu empty, excess vias, stranded pads); if completion is low or the layer balance is badly skewed, **retry with more balanced `--layer-costs` (e.g. `1.0 1.5`, down toward `1.0 1.0`)** — see "Dense 2-layer boards: rebalance layer costs" under Diagnose and Retry (issue #178). On **4+ layer** boards the all-1.0 default is plane-blind: **derive `--layer-costs` from the plane→layer map and penalize the plane-reserved inner layers (~3×)** so signals stay on F.Cu/B.Cu and the planes stay whole — see "Multi-Layer Boards (4+ layers)" (issue #185).
9. **Schematic sync is disabled by default** - After routing with swaps, offer to re-run with `--schematic-dir` if the user wants to update their schematic
10. **Rip-up and reroute is automatic** - When a route fails, the router automatically rips up blocking nets and retries (up to `--max-ripup` blockers)
11. **Component shortcut** - Use `--component U1` to route all signal nets on a component (auto-excludes GND/VCC/unconnected)
12. **Use --no-bga-zone for difficult boards** - Even when fanout is complete, use `--no-bga-zone` during routing to allow the router to find alternative paths through the dense pin area. This is especially important for 2-layer boards where routing channels are limited.
13. **Windows UTF-8 encoding** - On Windows, use `python3 -X utf8` to avoid Unicode encoding errors when scripts print special characters (like Ω for resistance). Example: `python3 -X utf8 route_planes.py ...`
14. **BGA/PGA power pins and planes** - When using power planes, BGA/PGA power pins (GND, VCC) connect most efficiently via direct vias to the plane rather than fanout routing. Create planes first, then fanout only signal nets. Through-hole PGA pads automatically connect to planes on that layer; SMD BGA pads need vias placed by `route_planes.py`. This approach:
    - Reduces routing congestion (power pins don't consume escape channels)
    - Provides lower impedance power connections
15. **Aggressive parameters for 2-layer BGA/PGA boards** - Use `--max-ripup 10 --max-iterations 1000000` from the start for boards with dense components. These parameters help resolve routing conflicts that would otherwise fail.
16. **Guide corridors and keepouts are user-drawn** - Never draw `User.1` guide polylines or `User.2` keepout polygons yourself; suggest in words where they should go and let the user draw them, then add `--guide-corridor` / `--keepout` to the plan.
17. **Companion skills** - Defer to `/identify-diff-pairs` (datasheet-based pair detection), `/recommend-stackup` (before impedance/time-matching work), `/diagnose-routing-failures` (after failures), and `/review-routed-board` (final verification) rather than duplicating their logic inline.

## Presenting the Plan

After generating the plan:
1. Show the board summary
2. Explain any special components found
3. List differential pairs if detected
4. Highlight any length-matching or impedance requirements
5. Present each step with the command AND a brief explanation of why
6. Ask the user if they want to proceed or modify the plan
7. Offer to run the commands if approved

## After Routing Completes

### Capture Logs for Analysis

Always capture command output to `/tmp` files for later analysis:

```bash
python3 -X utf8 route.py input.kicad_pcb output.kicad_pcb --nets "*" 2>&1 | tee /tmp/route_output.txt
python3 -X utf8 route_planes.py input.kicad_pcb output.kicad_pcb --nets GND --plane-layers B.Cu 2>&1 | tee /tmp/planes_output.txt
python3 -X utf8 check_connected.py output.kicad_pcb 2>&1 | tee /tmp/connectivity.txt
python3 -X utf8 check_drc.py output.kicad_pcb --clearance <floor> --hole-to-hole-clearance <floor> 2>&1 | tee /tmp/drc.txt
```

(`<floor>` = the manufacturing floor from `list_nets.py --design-rules`, not the
0.2 default — grade DRC at the rule the board's own Constraints + fab capability
define, per #111/#115.)

### Parse Logs for Failure Analysis

After routing, parse the log files to understand failures:

```bash
# Check routing summary (last 20 lines usually have the summary)
tail -20 /tmp/route_output.txt

# Look for failed nets
grep -i "failed\|FAILED" /tmp/route_output.txt

# Check JSON summary for detailed failure info
grep "JSON_SUMMARY" /tmp/route_output.txt | sed 's/JSON_SUMMARY: //' | python -m json.tool

# Find specific failure reasons
grep -A5 "FAILED NET HISTORIES" /tmp/route_output.txt
```

The JSON_SUMMARY line contains structured data including:
- `failed_single`: List of failed single-ended net names
- `failed_multipoint`: List of nets with unconnected pads (includes pad coordinates)
- `blockers`: Per still-failed net, which routed nets wall it off (`blocked_by` with cell counts; #409)
- `pad_pairs_connected`/`pad_pairs_total` + `pad_pairs_open`: Pad-pair routability tallies (PRR = connected/total) and per-open-net outcome — route-time failures are opens; shorts are DRC's domain (#409 follow-up)
- `multipoint_pads_connected` vs `multipoint_pads_total`: Connection success rate

### Tune mode (issue #153) — opt-in per-board feedback loop

When the user asks for **tune** (e.g. "plan routing with tune", "tune mode"),
don't just run the standard pipeline once with defaults: close the loop.
After EACH step, read the step's own diagnostics and adjust that board's
options before moving on. Off unless requested — the standard plan stays
deterministic and fast.

Rules of the loop:
- **Bounded, guided adjustment — not a grid sweep.** At most 2–3 targeted
  re-runs per step, each driven by a diagnosed failure mode (the symptom→knob
  table below and the failure-pattern table in Diagnose and Retry). Never
  loosen below the fab/board-constraint floor.
- **Signals to read after each step:** the `JSON_SUMMARY` line (failed nets,
  `rescue` block, `single_ended_diff_pairs`/`failed_diff_pairs`,
  `drc_grazes`), the FAILED NET HISTORIES block (`preexisting_blockers`
  hints, `no rippable blockers`, iteration exhaustion), fanout escape
  tallies (unescaped balls), and plane-step tap/`ripped`/`STILL FLOATING`
  reports. `/diagnose-routing-failures` automates most of this.
- **Symptom → knob map** (beyond the Diagnose and Retry table):
  - Fanout drops balls in one quadrant → re-run that fanout with
    `--escape-method underpad`, a smaller via from the fab ladder
    (0.30/0.15 → 0.25/0.15), or different `--primary-escape` direction.
  - Signal step fails a cluster of long cross-board nets while an inner
    layer is plane-reserved → revisit the plane→layer map (dense-board
    exception above): free one inner layer, drop its `--layer-costs` entry
    to 1.0–1.5, re-run the failed nets.
  - `preexisting_blockers` hints repeat for the same nets → re-run those
    nets with the hinted `--rip-existing-nets` set (the engine now
    self-escalates once in reconciliation; a manual retry may widen the set).
  - Power multipoint pads fail inside a BGA courtyard → shrink that rail's
    `--power-nets-widths` entry toward the ball-field width (0.15–0.2) or
    promote the rail to a plane/region and re-run.
  - Diff pairs deferred single-ended → re-run the pairs with smaller
    `--diff-pair-gap`/width/vias toward the fab floor (keep `--impedance`).
  - Plane step ships tap failures with fill nearby → re-run
    route_disconnected_planes with a larger `--max-search-radius`, or at the
    advanced fab tier so smaller tap vias fit.
  - A handful of nets fail on a NOT-saturated board (few failed nets, short
    detours available, failures share a corridor with early-routed nets) →
    try a **failed-first split**: re-run the step as two invocations, first
    `--nets <the failed nets>` on the clean input, then everything else to a
    fresh output. Ordering is the cheapest knob but rarely decisive:
    measured on castor / butterstick / ddr5 / glasgow, an automatic
    failed-first restart NEVER beat the normal order (twice it graded
    worse), so an in-engine restart was tried and removed — only reach for
    this manually when the failure histories actually show corridor
    competition, and expect it to matter on few boards.
- **Explainability:** keep a short tuning log per board — which knob changed,
  the before/after metric (completion / DRC / coupled pairs), and whether it
  helped. Revert a change that didn't help before trying the next.
- **Honest gates:** grade every accepted retry with `check_connected` AND
  `check_drc` at the routed clearance (plus the kicad oracle for final
  boards) — never accept a retry that trades new DRC for completion.

### Step 9: converge — score the board, pick a lever, repeat

**Full procedure, worked example and ledger schema:
[`references/convergence.md`](references/convergence.md).** The summary below is
the part you must not get wrong.

**A chain that ran is not a board that is done.** The failure this step exists to
prevent is concrete: a board went out at **39 of 44 nets connected, 762 DRC
errors, and 141 of 141 vias below its own spec**, and every tool in the chain
reported success. Nothing looped back, because nothing had measured the board.

#### 9.1 — Score it. The router's opinion is not evidence.

```bash
python3 -X utf8 .claude/skills/plan-pcb-routing/scripts/board_score.py \
    board.kicad_pcb --intent floorplan.json \
    --min-track-width 0.15 --min-via-diameter 0.6 --min-via-drill 0.3 \
    --net-min-widths wk/net_min_widths.json \
    --impedance-nets '<every net with a reference-plane clause>' \
    --length-groups '<every length-matched group>' \
    --json wk/score_iter3.json
```

**Every one of those flags is what makes its clause reach `blocking`. A component
with no flag reports `ungraded`, which is not a pass.** The pattern is identical
each time, and it is how a HARD clause ships unmeasured:

| flag | without it | measured worth on one board |
|---|---|---|
| `--net-min-widths` | `undersized` sees only BOARD-WIDE floors, so a clause naming ONE net — a 0.8 mm pair, a 0.4 mm rail — is invisible | `net_widths` 5, while `undersized` read 0 |
| `--impedance-nets` | the component returns *"no --impedance-nets given"* and a plane-continuity clause is never checked at all | `impedance` 10 — 68 reference crossings, 63 segments over void |
| `--length-groups` | length matching is ungraded | — |

Same board, same copper: **`blocking` 12 without those flags, 27 with them.** A
run that reports 12 has not found a better board; it has looked at less of it.

Also **read `net_widths.patterns_matching_no_routed_net`.** A width clause on a
net with NO copper never appears in `net_widths` — the component only walks nets
that HAVE segments — so an unrouted net's width requirement lands in that list
and nowhere else.

**And `blocking == 0` is not the whole gate when the repo ships its own spec
checker.** Some clauses are not expressible to `board_score` at all: an absolute
maximum length, a symmetry match between two *series chains* through a resistor,
a via ban per leg. If the repo has a `check_spec.py` (or equivalent), run it
**beside** `board_score.py` every iteration, treat a HARD failure as blocking even
when `blocking` reads 0, and wire it into `place_route_loop --accept-cmd` so the
inner loop stops accepting rounds that break it.

**Produce:** the command above, every iteration, on the board you just wrote.
**Read:** `blocking`, `blocking_by`, `ungraded`, `unknown`, `quality`.
**Decide:** `blocking == 0` → go to 9.4. Otherwise pick the lever by **9.1a**,
NOT by the largest `blocking_by` entry.

##### 9.1a — CONNECTIVITY FIRST. The largest number is not the lever.

The obvious rule — *"the biggest `blocking_by` entry names the lever"* — is wrong,
and it wrecked a run. On a board with 5 nets carrying **no copper at all**, the
biggest entry was `drc: 18`, of which **16 were grading artifacts**. The loop spent
eleven iterations on clearances while five nets sat dead, and the board could not
have booted.

**Work the components in this fixed order, regardless of size:**

| order | component | why it outranks the rest |
|---|---|---|
| 1 | `unrouted` | a net with no copper is a dead wire. Nothing else matters while one exists. **Run `converge.py where BOARD --nets <names>` before touching a parameter** — it names the gap endpoints and the foreign copper walling them in, per layer, nearest-first (9.1b-ii). Guessing from the score is how eleven iterations went to clearances while five nets sat dead |
| 2 | `broken` | a net in N pieces is N−1 dead wires. Same tool, same reason |
| 3 | `net_widths`, `undersized` | real copper, wrong size — fixable by re-routing what is already there |
| 4 | `floorplan` | placement or intent |
| 5 | `drc` | **last, and only after auditing it — see below** |

`unrouted` and `broken` are the **ratsnest**: they count connections the board is
supposed to have and does not. They are never artifacts, never a grading choice,
and they map one-to-one onto whether the thing works. Drive the loop on them.

##### 9.1b — Audit `drc` before you believe it

`check_drc` grades the whole board at **one clearance**. A board with more than one
net class therefore reports violations that are purely a grading choice. The
signature is unmistakable — **many violations, all the same net pair, all the same
overlap**:

```
SEGMENT-SEGMENT  USB_DM <-> USB_DP   Overlap: 0.010mm     x15
PAD-SEGMENT      USB_DP  <-> USB_DM   Overlap: 0.010mm     x1
```

0.010 mm is exactly `0.16 − 0.15`: the Default class grading a pair whose own class
permits 0.15. Re-grade at the tighter class and they vanish:

```bash
check_drc.py board.kicad_pcb            # 18 violations
check_drc.py board.kicad_pcb -c 0.15    #  2 violations
```

**Before letting `drc` drive anything:** group the violations by (type, net pair,
overlap). Any group that is large, uniform, and sits within a µm of a
class-clearance difference is an artifact of the grading scalar, not a defect.
Quote both numbers in the report and say which classes each applies to. Never pick
the flattering one silently.

##### 9.1b-ii — Tools that already answer this, which nothing in a chain calls

A whole convergence went by hand-rolling worse versions of four of these. Before
writing a script to answer a question, check whether one of them already does.

| you want | run | why it beats the obvious thing |
|---|---|---|
| where is the gap, and what is walling it in | `net_forensics.py --nets N --radius 1.0` | per net: the connected ISLANDS, the exact unclosed gap endpoints, and an inventory of the foreign copper around each gap — **named, per layer, nearest-first**. Better than a ratsnest, which tells you two pads are unjoined and nothing about why |
| the honest unconnected count | `kicad_unconnected.py board --items` | KiCad's own DRC, and it **refills the zones itself** — which is 9.1c's whole problem, already solved |
| what kind of failure is this | `converge.py where` / the router's own hint | the hint names the flag and the nets (9.3b); it diagnoses better than the score does |
| where should this part go, facing which way | `converge.py poses BOARD --ref R` | ranks legal (x, y, rotation) poses by placement cost in **milliseconds**, with a per-component breakdown, and `--route` pays for tier 3 on only the top few |
| is this even the engine I pinned | `route.py --capabilities` / `krt_capabilities.py --require` | a chain can otherwise run green against a clone missing the module it depends on |
| step back to iteration N | `converge.py step-back --iteration N` | byte-exact, because the board is addressed by content instead of by a path three iterations overwrote |
| re-run what iteration N did | `converge.py replay --iteration N` | replays the recorded argv. If it refuses, the ledger recorded prose instead of a command — fix the ledger, not the memory |

##### 9.1c — The authoritative ratsnest needs the zones FILLED

`route_planes` writes a zone **outline** with no `filled_polygon`. Until something
fills it, every KiCad-side check reads the pour as empty. Measured on one board,
same file, fill the only difference:

```
unfilled -> kicad-cli pcb drc:  48 unconnected items
filled   -> kicad-cli pcb drc:  15 unconnected items   == what check_connected says
```

So: **fill before you grade, and then the two agree.** If `check_connected` and
`kicad-cli` disagree by a lot on a board with a pour, the fill is the first
suspect — not the checker.

**Use `kicad_unconnected.py`, which refills for you** — it exists precisely for
this, and a hand-rolled fill has a trap the tool does not:

```bash
python3 -X utf8 kicad_unconnected.py board.kicad_pcb --items
```

If you must fill in place (to hand a filled board to something else), note that
`pcbnew.LoadBoard(...).Save()` **rewrites the sibling `.kicad_pro` and deletes
every non-Default net class**, leaving the netclass patterns orphaned. A board has
shipped that way. Restore the project afterwards and assert the classes are back
rather than trusting a success message.

**And re-assert the net classes afterwards.** `pcbnew.LoadBoard(...).Save()`
rewrites the sibling `.kicad_pro` and **deletes every non-Default net class**,
leaving the `netclass_patterns` orphaned. A board has shipped that way. Restore the
project after the fill, and assert the classes exist rather than trusting a
success message.

Three rules about that number:

- **`blocking` must reach 0 before a board is deliverable.** It is
  `unrouted + broken + drc + undersized + floorplan + impedance + length`.
  `quality` (vias, copper length) is a **tie-break only**, compared once
  `blocking` is 0 — otherwise a router buys off a disconnected net with a lower
  via count.
- **Pass the spec's size floors when the spec is tighter than the fab.**
  `check_drc` defaults to the fab minimum for the layer count. That is why 141
  vias at 0.25 mm graded clean against a 0.6 mm spec. If the spec gives numbers,
  pass them.
- **`ungraded` is not `passed`.** A component with no `--intent`, no
  `--impedance-nets`, no `--length-groups` is *unexamined*. Say so in the report;
  never let it read as clean.

**`place_route_loop`'s own `ACCEPTED` / `REJECTED` is NOT a quality verdict.**
`better()` (`place_route_loop.py:358`) compares `failures` and `iterations`, both
from route.py's own `JSON_SUMMARY`; it never runs a checker. Treat it as a cheap
pre-filter and **re-score with `board_score.py` before believing it.**

**It is also spec-blind, and `--accept-cmd` is the fix.** `better()` compares
failures then iterations; nothing in a route summary tells it a net exceeded a
maximum length, took a via where none is allowed, came out under a required
width, or drifted a decap past a proximity limit. On a board with a real spec
those are what decide whether a placement improved, so the loop will accept a
round that broke one and print ACCEPTED. Pass
`--accept-cmd 'CMD'` and the loop asks your judge instead:
`CMD <placed> <routed> <route.json>` printing one line `SCORE=<float>`, lower
better; a non-zero exit or a missing SCORE rejects the round.

#### 9.2 — Budget: 100 iterations per board, and they are cheap if you spend them right

**The budget is 100 per board.** Not 20 — 20 was set when every iteration meant a
full chain re-run, and that assumption is wrong (9.3a). A scoped retry takes
seconds, so a hundred of them is an afternoon, not a week.

**Count two kinds separately, and say which you are spending:**

| kind | what it does | example |
|---|---|---|
| **completion** | changes the copper: routes a net, heals a separation, fixes a width | `route.py --nets QSPI_SD1 ... --rip-existing-nets ...` |
| **systemic** | changes how the chain routes, measures or grades — no net gets connected by it | pinning the fab floor, restoring net classes, filling zones, fixing a checker |

Systemic iterations are necessary and they are not progress. A run once spent
**nine of eleven** on them, moved `blocking` every time, and finished with five
nets carrying no copper. **If three consecutive iterations are systemic, stop and
ask what is actually unconnected** — you are tuning the instrument, not the board.

Record `"kind": "completion" | "systemic"` in every ledger entry. The final report
states both counts.

```bash
python3 -X utf8 route.py board.kicad_pcb --list-groups --group-by auto
```

**The per-group budget needs groups that are separately convergeable — not just
groups that exist.** Test each candidate against all three:

1. its parts occupy a **distinct region** (not interleaved with other blocks),
2. its nets are mostly **internal** (`--list-groups` prints touching/internal),
3. routing it can **succeed or fail on its own**, without the others' copper.

Fail any of them and it is a *label*, not a convergence unit: take the per-board
budget and say so. A board of functional modules sharing one congested centre is
the common case — iterating per module there routes a fraction and reports
success on that fraction, which is the same defect the `route.py --group` rule
warns about.

**`kicad` groups exist on 0 of 27 boards *in this repo's corpus*** — that figure
is about KRT's own test boards, not about boards in general. A generated board
(e.g. Zener `.zen`) carries one `kicad:` group **per module**, so the naive
reading of "groups exist → per-group" authorised **8 × 20 = 160 iterations** on a
42-part board whose modules all fight over the same 21 mm of width. Take the
per-board budget there.

**Do not invent groups to iterate over** — a `sheet` block of 16–83 parts moved
on no board tried, so iterating per sheet-block burns the budget on a lever that
does not move.

#### 9.3 — Cheapest lever first, and revert what did not help

##### 9.3a — RE-ENTER AT THE FAILING STEP. Do not re-run the chain.

The single most expensive mistake available here. A full chain run is 3–5 minutes;
re-routing three nets from the board that failed them is **seconds**. The ledger
already records `parent_board` per iteration precisely so you can go back to it.

```bash
# NOT: bash chain.sh          (re-seeds, re-places, re-routes everything)
# THIS:
route.py wk/r4.kicad_pcb wk/i15.kicad_pcb --nets QSPI_SD1 ...
```

Re-run the chain only when a **placement** changed (which invalidates every routed
board downstream) or when you are producing the final artifact. Everything else is
a scoped retry on the board that already failed.

##### 9.3b — READ THE ROUTER'S HINT. It names the flag and the nets.

When `route.py` fails a net it prints the fix, and it is usually right:

```
ROUTE FAILED - no rippable blockers found
  Hint: the blocking copper belongs to pre-existing net(s) 'QSPI_SD2' 'QSPI_SS'
  'VCC3V3' (committed by an earlier run/step), which this run is not allowed to
  rip. Retry with --rip-existing-nets 'QSPI_SD2' 'QSPI_SS' 'VCC3V3' ...
```
```
  Hint: the start/target pads are boxed in by static obstacles ... try
  --grid-step 0.025 --clearance 0.15 --track-width 0.15
```

On one board these two hints, applied, took `unrouted` from 5 to 0. The router
diagnoses better than the score does — the score said `drc`, the router said
"rip these four nets", and the router was right.

##### 9.3b-ii — Carry `--fab-overrides` on EVERY retry when the spec floor is tighter

A scoped retry is a fresh `route.py` call, and it resolves its floor from the fab
tier unless told otherwise. Two things then happen quietly: the **per-net rescue
re-routes a failed net AT the tier floor**, and the `standard`→`advanced` tier
escalation is allowed, which is what puts sub-spec vias on a board that asked for
big ones. Both report the net routed.

So every route call in the loop — not only the first one — carries
`--fab-overrides <the spec file>` when the spec is tighter than the tier, plus
`--track-width-floor` for a width clause. Measured, one such file took a board's
`undersized` from **169 to 0**. Check `min_clearance_used` in the `JSON_SUMMARY`
afterwards: it is the only place a floor that was silently loosened shows up.

##### 9.3c — Ripping blocking nets IS a sanctioned lever

`--rip-existing-nets` rips named nets, re-routes them in the same run, and reports
honestly if one cannot be. It is often the **only** way past copper an earlier step
committed. Use it — with four rules, each of which cost a wasted iteration to
learn:

1. **Scope the rip.** Start with the set the hint names, then bisect if you want a
   minimal one. Do not reach for `'*'`.
2. **A ripped net returns at the CALLING command's parameters, not the ones it was
   originally routed with.** Ripping a 0.8 mm USB net from a plain signal call
   brings it back at 0.16 mm and silently destroys the spec geometry. **Whenever
   the rip set contains a width-bearing net, pass its `--power-nets` /
   `--power-nets-widths` (or `--impedance`) in the same call.**
3. **One net per call.** Routing two nets together let the second rip the first —
   reported as `1/2 routed` twice running, a different net each time. Sequential
   single-net calls connected both.
4. **A glob does not override a lock.** `--rip-existing-nets 'QSPI_*'` silently
   skips a locked or protected net (#521) while the router keeps asking for that
   exact rip. Name it EXACTLY, and if it is KiCad-locked, nothing overrides that —
   unlock it or route around it.

For plane-net pads that cannot reach their pour, the equivalent is
`route_disconnected_planes --rip-blocker-nets` (it leaves the ripped nets unrouted
for the Step 5c pass — never re-route them in-step, #141). Budget for it: on a
dense board it can run **20× longer** than the plain repair, so start it early
rather than discovering the cost at the end.

##### 9.3d — Classify the blocker, then pick

Never spend a full-chain iteration on something a parameter fixes. Classify the
top blocker on the exact keys, not on impressions:

| evidence | verdict | where to go |
|---|---|---|
| failures cluster into ≤2 pockets (`--focus` panels), their refs share one block, `blockers` non-empty | **floorplan** | back to **Step 0e** — re-zone. A 3 mm nudge cannot move a block 80 mm |
| failures scattered, `blockers` non-empty, every failing ref is a ≤40-pin passive | **placement detail** | back to **Step 0c**, `place_route_loop` with the caps above |
| `blockers` empty; the log says boxed in by static obstacles | **parameters** | stay here — grid, ripup budget, width. Placement is not the lever |
| 2-layer board, heavy F.Cu skew, via count far above a hand layout | **parameters** | layer-cost rebalance, below |
| `oob_count` or `overlap_area` rose after the last placement | **the placement is illegal** | discard it; do not route it |
| `check_floorplan` exits 4 with `zone_containment` | **intent violated** | fix the placement to match, or say why the intent changed. Do not quietly rewrite the intent to match the board |
| a whole net has no copper while `pad_pairs_connected` looks healthy | **coverage bug** | the Step 5b ledger — not a placement problem at all |
| `undersized` non-zero | **parameters** | re-route at the spec's width/via. Placement is not the lever |
| `unrouted` names a plane net | **the pour step** | it was excluded and never poured — Step 3/5, not placement |
| the log names **pre-existing nets** it is "not allowed to rip" | **rip lever** | 9.3c — `--rip-existing-nets` with the set it named |
| a net fails on ONE layer at every grid and rip set, and routes instantly with a second layer | **the single-layer constraint is the blocker** | not a router failure. Report it against the requirement that imposed the layer restriction, with both measurements |
| `drc` is large, uniform, one net pair, one overlap value | **grading artifact** | 9.1b — re-grade at the right class. Not a lever at all |
| `broken` is mostly plane-net pads | **the pour could not reach them** | `route_disconnected_planes --rip-blocker-nets`, then the Step 5c reconnect |
| `check_connected` and `kicad-cli` disagree badly | **the zones are unfilled** | 9.1c — fill, then re-read. Do not "average" them |

**Accept an iteration only if `blocking` strictly decreased**, or `blocking` is
unchanged and `quality` improved. Otherwise **revert to the parent board** and
take the next lever. An iteration that made it worse is not a starting point.

**One exception, and state it when you use it:** an iteration that reduces
`unrouted` or `broken` while raising a lower-ranked component may be accepted even
if `blocking` is level, because 9.1a ranks connectivity above the rest. Say so in
the ledger with both numbers. A dead net is worse than a wide trace, and the scalar
does not know that.

**Watch for whack-a-mole.** Ripping to route net A can leave net B unrouted, and
the tally still reads "1 failed" — a *different* net. Compare the failing net
**names** between iterations, never just the counts. If they alternate, route them
one per call with the other explicitly out of the rip set (9.3c rule 3).

**After ANY placement change every downstream routed board is stale** — re-run
the chain from the placed board. Never keep a routed artifact from before it.

#### 9.4 — Write the ledger, every iteration, before the next one starts

`wk/ledger.jsonl` in the work dir. It is what makes the run resumable, lets the
final report name which stop condition fired, and gives the film its frames.

**Write it with `converge.py record`, and read `converge.py status` back every
iteration.** The verbs that make a ledger worth keeping — `step-back` (byte-exact,
because the board is stored by content hash), `replay` (re-runs the recorded argv),
`status` (the systemic/completion split) and `make_film.py --from-ledger` — all
read append-only **JSONL** through `board_store.Ledger`. A hand-written single JSON
document is readable by a person and by nothing else, so every one of them is
unreachable from it.

```bash
python3 -X utf8 converge.py record --ledger wk/ledger.jsonl \
    --board wk/iter03.kicad_pcb --kind completion \
    --lever 'rip lever: --rip-existing-nets QSPI_SD2 + --grid-step 0.025' \
    --score "$(cat wk/score_iter03.json)" \
    --argv python3 -X utf8 route.py wk/iter02.kicad_pcb wk/iter03.kicad_pcb --nets QSPI_SD1 ...

python3 -X utf8 converge.py status --ledger wk/ledger.jsonl      # EVERY iteration
```

`status` is the alarm for 9.2's failure mode: it splits the budget into completion
vs systemic and warns when at least half went to the instrument. Nothing else in
the loop says that out loud, and the run that needed to hear it did not.

A record holds this; `record` writes it for you:

```jsonc
{"iteration": 3, "group": null, "parent_board": "wk/iter02.kicad_pcb",
 "kind": "completion",                       // or "systemic" -- see 9.2
 "lever": "rip lever: --rip-existing-nets QSPI_SD2 QSPI_SS + --grid-step 0.025",
 "commands": ["python3 -X utf8 route.py ..."],
 "score": {"blocking": 12, "blocking_by": {"unrouted": 1, "drc": 11}},
 "unrouted_nets": ["QSPI_SD1"],              // NAMES, not just the count
 "verdicts": ["VERDICT=FAIL:lens=drc;..."],
 "accepted": true, "reverted_to": null}
```

**`parent_board` is the board this iteration actually came from** — the last
*accepted* one, not iteration N−1. It is what `render_placement --before` takes;
using N−1 renders a delta that never existed. **Never reuse an output path across
iterations**: a ledger that says `wk/placed.kicad_pcb` when three iterations wrote
that name is unauditable, and one that named a *rejected* board as the parent of
everything downstream got shipped.

**Record `unrouted_nets` by NAME.** Counts hide whack-a-mole — "1 failed" twice
running can be two different nets, each ripped by the fix for the other (9.3d).

**Log the systemic/completion split in the final report**: *"41 iterations: 9
systemic, 32 completion"* is a fact about how the budget was spent, and a run that
cannot state it was not keeping a ledger.

#### 9.5 — Stop conditions. Only these four. Say which one fired, every time.

1. **`blocking == 0`, the repo's own spec checker passes, and every verifier lens
   passes** → done. All three are required. `board_score` exits **0** at
   `blocking == 0` even on a board with ten HARD clauses violated, because the
   clauses a repo checker measures are not `board_score` components — so
   exit-code-driven automation stops there unless you gate on the checker too.
   See "Verify with independent subagents".
2. **Budget exhausted** — you have actually written **100** ledger entries for this
   board. Report the best-scoring board **and the remaining blockers itemised with
   measurements**. Do not present it as finished.
3. **Five consecutive iterations with `unrouted` and `broken` both unchanged,
   after trying the rip lever, a finer grid, and a layer change on the failing
   nets** → floorplan-limited or spec-limited. Say which, with the number. (Five,
   and on the connectivity components — three iterations of `drc` not moving means
   nothing when the real blocker is a dead net.)
4. **A blocker is geometrically unsatisfiable** → stop and report it as a
   **finding about the requirement**, with the measurements that prove it. Worked
   example: a 2.4 mm clearance requirement written as a netclass also applies
   pad-to-pad, and on that connector the measured pad gaps were 0.500 mm (VBUS)
   and 1.300 mm (GND) — 23/44 nets with it, 38/44 without. That is unsatisfiable
   *as written*, and it took one measurement to prove. **Do not silently relax
   it, and do not grind iterations against it.**

##### These are NOT stop conditions

Stopping for any of these is a process failure, not an outcome. If one of them is
pulling at you, write the next ledger entry instead:

- **"This is taking a long time."** Wall-clock is not a stop condition. A run once
  stopped at 11 of 20, labelled it "budget exhausted", and recorded in its own
  ledger that the levers were *not* exhausted. Eleven is not twenty.
- **"The score stopped moving."** Check *which* component. `blocking` level while
  `unrouted` falls is progress (9.3d's exception).
- **"The remaining work is hard."** Hard is what the budget is for. A net that
  needs a scoped rip, a finer grid and a layer change is three cheap iterations,
  not a wall.
- **"I have written up the findings."** The report is not the deliverable while
  nets are unrouted. Finish the board, then write.
- **"The last lever failed."** Revert and take the next one. The ladder has more
  rungs than you have tried: rip set → grid → layer → via cost → width → order →
  placement.

**Before invoking condition 2 or 3, answer in writing:** how many nets are
unrouted, what is the router's own hint for each, and which of the 9.3c rip rules
has not been tried on them? If any of those is unanswered, the loop is not done.

Ending on 2, 3 or 4 is a legitimate outcome. Ending on any of them **while
calling the board finished is not**, and ending on none of them is not an ending.

### Diagnose and Retry

After running routing commands:
1. Report how many nets were routed successfully
2. **If routes failed**, invoke `/diagnose-routing-failures <board> <log files>` — it parses
   the JSON summary, failed-net histories, and blocking reports, correlates failures
   spatially, and outputs a targeted retry command. Apply its recommendation. If that skill
   is unavailable, fall back to this table:

| Failure Pattern | Likely Cause | Solution |
|-----------------|--------------|----------|
| "no rippable blockers found" | Route blocked by non-rippable obstacle | Use `--no-bga-zone`; if pads are "boxed in by static obstacles", shrink geometry / finer grid (see "Congestion escalation" below) |
| "Re-route FAILED: no path found" | Ripped net couldn't find new path | Increase `--max-iterations` |
| Many multipoint pads failed on same component | Congested area | Use `--max-ripup 10` or higher; shrink geometry toward the fab floor (see below) |
| Many failures cluster in one channel/region | Tracks too fat for the channel | **Congestion escalation**: re-route the failed nets at smaller track/via/clearance down to the fab floor (see below) |
| 2-layer board: low completion, via count far above a hand layout, or copper badly skewed to F.Cu while B.Cu sits empty | Default B.Cu cost (3.0×) over-penalizes the back layer | Retry with balanced `--layer-costs 1.0 1.5` (down toward `1.0 1.0`) — see "Dense 2-layer boards: rebalance layer costs" below |
| Routes near BGA boundary failing | BGA exclusion zone too aggressive | Use `--no-bga-zone` |

```bash
python3 -X utf8 route.py board_prev.kicad_pcb board_routed.kicad_pcb \
    --nets "*" \
    --no-bga-zone \
    --max-ripup 10 \
    --max-iterations 1000000 \
    2>&1 | tee /tmp/route_retry.txt
```

   Key parameters for difficult boards (especially 2-layer with BGA/PGA):
   - `--no-bga-zone` - **Critical**: Allows router to enter BGA area for alternative paths
   - `--max-ripup 10` (default 3) - More rip-up attempts to resolve conflicts
   - `--max-iterations 1000000` (default 200000) - 5x more search iterations
   - `--stub-proximity-radius 10 --stub-proximity-cost 3.0` - Spread out fanout stubs (optional, for aesthetics)

#### Dense 2-layer boards: rebalance layer costs (issue #178)

On 2-layer boards the router defaults to per-layer costs **F.Cu=1.0, B.Cu=3.0**
(best practice #8) to keep most signal copper on top. But with a GND/power plane
already filling B.Cu, that 3× back-side penalty can over-bias routing onto F.Cu:
the top channel fills up while B.Cu sits nearly empty, the router takes long F.Cu
detours that then need a via to reach a B.Cu pad, and on congested boards the
exhausted F.Cu channel strands pads that B.Cu could have carried. This is the
dominant route-quality gap on tight 2-layer keyboard/peripheral boards.

**When to suspect it** (check the route `JSON_SUMMARY` / `comparison` block, or
measure per-layer copper length and via count against a reference):
- Strong F.Cu skew — e.g. >80% of signal copper on F.Cu while B.Cu is sparse.
- Via count far above a hand layout (the F.Cu-detour-then-via pattern).
- Low completion with failed pads clustered where F.Cu is full but B.Cu is free.

**Retry with more balanced layer costs** so the router crosses to B.Cu for short
diagonal runs instead of detouring on F.Cu (order matches `--layers`: F.Cu first,
B.Cu second):
```bash
python3 -X utf8 route.py board_fanout.kicad_pcb board_signal.kicad_pcb \
    --nets "*" "!GND" "!VCC" \
    --track-width 0.127 --clearance 0.1 \
    --layer-costs 1.0 1.5 \
    --no-bga-zone --max-ripup 10 --max-iterations 1000000 \
    2>&1 | tee /tmp/route_balanced.txt
```
Start around **`1.0 1.5`** (down from the `1.0 3.0` default); if F.Cu is still
saturated, step to **`1.0 1.2`** or fully balanced **`1.0 1.0`** (fine when a
plane fills B.Cu — signals carve the pour and it reflows around them). This is
**complementary to**, not a replacement for, routing at the fab floor (below): a
balanced layer that's still too fat won't fit the channel either, so keep
`--track-width` thin. Re-route the **whole** signal step, not just the failures (a
victim is blocked by the successful F.Cu tracks already in its channel). Then
compare completion, via count, and F.Cu:B.Cu balance, and keep whichever connects
more pads with fewer vias.

Measured at `--track-width 0.127` (B/F = B.Cu:F.Cu copper-length ratio; both
boards stay 100% connected at every setting — the win is via count and balance):

| board | default `1.0 3.0` | `1.0 1.5` |
|-------|-------------------|-----------|
| urchin  | B/F 0.17, 177 vias | **B/F 1.01, 98 vias** |
| piantor | B/F 0.19, 102 vias | **B/F 1.85, 59 vias** |

`1.0 1.5` roughly **halves the via count** and pulls the layer balance from a
~6:1 F.Cu skew to near parity (the human urchin layout sits around B/F 0.89).
`1.0 1.0` lands in the same neighbourhood — pick the one with fewer vias.

#### Route signals at the FAB floor by default (thin is faster AND more complete)

**`track_width` and `via_diameter` are NOT DRC floors** (Step 4), and — this is
the subtlety — **the fab floor is NOT the board's `min_track_width` constraint
either.** Three different numbers get confused here; keep them straight:

- **Board `min_track_width`** (from `.kicad_pro`, e.g. ottercast = 0.2 mm) — the
  author's self-imposed DRC rule. Often conservative. Note `list_nets
  --design-rules` reports its "manufacturing floor" track as `max(this, JLC min)`,
  so it currently **clamps the track floor to this constraint** (0.2) and does NOT
  surface the finer fab capability — do not treat that printed track number as the
  real floor (it's right for clearance/via, just not for track).
- **Fab physical track minimum** (JLC ≈ **0.0889 mm / 3.5 mil** standard; **0.127
  mm / 5 mil** is the safe no-extra-cost width) — the actual floor. **This is the
  target.** It can be *below* the board's `min_track_width`: the human ottercast
  board routes most signals at 0.127 mm, under its own 0.2 mm constraint, which is
  exactly why it fits channels our 0.2 mm net-class tracks can't.

For ordinary signals there is **no benefit to routing fat** and a real cost.
Measured on ottercast_audio (signal pass, same clearance/grid, width only):

| Signal track width | Multipoint nets routed | Pads connected | Time |
|--------------------|------------------------|----------------|------|
| **0.127 (5 mil)**  | **122**                | **360/376**    | **2.69 s** |
| 0.15               | 118                    | 354/376        | 2.93 s |
| 0.20 (net-class)   | 103                    | 323/376        | 6.52 s |

Thinner is **monotonically better on both axes** — more nets complete *and* it
finishes faster (fat tracks cause ripup churn). So don't route fat and escalate;
**route the signal step at the fab floor from the start, and if still congested
go DOWN toward the fab physical minimum** (0.2 → 0.127 → 0.0889), not toward the
board's conservative `min_track_width`. There is no "knee" above the fab floor to
hunt for.

1. **Take the fab floor**, not the board constraint: the fab's physical track
   minimum (JLC 0.0889 mm / 3.5 mil; use 0.127 mm / 5 mil for a zero-cost,
   high-yield default). Going below the board's `min_track_width` is intended here
   — it's what the human did. (Keep DRC honest separately: grade at the clearance
   floor from `--design-rules`; a thinner track only *increases* clearance to
   neighbours, so it never creates a clearance violation.)
2. **Route the whole signal step at that width** (re-route everything, not just the
   failed nets — a victim is blocked by the *successful* wide tracks already in its
   channel, so thinning only the failures leaves the channel full):
   ```bash
   python3 -X utf8 route.py board_fanout.kicad_pcb board_signal.kicad_pcb \
       --nets "*" "!GND" "!VCC" \
       --track-width <fab floor, e.g. 0.127 or 0.0889> --clearance <floor, e.g. 0.1> \
       --via-size <floor via, e.g. 0.30> --via-drill <floor drill, e.g. 0.15> \
       --no-bga-zone --max-ripup 10 --max-iterations 1000000 \
       2>&1 | tee /tmp/route_signal.txt
   ```
   A finer `--grid-step` (0.05, or 0.025 for sub-0.4 mm pitch) is the complementary
   lever — a corridor that exists geometrically still needs a grid line on it to be
   found; pair it with the thin width at fine-pitch escapes ("boxed in by static
   obstacles"). If still congested, step the width down further toward the fab
   physical minimum and re-route.
3. **Keep only the nets that NEED width wide — by rule, not by sweep.**
   Power/high-current nets stay wide via `--power-nets`/`--power-nets-widths`, and
   impedance-controlled nets keep their calculated width (`--impedance`, or
   `route_diff.py` for pairs). Everything else routes at the fab floor. You do
   **not** need to find which signals are "genuinely congested": there's no reason
   to widen an ordinary signal at all, so the question never arises (and a net that
   passes wide can itself be the blocker of another, so a per-net width guess is
   unsound regardless).

3. **If swaps occurred** (polarity or target swaps):
   - Tell the user how many swaps were made
   - Ask if they want to sync the schematic
   - If yes, ask for the KiCad project directory path
   - Re-run the routing command with `--schematic-dir` added
4. Run verification: invoke `/review-routed-board` (falls back to the raw DRC and connectivity checks)
4b. **Apply the score gate (Step 6):** run `scripts/board_score.py`. If
   `blocking > 0` the board is NOT done — go to **Step 9**, spend an iteration,
   and re-score. A fully-unrouted multi-pad net, a DRC violation, or copper below
   the spec's sizes is a **defect to fix**, never an accepted shortfall.
4c. **Run the three routed-board verifier lenses** (`connectivity`, `drc`,
   `spec`). A `VERDICT=FAIL` re-enters the loop at its `route=` step.
5. Summarize the final state of the board — quoting `blocking`, the stop
   condition by number, and everything in `ungraded` as **unexamined**
6. **Offer to clean up intermediate files**:
   - List the intermediate `.kicad_pcb` files created (e.g., `board_step1.kicad_pcb`, `board_step2.kicad_pcb`, etc.)
   - Ask if the user wants to delete them, keeping only the final output
   - If yes, delete the intermediate files

Example cleanup prompt:
> "Routing complete. The following intermediate files were created:
> - board_step1.kicad_pcb (after GND/VCC planes)
> - board_step2.kicad_pcb (after fanout)
> - board_step3.kicad_pcb (after signal routing)
> - board_step4.kicad_pcb (after GND return vias)
>
> The final routed board is: board_step5.kicad_pcb
>
> Would you like me to delete the intermediate files?"
