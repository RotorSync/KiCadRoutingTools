import sys
sys.path.insert(0, '/home/austin/krt_work/py_router')
sys.path.insert(0, '/home/austin/krt_work/rust_router')
from kicad_parser import parse_kicad_pcb
pcb = parse_kicad_pcb('carrier_lab/d1.kicad_pcb')
# Show footprints with value hints of regulators/inductors/FETs/diodes
import re
for f in sorted(pcb.footprints.values(), key=lambda f: f.reference):
    v = (f.value or '')
    fn = (f.footprint_name or '')
    if re.search(r'reg|inductor|induct|L_|mosfet|fet|diode|schottky|buck|boost|switcher|driver|gate', v + ' ' + fn, re.I):
        pads = [(p.pad_number, p.net_name) for p in f.pads[:8]]
        print(f.reference, '|', fn, '|', v, '|', pads)
