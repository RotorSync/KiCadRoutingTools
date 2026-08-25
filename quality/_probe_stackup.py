import sys
sys.path.insert(0, '/home/austin/krt_work/py_router')
sys.path.insert(0, '/home/austin/krt_work/rust_router')
from kicad_parser import parse_kicad_pcb

boards = ['carrier_lab/d1.kicad_pcb', 'carrier_lab/d1_fixed2.kicad_pcb',
          'kicad_files/fanout_output1.kicad_pcb', 'kicad_files/fanout_output2.kicad_pcb',
          'kicad_files/fanout_starting_point.kicad_pcb',
          'kicad_files/lvds_converter_dualclk_gnd.kicad_pcb',
          'kicad_files/orangecrab_ext_pll.kicad_pcb',
          'kicad_files/qfn_diffpair_escape.kicad_pcb', 'kicad_files/qfn_fanned_out.kicad_pcb',
          'kicad_files/qfn_interior_pads.kicad_pcb', 'kicad_files/qfn_underpad_coupling.kicad_pcb',
          'kicad_files/routed_output.kicad_pcb', 'kicad_files/rp2350_fpga_eensy_prePlane.kicad_pcb']
for b in boards:
    pcb = parse_kicad_pcb(b)
    st = pcb.board_info.stackup
    cl = pcb.board_info.copper_layers
    # zones per layer with net names
    zones = {}
    for z in pcb.zones:
        zones.setdefault(z.layer, []).append(z.net_name)
    print('==', b)
    print('   copper_layers:', cl)
    print('   stackup:', [(s.name, s.layer_type, s.thickness) for s in st] if st else 'EMPTY')
    print('   zones:', {k: v[:4] for k, v in zones.items()})
