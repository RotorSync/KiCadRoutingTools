import sys
sys.path.insert(0, '/home/austin/krt_work/py_router')
sys.path.insert(0, '/home/austin/krt_work/rust_router')
sys.path.insert(0, '/home/austin/krt_work/quality')
from kicad_parser import parse_kicad_pcb
import score

for b in ['carrier_lab/d1.kicad_pcb', 'kicad_files/routed_output.kicad_pcb',
          'kicad_files/orangecrab_ext_pll.kicad_pcb', 'kicad_files/rp2350_fpga_eensy_prePlane.kicad_pcb']:
    pcb = parse_kicad_pcb(b)
    sic = score.metric_si_coupling(pcb)
    print('==', b)
    print('   value:', sic['value'], 'victims:', sic['n_victim_nets'], 'aggressors:', sic['n_aggressor_nets'], 'pairs:', sic['n_exposed_pairs'])
    for r in sic.get('top_offender_pairs', [])[:8]:
        print('   ', r['victim'], '<-', r['aggressor'], r['layer'], r['exposed_mm'], 'mm @', r['mean_sep_mm'], 'mm')
