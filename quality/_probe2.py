import sys
sys.path.insert(0, '/home/austin/krt_work/py_router')
sys.path.insert(0, '/home/austin/krt_work/rust_router')
from kicad_parser import parse_kicad_pcb
import glob, os

def probe(path):
    try:
        pcb = parse_kicad_pcb(path)
    except Exception as e:
        return f"ERR {type(e).__name__}: {e}"
    segs = pcb.segments
    vias = pcb.vias
    nets = pcb.nets
    return f"segs={len(segs)} vias={len(vias)} nets={len(nets)}"

for f in sorted(glob.glob('/home/austin/krt_work/kicad_files/*.kicad_pcb')):
    print(os.path.basename(f), '->', probe(f))
