#!/bin/bash
# Phase B Task 3: carrier-chain A/B for planner ordering.
set +e
ROOT=/home/austin/krt_work
PY=/home/austin/eda/.venv/bin/python
export PYTHONPATH=$ROOT/py_router:$ROOT/rust_router
R=$ROOT/carrier_lab
OUT=/tmp/phase_b_ab
rm -rf $OUT; mkdir -p $OUT

run_arm() {
  local ARM=$1
  local PO=$2
  local PREFIX=$OUT/$ARM/routed
  local LOGDIR=$OUT/$ARM/logs
  mkdir -p $OUT/$ARM/logs $OUT/$ARM/routed
  echo "===== ARM $ARM (KICAD_PLANNER_ORDERING=$PO) ====="
  KICAD_PLANNER_ORDERING=$PO nice -n 19 bash $R/ab_chain.sh $ROOT $PREFIX $LOGDIR > $OUT/$ARM/chain.log 2>&1
  echo "chain exit=$?"
  local BOARD=${PREFIX}_routed.kicad_pcb
  $PY $ROOT/py_router/check_connected.py $BOARD > $OUT/$ARM/connected.log 2>&1
  echo "check_connected exit=$? (0=clean)"
  $PY $ROOT/py_router/check_drc.py $BOARD --clearance 0.1 --clearance-margin 0.1 > $OUT/$ARM/drc.log 2>&1
  echo "check_drc exit=$?"
  grep -E 'FOUND [0-9]+ DRC VIOLATIONS|NO DRC VIOLATIONS' $OUT/$ARM/drc.log | tail -1
  $PY $ROOT/quality/score.py $BOARD --json $OUT/$ARM/score.json > $OUT/$ARM/score.log 2>&1
  echo "score exit=$?"
  grep -iE 'final|score' $OUT/$ARM/score.log | tail -3
  echo "--- route.py bulk counters (5.log) ---"
  grep -o '"rescue": {[^}]*"attempted": [0-9]*' $LOGDIR/5.log | head -1
  grep -o '"rerouted_pairs": [[^]]*]' $LOGDIR/5.log | head -1
  grep -o '"ripup_success_pairs": [[^]]*]' $LOGDIR/5.log | head -1
  grep -o '"total_iterations": [0-9]*' $LOGDIR/5.log | head -1
  grep -o '"total_time": [0-9.]*' $LOGDIR/5.log | head -1
  grep -o '"total_vias": [0-9]*' $LOGDIR/5.log | head -1
  grep -o '"failed_multipoint": [[^]]*]' $LOGDIR/5.log | head -1
  echo "--- timing (step 6 route.py) ---"
  grep -E 'Elapsed|User time' $LOGDIR/5.log | head -2
}

echo "=== ARM A: baseline (planner ordering OFF) ==="
run_arm base 0
echo ""
echo "=== ARM B: planner ordering ON ==="
run_arm head 1
echo ""
echo "AB-DONE"
