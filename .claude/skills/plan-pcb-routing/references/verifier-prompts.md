# Verifier prompts

Fan these out **in one response**, each handed only its slice. **Never hand a
verifier the raw `.kicad_pcb`** — it is 100k+ lines of s-expressions and the
verifier will skim it and guess.

Every verifier ends with exactly one line:

```
VERDICT=PASS:lens=<lens>
VERDICT=FAIL:lens=<lens>;finding=<one line>;evidence=<path#json-pointer|path@x,y>;route=<step>
```

`evidence=` must point into the round's own files — `wk/place.log#JSON_SUMMARY.
crossings_after`, `wk/intent.json#/violations/3`, `wk/view/board_F.png@112.4,63.1`.
**A verifier that cannot fill it has not verified anything.**

`VERDICT=`, not `RESULT=`: the GUI parses the last `RESULT=` line in a reply as
the plan JSON.

## The round bundle

Build once per turn, then slice it:

```
wk/turn<N>/
  locks.json  locks.log          # A -- lock advisor
  place.log                      # B -- place_optimize JSON_SUMMARY (tee'd)
  view/*.png  view.log           # C -- renders + their --json
  loop_round*.json               # D -- loop sidecars
  intent.json  intent_result.json  # E -- check_floorplan
  route.log  drc.txt  conn.txt   # F -- routing + the authoritative checkers
  list_groups.txt                # G
```

## The six lenses

**1. `intent`** — given `intent.json`, `intent_result.json`, the front panel.
> Does this board honour the declared floorplan? Report every `violations[]`
> entry with its `measured` vs `expected`. Check `rules_run` and
> `rules_skipped`: if `rules_run` is 0 this is a vacuous pass and you must FAIL
> it. You may not conclude anything about DRC or connectivity.

**2. `legality`** — given `view.log`'s JSON, both side panels.
> Did legality regress? Compare `overlap_area` and `oob_count`/`oob_amount`
> against the seed's. Absolute zero is NOT the test — `watchy` legitimately
> ships 81 of 82 parts in courtyard violation. **Never use `oob_area`**: it is
> measured against a bounding-box inset, so a part inside a cutout scores 0.0.
> You may not conclude that a specific pair collides — that is `check_drc`'s job.

**3. `delta`** — given `place.log`'s JSON, `locks.json`, the loop sidecars'
`moved[]`, and the delta render.
> Did the numbers improve, and did anything move that should not have?
> `crossings_after <= crossings_before` AND `hpwl_after <= hpwl_before`, or the
> result is discarded. Then intersect `moved[].reference` with the advisor's
> high-confidence findings and with `locked_refs`: any overlap is a FAIL.
> **Do not judge by how much moved** — "lots moved, looks broken" and "barely
> moved, looks safe" are both wrong.

**4. `blocks`** — given `list_groups.txt`, the `blocks`/`block_parts` keys, any
zoom panels.
> Is the block set the right scope, and did grouping do anything at all?
> `blocks: 0` with a `--group-by` passed means the source found nothing. Check
> that no routing command silently acquired a `--group` the user did not ask for.

**5. `routing-feedback`** — given `route.log`'s JSON, the loop sidecars'
metrics, the `--focus` panels.
> Are these failures floorplan-shaped, placement-detail-shaped, or
> parameter-shaped? Use `blockers` (empty ⇒ nothing to move ⇒ not placement),
> the spatial clustering of the focus panels, and the pin counts of the failing
> refs. **The default answer is parameters.** You may not conclude that a
> specific move will fix it.

**6. `coverage`** — given the Step 5b net ledger, the route `--nets` list, the
plane nets, and any placement `--ignore-nets`.
> Is every routable net claimed exactly once? Does `--ignore-nets` equal the
> plane-net set? A placement step claims no nets and must not appear in the
> partition.

## Adversarial completeness

On anything high-stakes, add one more whose only job is to disprove the result:

> Actively try to PROVE this placement is not ready to route. Find a part that
> moved and should not have, a metric that regressed, an intent rule that did
> not run, a render being used as evidence for something only a number can
> establish, or a claim in the report with no `evidence=` behind it. **Report
> the single most damning finding.**

## Resolving disagreement

- `route=` names where the finding goes back to, so routing is mechanical.
- The gate is met when every lens PASSES **or every finding is dispositioned in
  writing** — a disposition lives in the report, not in your head.
- A machine check failing outranks any verifier's judgement.
- If two sources disagree, believe the JSON.
