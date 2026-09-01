# Oracle finalize — measured, and the requested early-exit already exists

**Verdict: DO NOT LAND a code change.** The Phase-2 early-exit ("stop the oracle
recheck loop when a round makes no progress") is **already implemented in HEAD**
(`kicad_oracle.py` line 2954: `if not progress: break`). The remaining waste it
could have cut — carrier's round-3 — is the *final* round, so no early-exit can
remove it, and it costs ~2 s (~0.4% of the step). Every alternative early-exit
rule that would fire on real boards is either provably dead or provably unsafe.

## 1. What was measured (Phase 1)

Carrier chain run exercising plane finalize (#562), per-line wall timestamps
(`/tmp/oracle_p2/step6.log`, 502 s total):

| phase | wall | share of step |
|-------|------|---------------|
| engine leg | 29.9 s | ~6.0% |
| cleanup leg | 1.4 s | ~0.3% |
| **oracle leg** | **33.2 s** | **~6.6%** |
| final reconciliation | ~11 s | ~2.2% |

Oracle leg internals (rounds 23→17→17):

| round | wall | links | outcome |
|-------|------|-------|---------|
| R1 | ~16 s | 23 | productive (11 welds) |
| R2 | ~8.7 s | 17 | productive (1 weld) |
| R3 | ~2.1 s | 17 | **zero progress** — all dead branches |

R3's ~2.1 s is pure waste: 32 dead-branch log lines (already-attempted /
coincident-skip / one DECLINED raw retry) plus the for-else final-count fetch.
That is **~0.4% of the whole step**. The oracle leg is material (~6.6%) but its
cost is dominated by the *productive* rounds (pcbnew refills + weld routing), not
by the zero-progress tail.

## 2. The requested early-exit already exists

`kicad_oracle.py` line 2954, inside the round loop:

```python
if not progress:
    break
```

`progress` is set True only when a link actually emits copper (or deletes a
stranded fragment). A round that welds nothing breaks immediately — no extra
refill, no extra fetch. This is exactly the Phase-2 "early-exit when a round
makes no progress" ask, and it is already in HEAD.

## 3. Why the remaining R3 waste cannot be cut safely

Carrier's R3 is the **last** round (max_rounds=3, all callers use the default).
An early-exit fires *before* a round; there is no round after R3 to skip. The
only way to cut R3 itself is to skip it *before running it*, which requires
predicting it will make no progress — and every such predictor is either dead or
unsafe:

- **Exhaustion-based** (skip when every reported link's key has `attempted >= 2`):
  provably **never fires** on any corpus board. Simulated over every oracle log in
  `carrier_lab/**/*.log` (`/tmp/sim_exact.py`): Rule A (all keys >=2) and Rule B
  (all keys dead) both fired **0 times**. Reason: with max_rounds=3 a key needs >=2
  prior appearances to reach attempt>=2, so the earliest possible fire is round 3 —
  the last round — and every real round-3 has fresh keys first seen in round-2
  (`/tmp/sim_r3.py`: 13–33 fresh keys per round-3 across the corpus).
- **Count/set-based** (skip when the reported link set is unchanged): **unsafe**.
  A flat-count comparison welds productive copper — keys first seen one report
  earlier legitimately get their force_raw retry in the next round (carrier R2
  welded exactly this way: `(99.22,25.50)` OK after a coincident-skip in R1).
- **Cluster-gap dedup** (skip re-processing a work item whose cluster-mode rebound
  true gap was already processed this round with no copper landed since): would
  fire on carrier R1 (~11 duplicate `(99.22,25.50)` cluster-mode runs ≈ ~0.7 s)
  but is output-preserving only under a "no copper landed since" guard that must
  be re-derived per item, and its saving (~0.7% of step) is below the bar set by
  prior findings (C5: ~14 s / ~40% of window).

## 4. Corpus check — where the oracle is actually expensive

| board | oracle leg | rounds | R3 productive? |
|-------|-----------|--------|----------------|
| carrier | 33.2 s | 23→17→17 | no (~2 s waste) |
| carrier_fix | 18.6 s | 22→17 | n/a (2 rounds) |
| kitdev | **53.6 s** | 15→9→14 | **yes** (5 welds + debris deletion) |
| sonde_u | 0.8 s | 0 | n/a |

kitdev's oracle leg is the largest finalize component measured (53.6 s), and its
round-3 is *productive* — an early-exit there would **lose copper**. This is the
decisive counter-example: the zero-progress tail is not a general property of the
oracle loop; it is specific to boards whose stubborn links are already exhausted.

## 5. Conclusion

- The Phase-2 early-exit is already in HEAD; nothing to add.
- Carrier's zero-progress R3 costs ~2 s (~0.4% of step) and is structurally
  uncuttable by any early-exit (it is the final round).
- All alternative early-exit rules are dead code or unsafe on real boards.
- kitdev shows the oracle leg can be 53.6 s with a *productive* final round —
  the real optimization target for that board is the per-round refill/weld cost,
  not loop termination.
- No code change shipped. This document records the measurement and the negative
  result so the next investigation does not re-derive it.

## Appendix: artifacts

- `/tmp/oracle_p2/step6.log` — carrier measurement (timestamps, JSON_ORACLE)
- `/tmp/sim_exact.py`, `/tmp/sim_r3.py` — corpus simulations (Rule A/B = 0 fires)
- `/tmp/oracle_direct_harness.py` — direct oracle harness mirroring route.py's
  finalize leg (used for the ON/OFF gate runs)
- `/tmp/oracle_gate/{off,on}/` — gate outputs (identical JSON_ORACLE; time delta
  was run-to-run variance, early-exit never fired)
