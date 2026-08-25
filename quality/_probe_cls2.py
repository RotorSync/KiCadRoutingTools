import sys
sys.path.insert(0, '/home/austin/krt_work/py_router')
import si_classes as si
tests = ['SPI0_SCLK', 'SPI0_MOSI', 'SCL1', 'SDA1', 'UART0_TX', 'CAN_H', 'CAN_H_BUS',
         'SSRX2_P', 'SSTX1_N', 'VBUS', '+3V3', '+5V', 'GND', 'GNDA', 'TC1_AN', 'TC1_AP',
         'ADC1_nDRDY', 'ADC2_nCS', 'ISO_SCLK', 'ISO_nRST', 'REFOUT1', 'GPIO_VREF',
         'RTL_XI', 'RTL_XO', 'Net-(U5-SW)', 'Net-(U3-VIN)', 'Net-(U4-RT{slash}CLK)',
         'Net-(U5-BST)', 'Net-(Q3-D)', 'Net-(DC1-K)', 'Net-(DZ1-A)', 'VIN_RAW', 'VBULK',
         'VOUT_PD', 'VBUS_IN', 'VIN_PROT', 'CM4_3V3', 'ISO_3V3', 'VISO_RAW', 'CAN_5V',
         'CAN_TX', 'CAN_RX', 'MUX_SEL', 'DIN_A', 'DIN_SENSE', 'AUX_V_ADC', 'AUX_I_ADC',
         'AUX_EXC_SENSE', 'AUX_5V_EXC', 'AUX_LOOP_EXC', 'GLOBAL_EN', 'EN54560', 'ENDIV',
         'PD_EE_SCL', 'PD_EE_SDA', 'RTL_TXP', 'RTL_RXN', 'IPAD_DM', 'IPAD_DP',
         'USB2_P', 'USB2_N', 'TRD0_P', 'TRD0_N']
for t in tests:
    print(f'{t:<24} -> {si.classify_net(t)}')
