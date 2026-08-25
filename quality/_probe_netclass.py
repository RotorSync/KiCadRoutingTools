import sys, os
sys.path.insert(0, '/home/austin/krt_work/py_router')
sys.path.insert(0, '/home/austin/krt_work/rust_router')
import list_nets
for b in ['carrier_lab/d1.kicad_pcb', 'kicad_files/routed_output.kicad_pcb', 'kicad_files/rp2350_fpga_eensy_prePlane.kicad_pcb']:
    dr = list_nets.read_design_rules(b)
    print('==', b)
    print('  classes:', dr['classes'])
    print('  n assignments:', len(dr['assignments']))
    print('  patterns:', dr['patterns'][:10])
    print('  source:', dr['source'])
