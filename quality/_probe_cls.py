import sys
sys.path.insert(0, '/home/austin/krt_work/py_router')
import si_classes as si
# +3V3 with ADC_AVDD power pin
print('+3V3:', si.classify_net('+3V3', pinfunctions=['ADC_AVDD_44'], pintypes=['power_in'], on_regulator_footprint=False))
print('+1V1:', si.classify_net('+1V1', pinfunctions=['VCCA_PLL_A1'], pintypes=['power_in'], on_regulator_footprint=False))
print('+1V1 no meta:', si.classify_net('+1V1'))
print('+3V3 no meta:', si.classify_net('+3V3'))
print('UART0_TX:', si.classify_net('UART0_TX'))
print('SSTX1_P:', si.classify_net('SSTX1_P'))
print('SSRX1_P:', si.classify_net('SSRX1_P'))
print('VBUS:', si.classify_net('VBUS'))
print('+5V:', si.classify_net('+5V'))
