import sys, os, glob
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
from kicad_parser import parse_kicad_pcb

boards = sorted(glob.glob('kicad_files/*.kicad_pcb')) + ['carrier_lab/d1.kicad_pcb', 'carrier_lab/d1_fixed2.kicad_pcb']
for b in boards:
    try:
        pcb = parse_kicad_pcb(b)
        print(f"{b}\tsegments={len(pcb.segments)}\tvias={len(pcb.vias)}\tnets={len(pcb.nets)}")
    except Exception as e:
        print(f"{b}\tERROR {type(e).__name__}: {e}")
