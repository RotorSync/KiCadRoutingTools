import sys
sys.path.insert(0, '/home/austin/krt_work/py_router')
import si_classes as si
for t in ['Net-(U1A-DQS0_c_A)', 'Net-(U1A-DQS0_t_A)', 'Net-(U1B-CK_t_A)', 'Net-(U1B-CK_c_A)',
          'Net-(U1A-DQ0_A)', 'Net-(U1B-CA0_A)', '/fpga_adc/lvds_rx_top_clkin1_P']:
    print(f'{t:<35} -> {si.classify_net(t)}')
print()
# check pin metadata
for pf in ['DDR_DQS_N[0]', 'DQS0_c_A', 'DDR_CK', 'CK_t_A', 'DDR_A[0]', 'DQ0_A', 'DATA_0', 'lvds_rx_top_clkin1']:
    agg = [p.pattern for p in si.PIN_AGGRESSOR_PATTERNS if p.search(pf)]
    vic = [p.pattern for p in si.PIN_VICTIM_PATTERNS if p.search(pf)]
    print(f'{pf:<25} AGG={agg} VIC={vic}')
