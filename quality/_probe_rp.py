import sys
sys.path.insert(0, '/home/austin/krt_work/py_router')
sys.path.insert(0, '/home/austin/krt_work/rust_router')
from kicad_parser import parse_kicad_pcb
import si_classes as si
pcb = parse_kicad_pcb('kicad_files/rp2350_fpga_eensy_prePlane.kicad_pcb')
for nid, net in pcb.nets.items():
    if net.name in ('+3V3', '+1V1'):
        print('==', net.name)
        for p in net.pads:
            if p.pinfunction:
                hits = [pat.pattern for pat in si.PIN_VICTIM_PATTERNS if pat.search(p.pinfunction)]
                ahits = [pat.pattern for pat in si.PIN_AGGRESSOR_PATTERNS if pat.search(p.pinfunction)]
                print('   ', p.component_ref, p.pad_number, repr(p.pinfunction), repr(p.pintype), 'VICTIMhits=', hits, 'AGGhits=', ahits)
