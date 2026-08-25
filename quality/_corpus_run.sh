#!/bin/bash
# Corpus scoring driver (v1.3): runs score.py --json on each scoreable board.
set -u
PY=/home/austin/eda/.venv/bin/python
cd /home/austin/krt_work
BOARDS=(
  kicad_files/fanout_output1.kicad_pcb
  kicad_files/fanout_output2.kicad_pcb
  kicad_files/fanout_starting_point.kicad_pcb
  kicad_files/lvds_converter_dualclk_gnd.kicad_pcb
  kicad_files/orangecrab_ext_pll.kicad_pcb
  kicad_files/qfn_diffpair_escape.kicad_pcb
  kicad_files/qfn_fanned_out.kicad_pcb
  kicad_files/qfn_interior_pads.kicad_pcb
  kicad_files/qfn_underpad_coupling.kicad_pcb
  kicad_files/routed_output.kicad_pcb
  kicad_files/rp2350_fpga_eensy_prePlane.kicad_pcb
  carrier_lab/d1.kicad_pcb
  carrier_lab/d1_fixed2.kicad_pcb
)
for b in "${BOARDS[@]}"; do
  base=$(basename "$b" .kicad_pcb)
  echo "=== $b ==="
  PYTHONPATH=/home/austin/krt_work/py_router:/home/austin/krt_work/rust_router \
    nice -n 19 "$PY" quality/score.py "$b" --json "quality/out/corpus/$base.json" \
    > "quality/out/corpus/$base.txt" 2> "quality/out/corpus/$base.err"
  echo "exit=$?"
  if [ -s "quality/out/corpus/$base.err" ]; then
    echo "STDERR present:"; cat "quality/out/corpus/$base.err"
  fi
done
