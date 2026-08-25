import sys
sys.path.insert(0, '/home/austin/krt_work/py_router')
sys.path.insert(0, '/home/austin/krt_work/rust_router')
sys.path.insert(0, '/home/austin/krt_work/quality')
from kicad_parser import parse_kicad_pcb
import score

pcb = parse_kicad_pcb('kicad_files/orangecrab_ext_pll.kicad_pcb')
# find IO_MOSI segments on In1.Cu
for s in pcb.segments:
    if s.net_id in pcb.nets and pcb.nets[s.net_id].name == 'IO_MOSI' and s.layer == 'In1.Cu':
        print('IO_MOSI', round(s.start_x,3), round(s.start_y,3), round(s.end_x,3), round(s.end_y,3), 'w=', s.width)
