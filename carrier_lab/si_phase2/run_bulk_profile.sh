#!/bin/bash
# Bulk route step profiling driver (FINDINGS ONLY -- no engine changes).
# Runs the exact step-6 route.py invocation from ab_chain_v2.sh on a fresh
# output path, under a chosen profiler, with per-line wall-clock timestamps.
#
# Usage: run_bulk_profile.sh <mode> <outdir> [input_board]
#   mode = cprofile | pyspy | pyspyraw | plain
#   outdir = fresh output directory (created)
#   input_board = step-6 input .kicad_pcb (default: tuned2 arm's d2, which was
#                 produced under the current production SI config R0.8/C0.1)
#
# Env: KICAD_SI_ENFORCE etc. are inherited from the caller (defaults apply).
set +e
ROOT=/home/austin/krt_work
PY=/home/austin/eda/.venv/bin/python
PYSPY=/home/austin/eda/.venv/bin/py-spy
MODE="$1"
OUTDIR="$2"
IN="${3:-/tmp/si_tune2_tuned2/routed_d2.kicad_pcb}"
mkdir -p "$OUTDIR"
export PYTHONPATH=$ROOT/py_router:$ROOT/rust_router

OUT="$OUTDIR/routed.kicad_pcb"

ARGS=( "$IN" "$OUT" --power-nets '*GND*' VIN_PROT VBULK VOUT_PD +5V +3V3 CM4_3V3 VBUS \
       --power-nets-widths 0.8 1.2 1.2 1.2 0.8 0.5 0.5 0.8 \
       --track-width 0.2 --via-size 0.3 --via-drill 0.15 )

echo "=== bulk profile mode=$MODE outdir=$OUTDIR ==="
echo "input=$IN"
echo "uptime-before: $(uptime)"
echo "env: KICAD_SI_ENFORCE=${KICAD_SI_ENFORCE:-default} RADIUS=${KICAD_SI_ENFORCE_RADIUS:-default} COST=${KICAD_SI_ENFORCE_COST:-default}"

case "$MODE" in
  cprofile)
    /usr/bin/time -v $PY -m cProfile -o "$OUTDIR/bulk.prof" $ROOT/py_router/route.py "${ARGS[@]}" \
      > >($PY $ROOT/carrier_lab/si_phase2/tslog.py "$OUTDIR/step6.log") 2> >(cat >> "$OUTDIR/step6.err")
    ;;
  pyspy)
    /usr/bin/time -v $PYSPY record -o "$OUTDIR/bulk_flame.svg" -f flamegraph -n \
      -- $PY $ROOT/py_router/route.py "${ARGS[@]}" \
      > >($PY $ROOT/carrier_lab/si_phase2/tslog.py "$OUTDIR/step6.log") 2> >(cat >> "$OUTDIR/step6.err")
    ;;
  pyspyraw)
    /usr/bin/time -v $PYSPY record -o "$OUTDIR/bulk_raw.txt" -f raw -n \
      -- $PY $ROOT/py_router/route.py "${ARGS[@]}" \
      > >($PY $ROOT/carrier_lab/si_phase2/tslog.py "$OUTDIR/step6.log") 2> >(cat >> "$OUTDIR/step6.err")
    ;;
  plain)
    /usr/bin/time -v $PY $ROOT/py_router/route.py "${ARGS[@]}" \
      > >($PY $ROOT/carrier_lab/si_phase2/tslog.py "$OUTDIR/step6.log") 2> >(cat >> "$OUTDIR/step6.err")
    ;;
esac
echo "exit=$?"
echo "uptime-after: $(uptime)"
echo "BULK-PROFILE-DONE"
