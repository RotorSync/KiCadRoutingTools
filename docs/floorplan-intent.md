# Floorplan intent, graded (#549)

Placement in this toolchain is judged by `crossings` and `hpwl`. Both are
indifferent between a sensible layout and a scattered one with the same
wirelength — and a render only moves the judgement from a number to a vibe.
Nothing declares where parts *belong*, so nothing can check whether they went
there.

`check_floorplan.py` is the check. You declare what the floorplan is meant to
be; it measures the board and exits non-zero with the number that broke.

```bash
# start from what the board already is, then edit it down
python3 check_floorplan.py board.kicad_pcb --emit-intent floorplan.json

# grade
python3 check_floorplan.py board.kicad_pcb --intent floorplan.json
python3 check_floorplan.py board.kicad_pcb --intent floorplan.json --json findings.json
```

## The board outline is not editable by this toolchain

`envelope` is **read from the board**, never authored. A part outside it is a
finding about the **part**.

Board size, cutouts, slots and mounting-hole geometry are mechanical decisions —
enclosure fit, panel rails, connector apertures — and they belong to whoever owns
the mechanical design. If a board is genuinely too small for its parts, the
honest response is to say so with the measured number and stop. Nothing in this
module writes `Edge.Cuts`, and `--emit-intent` reports the cutouts as read-only
`context` so an editor can see what the parts must avoid without mistaking it for
something to change.

## Exit codes

| code | meaning |
|---|---|
| `0` | graded, no error-severity violations (or `--exit-zero`) |
| `1` | crash |
| `2` | argparse, **including an unreadable or malformed intent** |
| `3` | the board is not in a state this tool can work on — unplaced, or no trustworthy outline |
| `4` | graded successfully, **violations found** |

`4` rather than `1` because `3` is already taken by the placement-family
board-state gate and `1` is ambiguous with a crash. A caller has to be able to
tell *"the floorplan is wrong"* from *"the grader is broken"* — that distinction
is the entire product. (`check_drc.py` uses `1` for its violations; this diverges
knowingly.)

## The intent file

```jsonc
{
  "schema": 1, "kind": "floorplan-intent", "units": "mm",

  // READ from the board. A mismatch is a finding about the intent.
  "envelope": { "rect": [94.1, 61.42, 188.08, 112.22], "tolerance_mm": 0.5 },
  "defaults": { "zone_tolerance_mm": 0.5 },

  "blocks": [
    { "name": "power",
      "refs": ["U3", "C1?", "L1"],      // globs, the PRIMARY form
      "group": "sheet:58d913ec",        // optional; raw key or short_name
      "zone": [2, 2, 40, 30],
      "side": "F",
      "exclusive": false,
      "note": "buck; keep the switch node tight" }
  ],

  "keepouts": [
    { "name": "mount-NW", "rect": [0, 0, 6, 6], "sides": ["F","B"], "allow": ["MH1"] },
    { "name": "antenna",  "circle": [50, 5, 8], "sides": ["F"] }
  ],

  "edge_connectors": [
    { "ref": "J1", "edge": "north", "overhang_mm": { "min": 0.0, "max": 1.5 } }
  ],

  "decaps": { "max_distance_mm": 2.5, "exempt": ["C99"] },
  "must_lock": ["MH*", "J1"],
  "legality_budget": { "overlap_area": 0.0, "oob_count": 0 },
  "severity": { "decap_distance": "warn" }
}
```

### `refs` is the primitive, not `group`

Sheet group keys are opaque uuid paths — KiCad's `Sheetname` property is absent
from every corpus board — so nothing can author `"group": "sheet:1a2b3c4d"`
without listing the board first. `group` is accepted and matched against **both**
the raw `derive_groups` key and its `short_name` form (what `--list-groups`
prints, and therefore what anyone would copy), but reference globs are what a
human or a model can actually write.

### A block that resolves to nothing is an error

Not an empty block. A typo'd `refs` matches nothing, every rule over it iterates
an empty set, and the board grades clean while nothing was checked. That is the
exact failure this tool exists to prevent, so it is reported as
`block_unresolved` at error severity.

### `rules_run` and `rules_skipped`

Both are in the `JSON_SUMMARY`. **"0 violations" and "0 rules ran" must not look
the same to a machine** — a caller reading only `pass` would treat a fully
skipped grade as a clean board. A rule is skipped when nothing in the intent asks
for it, and the reason is printed:

```
  4 rule(s) did not run:
    - decap_distance: the intent declares no decaps.max_distance_mm
    - keepout: the intent declares no keepouts
```

## The rules

| rule | fires when | measured with |
|---|---|---|
| `envelope` | the declared envelope is not the board's outline | `board_bounds` |
| `zone_containment` | a member's courtyard leaves its block's zone | `GradedPart.rect` |
| `zone_side` | a member is on the other face | `legality.footprint_side` |
| `zone_exclusive` | a non-member intrudes on a reserved zone | `rect_overlap_area` |
| `keepout` | any part enters a keep-out, unless in `allow` | courtyard **and** through-hole rect |
| `edge_connector` | overhang outside `[min,max]`, or the wrong edge | `BoardOutlineGate.rect_outside_amount` |
| `decap_distance` | a decoupling cap is too far from its own IC | `groups.decap_tethers` |
| `must_lock` | a declared-critical part is not locked in the file | `parser.extract_locked_refs` |
| `legality` | overlap or off-board parts exceed a budget | `QuenchState.legality_metrics` |
| `block_unresolved` | a block matched no footprint | — |
| `intent_zone_overlap`, `intent_zone_outside_envelope` | the intent contradicts itself (no board needed) | — |

