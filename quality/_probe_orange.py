import sys
sys.path.insert(0, '/home/austin/krt_work/py_router')
sys.path.insert(0, '/home/austin/krt_work/rust_router')
from kicad_parser import parse_kicad_pcb
import si_classes as si

pcb = parse_kicad_pcb('kicad_files/orangecrab_ext_pll.kicad_pcb')
for target in ['P1.35V', 'P3.3V', 'P2.5V']:
    print('==', target)
    for f in pcb.footprints.values():
        for p in f.pads:
            if p.net_name == target:
                kinds = []
                if si._is_inductor_footprint(f): kinds.append('ind')
                if si._is_diode_footprint(f): kinds.append('diode')
                if si._is_fet_footprint(f): kinds.append('fet')
                if si.REGULATOR_HINT.search((f.value or '')+' '+(f.footprint_name or '')): kinds.append('reg')
                if kinds:
                    print('   ', f.reference, f.footprint_name, f.value, p.pad_number, kinds)
