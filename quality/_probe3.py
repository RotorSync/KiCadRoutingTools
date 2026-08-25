import sys
sys.path.insert(0, '/home/austin/krt_work/py_router')
sys.path.insert(0, '/home/austin/krt_work/rust_router')
from kicad_parser import parse_kicad_pcb
from collections import defaultdict

pcb = parse_kicad_pcb('/home/austin/krt_work/carrier_lab/d1.kicad_pcb')
# group segments by net
by_net = defaultdict(list)
for s in pcb.segments:
    by_net[s.net_id].append(s)
# show nets with most segments
ranked = sorted(by_net.items(), key=lambda kv: -len(kv[1]))
print("Top nets by segment count:")
for nid, segs in ranked[:8]:
    name = pcb.nets[nid].name if nid in pcb.nets else '?'
    print(f"  net {nid} '{name}': {len(segs)} segs, {len(pcb.pads_by_net.get(nid,[]))} pads")
    for s in segs[:3]:
        print(f"    ({s.start_x:.3f},{s.start_y:.3f})->({s.end_x:.3f},{s.end_y:.3f}) w={s.width} layer={s.layer}")
