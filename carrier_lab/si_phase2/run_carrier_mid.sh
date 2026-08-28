#!/bin/bash
# SI Phase 2 carrier middle-point A/B: OFF vs ON-mid (RADIUS=1.0 COST=0.2).
# Back-to-back arms on the full 6-step chain, fresh output paths each.
set +e
ROOT=/home/austin/krt_work
PY=/home/austin/eda/.venv/bin/python
export PYTHONPATH=$ROOT/py_router:$ROOT/rust_router

run_arm() {
  local ARM="$1"
  local PREFIX="/tmp/si_mid_${ARM}/routed"
  local LOGDIR="/tmp/si_mid_${ARM}/logs"
  rm -rf "/tmp/si_mid_${ARM}"
  mkdir -p "$LOGDIR"
  case "$ARM" in
    off) export KICAD_SI_ENFORCE=0; unset KICAD_SI_ENFORCE_RADIUS; unset KICAD_SI_ENFORCE_COST ;;
    mid) export KICAD_SI_ENFORCE=1; export KICAD_SI_ENFORCE_RADIUS=1.0; export KICAD_SI_ENFORCE_COST=0.2 ;;
  esac
  echo "=== ARM $ARM KICAD_SI_ENFORCE=$KICAD_SI_ENFORCE RADIUS=${KICAD_SI_ENFORCE_RADIUS:-default} COST=${KICAD_SI_ENFORCE_COST:-default} ==="
  echo "uptime-before: $(uptime)"
  bash $ROOT/carrier_lab/ab_chain_v2.sh "$ROOT" "$PREFIX" "$LOGDIR" 0
}

run_arm off
run_arm mid
echo "CARRIER-MID-DONE"
