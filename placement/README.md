# Placement

Perturbative placement optimization for KiCad PCB files. Starts from an
existing (hand- or AI-made) placement and improves it for routability —
it does not place boards from scratch. Background research and experiment
results: [docs/placement-optimization.md](../docs/placement-optimization.md).

Two command-line tools sit on top of this module:

## place_optimize.py — greedy quench

Small nudges (capped by `--max-displacement`), 90° rotations (on each part's
own angular lattice, so a part placed at 45° rotates to 135/225/315 rather
than snapping to the axes), and same-footprint swaps (capped by
`--swap-max-displacement`, default: the same cap) that reduce airwire length
+ crossings + a whitespace (halo) penalty scaled by pin count + a soft
board-edge margin. Locked footprints never move.

```bash
# Conservative polish (recommended starting point)
python place_optimize.py input.kicad_pcb optimized.kicad_pcb \
    --max-displacement 3 --length-weight 0.3 --crossing-penalty 30 \
    --halo-coef 0.15 --halo-weight 2 --edge-halo 2 \
    --ignore-nets GND "+3.3V" \
    --lock "J*" "P30*" "*PORT*"
```

Key options:

| Option | Default | Description |
|--------|---------|-------------|
| `--max-displacement` | 10 mm | Max distance a part may move from its seed position; applies to nudges and swaps alike (3 mm recommended; large values can destroy the placement's macro structure) |
| `--swap-max-displacement` | = max-displacement | Displacement cap for swap moves; must be ≤ `--max-displacement` |
| `--ignore-nets` | – | Net patterns excluded from airwire scoring (plane-routed power nets) |
| `--lock` | – | Reference patterns to pin in place (connectors, mounting-critical parts) |
| `--halo-coef` | 0.25 | Extra whitespace per √(pin count); keep modest (~0.15) on dense boards |
| `--no-rotate` / `--no-swap` | off | Disable rotation / swap moves. `--no-rotate` freezes every part's angle: nudges keep the current rotation, and same-footprint swaps are restricted to pairs that already share one, since a swap exchanges full poses and a mixed-angle pair would rotate both parts |

## place_route_loop.py — router-in-the-loop repair

Routes the board with the real router, reads the failure diagnostics
(failed nets + the blocker nets named in the frontier analysis), and
micro-quenches only the small parts that could help those routes succeed,
weighting the failed nets so both their airwire length and any crossing they
take part in cost more. Re-routes and keeps the new placement only if
(failures, router effort) actually improves; otherwise reverts and widens the
search.

The tally counts the router's end-of-run reconciliation pass, so a net that
pass recovers is not treated as a failure in the next round. A rejected round
widens the **nudge** search 1.5×; the swap cap does not move. It stays at
`--swap-max-displacement` (default: the initial `--max-displacement`), so
widening the search can never turn into a long-range swap.
`--swap-max-displacement`, `--no-rotate` and `--no-swap` work here exactly as
they do in `place_optimize.py`, and `--verbose` surfaces each accepted quench
move plus the per-pass `swap-capped=N` count.

```bash
python place_route_loop.py input.kicad_pcb repaired.kicad_pcb \
    --route-args '--nets "/*" "Net-*" --track-width 0.2 --clearance 0.2 ...' \
    --ignore-nets GND "+3.3V" --lock "J*" --swap-max-displacement 2
```

On the kit-dev-coldfire demo board this repaired the hand placement from
3 failed nets to 0 with 4.8× less router effort, moving only
resistors/caps/jumpers.

## place_fanout_clearance.py — decoupling-cap clearance repair (issue #130)

Run **after** `bga_fanout.py`. Nudges decoupling caps near a BGA so their
pads clear every foreign-net fanout via, every foreign track on the cap's
own copper side, and every foreign component pad (#130/#278/#275 — a graze
already present at the seed placement is a violation to fix, not a baseline
to preserve), and pulls each pad toward the nearest **same-net** ball — so
a power/GND via dropped at that ball later also lands on the cap pad (one
shared via connects ball + cap + plane). Caps move as little as possible
(90° rotations allowed), never overlap each other or a locked part, and a cap
that can't clear within the (auto-grown) displacement budget is reported
unresolved for a manual nudge.

It reads each via's actual size from the board, so the only setting that
matters is `--clearance`, which must match the fanout / DRC floor:

```bash
# after: bga_fanout.py board.kicad_pcb -o fanned.kicad_pcb --clearance 0.1 ...
python place_fanout_clearance.py fanned.kicad_pcb capclean.kicad_pcb --clearance 0.1
```

| Option | Default | Description |
|--------|---------|-------------|
| `--clearance` | 0.25 mm | DRC clearance; **set to the fanout/DRC floor** |
| `--cap-prefix` | `C,R` | Comma-separated reference prefix(es) for movable passives near a BGA (caps **and** resistors by default). Only 2-copper-pad parts move, so RN-style arrays are auto-excluded; paste-only apertures are ignored when counting pads. |
| `--capture-radius` | 2 mm | Max distance over which a same-net ball attracts a pad |
| `--max-displacement` / `--max-displacement-cap` | 2 / 3 mm | Initial and grown move budget per cap |
| `--default-via-size` | 0.3 mm | Fallback only, for vias with no readable size |
| `--lock` | – | Extra reference patterns to pin in place |

On ulx3s U1 (22×22, 0.8 mm) this took the fanned board from 4 PAD-VIA to
fully DRC-clean, tidying 19 caps toward same-net balls (all ≤1.9 mm). In the
GUI, the **"Optimize decoupling cap placement"** checkbox on the BGA fanout
tab runs the same engine automatically right after fanout (off by default).
The advanced knobs above (capture radius, near margin, search step, max
displacement / cap / growth, max passes, cap-ref prefix, allow-rotation) are
exposed in that tab's **"Cap Placement (advanced)"** box; `--clearance`,
`--grid-step`, and the via size come from the Basic tab.

### animate_fanout_clearance.py — visualize the repair as a GIF

`animate_fanout_clearance.py` (repo root) runs the **same** repair engine and
records every accepted cap move via the engine's optional `on_move` hook, then
renders an animated GIF of the caps gliding from their seed placement to their
final, via-clearing positions. The view is framed to the BGA ball field (not
the whole board); fanout vias appear as net-coloured disks with their keep-out
ring, cap pads are coloured by net, and a faint ghost rectangle marks each
cap's seed position. It accepts all of `place_fanout_clearance.py`'s repair
options plus `--size`, `--fps`, and `--sub-frames` (motion smoothness).

![Decoupling caps gliding off foreign-net fanout vias on the glasgow revC BGA](../docs/fanout-cap-placement.gif)

```bash
# after: bga_fanout.py board.kicad_pcb -o fanned.kicad_pcb --escape-method underpad \
#            --via-size 0.3 --via-drill 0.2 --track-width 0.1 --clearance 0.1
python animate_fanout_clearance.py fanned.kicad_pcb capmove.gif --clearance 0.1
```

This is a read-only visualization tool: the `on_move` hook defaults to `None`,
so `place_fanout_clearance.py`, the GUI, and the engine itself behave exactly
as before when it is unused. Requires only **Pillow** -- as of #431 it renders
through `route_render.BoardRenderer` and encodes through
`animate_route.save_movie`, so it no longer carries its own world->pixel
transform, GIF writer or font handling, and `pygame` is no longer needed. Two
things came free with the port: the **real board** beneath the BGA field
(outline, cutouts, zones), which the pygame version never drew, and `.mp4`
output (extension picks the format, falling back to a sibling `.gif` when
imageio-ffmpeg is absent).

## Testing

The placement tests are standalone scripts (no pytest needed), all in
`tests/run_all.py`'s `--fast` lane:

```bash
python3 tests/test_quench_swap_cap.py        # swap displacement cap (#430)
python3 tests/test_quench_neighbor_lists.py  # pruned-scan bit-exactness (#430)
python3 tests/test_458_loop_steering.py      # loop caps, tally, summary merge
python3 tests/test_458_quench_net_weights.py # weighted crossings
python3 tests/test_458_quench_rotations.py   # rotation lattice, --no-rotate
python3 tests/test_fanout_clearance.py       # cap clearance repair (#130)
python3 tests/test_456_courtyard_parser.py   # courtyard shapes + silk bleed (#456)
python3 tests/test_456_side_and_outline.py   # board side, real outline, graders (#456)
python3 tests/test_459_groups.py             # block sources + parsing (#459)
python3 tests/test_459_group_moves.py        # rigid block translation (#459)
```

Quench output is reproducible across processes — the same board and arguments
give the same placement, with no `PYTHONHASHSEED` pinning (#457). It used to
depend on the hash seed: `net_refs` was a set of reference strings, its iteration
order became the MST's point order, and Prim's tie-break is first-index-wins, so
equidistant pads (uniform-pitch GND arrays, decaps on a grid, symmetric
connectors) built a different tree per process. `interf_u_unrouted` scored 447 /
457 / 450 crossings under three seeds before a single move was made. `net_refs`
now holds sorted lists, and `tests/test_457_determinism.py` pins it.

When comparing two placements, `hpwl` is the metric to reach for first: it reads
only each net's pad-position extremes, so unlike the MST length and the crossing
count it is invariant to airwire order. If HPWL agrees and crossings do not, the
two runs differ in tie-breaks rather than in placement quality.

## Ratsnest metrics, and the pre-route screen (#504)

The quench cost function computes airwire length and crossings on every pass, and
`hpwl()` is pure pad geometry. Those numbers used to be printed and discarded.
They are now exported:

- `quench(..., metrics_out=d)` fills `d` with `{'before', 'after', 'legality'}` —
  an out-param rather than a changed return, since the return is consumed
  positionally by both CLIs and four test files.
- `place_optimize.py` emits them as a `JSON_SUMMARY:` line, so a chain or grader
  can gate on what a run achieved instead of scraping stdout.
- `place_route_loop.py` records `ratsnest_crossings` / `ratsnest_hpwl` /
  `ratsnest_length` in each round's metrics dict, report-only beside the
  `pad_pairs_*` keys. `better()` is deliberately untouched — reworking the
  comparator is #458.

**Which numbers compare.** `crossings` (a raw count by contract) and `hpwl` are
unweighted, so they are comparable across runs. `length` and `total` are scaled
by `net_weights`, so they only compare between the `before` and `after` of the
*same* call — which is why the screen thresholds on the first two.

**`--ratsnest-screen N`** (percent, `0` = disabled, the default) skips the routing
run when a candidate's crossings or HPWL regress by more than N% against the board
it came from. Routing is the honest judge but an expensive one, often minutes per
round; a candidate whose ratsnest clearly got worse is very unlikely to route
better. The baseline is free — quench is handed the current best board, so its own
`before` *is* that board's ratsnest. Every decision is logged with its numbers, so
it is auditable whether the screen ever skipped a placement that would have won.

## Alignment and orientation (`--align-weight`, `--orient-weight`, #548)

Two cost terms for the things a human does constantly and the quench does not:
put parts that belong together on a shared axis, and turn a part toward the net
it exists to serve. **Both default to 0 (off).**

```bash
python3 place_optimize.py board.kicad_pcb out.kicad_pcb --max-displacement 3 \
    --align-weight 5 --orient-weight 1
```

### The premise in #548 is wrong in a way worth recording

The issue proposes to *"score airwires from the actual pad the net lands on"*.
The cost path **already does that** — `_net_points` emits one MST node per
connected pad from `pad_globals()`, full rotation applied, and there is no
centroid anywhere in the objective.

The gap is **numeric**, not geometric. Measured on the test fixture: the four
rotations of a part whose one connected pad faces away from its anchor differ by
**0.500 mm across a 19.8 mm net**. The directional signal is present and drowned.
So what was missing is a term that is directional at *part* scale with its own
weight, which is what `--orient-weight` is.

### Alignment

A pairwise penalty between **peers** — same `footprint_name`, the pairing the
swap phase already indexes — on the nearer shared axis:
`w * min(d, radius)²`.

- **Continuous** at the radius. The obvious charge-inside/zero-outside shape has
  a cliff there that pays a part to *flee* the row rather than join it.
- **Saturating** beyond it, so a distant peer contributes a constant that
  cancels between one part's candidate poses instead of dragging it across the
  board.
- **Zero** on a shared axis, so a tidy row is free.

Four caps seeded at y ∈ {99.8, 100.0, 100.2, 100.3} come out at four distinct y
today, and on one shared y with the term on.

The peer index is built in `__init__` and deliberately does **not** ride on the
pruned neighbour lists: that prune is a 2-D *box* overlap test, so a pair must be
near in both axes — but alignment is inherently long-range *along* the shared
axis (two caps 50 mm apart in x and 0.1 mm apart in y **are** aligned). It is a
*lossy* prune, unlike `_neighbors`' exact one: peers are fixed from seed
positions, so a pair that drifts within `--align-span` later is not picked up.

### Orientation

Sums `|r| − r·û` per connected pad, where `û` points from the pose origin toward
the centroid of that net's pads owned by *other* parts. Zero facing the anchor,
`2|r|` facing away — **bounded at part scale on purpose**: it breaks a rotation
tie, it does not outrank a real length win.

Two things it is not, worth knowing: it is not purely rotational (moving the part
changes `û` too, so a large weight also pulls the part toward its nets), and
through `part_geometry_cost` it reaches the group phase, so a rigid block
translate is steered by it as well.

### Measured, so the recommendation is not a guess

Sweep at `--max-displacement 3`, 3 passes. *tidy* is the mean share of distinct
axis positions within each same-footprint group — **lower is tidier**.

| board | align | orient | crossings | hpwl | tidy | overlap / oob |
|---|---|---|---|---|---|---|
| splitflap_driver | 0 | 0 | 194 | 2407.1 | 0.652 | 0.0 / 6 |
| | 5 | 0 | **187** | 2386.2 | 0.581 | 0.0 / 6 |
| | 15 | 0 | 187 | 2404.7 | **0.560** | 0.0 / 6 |
| | 0 | 3 | 185 | 2415.5 | 0.712 | 0.0 / 6 |
| | **5** | **1** | **185** | 2391.2 | 0.605 | 0.0 / 6 |
| interf_u_unrouted | 0 | 0 | 404 | 4175.0 | 1.000 | 0.0 / 2 |
| | 5 | 0 | 404 | 4174.5 | 1.000 | 0.0 / 2 |
| | **5** | **1** | **402** | **4162.3** | **0.889** | 0.0 / 2 |

**`--align-weight 5 --orient-weight 1`** is the recommended pair: best or
joint-best crossings on both boards, tidier, and **no legality regression
anywhere** — overlap stayed 0.0 and `oob_count` never moved.

Two honest caveats. **n = 2 boards, and no re-route** — crossings and HPWL are
proxies, and the router is the only judge that counts, so do not read the
crossing improvements as a routability claim. And **over-weighting trades
wirelength for tidiness**: `--align-weight 15` is the tidiest row in the table
and its HPWL is *worse* than at 5. `interf_u_unrouted` shows the other limit —
alignment does nothing at all on a board with few repeated footprints, because
there are no peers to align.

### Why off by default

The router, not a tidiness score, is the judge of a placement, and neither term
has been measured against routing *outcomes* — only against proxies, above.
On-by-default would silently change every user's board to buy legibility.

It would also corrupt the isolated fixtures in `test_458_*`, which zero every
geometry knob they know about so the objective is clean enough to assert
`total == 30.0`. A term with a nonzero default is invisible to those and breaks
the isolation — that is degrading a correctness test, not re-baselining a golden.

At `0.0` both hooks return before touching any geometry and the peer index is
empty, so a default run is **bit-identical**, verified on `interf_u_unrouted` and
`splitflap_driver` rather than argued.

## Placement blocks (`groups.py`, #459)

The per-part nudge cannot express "these parts need to travel together": an IC
and its decoupling caps fight each other one at a time, because moving either
alone worsens the pair. `--group-by` gives a block a **rigid translate** move —
the whole body shifts by one offset.

Sources, in precedence order, first match wins per part (a part is in at most one
block):

| source | what it is | corpus evidence |
|---|---|---|
| `kicad` | KiCad `(group ...)` blocks | **0 of 27** in-repo boards have one. Exact when present; verified against a synthetic fixture. |
| `sheet` | schematic sheet path | **12 of 22** boards with `(path ...)` have >1 sheet. `ulx3s`: 10 blocks sized 83/34/23/20/20/12. The workhorse. |
| `netprefix` | net-name prefix (`AUDIO_*`) | The **weakest** source. Raw prefixes are dominated by KiCad's auto-generated `Net-(U1-Pad)` names — one bogus bucket of up to 92 refs spanning 75mm — so those and power nets are excluded and a 20mm coherence gate applied. What survives is small but real: ulx3s 9 blocks, tigard 0. |
| `decap` | 2-pad caps tethered to their IC | Strong. A cap sits 0.0–2.6mm (median) from the nearest IC and **shares a net with it 93–100%** of the time; the net check rejects the rest (13 of ulx3s' 70). |

`--group-by auto` means `kicad,sheet`. Default is `none`: grouping is opt-in, and
with it off the group phase never runs and output is byte-identical to the
ungrouped engine — which is what lets every existing bit-identity test stand.

**What actually moves.** Small blocks move; large ones do not. On
`splitflap_driver` with `--group-by decap`, 6 blocks of 2–3 parts translate and
the objective improves (total 4781.6 → 4689.7, crossings 211 → 208, HPWL
2468.0 → 2436.3 against the ungrouped run). Sheet blocks of 16–83 parts moved on
none of the boards tried — at a few mm of displacement a rigid shift of that many
parts rarely finds a legal, improving offset. That is the expected shape: #459's
80mm block *relocation* is separate, unimplemented work.

**Why the cap is per-member.** Every member must land within `--max-displacement`
of **its own seed**. That single rule is what keeps three existing guarantees
true: `build_neighbor_lists`' pruning stays exact rather than lossy, the outline
gate's per-ref cached reach cannot be outrun, and the no-stranding invariant
holds unchanged. Lifting it is precisely what makes the 80mm case hard.

### Blocks are also a ROUTING scope

The same blocks drive `route.py`: `--group BLOCK` scopes a routing run to a
block's nets, `--preview` reports what a run would add without writing a board,
and `--undo` strips a block's copper back to unrouted. `--list-groups` prints
what is inferred, with both net counts, before you trust any of it.

Which nets a block "owns" has two honest answers, so `--group-scope` picks:

| scope | means | when it is the right one |
|---|---|---|
| `internal` | every pad of the net is inside the block | a schematic sheet — measured **60–70% internal** (glasgow's two 68-part sheets: 52 internal vs 23 boundary), so "route this sheet" is a real self-contained job |
| `touching` | any pad inside the block, interface nets included | matches what `--component` already means, and it is the **only** useful reading for a `decap` block: those are **0% internal** by construction, since a decoupling cap bridges VCC to GND and both span the board |

**The default depends on the operation**, because the same set of nets is right
for one and dangerous for the other. Routing defaults to `touching` — routing a
block's interface is the point. `--undo` defaults to `internal`, because a
block's touching set contains GND/VCC, and undoing *those* strips their copper
across the **whole board**: on the rp2350 fixture, `touching` removes 170
segments, 54 of them nowhere near the block, versus 75 for `internal`. Asking
for `--group-scope touching --undo` explicitly still works, and warns.

`--undo` refuses KiCad-**locked** copper — `locked` gets no override anywhere
else in the toolchain (#521) and an undo is not the place to invent one, in the
segment path or the arc path. Nets protected for a *reason* (length-matched,
diff-pair) are removed, because naming a net exactly is the deliberate targeting
those reasons exist to allow. It carries the sibling `.kicad_pro` across (#441):
an undo only removes copper, so the input's DRC floor is still the correct one
and must travel.

It removes `(segment ...)` **and** `(arc ...)` tracks. Arcs need their own pass:
the parser *linearizes* them into `pcb_data.segments`, so an undo counts them,
but the text writer only matches `(segment ...)` blocks — without the arc pass an
arc-routed (i.e. hand-routed) net kept its copper while the run reported success.

What `--undo` does **not** do, and says so at runtime:

- **Zone pours.** A filled plane is copper, so a net with a surviving pour is not
  back to "no copper". Deleting a pour is a plane decision, not an undo, so it
  reports and leaves it — re-routing such a net gives a track web beside the
  pour, not the original plane.
- **Pad/target swaps.** No inverse exists, and they can touch nets outside the
  scope. It returns the named nets to "no copper", not the board to a prior state.
- **Copper graphics.** Net-tagged artwork has no `(segment)` block to delete; it
  is counted and named rather than silently left behind.

It also **refuses to run unscoped** — for a routing run "no scope" sensibly means
the whole board, but for an undo that silently means erasing every track on it.

## Board-state gates (#431)

Both CLIs refuse, with **exit code 3**, two board states they cannot do anything
useful with. `0` ok, `1` crash, `2` argparse, `3` "the board is not in a state
this tool can work on" -- a distinct code so a caller branches on the number
rather than scraping text.

| state | why refusing beats trying | override |
|---|---|---|
| **unplaced** (parts stacked at one coordinate) | the quench REFINES a placement. On a pile every candidate pose is illegal, so the run prints "0 parts moved" plus a legality block that looks like a result, and a large `--max-displacement` yields a tiny scatter around the origin that looks like progress | `--allow-unplaced` |
| **already routed** | the quench models no copper at all: legality is courtyard + outline, cost is pad-to-pad airwires, and `writer.write_placed_output` rewrites footprint positions only. Every track would be left behind, detached from its pad | `--allow-routed` |

`place_route_loop` gates BEFORE round 0, which routes the whole board -- refusing
there saves minutes-to-hours of A* that would fail everything and then quench a
pile. `render_placement.py` WARNS and renders instead: being able to SEE an
unplaced board is the point of having a renderer for one.

Detection is board-relative. Coincident positions is the only signal strong
enough to fire alone -- two parts at the *same* coordinate is physically
impossible in a real placement, however dense. Low spread never fires alone. The
outside-the-outline share fires at 0.9, not 0.5, because castellated boards
legitimately overhang; and with no usable outline it is **unavailable**, never
true, so a misparsed outline cannot condemn a placed board. Measured: none of
the 27 tracked boards trips any of it, `watchy` included -- and `watchy` is the
worst case, with 81 of 82 parts in courtyard violation. Density is not
unplacedness, which is why this does not live in `legality.py`.

## Floorplan intent (`floorplan.py`, #549)

Everything above judges a placement by `crossings` and `hpwl`, and both are
indifferent between a sensible layout and a scattered one with the same
wirelength. Nothing declares where parts *belong*, so nothing can check whether
they went there.

`check_floorplan.py` closes that: declare the floorplan, grade the board against
it, exit non-zero with the number that broke.

```bash
python3 check_floorplan.py board.kicad_pcb --emit-intent floorplan.json   # start here
python3 check_floorplan.py board.kicad_pcb --intent floorplan.json        # 0 clean, 4 violations
```

Full reference: [docs/floorplan-intent.md](../docs/floorplan-intent.md). The
parts worth knowing from here:

- **The board outline is not editable by this toolchain.** `envelope` is READ
  from the board; a part outside it is a finding about the **part**. Board size,
  cutouts and slots are mechanical decisions the user owns.
- **Every rule reuses the geometry the optimizer gates on** — `QuenchState`'s
  `legality_metrics` and `edge_gate`, `GradedPart` rects and sides, and
  `groups.decap_tethers` for the cap→IC rule. A grader with its own idea of
  "legal" grades the reimplementation, so tests assert the two agree exactly.
- **A block that resolves to nothing is an error**, not an empty block. A typo'd
  `refs` grades clean while nothing was checked.
- **`rules_run` / `rules_skipped` are reported**, because "0 violations" and
  "0 rules ran" must not look the same to a machine.
- **A board with no trustworthy outline is refused (exit 3), not graded.** The
  fallback is invisible: no rings ⇒ `BoardOutlineGate.active` False ⇒ every
  containment test silently degrades to the bounding box.
- **`oob_area` cannot be budgeted.** It is measured against the bbox inset, so a
  part sitting entirely inside a cutout scores `0.0`. Refused at load time with
  that reason; use `oob_count` / `oob_amount`.

One thing `--emit-intent` will not do: claim a `zone` for a sheet block. A
schematic sheet is a *functional* grouping, so its members scatter and all ten of
ulx3s's sheet bounding boxes mutually overlap (up to 4508 mm²). Zones are emitted
only where disjoint — the same spatial incoherence that makes sheet blocks
useless for movement, above.

## Lock advisor (#431)

`--suggest-locks` on either CLI reports which parts look position-critical, with
a reason each, and prints a paste-ready `--lock` list. It **never locks
anything**: a wrong auto-lock silently freezes a part that needed to move, and
the run just quietly does less.

Its strongest rule is a code fact rather than folklore. `quench.py:207` keeps
only `net_id > 0` pads, so a net-less NPTH mounting hole has `pin_count == 0`
and no airwires -- while the only skip in the part loop is `if not fp.pads`, and
a mounting hole HAS a pad. It is movable with **nothing opposing it but the halo
term**. `tigard` ships four unlocked M3 holes in exactly that state.

Geometric rules (NPTH, outline overhang, edge proximity) measure the board.
Lexical ones (footprint name, reference prefix, pin function) guess from names
and are asymmetric: near-exact on stock KiCad libraries, silent on a house
library like `interf_u:PGA120`. False negatives are common, false positives
rare, and the output says so -- treat a quiet result as "nothing detected", not
"nothing to lock". A ref matched by rules from two different evidence channels
promotes to high.

High-pin parts are an **advisory**, not a suggestion: `place_route_loop` guards
them with `--max-target-pins` but `place_optimize` does not, and "large" is not
"position-critical". Exact refs by default, never invented globs -- a `J*` you
did not inspect freezes parts you never looked at, which is the auto-lock
failure merely deferred.

```bash
python3 place_optimize.py board.kicad_pcb --suggest-locks     --suggest-locks-json /tmp/locks.json     # writes NO board
# review the reasons, then:
python3 place_optimize.py board.kicad_pcb out.kicad_pcb --lock H1 J1 J2 ...
# confirm you covered them -- unlocked_high must be 0:
python3 place_optimize.py board.kicad_pcb --suggest-locks --lock H1 J1 J2 ...
```

## Reviewing a placement: render it (#431)

```bash
python3 render_placement.py placed.kicad_pcb --before seed.kicad_pcb -o delta.png
python3 render_placement.py board.kicad_pcb --list-groups --group-by sheet
python3 render_placement.py board.kicad_pcb --zoom-group sheet:58d913ec --per-side -o out/
```

**The render is triage, not a verdict.** The verdict is the caption's numbers.
Do not judge a placement by how much moved -- "lots moved, looks broken" and
"barely moved, looks safe" are both wrong.

### What it renders

Every panel below is `render_placement.py` output, and every one carries a
metrics caption — the render is triage, the caption is the verdict.

**The placement diff, drawn.** `--before` turns on the dashed seed rects and the
displacement arrows, so "what moved and how far" is the picture rather than a
diff of two files. 22 parts here; `C4` travelled 95.65 mm.

```bash
python3 render_placement.py placed.kicad_pcb --before seed.kicad_pcb -o delta.png
```

![placement delta](../docs/placement-delta.png)

**Per-side panels.** Placement lives on two sides, so `--per-side` emits one
panel per side instead of one flattened projection. Parts on the side you asked
for are bright; the far side's SMD pads are dimmed (still context — a back-side
part is why a front trace has to detour); **through-hole pads stay full
brightness on both**, because they are physically on both and a hole cannot move
on one side only.

```bash
python3 render_placement.py board.kicad_pcb --before seed.kicad_pcb --per-side -o out/
```

| front | back |
|---|---|
| ![front](../docs/placement-side-F.png) | ![back](../docs/placement-side-B.png) |

**Zoom to one placement block.** `--zoom-group` takes the same block names as
`route.py --group` — `--list-groups` prints them with their part and net counts.

```bash
python3 render_placement.py board.kicad_pcb --list-groups --group-by sheet
python3 render_placement.py board.kicad_pcb --zoom-group 58d913ec --group-by sheet -o blk.png
```

![group zoom](../docs/placement-group-zoom.png)

**An unplaced board.** The placement CLIs refuse this one (exit 3); the renderer
warns and draws it anyway, framed to the PARTS rather than the outline — a pile
at the origin would otherwise render as a dot in the corner of an empty board.
13 footprints at 1 distinct position, and `overlap 792 mm²` gives it away.

![unplaced](../docs/placement-unplaced.png)

**Reference designators.** On by default, and sized to the PART rather than to
the image — scaling by image height alone gives an 8px label at any zoom:
drawn, technically present, and unreadable. A part too small to letter is
skipped, and its arrow carries the meaning instead.

![labels](../docs/placement-labels.png)

**Every toggle, on one board.** The same 13-part delta, one flag at a time:

![toggles](../docs/placement-toggles.png)

`--no-delta-first` looks unchanged there for a real reason: 12 of the 13 parts
moved, so there is no context left to dim. It matters on a board where the delta
is a genuine subset — which is also where `--ratsnest-all` earns its keep:

![ratsnest](../docs/placement-ratsnest.png)

By default only the moved and attributed nets are drawn. `--ratsnest-all` is the
deliberate hairball switch: on a dense board it reproduces exactly the
unreadable mess KiCad already shows, which is limitation #1 in the issue and the
reason delta-first is a default rather than polish.

**Just the nets you are chasing.** `--ratsnest-nets` takes the same globs as
`route.py --nets`, exclusions included, and draws only those airwires — in their
own colour, with the parts they run between labelled and un-dimmed. Between "the
nets that moved" and "every net on the board" this is the case that actually
comes up.

```bash
python3 render_placement.py board.kicad_pcb --before seed.kicad_pcb     --ratsnest-nets '/CLK*' '/DATA*' '!*_N' -o clk.png
```

![named nets](../docs/placement-ratsnest-nets.png)

The matching goes through `net_queries.matches_net_filter` — the same helper
`route.py --nets` and the plan executor use — so a pattern means here exactly
what it means there. A pattern that matches nothing says so on stderr rather
than rendering an empty ratsnest, which would read as "this net has no airwires".

`--focus` adds one cropped panel per failed-net cluster.

### The movie

`make_movie.py WORKDIR --camera auto` animates a `place_route_loop` work dir:
an establishing overview, a zoom to the parts each round moved, and — when the
work moves to the other face — the board **turns over**, 180° about the vertical
axis, with every frame after that mirrored, because you are now looking at the
back.

13 components, 4 on the back, routed. `--tween 0` cuts to each new placement
instead of gliding.

![placement movie](../docs/placement-movie.gif)

One rule makes the sequencing legible: **a frame either moves the camera or
changes the board, never both.** Every transition happens over a frozen board and
is followed by a settle beat, so the moves only play once the camera has arrived.

## Module layout

| File | Purpose |
|------|---------|
| `quench.py` | The optimizer: cost terms, move generation, greedy quench |
| `../group_routing.py` | Block → net scoping, and the undo, for `route.py` (#459) |
| `fanout_clearance.py` | Post-fanout decoupling-cap clearance repair (#130) |
| `groups.py` | Placement blocks: which parts move as one rigid body (#459) |
| `lock_advisor.py` | Which parts should not be moved, and why (#431). Advice only |
| `placement_state.py` | Board-state gates: unplaced, and already-routed (#431) |
| `cli_gates.py` | argparse shared by both placement CLIs so they cannot drift |
| `../render_placement.py` | Headless PNG stills of placement status (#431) |
| `legality.py` | Hard constraints shared by both engines: board side, real Edge.Cuts containment, and the OO/OoB graders (#456) |
| `parser.py` | Courtyard boundary and locked-footprint extraction |
| `writer.py` | Writes new positions/rotations (rotates pad angles with the footprint, as KiCad stores pad angle = footprint + pad rotation) |
| `utility.py` | Shared utilities (bbox from pads, grid snapping) |

## Legality model (`legality.py`, #456)

What counts as a *legal* placement is decided in one place, so the optimizer and
any grader cannot disagree:

- **Board side.** A part occupies its own side with its full courtyard, and the
  opposite side only with the bounding box of its **drilled** pads. So a
  back-side decoupling cap may sit under a front-side BGA (they overlap in XY,
  not in copper), but not inside a front-side connector's pin field. Cross-side
  pairs also pay no halo penalty — spreading them apart buys no routing room.
  On a single-sided board every test reduces to plain courtyard-vs-courtyard.
- **Board containment** measures against the real Edge.Cuts rings, not an inset
  of the axis-aligned `board_bounds`, so parts are not nudged into an L-shaped
  board's notch or an interior cutout. Three levels of short-circuit keep it
  cheap: the gate self-disables when the outline *is* its bounding box (or when
  the parser found no usable ring, where the bbox is all there is), then a cached
  per-part reachable-disk prune, then the exact ring test.
- **Off-board seeds are not frozen.** Only candidate poses are validated, never
  the incumbent, so a part sitting outside the board had every alternative
  rejected and could never move — not even toward the board. A part whose only
  violation is board containment may now take a pose that moves it strictly back
  toward the board without overlapping anything.

  This is deliberately limited to the board term. An *overlapping* part keeps the
  original rule (it may move only to a fully legal pose), because the violation
  measure is a distance while the thing at stake is an area: trading one deep
  narrow overlap for a shallow wide one lowers the distance and raises the area.
  Measured on `watchy`, where 81 of 82 parts start in violation — its hand
  placement is tighter than the 0.25 mm courtyard clearance quench asks for — a
  permissive rule took total courtyard overlap from 9.1 mm² to 37.9 mm²
  (strict-decrease: 16.8) where the board-only rule gets 0.23 while also walking
  10 of the 13 off-board parts back on.
- **Graders.** `placement_overlap_area` (OO, mm²) and `placement_out_of_board`
  (OoB) report the same geometry the optimizer gates on;
  `QuenchState.legality_metrics()` returns both for a live placement. All zero
  means fully legal. Intended for the placement scorecard in #411/#110.

**Cost on two-sided boards.** Side-awareness removes a large number of false
collisions, so many candidate poses that used to be rejected outright now reach
the cost function. On `glasgow_revC` (172 front / 92 back parts) a bounded 40-part
pass makes **3.4× more airwire cost evaluations** than before (9.3k → 31.4k
`_count_crossings_np` calls) and takes correspondingly longer. Nothing per-call
got slower — the optimizer is searching the space it was previously, wrongly,
skipping. Small boards are unaffected or faster (`watchy`: 69 s against 109 s
before).

Courtyard extraction (`parser.py`) reads `fp_line`/`fp_rect`/`fp_arc`/
`fp_circle`/`fp_poly` per side. A footprint with no courtyard on any layer falls
back to its pad bounding box — which carries no courtyard margin at all, so the
part is modelled smaller than it is; that fallback now warns and names the refs.

Note: an earlier from-scratch constructive placer (`place.py` +
`rust_placer/`) was removed after experiments showed hand placements beat it
by ~500× in router effort; see git history and
docs/placement-optimization.md for details.
