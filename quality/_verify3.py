import sys
sys.path.insert(0, '/home/austin/krt_work/py_router')
sys.path.insert(0, '/home/austin/krt_work/rust_router')
from kicad_parser import parse_kicad_pcb
from collections import defaultdict
pcb = parse_kicad_pcb('/home/austin/krt_work/carrier_lab/d1.kicad_pcb')
by_net = defaultdict(list)
for s in pcb.segments:
    by_net[s.net_id].append(s)
small = sorted([(len(v), nid) for nid, v in by_net.items() if 2 <= len(v) <= 6])
for cnt, nid in small[:15]:
    name = pcb.nets[nid].name if nid in pcb.nets else '?'
    print(f"net {nid} '{name}': {cnt} segs")
