---
name: plan-pcb-placement-and-routing
description: Plans a KiCad PCB end to end when ONE run must both place and route it. Sequences the placement skill and the routing skill, and owns the rules that exist only when the two meet: that placement invalidates every downstream routed board, that a routing failure must be classified before it is retried, and that a placement-shaped failure re-enters placement rather than burning retries in the router. Use when the board needs both; use the individual skills when it needs one.
---

# Plan PCB Placement and Routing

Use this when a single run must do both. It does not restate either half: it
sequences `/plan-pcb-placement` and `/plan-pcb-routing` and owns the four rules
that only exist when they meet.

<non_negotiable>
1. Placement runs FIRST and ONCE. Every routed board downstream of a placement
   change is stale -- re-run the chain from the placed board, never patch
   around it.
2. A routing failure is CLASSIFIED before it is retried: parameter-shaped,
   placement-shaped, or floorplan-shaped. Retrying a placement-shaped failure
   with router parameters is the most expensive mistake available here.
3. The placement half's acceptance gates still apply to a placement produced
   mid-chain. A board that reaches routing with a blocking assembly pair, a
   locked-part contact, or a rigid inconsistency is a board that will fail
   routing for reasons routing cannot fix.
4. Copper is not evidence about placement. A route that completed does not
   ratify the placement it ran on; a route that failed does not condemn it.
   Classify first.
</non_negotiable>

## The sequence

1. **Place.** Follow `/plan-pcb-placement` to its close-out. It ends with a
   board whose copper-free `check_drc` and `check_assembly` are clean, or with
   the residue NAMED and measured as unfixable.
2. **Freeze what the placement decided.** Lock the refs whose poses are
   decisions (mechanically fixed parts, anything a spec pins). A later step
   that moves them silently undoes the placement work.
3. **Route.** Follow `/plan-pcb-routing` from Step 1 on the placed board. Its
   Step 0 gate will pass, because you just did that work.
4. **On a routing failure, classify before retrying** (the routing skill's
   convergence section owns the classifier):

   | the diagnosis says | re-enter at |
   |---|---|
   | parameters (grid, rip-up depth, layer costs, width) | the failing ROUTING step |
   | congestion at a fine-pitch part's escape face | placement -- the face has no lanes, and no router setting adds any |
   | a part sits where its net cannot reach | placement |
   | a spec clause a different arrangement would satisfy | placement, with the clause stated as intent |

5. **Re-entering placement restarts the chain.** Go to 1, and discard every
   routed board produced from the old placement.

## When the loop is the tool

For congestion-shaped failures the repo has a loop that alternates the two
halves under one budget:

```bash
python3 -X utf8 place_route_loop.py board.kicad_pcb out.kicad_pcb \
    --rounds <N> --target-nets "<the nets the failure named>" \
    --accept-cmd "<the command that tells better from worse>"
```

Both flags matter: without `--target-nets` the loop moves parts unrelated to
the failure, and without `--accept-cmd` its comparator cannot see the thing you
are trying to fix (a length, a width, a clause). An ACCEPTED round is not a
verdict -- grade the output with the same battery either skill would use.

<agent_identity>
You run a board end to end. You place first and once, you classify every
routing failure before retrying it, and you send placement-shaped failures back
to placement instead of spending router retries on them.
</agent_identity>
