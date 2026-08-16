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
python3 py_tools/check_floorplan.py board.kicad_pcb --emit-intent floorplan.json

# grade
python3 py_tools/check_floorplan.py board.kicad_pcb --intent floorplan.json
python3 py_tools/check_floorplan.py board.kicad_pcb --intent floorplan.json --json findings.json
```

The intent is also a GENERATOR input, not only a grader's: `place_seed.py`
turns a declared intent into an initial placement for an unplaced board
(zones, edge bands, locks and decap rules become placement constraints, and
the emitted seed is graded against the same intent it was built from), and
`place_portfolio.py` uses the intent as the hard gate plus the `health`
signals when ranking K perturbed placement candidates. See
`placement/README.md` for both.

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
| **net affinity** | from geometry alone | the per-PART inverse of block displacement: which single part carries a net its own block sits away from. See below |
| **escape lanes** | from geometry alone | per fine-pitch part, per face: lanes that fit against nets that must leave. Needs no declaration. See below |
| **blocked-cell share** | **not pre-route at all** | needs #409's blocker JSON, which only exists after a routing attempt. Reported as skipped, with that reason |

### `net_affinity`: the member a block metric averages away

Block displacement is an average over a block's members, so it is quiet when
*one* member is the problem. Measured on a real board: a series resistor zoned
into a far-edge block carried **85.7% of a critical bus net's routed length**,
forcing ten drop-vias and eight reference-plane voids. The block it sat in was
flagged as displaced; the resistor was not, and four runs went by before a
human found it.

Reported per (part, net), ranked, advisory. Two numbers reach `JSON_SUMMARY`:
`health_net_affinity_offenders` (rows that dominate a net *and* pierce a
declared corridor) and `health_net_affinity_worst_norm` (the largest
recoverable length as a fraction of the board diagonal — mm never compares
across boards).

Four entry conditions, none of them a tuned constant, because a diagnostic
that cries wolf is worse than none:

- the same fanout / `ignore_net_ids` cut as block displacement, so a rail never
  appears;
- the part must sit in a block **that has a zone** — without a declared seat
  there is nothing to blame for where it ended up;
- **three or more owning parts.** A two-owner net has one MST edge, incident on
  both parts, so `share` is 1.0 for each and dominance would mean nothing;
- moving the part onto its own net's centroid must free at least 10% of what it
  carries. A part in the MIDDLE of a net's span is incident on the edges either
  side of it and also scores 1.0, while being exactly where it belongs — the
  recoverable test is what separates a misplacement from a topological hub.

Locked parts are reported **with a flag**, not suppressed: "this cannot move"
is triage, not absence. `health.affinity_exempt_nets` (globs) silences a
deliberately long net.

`recoverable_mm` is a mechanical counterfactual, not a guess — the net's MST is
rebuilt with the part translated onto the centroid of the pads it talks to,
using the same override primitive the quench scores real moves with. It is an
upper bound: nothing checks that the part may legally sit there.

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

### `escape_lanes`: the difference between "the router failed" and "this was never routable"

For each fine-pitch part, per face: lanes **supplied** (the face's usable span
divided by one track plus one clearance, at the board's own floor) against lanes
**demanded** (nets that must leave through it). A face in deficit is a *binding
constraint* — net ordering only chooses which nets strand there, never how many.
Runs have been spent on ordering experiments against a face this settles in
seconds.

Reported without any declaration, on every board: a face that cannot pass its
own nets is a fact about geometry, and requiring an opt-in would mean it is only
measured where someone already suspected it. `health_escape_deficit_parts` and
`health_escape_worst_deficit` reach `JSON_SUMMARY`.

Three things keep it honest:

- **The lane pitch is read from the board**, not assumed. At 0.20 mm a face
  passes and at 0.35 mm the same face is short, so a constant would manufacture
  or hide a structural finding depending on the board it met.
- **`blockers` names who ate the lanes.** A count says a face is short; the
  blocker list says which neighbour to move. Read that field first — it is the
  difference between a signal and an action.
- **Interior pads count toward no face** and are reported separately. A boxed-in
  pad does not escape sideways at any pitch; it needs a via. Rolling it into a
  face's demand would blame the face for a fanout problem.

Detection is by **pad pitch, not footprint name** (a house library carries no
pitch in its name) and is deliberately wider than the fanout test: fanout asks
"is this pad boxed in", which needs interior pads; escape asks "do this face's
pads fit through the channel beside it", which a fine-pitch *perimeter* part
fails with no interior pad at all. Through-hole parts are excluded — a THT pin
is reachable on every copper layer, so there is no escape to be short of.

A worked pairing from ulx3s: the ledger reports `U9 west: supply 6 < demand 14`
with `15.35mm of that face is taken by SD1`, and `net_affinity` independently
reports SD1 carrying 57–63% of `SD_D0`, `SD_D1` and `SD_CMD`. Two signals
computed from different quantities naming the same part is the case worth
acting on.

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
