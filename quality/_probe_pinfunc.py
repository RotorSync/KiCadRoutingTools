import sys
sys.path.insert(0, '/home/austin/krt_work/py_router')
sys.path.insert(0, '/home/austin/krt_work/rust_router')
from kicad_parser import parse_kicad_pcb
from collections import Counter

pcb = parse_kicad_pcb('kicad_files/routed_output.kicad_pcb')
# Counter of pinfunction values across all pads (with net names that are routed or not)
pinc = Counter()
pintype_c = Counter()
for f in pcb.footprints.values():
    for p in f.pads:
        if p.pinfunction:
            pinc[p.pinfunction] += 1
        if p.pintype:
            pintype_c[p.pintype] += 1
print('pinfunction values:', pinc.most_common(40))
print()
print('pintype values:', pintype_c.most_common(20))
