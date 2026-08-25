import sys
sys.path.insert(0, '/home/austin/krt_work/py_router')
sys.path.insert(0, '/home/austin/krt_work/rust_router')
from kicad_parser import parse_kicad_pcb
from collections import defaultdict
import geometry as G
import math

for path in ['/home/austin/krt_work/kicad_files/qfn_diffpair_escape.kicad_pcb',
             '/home/austin/krt_work/kicad_files/qfn_interior_pads.kicad_pcb',
             '/home/austin/krt_work/kicad_files/qfn_underpad_coupling.kicad_pcb']:
    pcb = parse_kicad_pcb(path)
    print("=====", path.split('/')[-1])
    by_net = defaultdict(list)
    for s in pcb.segments:
        by_net[s.net_id].append(s)
    for nid, segs in by_net.items():
        name = pcb.nets[nid].name if nid in pcb.nets else '?'
        print(f"  net {nid} '{name}': {len(segs)} segs")
        for s in segs:
            print(f"    ({s.start_x:.3f},{s.start_y:.3f})->({s.end_x:.3f},{s.end_y:.3f}) w={s.width} layer={s.layer}")
