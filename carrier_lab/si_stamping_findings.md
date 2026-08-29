# SI Stamping Batch (crate 0.26.0) — Equivalence Proven, Timing Fails: REVERTED

Date: 2026-08-28
Branch: optimize-via-protection-parse
Verdict: **DO NOT LAND.** Both pieces are bit-for-bit equivalent to HEAD, but the
Rust sort-dedupe is a ~10 s obstacle-window REGRESSION and the Python direct-cache
is a ~1 s win — neither meets the >=5 s bar. Full revert; findings only.

## 1. What was attempted

Two gated speed candidates for the SI-stamping obstacle window (~36 s on carrier
step-6, dominated by Rust FFI stamping):

1. **Rust set_layer_proximity_batch sort-dedupe** (obstacle_map.rs): sort rows by
   (layer, packed key), fold duplicates to per-cell MAX, one hashmap insert per
   unique cell instead of one per row. Claimed identical final map (max is
   associative/commutative).
2. **Python direct-path field cache** (si_enforce.py, KICAD_SI_DIRECT_CACHE,
   default ON): cache _accumulate_field results for the rare same-interface
   exclusion path, keyed on (fingerprint, radius, cost, frozenset(exclude_nids)).

## 2. Equivalence — PROVEN (all four combinations, seed-pinned)

Baseline determinism first: OLD path (gate=0) run twice at PYTHONHASHSEED=42 and
once at 7 — all three **99,481,095** iterations, full per-line sequences identical.
The original code is hash-seed-stable at the iteration level; no seed pinning needed
beyond consistency.

Then all four rust/python combinations on carrier step-6 (fresh .so built from each
source, PYTHONHASHSEED=42):

| arm | Rust | Py cache | iterations | seq identical |
|---|---|---|---|---|
| h_old | HEAD | OFF | 99,481,095 | — |
| h_new | HEAD | ON | 99,481,095 | = h_old |
| f42a | new | OFF | 99,481,095 | = h_old |
| n42a | new | ON | 99,481,095 | = h_old |

Full per-line iteration sequences byte-identical across all four. Corpus boards
(glasgow_revC, kit-dev-coldfire-xilinx_5213), old vs new: **58,845,728** and
**126,174,250**, sequences identical. check_drc/check_connected identical on all
three boards (carrier: 0/0; glasgow/kitdev: the pre-existing 2 disconnects both
sides). The Rust change is value-identical by construction (max commutes; routing
does point lookups only) and the Python cache returns the historical array.

**The inherited "mismatch" (99,491,282 vs 99,481,095) was a stale-binary artifact:**
the working-tree .so (built 20:31) predated the current obstacle_map.rs source
(22:13), and several eq_old logs ran against yet older binaries. Different logs used
different Rust binaries AND different Python code (si_enforce.py edited after some
logs ran). Not a real divergence.

## 3. Timing — FAIL (Rust regresses ~10 s; Python wins ~1 s)

Instrumented obstacle-window (build_incremental + prepare_inplace + precompute),
back-to-back on a quiet machine, /usr/bin/time -v:

| arm | Rust | Py cache | build_incremental | prepare_inplace | precompute | window TOTAL | user |
|---|---|---|---|---|---|---|---|
| s_head | HEAD | OFF | **19.83 s** | 5.76 s | 1.88 s | 41.05 s | 226.96 |
| s2_head | HEAD | OFF | **19.79 s** | 5.78 s | 1.95 s | 41.07 s | 229.69 |
| s4_headon | HEAD | ON | **18.89 s** | 5.79 s | 1.92 s | 38.27 s | 227.91 |
| r_old | new | OFF | **30.08 s** | 10.89 s | 1.88 s | 56.94 s | 240.91 |
| r_new | new | ON | **29.24 s** | 10.96 s | 1.88 s | 54.26 s | 241.39 |
| s2_new | new | ON | **29.35 s** | 10.96 s | 1.87 s | 54.49 s | 240.15 |

- **Rust sort-dedupe: ~10 s REGRESSION** (19.8 -> 29–30 s build_incremental;
  user 227 -> 240). Reproducible across three runs.
- **Python direct-cache: ~0.9–1 s win** on HEAD rust (19.83 -> 18.89 build_incremental;
  user ~flat). Below bar.
- Combined: net regression. Neither piece meets the >=5 s gate.

### Why the Rust premise was wrong

The dup-ratio instrumentation (wrapping set_layer_proximity_batch over a full run)
showed **42% duplicates** across all batches (892M rows -> 516M unique) — so dedup
IS happening — yet the sort-dedupe is still slower. FxHashMap inserts are ~O(1) and
cheap; the O(n log n) sort over 16-byte tuples costs more than the dedup saves.
The historical per-row max-insert was already near-optimal for this workload.

## 4. What was reverted

git checkout HEAD -- on: py_router/si_enforce.py, rust_router/src/obstacle_map.rs,
rust_router/Cargo.toml, rust_router/Cargo.lock, rust_router/README.md, VERSION,
metadata.json. The .so was rebuilt from HEAD source (0.25.0) so no stale binary
remains. Working tree clean of tracked modifications.

## 5. Recommendation

The real lever remains the Rust FFI stamping itself (out of python-only scope).
A future candidate should target the per-row hashmap insert with a cheaper dedup
(e.g. a flat Vec + sort only when duplicates are actually detected, or a radix/
counting approach) — but the measured evidence here says the historical loop is
already hard to beat on this workload.

## 6. Memory-safety audit (sibling SIGBUS evidence — NOT attributable to this change)

A sibling session reported the 0.26.0 binary crashing with 'Bus error (core dumped)'
twice on a dense board. Unifying hypothesis: OOB write in the new stamping code.
Audit result: **the SIGBUS cannot be from the new stamping code — it predates it.**

Timeline (file mtimes):
- /tmp/build026.log (the sibling's "0.26.0" build): **19:44** — built from the OLD
  obstacle_map.rs (only the Cargo.toml version bump was in the working tree then).
- The new sort-dedupe obstacle_map.rs source: **22:13**.
- The stale grid_router.so (old code, version-bumped): **20:31**.

So the binary the sibling crashed with contained the HISTORICAL per-row max-insert
loop, not the sort-dedupe. The crash is a pre-existing phenomenon (or an ABI/stale-
binary artifact), not a regression from this campaign.

Direct audit of the new code:
- **No `unsafe` blocks anywhere in the crate** (grep across rust_router/src). All
  indexing is bounds-checked safe Rust; the fold loop uses only `rows[i]` with
  `i < rows.len()` guards and `layer_proximity_costs[layer]` with
  `layer < self.num_layers` pre-checked.
- **cargo test with debug assertions** (bounds checks on): 3/3 pass.
- **50 adversarial stress trials** of set_layer_proximity_batch under the debug
  build (out-of-range layers both sides, extreme +/-100000 coords, non-positive
  costs, up to 500K rows): no panic, no OOB.
- **Dense boards route cleanly** with the new source in BOTH debug and release
  builds: haasoscope_chain3/off_a (118,970,202 iterations, EXIT=0) and watchy
  (4,517,399, EXIT=0). No SIGBUS reproduced.

The iteration-sum mismatch that started this investigation was already proven to be
a stale-binary artifact (different logs used different Rust binaries AND different
Python code), not silent memory corruption — the four-combination equivalence test
(HEAD/new Rust x cache off/on) produced byte-identical sequences, which an OOB write
corrupting cost cells would have perturbed.
