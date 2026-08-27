#!/bin/bash
# Grade one tuned-arm carrier output: check_connected + check_drc + score.
# Usage: grade_tune.sh <arm>  (arm in off|on|tuned)
set +e
cd /home/austin/krt_work
PY=/home/austin/eda/.venv/bin/python
export PYTHONPATH=/home/austin/krt_work/py_router:/home/austin/krt_work/rust_router
ARM="$1"
B=/tmp/si_tune_${ARM}/routed_routed.kicad_pcb
echo "=== ARM $ARM $B ==="
echo "--- check_connected ---"
$PY py_router/check_connected.py "$B" 2>&1 | grep -E 'ALL NETS|NOT all|disconnected|Unrouted|Checking|EXIT' | head -8
echo "--- check_drc (floor 0.1) ---"
$PY py_router/check_drc.py "$B" --clearance 0.1 --clearance-margin 0.1 2>&1 | grep -E 'NO DRC|FOUND|violations|VIOLATIONS|EXIT' | head -8
echo "--- score ---"
$PY quality/score.py "$B" --json /tmp/si_tune_${ARM}_score.json >/dev/null 2>&1
$PY -c "import json; d=json.load(open('/tmp/si_tune_${ARM}_score.json')); m=d['metrics']['si_coupling']; print('si_coupling raw:', m['value'], 'sub:', d['sub_scores']['si_coupling']['sub_score']); print('final:', d['final_score'])"
