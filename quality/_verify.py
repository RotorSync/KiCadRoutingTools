import sys
sys.path.insert(0, '/home/austin/krt_work/py_router')
sys.path.insert(0, '/home/austin/krt_work/rust_router')
from kicad_parser import parse_kicad_pcb
from collections import defaultdict
import geometry as G
import math

pcb = parse_kicad_pcb('/home/austin/krt_work/carrier_lab/d1.kicad_pcb')
by_net = defaultdict(list)
for s in pcb.segments:
    by_net[s.net_id].append(s)
# list nets with 3-8 segments (small, easy to hand-verify)
small = sorted([(len(v), nid) for nid, v in by_net.items() if 3 <= len(v) <= 8])
for cnt, nid in small[:12]:
    name = pcb.nets[nid].name if nid in pcb.nets else '?'
    print(f"net {nid} '{name}': {cnt} segs")
