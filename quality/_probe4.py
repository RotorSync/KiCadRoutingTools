import sys
sys.path.insert(0, '/home/austin/krt_work/py_router')
sys.path.insert(0, '/home/austin/krt_work/rust_router')
from kicad_parser import parse_kicad_pcb
import math
pcb = parse_kicad_pcb('/home/austin/krt_work/carrier_lab/d1.kicad_pcb')
# find J2.A10 pad
for nid, pads in pcb.pads_by_net.items():
    for p in pads:
        if p.component_ref=='J2' and p.pad_number=='A10':
            print("pad:", p.component_ref, p.pad_number, "shape", p.shape, "rot", p.rotation,
                  "size", p.size_x, p.size_y, "pos", p.global_x, p.global_y, "net", p.net_name)
            # find segments of this net touching pad
            for s in pcb.segments:
                if s.net_id==nid:
                    for (px,py) in ((s.start_x,s.start_y),(s.end_x,s.end_y)):
                        if math.hypot(px-p.global_x, py-p.global_y)<1e-3:
                            print("  seg", (s.start_x,s.start_y),"->",(s.end_x,s.end_y))
