# What predicts routed `blocking` — the first measurement (#703)

**Status: MEASURED, on 4 boards of a declared 6. No predictor here is "validated"
in a strong sense; the acceptance rule's own false-positive rate at N=4 is
**9.0%–18.0%** (median 11.5%) over the 21 predictors defined on all four
boards, measured, and that number belongs beside every PASSES below.**

Every correlation number this repo quoted before #703 was measured against
*distance-to-the-correct-placement* or against *the gap a human left*.
`r(crossings) = +0.780` is 29 candidates on ONE board against distance-to-truth;
the corridor law's `r = +0.41 … +0.90` is against the human's gap. CLAUDE.md's
*"What a placement run is FOR"* says the headline is routed `blocking`, and no
predictor had ever been correlated with it.

This is that measurement. Nothing in it is a proxy for a proxy: 80 placements
were generated, each routed once with an argv frozen before any variant existed,
and each graded by `board_score`.

> **Reproduce any of it.** The rows live in `tests/stress/predictor_rows.json`.
> Every statistic re-derives with no routing at all:
> ```bash
> python3 -X utf8 tests/stress/predictor_study.py --from-rows tests/stress/predictor_rows.json
> python3 -X utf8 tests/stress/predictor_study.py --from-rows tests/stress/predictor_rows.json --shuffle-control 200
> ```
> Regenerating one placement and re-measuring its predictors costs seconds
> (`--verify-row esp_prog:portfolio-3`); re-routing it costs one route.

*(This file is deliberately NOT in `test_431_skill_commands.py`'s `SOURCES`. Its
commands invoke `tests/stress/*.py`, which `krt_capabilities._tool_path` cannot
resolve — it searches only `''`, `py_router`, `py_tools`, `py_placer` — so
adding it would turn a green gate red for a reason unrelated to skills.)*

---

## The headline

**The predictors that rank routed `blocking` are the LEGALITY counts. The
classical placement proxies — `crossings`, `hpwl`, `halo`, `overlap_area` — do
not.**

| predictor | boards right / wrong | median ρ | verdict |
|---|---|---|---|
| `pad_shortfall` | 4 / 0 | +0.725 | **passes** |
| `pad_intersection_pairs` | 4 / 0 | +0.724 | **passes** |
| `pad_overlap_pairs` | 4 / 0 | +0.724 | **passes** |
| `body_overlap_pairs` | 4 / 0 | +0.724 | **passes** |
| `pad_conflict_pairs` | 4 / 0 | +0.724 | **passes** |
| `pad_clearance_pairs` | 4 / 0 | +0.724 | **passes** |
| `courtyard_blocking_pairs` | 4 / 0 | +0.652 | **passes** |
| `pad_copper` (off-outline pad copper) | 4 / 0 | +0.615 | **passes** |
| `hole_shortfall` | 3 / 0 | +0.422 | **passes** |
| `hole_conflicts` | 3 / 0 | +0.412 | **passes** |
| `oob_amount` / `oob_area` / `oob_count` | 3 / 1 | +0.45…+0.48 | fails |
| `halo` | 3 / 1 | +0.409 | fails |
| **`crossings`** | **3 / 1** | **+0.396** | **fails** |
| `overlap_area` | 3 / 1 | +0.372 | fails |
| `length` | 3 / 1 | +0.239 | fails |
| **`hpwl`** | **2 / 2** | **+0.158** | **fails** |
| `edge` | 2 / 2 | −0.031 | fails |
| `cross_side_stacks` | 0 / 1 | — | **no verdict** (defined on 1 board) |
| `align`, `corridor_cut`, `orient`, `locked_contact_pairs` | 0 / 0 | — | **no verdict** (constant everywhere) |

The rule is `test_placement_ab.gate()`'s, transposed from marks to signs: right
direction on ≥ N−1 boards, wrong direction on none, over the boards that
produced a *defined* ρ. Spearman is computed **within each board** and never
pooled.

### Three things this says that the repo did not know

