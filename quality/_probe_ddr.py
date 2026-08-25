import sys
sys.path.insert(0, '/home/austin/krt_work/py_router')
sys.path.insert(0, '/home/austin/krt_work/rust_router')
from kicad_parser import parse_kicad_pcb
import si_classes as si
pcb = parse_kicad_pcb('kicad_files/routed_output.kicad_pcb')
targets = ['Net-(U2A-~{WAKEUP})', 'Net-(U2A-DATA_0)', 'Net-(U2A-~{SIWU})',
           'Net-(U1A-DQ0_A)', 'Net-(U1A-DQS0_c_A)', 'Net-(U1B-CA0_A)', 'Net-(U1B-CK_t_A)',
           '/fpga_adc/lvds_rx_top_clkin1_P']
for nid, net in pcb.nets.items():
    if net.name in targets:
        print('==', net.name)
        for p in net.pads:
            if p.pinfunction:
                print('   ', p.component_ref, p.pad_number, repr(p.pinfunction), repr(p.pintype))
