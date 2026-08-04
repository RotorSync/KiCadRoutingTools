# se-rip-arbitration experiments (2026-08-02/03)

Prototypes + reproducers for the pours-first / no-taps / router-owned-taps
architecture (issue #562) and the rip-arbitration study. Full measurements
and verdicts: session memories `pour-doctrine-466-dynamic-0802` and
`soft-knobs-rip-study-0802`; tuning data in
`~/Documents/kicad_stress_test/param_tune_0802/` (tune_driver.py +
results.jsonl).

## Env knobs on this branch (all default-off)

| knob | file | verdict |
|---|---|---|
| KICAD_PLANE_NO_TAPS | route_planes.py | ARCHITECTURE CORE — pour only, skipped pads seed split-layer Voronoi |
| KICAD_POUR_LAUNCH | single_ended_routing.py | ARCHITECTURE CORE — pour fill as laddered attach surface in the route pass |
| KICAD_PLANE_DEFER_BGA | route_planes.py | superseded by NO_TAPS (subset), kept |
| bga_fanout --plane-net-layers | bga_fanout/__init__.py | only for plans that intentionally delay a pour |
| KICAD_PLANE_TAP_PREFER_REUSE (+RMULT/COEF) | route_planes.py | REFUTED at chain level (v2=v3=v4: −6.1 pts) |
| KICAD_FANOUT_TRACK_CONNECT | bga_fanout/underpad.py | moot in pour-first chains |
| KICAD_SE_RIP_VALUE_GATE / _CHURN_GATE / _PROBE / _EAGER | single_ended_loop.py, reroute_loop.py | REFUTED — soft rra pricing beats refusal |

## Reproducers

- `arch_chain_3boards.sh` — the 4-stage architecture chain on
  ulx3s/cynthion/schoko (edit paths; orangecrab variant in the #562 tables).
  Measured: 2 wins / 2 ties / 0 losses vs tap-flow.
- `score_pour_integrity.py <board.kicad_pcb>` — per-zone fill connectivity
  (whole-zone + under-BGA connected-to-main %), the plane-integrity metric
  used throughout.

Graduation checklist lives in #562. Fanout wall-time: #561.
