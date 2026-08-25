import sys
sys.path.insert(0, '/home/austin/krt_work/py_router')
import si_classes as si
print('PIN_AGGRESSOR:', [p.pattern for p in si.PIN_AGGRESSOR_PATTERNS])
print()
for pf in ['lvds_rx_top_clkin1', 'DBCLK+', 'ftdi_data[1]', 'ftdi_wakeupn', 'DDR_CK', 'DQS0_c_A', 'DQ0_A', 'DATA_0']:
    agg = [p.pattern for p in si.PIN_AGGRESSOR_PATTERNS if p.search(pf)]
    vic = [p.pattern for p in si.PIN_VICTIM_PATTERNS if p.search(pf)]
    print(f'{pf:<22} AGG={agg} VIC={vic}')
print()
for t in ['/fpga_adc/lvds_rx_top_clkin1_P', 'Net-(U2A-~{RD})', 'Net-(U2A-DATA_0)', 'Net-(U1B-CK_c_A)', 'Net-(U1A-DQS0_t_A)']:
    print(f'{t:<38} name-only -> {si.classify_net(t)}')
    print(f'{t:<38} with-pin  -> {si.classify_net(t, pinfunctions=["ftdi_data[1]"], pintypes=["bidirectional"])}')