**1. `pad_copper` — the one pre-route number that already refuses — is
validated.** `loop_driver.py`'s L2 gate blocks the first route on
`checklist.a_off_outline.pad_copper`, and it is the only pre-route quantity in
this repo that refuses anything. It ranks `blocking` positively on 4 of 4 boards
(ρ +0.48 / +0.68 / +0.55 / +0.73). The gate was right, and now it is measured.

**2. `hpwl` — which the drivers DO gate on — is the worst of the classical
trio.** 2 boards right, 2 wrong, median +0.158, two-sided p = 1.0:

| board | ρ(hpwl, blocking) |
|---|---|
| esp_prog | **−0.525** [LOO −0.648…−0.443, K=20] |
| splitflap_driver | +0.739 [LOO +0.683…+0.769, K=19] |
| tigard | **−0.045** [LOO −0.235…+0.039, K=19] |
| watchy | +0.360 [LOO +0.184…+0.472, K=13] |

The reasoning behind gating on it — *"hpwl's minimum is at the truth, so it is
the one that can carry a gate"* — is about distance-to-truth and remains
untouched. What is new is that its relationship to the routed outcome is a coin
flip across boards. **This does not license removing that gate**; it means the
gate is justified by the distance argument alone, and nobody should describe it
as a routability gate.

**3. `crossings` fails, and its sign flips.** ρ = −0.179 on esp_prog and
+0.56 / +0.65 / +0.23 elsewhere. More crossings went with *less* blocking on one
of four boards.

---

## The circularity control changed the answer for `crossings`

The realistic-end sampler is `portfolio.generate`, whose quench **minimises
crossings and hpwl**. Those rows carry `generator: portfolio_quench`, and every
statistic is computed twice:

| predictor | quench rows INCLUDED | quench rows EXCLUDED |
|---|---|---|
| `pad_copper` | 4/0, +0.615, **passes** | 4/0, +0.735, **passes** |
| `pad_clearance_pairs` | 4/0, +0.724, **passes** | 4/0, +0.885, **passes** |
| **`crossings`** | **3/1, +0.396, fails** | **4/0, +0.479, passes** |
| `halo` | 3/1, +0.409, fails | 4/0, +0.932, passes |
| `overlap_area` | 3/1, +0.372, fails | 4/0, +0.965, passes |
| `hpwl` | 2/2, +0.158, fails | 3/1, +0.536, fails |

**Whether `crossings` passes depends on whether the sample includes placements
made by an optimizer that minimises crossings.** That is the circle #703 exists
to break, and it is why neither arm is reported as *the* answer for it. The
legality predictors pass in **both** arms, which is what makes them the finding.

---

## How much to believe a PASSES

Permuting the truth **within each board** and re-running the whole aggregation
200 times (`--shuffle-control 200`) measures the acceptance rule's own
false-positive rate:

```
halo                          18.0%
courtyard_overlap_mm2         17.0%
hpwl                          15.5%
length                        15.0%
crossings                     14.5%
...
cross_side_stacks              0.0%   <- defined on ONE board; see below
```

Full sweep over the 21 predictors defined on all four boards: **min 9.0%, max
18.0%, median 11.5%**.

Two consequences, both load-bearing:

- **A predictor defined on one board used to pass the rule every single time.**
  Before the fix below, `cross_side_stacks` — constant on three of four boards,
  so defined on one — passed **100%** of these shuffles, because
  `consistent >= max(1, N-1)` is `1 >= 1` at N=1. `rank_stats.MIN_SIGN_BOARDS`
  is now 3, the same value and the same reason as
  `test_placement_ab.MIN_TRIAL_BOARDS`, and such a predictor reports **NO
  VERDICT** instead. Its rate in the block above is 0.0% *because that guard is
  in place* — the 100% is what the control measured before it existed, and it
  is why it exists.
- **At N=4 the rule's empirical false-positive rate is 9–18%**, not the 0.125
  the two-sided p-value suggests. Ten predictors passing is therefore *evidence*
  and not proof; the six that share a median of +0.724 are also plainly
  measuring one underlying quantity, so they are not ten independent findings.

