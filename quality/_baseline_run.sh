#!/bin/bash
# v1.3 baseline scoring driver: runs score.py --json on each of the 6 baseline
# boards, overwriting quality/out/json/.
set -u
PY=/home/austin/eda/.venv/bin/python
cd /home/austin/krt_work
BOARDS=(
  carrier_lab/d1.kicad_pcb
  carrier_lab/d1_fixed2.kicad_pcb
  kicad_files/fanout_output2.kicad_pcb
  kicad_files/routed_output.kicad_pcb
  kicad_files/rp2350_fpga_eensy_prePlane.kicad_pcb
  kicad_files/orangecrab_ext_pll.kicad_pcb
)
for b in "${BOARDS[@]}"; do
  base=$(basename "$b" .kicad_pcb)
  echo "=== $b ==="
  PYTHONPATH=/home/austin/krt_work/py_router:/home/austin/krt_work/rust_router \
    nice -n 19 "$PY" quality/score.py "$b" --json "quality/out/json/$base.json" \
    > "quality/out/json/$base.txt" 2> "quality/out/json/$base.err"
  echo "exit=$?"
  if [ -s "quality/out/json/$base.err" ]; then
    echo "STDERR present:"; cat "quality/out/json/$base.err"
  fi
done
