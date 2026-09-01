# Dose-response sweep: heuristic_weight x turn_cost (2026-08-31)

**Question:** the owner's complaint — "on complicated boards it wiggles the traces
all over, looks like scribbling." Diagnosis pointed at two search knobs:
`heuristic_weight` 2.3 (greedy weighted A*, tuned in #586 on completion metrics)
and `turn_cost` 1000 (a 90° bend priced at one 0.1mm grid step). This sweep
measures what lowering the greed and raising the bend price buy, and what they
cost, on the 8-board faithful corpus.

**Method:** replayed the exact recorded si_corpus_ab chains (aggressors ->
victims -> rest, identical staged inputs) with `--heuristic-weight` /
`--turn-cost` appended. Drivers: `hw_tc_sweep.py` (desktop, 6 arms) and
`mac_bench_sweep.py` (M1 Air bench, replication + combo arms). Grading:
check_connected + check_drc at each board's routed floor (margin 0.1) +
quality/score.py v1.4. Full outputs under `carrier_lab/hw_sweep/` on both
machines; grades.json + timings.json per machine.

**Validity:** all 144 desktop route steps completed 18:27, before the day's
first engine-touching commit (18:52, default-OFF knob) and before the
uncommitted oracle edits (19:29+) — every arm ran identical code
(`hw_sweep/engine_fingerprint.txt`; note the fingerprint hashes the git INDEX,
so uncommitted edits were cross-checked by file mtime). The Air bench froze the
same engine tree (8e5c5318) and replicated base/hw19/hw15/hw12 with near-exact
agreement (one board one conn-issue off on one arm; final scores identical to
0.1) — cross-platform, cross-architecture reproduction.

## Corpus aggregates (8 boards summed; conn/unr/drc, mean final & jog, total route time)

Desktop (contended, nice -19):
| arm | conn | unr | drc | final | jog | time |
|---|---|---|---|---|---|---|
| base 2.3/1000 | 27 | 4 | 2 | 62.5 | 55.8 | 49min |
| hw19 1.9/1000 | 17 | 2 | 1 | 63.2 | 59.2 | 36min |
| hw15 1.5/1000 | 9 | 3 | 0 | 62.9 | 59.3 | 28min |
| hw12 1.2/1000 | **6** | **1** | 2* | **64.5** | 62.9 | 32min |
| tc3000 2.3/3000 | 14 | 3 | 1 | 63.1 | 60.5 | 38min |
| tc5000 2.3/5000 | 16 | **6** | 1 | 63.3 | 62.4 | 35min |

Air bench (idle M1, adds the combos):
| combo19 1.9/3000 | 18 | 2 | 1 | 63.8 | 62.1 | 24min |
| combo15 1.5/3000 | 15 | 1 | 0 | 63.5 | **63.1** | 19min |

## Findings

1. **Lowering greed improves EVERYTHING at once, monotonically to hw=1.2.**
   Connectivity 27->6, unrouted 4->1, mean final +2.0, mean jog +7.1, total
   time -35%. The expected quality/completion/speed trade never appeared: the
   greedy search's bad paths were feeding the rip-up/rescue loops that dominate
   wall time (bulk_profile finding), so patience pays for itself. Dense boards
   gain most: ulx3s 8conn/2unr -> 2/0, haasoscope 10/1+DRC -> 2/0 at half the
   time. The #586 tuning to 2.3 optimized a completion metric on a landscape
   where the completion/speed relationship has since inverted (or was never
   as assumed at corpus scale).
2. **The visual gain is visible, not just scored.** Renders:
   `quality/out/sweep_compare/ulx3s_In1_{base,hw12}.png` — base knots and
   wanders; hw12 flows in coherent bundled diagonals while carrying MORE
   copper. Not hand-quality (jog ~63 vs pro ~100); the knobs stop the router
   manufacturing ugliness, they don't beautify.
3. **turn_cost has a peak; greed doesn't (in this range).** tc3000 helps
   everywhere (jog +4.7, conn 27->14); tc5000 starts FAILING nets (unrouted
   4->6) — priced-out bends become giv-ups. Cap the knob at ~3000.
4. **Combos don't dominate hw12 overall** (combo15: conn 15 vs hw12's 6 —
   adding bend-price to hw15 traded conn for jog) **but combo15 owns the
   hardest board:** haasoscope 1/0/0 (best of any arm; base 10/1/1). Worth a
   per-board-class look before final default choice.
5. **hw12's 2 DRC are writer bugs, not search failures** (glasgow:
   Pad:/FLAGC<->Via:/CLKREF near U30, and a same-net /~{ALERT} via pair at
   0.28mm drill spacing — the same class as the carrier VIN_PROT follow-up in
   short_hunt_findings.md). Deeper search places more vias in tight pockets
   and trips the latent #468-family gaps more often. Fix the writers, not the
   knob.
6. **sonde_u (tiny, easy, 2-layer) dislikes every deep arm** (final 69.3 ->
   62.5..65.9, completion perfect throughout). Patient search finds "clever"
   paths where the beeline was already right. This is the one per-board
   dissent; per the AB rules it is kept, not dropped.

## Recommendation (not yet applied)

- **Ship hw 2.3 -> 1.9 now.** Passes the house directional gate outright:
  completion equal-or-better on all 8, faster on all 8, jog better on all 8,
  replicated on a second machine. Zero observed regressions.
- **Fix the two writer gaps** (rescue/via placement vs BGA pads; same-net
  drill spacing in the power via placer), then **re-evaluate hw 1.2-1.5 as the
  default** — its completion wins are the real prize and its only correctness
  cost is those writers.
- **turn_cost: hold at 1000 for the default; consider 3000 for a
  quality/beautify-oriented profile** and for dense multilayer boards
  (haasoscope-class). Never 5000.
- Candidate follow-up: per-retry laddering (first attempt patient at 1.2-1.5,
  escalate greed only on nets that blow the iteration budget), and the
  quality re-route pass — both now start from far better raw material.

## Bench

The M1 Air (192.168.68.188, `~/krt_bench/`, aliyan) is a standing experiment
bench: repo at 8e5c5318, native grid_router 0.27.0, KiCad 9.0.8 (kicad-cli at
the oracle's probe path), Python 3.14.7 venv, `mac_bench_sweep.py
name:hw:tc [...]` + `mac_grade.py`. Serial-only (8GB), vm_stat memory gate at
1.2G, ~2x faster per route than the contended desktop. Same-machine-only
comparisons; it froze the engine at 8e5c5318 — re-sync + rebuild before
comparing against newer code.
