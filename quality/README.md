# quality/ — routing-aesthetics scoring harness

The measuring stick for the "make the router look like an experienced PCB
designer" initiative. Scores *how routing looks*, not whether it is correct —
functional gates remain check_drc.py + check_connected.py (see repo
CLAUDE.md; never whole-file-diff outputs, they carry per-run UUIDs).

## Usage

PY=/home/austin/eda/.venv/bin/python
export PYTHONPATH=/home/austin/krt_work/py_router:/home/austin/krt_work/rust_router

$PY quality/score.py BOARD.kicad_pcb                 # human table
$PY quality/score.py BOARD.kicad_pcb --json out.json # + machine-readable
$PY quality/render.py BOARD.kicad_pcb --out DIR      # one PNG per copper layer
                                                     # (--out is a DIRECTORY)
$PY quality/verify_independent.py BOARD.kicad_pcb    # independent re-check of
                                                     # 4 metrics (see report)

## Metrics (v1.4)

Eleven sub-scores, each 0–100 via thresholds, weighted into a final score
(weight table at the top of score.py; provisional). Full definitions live in
the metric docstrings in score.py — summary:

| Metric | Measures | Units |
|---|---|---|
| bends | direction changes >2° along chained traces | bends/mm |
| off_angle | joints off any 45° multiple (>1° tolerance) | fraction |
| vias | vias per routed net | vias/net |
| pad_entry | trace-to-pad entry angles; flags acute entries / acid traps | fraction flagged |
| fragmentation | segments per mm (collinear runs should merge) | segs/mm |
| parallel | spacing variance of adjacent co-running traces | mm variance |
| channel | asymmetry of side clearances along traces | ratio |
| layer_direction | anti-axis fraction of long (>3 mm) runs vs the layer's dominant axis (diagonals neutral) | fraction anti-axis |
| stubs | dangling segment endpoints (not reaching pad/via) | count/net |
| jog_chains | stair-stepping: clusters of 2+ bends within a short arc-length window, plus excess bends over the minimal octilinear count | chains/mm & excess/mm |
| si_coupling | cross-bus VICTIM/AGGRESSOR parallel-exposure (same layer within max(3W,1.0mm), weighted 1/sep) + broadside overlap on adjacent layers with no GND plane between (sep = dielectric thickness) | exposure/mm |

si_coupling (v1.4) is the first signal-integrity-aware metric. It uses the net
classifier in py_router/si_classes.py (AGGRESSOR / VICTIM / NEUTRAL, with a
per-board <board>.si.json override file that always wins). Same-interface
(same-bus) pairs are excluded — DDR data beside its own strobe is intentional,
serial data beside switching power is not.

## Baseline result

See report_baseline.md (v1.4): six boards score 47–72/100; worst offenders
are stubs, pad_entry, jog_chains, vias, and (new) si_coupling on the two real
boards with cross-bus violations. Raw outputs under out/json/, renders under
out/render_*/.

## Known limitations (v1.4)

1. stubs zone-awareness uses zone *outlines* as the filled-copper proxy (the
   board files store no filled_polygon data); it ignores thermal-relief gaps
   and keepout islands inside a pour.
2. parallel/channel are sampled heuristics; treat small deltas as noise.
3. layer_direction (v1.3) ignores short runs (≤ 3 mm) and treats clean 45°
   diagonals as neutral — it only docks long runs running perpendicular to the
   layer's dominant axis. A board with no long runs scores a perfect 100 (no
   long-run direction signal), which is not a claim of direction discipline.
4. si_coupling depends on the net classifier's name/metadata heuristics; nets
   that carry no name signal are missed unless the per-board override file
   (<board>.si.json) names them. The same-interface exclusion is a heuristic
   (shared name prefix / component token).
5. Weights are provisional; use score *movement*, not absolute value.

## Provenance

Core (score.py, render.py, geometry.py): DeepSeek session fb24d189.
Verification + docs: supervising agent. _probe*.py/_verify*.py: session
scratch, kept for reference. v1.4 si_coupling metric + py_router/si_classes.py
net classifier + reports: DeepSeek session (v1.4).
