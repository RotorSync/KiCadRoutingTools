import sys
sys.path.insert(0, '/home/austin/krt_work/py_router')
sys.path.insert(0, '/home/austin/krt_work/rust_router')
from kicad_parser import parse_kicad_pcb
pcb = parse_kicad_pcb('/home/austin/krt_work/carrier_lab/d1.kicad_pcb')
for nid, pads in pcb.pads_by_net.items():
    for p in pads:
        if p.component_ref=='Q7':
            print("Q7 pad", p.pad_number, "pos", round(p.global_x,3), round(p.global_y,3), "net", p.net_name)
