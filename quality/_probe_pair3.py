import sys
sys.path.insert(0, '/home/austin/krt_work/py_router')
sys.path.insert(0, '/home/austin/krt_work/rust_router')
from kicad_parser import parse_kicad_pcb
pcb = parse_kicad_pcb('kicad_files/orangecrab_ext_pll.kicad_pcb')
for nid, net in pcb.nets.items():
    if net.name in ('IO_MOSI', 'IO_MISO', 'IO_SCK', 'IO_SDA'):
        segs = [s for s in pcb.segments if s.net_id == nid]
        print(net.name, 'n_segs=', len(segs))
        for s in segs[:5]:
            print('   ', s.layer, round(s.start_x,3), round(s.start_y,3), round(s.end_x,3), round(s.end_y,3))
