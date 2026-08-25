import sys
sys.path.insert(0, '/home/austin/krt_work/py_router')
sys.path.insert(0, '/home/austin/krt_work/rust_router')
from kicad_parser import parse_kicad_pcb
pcb = parse_kicad_pcb('carrier_lab/d1.kicad_pcb')
for ref in ['U3','U4','U5','U19','U25','U6','U10','U11','U12','U23','Q1','Q2','Q3','Q4','Q5']:
    if ref in pcb.footprints:
        f = pcb.footprints[ref]
        pads = [(p.pad_number, p.net_name) for p in f.pads]
        print(ref, '|', f.footprint_name, '|', f.value, '|', pads)
    else:
        print(ref, 'not found')
