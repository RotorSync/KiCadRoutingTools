import sys
sys.path.insert(0, '/home/austin/krt_work/py_router')
sys.path.insert(0, '/home/austin/krt_work/rust_router')
sys.path.insert(0, '/home/austin/krt_work/quality')
from kicad_parser import parse_kicad_pcb
import score

pcb = parse_kicad_pcb('kicad_files/orangecrab_ext_pll.kicad_pcb')
# find IO_MOSI and EXT_PLL+ segments on In1.Cu
for s in pcb.segments:
    if s.net_id in pcb.nets and pcb.nets[s.net_id].name in ('IO_MOSI', 'EXT_PLL+') and s.layer == 'In1.Cu':
        print(pcb.nets[s.net_id].name, s.start_x, s.start_y, s.end_x, s.end_y, 'w=', s.width)
