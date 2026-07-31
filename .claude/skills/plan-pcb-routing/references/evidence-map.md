# Evidence map: produce X → read key Y → do Z

Every artifact the placement tools emit, the exact JSON key to read from it, and
the decision that key drives. Read this before quoting any number.

**The governing rule: never read a picture on its own.** Every render is paired
with a number that either confirms or contradicts it, and the number wins.

Keys are literal. If a key below is not in the output you are looking at, you are
looking at the wrong artifact — do not substitute a similar-looking one.

---

## A. Lock advisor — `place_optimize.py BOARD --suggest-locks --suggest-locks-json wk/locks.json`

Writes no board. Run it **before** the first placement run, and read the reasons.

| key | decision |
|---|---|
| `JSON_SUMMARY.unlocked_high` | **The gate.** Must be `0` before placement runs. Re-run with your `--lock` list until it is, or name each finding you are leaving free and why |
| `findings[].evidence.npth_pads > 0` and `plated_pads == 0` | A structural mounting hole. It has **no net, so no airwire at all** — nothing but the halo term decides where the optimizer slides it. Always lock |
| `findings[].evidence.outside_amount_mm > 0` | The body legitimately leaves the outline (card edge, USB shell, HAT header). Lock it **and** add it to the intent's `edge_connectors` |
| `findings[].evidence.edge_clearance_mm` | Measured, exact. Use it as the intent's own limit rather than inventing one |
| `findings[].evidence.connected_pins` | `0` → invisible to the cost. `>= 40` → see `advisories` |
| `findings[].evidence.side` | Which face — decides whether the verify render needs `--per-side` |
| `advisories[].kind == "high_pin_count"` | `place_optimize` has **no** `--max-target-pins` guard (`place_route_loop` does). Either lock these or use the loop. Never let a ≥40-pin part move in a bare `place_optimize` run |
| `lock_argv` | Paste verbatim — it is already argv-shaped. Do not retype `lock_patterns` by hand |
| `locked_refs` | Refs the FILE already pins. Advice telling the user to move one of these is wrong |
| `findings_covered` vs `findings_high` | Round-trip proof your `--lock` list actually matched, rather than matching nothing |

A **quiet result is "nothing detected", not "nothing to lock"** — the lexical
rules (footprint name, reference prefix) miss house libraries entirely.

---

## B. `place_optimize.py IN OUT ... 2>&1 | tee wk/place.log`

The `JSON_SUMMARY` goes to **stdout only**; `tee` is what makes it citable.

| key | decision |
|---|---|
| `parts_moved` | `0` → nothing happened. Do not render a delta or claim improvement; widen `--max-displacement` or narrow `--lock` |
| `crossings_before` / `crossings_after` | **Discard gate.** Any increase → route from the original board |
| `hpwl_before` / `hpwl_after` | **Discard gate.** Same |
| `overlap_area` | Courtyard overlap of the output. Must not increase. **Absolute zero is the wrong test** — `watchy` ships 81 of 82 parts in courtyard violation and is a legitimately placed board |
| `oob_count` | Parts leaving the outline. Must not increase, and every one should be in the intent's `edge_connectors` |
| `oob_amount` | Severity. Count flat but amount up = an already-overhanging part got pushed further out |
| `oob_area` | **Do not gate on this.** Measured against the bounding-box inset, so a part sitting entirely inside a **cutout** scores `0.0` |
| `cost_before` / `cost_after`, `airwire_length_*` | **Weighted.** Comparable only within one run. A big `cost` drop with flat crossings/hpwl means halo and edge moved: cosmetic spreading, not routability. Do not report it as an improvement |
| `blocks` / `block_parts` | Proof `--group-by` did anything. `blocks: 0` → drop the flag |

---

## C. `render_placement.py ... --json -o wk/view/`

Always pass `-o`. Without it the tool writes `<board>_placement.png` **next to
the board**.

| key | decision |
|---|---|
| `metrics.crossings`, `metrics.hpwl` | **The re-measurement channel.** Run this on the *written output board*; it must reproduce B's `*_after`. A mismatch means something was lost between the objective and the file — this is the self-assertion ban with teeth |
| `metrics.overlap_area`, `metrics.oob_*` | Independent read of B's legality numbers off the file |
| `metrics.halo`, `metrics.edge` | Cost decomposition. `total` improved while `length` and `crossings` did not ⇒ the win was halo/edge only ⇒ not a routability win |
| `no_outline: true` | The oob metrics are **unavailable**, not zero. Say "unavailable" — never "0 parts off board" |
| `unplaced: true` | The placement CLIs would exit 3. Report and stop |
| `moved` | Cross-check against B's `parts_moved`. A mismatch means you rendered the wrong `--before`/board pair |
| `failed_nets`, `blocker_nets` | Which nets the picture colours. Non-empty only when `--summary-json` was given — this is what ties a render to a specific route log |
| `panels[].path` | The exact PNG to `Read`, and what goes in a verifier's `evidence=` |
| `panels[].view` | The world rect that panel covers. **A finding at a coordinate outside it was not visible in that panel** — reject the claim |