Every one of them measures with the geometry the **optimizer itself gates on**.
A grader with its own idea of what "legal" means grades the reimplementation
rather than the board — so where re-deriving was plausible, a test asserts the
two agree exactly (all 34 of ulx3s's cap distances identical to the grouper's,
all five legality numbers identical to the quench's).

### A through-hole part is in a keep-out from either side

Its leads pass through. `keepout` tests the courtyard **and** the drilled-pad
rect against every face the part occupies, so a mounting-hole keep-out cannot be
walked through from the back.

### `oob_area` cannot be budgeted, and says so

`legality_budget.oob_area` is **refused at load time**:

```
legality_budget.oob_area: not gateable. out_of_board_area is measured against
the bounding-box inset, so a part sitting inside a CUTOUT scores 0.0 area and
would grade clean. Use oob_count or oob_amount, which both see the real
Edge.Cuts rings.
```

`out_of_board_area` measures against the rectangular usable inset — its own
docstring calls itself *"a lower bound on a notched one"*. A part sitting
entirely inside a milled slot scores `oob_count=1, oob_amount>0, oob_area=0.0`.
Refused loudly rather than ignored, so the reason reaches whoever wrote it.

## A board whose outline did not parse is refused, not graded

A broken outline degrades **silently**: unclosable segment groups are dropped,
`extract_board_contours` returns `([], [])`, `BoardOutlineGate.active` goes
`False`, and every containment check quietly falls back to the bounding box. No
exception, no warning. A grader that inherits that reports a clean board because
it stopped checking.

So `outline_state` validates the envelope before anything is graded against it,
and the run exits **3** rather than producing a verdict. It reproduces the
parser's own simple-rectangle short-circuit to tell the three "no rings" cases
apart, because a plain axis-aligned rectangular board **is** its bounding box
exactly — refusing that would refuse most of the corpus.

The case that motivated it is [#550](https://github.com/drandyhaas/KiCadRoutingTools/issues/550):
`extract_board_bounds` reads neither board-level `gr_circle` nor `gr_curve`, so a
round board reports `board_bounds: None` while its 64-point ring parses fine.

## `--health`: what tells you the floorplan is wrong

Separate from the rules, and advisory. An intent violation says *"this is not the
floorplan you declared"*; a health signal says *"this floorplan will fight the
router whatever you declared"*. That is
[discussion #407](https://github.com/drandyhaas/KiCadRoutingTools/discussions/407)'s
question — *knowing when to stop routing and go move something* — whose two scars
were a magnetics block 80 mm from both its own endpoints, and ~22 nets no knob
could fix because the answer was re-floorplanning a quadrant.

```jsonc
"health": {
  "block_displacement_mm": 15.0,
  "bus_corridors": [ { "name": "sdram", "nets": ["SDRAM_*"], "width_mm": 8.0 } ],
  "classes":      { "SDRAM": ["SDRAM_*"], "USB": ["USB_*"] }
}
```

| signal | computable | what it means |
|---|---|---|
| **block displacement** | from geometry alone | the block's own pad centroid vs the centroid of everything it connects to. This is #459's "connectivity-centroid displacement" |
| **bus crossings** | pre-route, but the corridor is a **model** | a straight rectangle between the bus's two pad clusters; its long sides are fed to the quench's own crossing kernel. A screening signal, not a verdict — real routes bend |
| **convergence** | only with declared `classes` | which critical classes crowd one corridor. Placement has no net-class notion and "critical" is design intent, not a fact in the file, so **it is skipped rather than guessed** |
| **blocked-cell share** | **not pre-route at all** | needs #409's blocker JSON, which only exists after a routing attempt. Reported as skipped, with that reason |

### Power and ground are excluded, and that is what makes these signals work

GND owns **96** of ulx3s's parts and `+3V3` owns **45**, out of 329 nets whose
*median* is 2. Left in:

- 8 of 10 blocks report a foreign-pad count within 10% of the same median — they
  are all seeing the board's power nets, so the "net centroid" is really the
  board centroid and displacement degenerates into *distance from the middle of
  the board*. Filtered, the median drops from 332 to 40 and the ranking changes.
- The same rails cross **every** corridor, because a 96-part MST sprays airwires
  board-wide. On ulx3s's SDRAM corridor the unfiltered top three offenders were
  `GND`, `+5V`, `+3V3` — a fiction, since those route on a plane rather than as
  traces through the channel. Filtered: 24 crossings → 18, and the offenders
  become real signal nets.

Pass `health.ignore_net_ids` to name the plane nets explicitly (as `--ignore-nets`
does elsewhere); `health.max_fanout` is the backstop, default 20.

## What `--emit-intent` does and does not claim

It writes an intent that **grades clean by construction** — a baseline to
tighten, and the round trip is what proves the rules are wired to real geometry
rather than silently skipping.

It claims a `zone` only where it can defend one. A schematic sheet is a
*functional* grouping, not a spatial one: its members scatter across the board,
so per-sheet bounding boxes mutually overlap — all ten of ulx3s's do, by up to
4508 mm². Emitting those as zones produces an intent no placement could satisfy,
and it would be the emitter that was wrong. Zones are emitted only where
**disjoint**, tightest first (ulx3s: 4 of 10), clamped to the envelope; the rest
carry membership and say why they have no zone.

This is the same spatial incoherence that makes sheet blocks useless for
*movement* — see `placement/README.md`.

Parts already overhanging the outline are recorded as `edge_connectors` by
observation, which is what stops `oob_count` reporting a card edge or USB shell
as a defect forever.
