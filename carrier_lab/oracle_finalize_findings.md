# Oracle finalize — WHERE time goes, and the parse-reuse fix

**Verdict: LAND the parse-reuse fix.** The owner's directive supersedes the
earlier "DO NOT LAND" finding: the oracle recheck loop IS the slow part of the
pour-verification tail, and the no-progress round is provably wasted work. This
document records (1) where time goes inside a round, (2) why the set-identity
early-exit the owner proposed is unsafe on real boards, (3) the safe fix that
was landed, and (4) its measured equivalence + timing.

## 1. WHERE time goes inside a round (Phase 1, measured)

Carrier prefinalize board, direct harness mirroring route.py's finalize oracle
leg (`/tmp/oracle_direct_harness.py`), cProfile + per-phase instrumentation:

| phase | calls | time | share |
|---|---|---|---|
| fetch (`exact_unconnected`: refill + cluster + parse) | 2 | 8.9 s | 52% |
| obstacles (`build_base_obstacles`) | 6 | 2.4 s | 14% |
| parse (`parse_kicad_pcb`) | 5 | 1.7 s | 10% |
| routing (`route_plane_connection_wide`) | 98 | 1.6 s | 9% |
| debris (`_delete_stranded`) | 12 | 1.0 s | 6% |
| track (`_net_track_components`) | 583 | 0.8 s | 5% |
| exact_tier | 24 | 0.5 s | 3% |
| cluster (`_cluster_points`) | 1218 | 0.1 s | <1% |

**The loop IS the slow part — dominated by re-derivation (fetch + obstacles +
parse = 76%), NOT repair routing (9%).** The owner's instinct was right.

`exact_unconnected` internals (same run): refill (pcbnew subprocess) 5.6 s /
57%, `exact_clusters` 2.4 s / 24%, parse 1.7 s / 17%, nearest_pair 0.2 s / 2%.
The refill memo already dedups identical-file refills (1 miss + hits); the
parse + clustering are NOT memoized.

## 2. Set-identity vs count (owner's question)

Consecutive rounds' link sets genuinely differ on every corpus board:

| board | R1->R2 overlap | R2->R3 overlap |
|---|---|---|
| carrier | 3/13 vs 26 (23 new) | 2/26 vs 31 (29 new, 24 dropped) |
| kitdev | — | 4/9 vs 15 (11 new) |
| tc5000 | 6/21 vs 17 (11 new) | 9/17 vs 13 (4 new) |

**Count equality would NOT mask a different set here — sets genuinely differ
each round.** Cluster-mode rebinding converges distinct original links onto
shared true gaps, and new links appear as copper lands. So a set-identity
early-exit would NOT fire on these boards either.

## 3. Why set-identity early-exit is UNSAFE

Even if a repeated set were detected, skipping it would **lose copper**:

- **force_raw retries succeed.** The attempt ladder gives a key first seen one
  report earlier its force_raw retry in the next round; measured across corpus
  logs, those retries land OK (kitdev: 1-3 retry->OK per log).
- **Late rounds carry fresh keys.** R3 on kitdev has fresh keys at attempt<2
  that get real attempts (6 welds in R3). tc5000 R3 welds 1.
- **The exhaustion rule never fires.** Simulated over every oracle log
  (`/tmp/sim_exact.py`): Rule A (all keys >=2) and Rule B (all dead) both
  fired **0 times**. With max_rounds=3 a key needs >=2 prior appearances to
  reach attempt>=2, so the earliest possible fire is round 3 — the last round.

So there is no safe early-exit that fires on real data. The waste must be cut
by making the re-derivation cheaper, not by skipping rounds.

## 4. The provable waste: double parse per round

The round loop parsed the board TWICE per round:
- `exact_unconnected(board_file, ...)` internally parses (kicad_exact_fill
  line 819)
- then `pcb_data = parse_kicad_pcb(board_file)` parses again (line 1802)

Plus the for-else final-count fetch parses a THIRD time. `exact_unconnected`
accepts a `pcb_data` param documented to accept a pre-parsed board; the file
cannot change between the fetch and the round's parse (no copper lands until
the link loop), so the clustering sees identical input.

## 5. The fix (landed)

`kicad_oracle.py`: hoist the round's `pcb_data` parse to before the fetch,
pass it to `exact_unconnected`, and reuse it for the round body. Gated by
`KICAD_ORACLE_REUSE_PARSE` (default ON; `0` restores old behavior for A/B).
In LEGACY_ORACLE mode the extra parse is skipped entirely (the fetch is
kicad-cli, which needs no pcb_data).

Measured effect (carrier prefinalize, direct harness):

| metric | OFF | ON | delta |
|---|---|---|---|
| parse calls | 5 | 3 | -2 |
| parse time | 1.66 s | 1.00 s | -0.66 s |
| fetch time | 8.89 s | 8.22 s | -0.67 s |
| oracle leg | 16.0 s | 15.3 s | -0.7 s (~4%) |

## 6. Equivalence gate (PASSED)

ON vs OFF on the direct harness, same input board:

- **JSON_ORACLE byte-identical** (rounds, links_routed, links_failed,
  remaining, remaining_links, cross_board, removed_segments/vias all equal).
- **check_connected**: both "ALL NETS FULLY CONNECTED".
- **check_drc**: identical violation breakdown (SEGMENT-ENDPOINT-GAP 1,
  VIA-SEGMENT 2, VIA-DRILL-HOLE 1; 19 same-net warnings).

The oracle still FINDS exactly what it found before — only the repeated parse
is gone.

## 7. max_rounds sizing

All three oracle callers (route.py cap-move ~4257, route.py finalize leg
~4847, repair_planes.py ~3624) use the default max_rounds=3; no caller
overrides it. Raising it would only add more rounds, not fix waste; lowering it
risks losing productive late rounds (kitdev R3 welds 6). The cap is not the
problem — the per-round re-derivation cost is.

## 8. Conclusion

- The oracle loop is the slow part of the pour-verification tail, dominated by
  re-derivation (fetch + obstacles + parse = 76%), not repair routing (9%).
- Set-identity early-exit is unsafe on real boards: force_raw retries succeed,
  late rounds carry fresh keys, and consecutive-round sets genuinely differ.
- The provable waste is the double/triple parse per round; reusing the round's
  pcb_data in the fetch eliminates it with byte-identical output.
- Landed: `KICAD_ORACLE_REUSE_PARSE` (default ON), ~4% oracle-leg saving,
  equivalence gate PASSED.

## Appendix: artifacts

- `/tmp/oracle_p2/step6.log` — carrier chain measurement (timestamps)
- `/tmp/oracle_instr_timing.py`, `/tmp/oracle_fetch_breakdown.py`,
  `/tmp/oracle_cnc_bycaller.py` — phase attribution harnesses
- `/tmp/oracle_direct_harness.py` — direct oracle harness (ON/OFF gate)
- `/tmp/oracle_gate3/`, `/tmp/oracle_timing_gate/` — gate outputs
- `/tmp/sim_exact.py`, `/tmp/sim_r3.py` — corpus simulations (Rule A/B = 0 fires)