---

## D. `wk/loop_round{N}.json` — the `place_route_loop` sidecars

Nobody reads these today, and they are where the loop's actual history lives.
`place_route_loop` prints **no** `JSON_SUMMARY` for a normal run.

| key | decision |
|---|---|
| `accepted` | Only accepted rounds are progress. Never quote a rejected round's board as the result |
| `parent` | **The board this round derived from — the last ACCEPTED board, not N−1.** Use it as `--before`. Using N−1 renders a delta that never existed |
| `screened` | `true` → the ratsnest screen skipped the routing run and `metrics` is empty. Never report failures for a screened round |
| `metrics.failures` | **The ratchet.** Unchanged across two consecutive non-accepted rounds → floorplan-limited → stop, do not raise `--rounds` |
| `metrics.failed_nets` | Feed to `--ratsnest-nets` and to `/diagnose-routing-failures` |
| `metrics.blockers` | The other half of the move-candidate set. **Empty blockers with non-empty failures means there is nothing to move** — not a placement problem |
| `metrics.iterations` | `better()` requires `< best × 0.95`, so a round improving effort by under 5% at equal failures is **rejected by construction**. That is the tool working, not evidence the placement is wrong |
| `metrics.vias` | The cost side of the trade. Report it; do not hide a failures win paid for in vias |
| `metrics.pad_pairs_connected` / `_total` | A round that lowers `failures` while lowering the connected ratio is not progress |
| `metrics.ratsnest_crossings` / `_hpwl` | The proxy beside the router's verdict. Proxy improving while `failures` is flat ⇒ proxy exhausted, stop |
| `metrics.ratsnest_length` | **Weighted, report-only.** Never compare across rounds |
| `targets` | Which refs the round was allowed to move. Any `must_lock` ref here → tighten `--lock` and re-run |
| `groups` | What blocks pulled extra parts in. A 40-part block here means the round moved far more than you targeted |
| `moved[].reference` / `.from` / `.to` | Exact per-part deltas. Intersect with the lock advisor's high-confidence findings; a non-empty intersection is a blocker, not a note |

---

## E. `check_floorplan.py BOARD --intent ... [--health]`

| key | decision |
|---|---|
| exit code | `0` clean · `2` argparse or malformed intent · `3` board state / untrustworthy outline · `4` **violations found** |
| `pass` | The gate |
| `violations_by_rule` | Which rule fired, and how often — names the thing to fix |
| `violations[].measured` vs `.expected` | The falsifiable number. This is what goes in a verifier's `evidence=` |
| `rules_run` / `rules_skipped` | **Anti-vacuity.** `0 violations` with `rules_run: 0` means nothing was checked. Quote both |
| `blocks_resolved` vs `blocks` | A block resolving to nothing is reported as an error, but check this too |
| `state_*` | `duplicate_fraction`, `spread_ratio`, `outside_fraction`, `partially_unplaced` — the signals behind the exit-3 verdict, and the only place they are emitted at all. `partially_unplaced` is the case exit 3 **hides**: a netlist re-import dropped a few new parts at the origin on an otherwise-placed board, and "place these specific refs" is the right instruction |
| `outline.cutouts` / `.edge_contours` / `.simple_rectangle` | What the parts must avoid. `edge_contours` are clearance-bearing and **invisible in every render** |
| `health_block_displacement_max_mm`, `health_blocks_displaced` | How far a block sits from what it connects to. The 80 mm-magnetics failure mode a nudge cannot fix |
| `health_bus_foreign_crossings` | What crosses a declared bus corridor |
| `health_signals_skipped` | Signals that did not run, each with a reason. `blocked_cell_share` always needs a route first |

---

## F. Routing summary — `route.py`'s `JSON_SUMMARY`, in `wk/route.log`

The feedback edge back into placement.

| key | decision |
|---|---|
| `failed_single`, `failed_multipoint[].net_name` | The failure set → Step 9's classifier, and `--summary-json` for the render |
| `blockers[].blocked_by[].net` | Which routed nets wall off each failure → the move-anchor set |
| `pad_pairs_connected` / `pad_pairs_total` | The honest completion number for comparing two placements |
| `total_iterations` | Effort. A placement that halves iterations at equal completion is a real win |
| `total_vias` | The cost side of the same trade |

---

## G. Blocks

| output | decision |
|---|---|
| `route.py --list-groups`: `parts=`, `touching=`, `internal=` | The routing-scope decision. `internal ≈ 0` ⇒ only `touching` is meaningful (always true of `decap`) |
| `render_placement.py --list-groups`: `parts=`, `front=`, `back=` | A block with parts on both faces cannot be reviewed in one panel |
