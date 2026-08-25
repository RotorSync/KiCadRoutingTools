import sys
sys.path.insert(0, '/home/austin/krt_work/py_router')
sys.path.insert(0, '/home/austin/krt_work/rust_router')
from kicad_parser import parse_kicad_pcb
import si_classes as si

pcb = parse_kicad_pcb('carrier_lab/d1.kicad_pcb')

# 1. Which netclass pattern matched GND?
import list_nets
dr = list_nets.read_design_rules('carrier_lab/d1.kicad_pcb')
print('all patterns:')
for pat, cls in dr['patterns']:
    print('   ', repr(pat), cls)
print()
for n in ['GND', '+3V3', '+5V', 'CM4_3V3', 'ISO_3V3']:
    matched = [(pat, cls) for pat, cls in dr['patterns'] if __import__('fnmatch').fnmatch(n, pat)]
    print(n, '->', matched)

# 2. switch hints
hints = si.switch_node_hints(pcb)
for nid in hints:
    print('hint', nid, pcb.nets[nid].name if nid in pcb.nets else nid)
print('U5-SW in hints?', any(pcb.nets[nid].name == 'Net-(U5-SW)' for nid in hints))

# check U5 / L5 footprint detection
for ref in ['U5','L5']:
    f = pcb.footprints[ref]
    print(ref, f.footprint_name, f.value,
          'is_ind=', si._is_inductor_footprint(f),
          'is_diode=', si._is_diode_footprint(f),
          'is_fet=', si._is_fet_footprint(f),
          'reg_hint=', bool(si.REGULATOR_HINT.search((f.value or '')+' '+(f.footprint_name or ''))))
    for p in f.pads:
        print('   pad', p.pad_number or p.net_name, p.net_id)