The control permutes truth over the same deduplicated sample the ρ values use.
It did not at first — watchy entered the null with its six duplicate placements
still in, at K=19 against the K=13 its ρ was computed on — and correcting that
moved individual rates by up to 5.5 points in both directions.

---

## The boards and what was actually run

| board | K | classification | `blocking` values observed | excluded |
|---|---|---|---|---|
| esp_prog | 20 | measurable | 0, 2, 3, 6, 14, 32, 466 | — |
| splitflap_driver | 20 (19 ranked) | measurable | 0, 12, 29, 47, 55, 65 | `perturb-pile` (route timed out at 2400 s) |
| tigard | 20 (19 ranked) | measurable | 0, 1, 2, 64, 71, 73, 114, 158 | `perturb-pile` (route timed out at 2400 s) |
| watchy | 20 (13 ranked) | measurable | 1, 3, 5, 44, 94 | `perturb-pile` (timeout) + **6 duplicate placements** |

Every board is git-tracked, so every row is regenerable from this repo.
`portfolio.generate(..., only=i)` is byte-identical by contract, the route argv
is frozen per board in `ARGV.json`, and each row carries its
`poses_sha256`, `input_board_sha` and `argv_sha`.

**What is NOT in this run, said plainly:**

- **The declared table is 6 boards; 4 were run.** `sonde_u` and
  `kit-dev-coldfire-xilinx_5213` are declared in `STUDY_BOARDS` and were not
  routed. Every p-value and every N above is over the 4 that were — never over
  the planned count. `predictor_study.py` prints that denominator on the same
  line as the p-value for exactly this reason.
- **`perturb-pile` timed out on 3 of 4 boards** at a 2400 s route budget. Piling
  every free part onto one coordinate produces a board the router cannot finish,
  which is a true fact about the damage and also a systematic exclusion of the
  most-damaged sample from three boards.
- **watchy lost 6 of 20 slots to duplicate placements.** Its `translate` and
  `scatter` blocks have essentially no feasible travel on that outline, so those
  variants reproduced the authored board exactly. The duplicate guard collapsed
  them to one sample and named every drop. Its effective K is 13.

---

## Pre-registered decision rules

*Recorded 2026-08-28, before the sign test over the declared board set is
complete. Any later change to a rank key or a gate should cite the rule it is
discharging, not a number chosen afterwards.*

1. **`portfolio.rank_key` leads with `crossings`, and `rule1_check` bars a
   candidate on it, while `loop_driver.py` and the placement skill say in
   capitals never to gate on crossings.** Both cannot be right. **Neither is
   changed by this study**: `crossings` passes in one arm and fails in the
   other, so the evidence does not license a reorder in either direction. The
   contradiction is disclosed at both code sites and stays disclosed.
2. If a future run finds ≥ 1 board on which a rule-1 violator routed to strictly
   lower `blocking` than the baseline, the `crossings` clause of `rule1_check`
   is withdrawn — and the withdrawal keeps its row, with its measured direction,
   in the `rejected` style `test_placement_ab.py` uses.
3. A predictor is reported as ranking `blocking` only on ≥ N−1 boards right and
   none wrong, over boards with a *defined* ρ, with N ≥ 3 and the shuffle-control
   rate printed beside it.
4. Saturated and starved boards are reported with their constant value and
   excluded from the denominator. They are never dropped.

## What this does not claim

- Not that the passing predictors *cause* anything. They rank an outcome on four
  boards.
- Not that `crossings` or `hpwl` are useless. Both are measured against
  distance-to-truth, both retain the roles that measurement supports, and the
  drivers' existing prohibition on gating `crossings` is untouched.
- Not that the legality family is ten findings. Six of them share a median to
  three decimals; they are one quantity seen through six counters.
- Not a corpus-wide result. Four boards, one machine, one router build. Each
  row records `provenance.measured_git` (`v0.21.3-199-g3b8edc19`), from which
  the router version follows; there is no explicit router-version or machine
  field on a row, and this document previously claimed there was.
