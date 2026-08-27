#!/bin/bash
# Full grading of one tuned-arm carrier output: conn + drc + score, all captured.
# Usage: grade_tune_full.sh <arm>
set +e
cd /home/austin/krt_work
PY=/home/austin/eda/.venv/bin/python
export PYTHONPATH=/home/austin/krt_work/py_router:/home/austin/krt_work/rust_router
ARM="$1"
B=/tmp/si_tune_${ARM}/routed_routed.kicad_pcb
OUT=/tmp/si_tune_${ARM}/grade.txt
echo "=== ARM $ARM $B ===" > "$OUT"
echo "--- check_connected ---" >> "$OUT"
$PY py_router/check_connected.py "$B" >> "$OUT" 2>&1
echo "--- check_drc (floor 0.1) ---" >> "$OUT"
$PY py_router/check_drc.py "$B" --clearance 0.1 --clearance-margin 0.1 >> "$OUT" 2>&1
echo "--- score ---" >> "$OUT"
$PY quality/score.py "$B" --json /tmp/si_tune_${ARM}_score.json >> "$OUT" 2>&1
$PY -c "import json; d=json.load(open('/tmp/si_tune_${ARM}_score.json')); m=d['metrics']['si_coupling']; print('si_coupling raw:', m['value'], 'sub:', round(d['sub_scores']['si_coupling']['sub_score'],3), 'n_victim:', m.get('n_victim_nets'), 'n_exposed:', m.get('n_exposed_pairs')); print('final:', d['final_score'])" >> "$OUT"
cat "$OUT"
