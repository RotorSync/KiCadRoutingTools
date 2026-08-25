import sys
sys.path.insert(0, '/home/austin/krt_work/py_router')
sys.path.insert(0, '/home/austin/krt_work/rust_router')
from kicad_parser import parse_kicad_pcb
import si_classes as si
from collections import defaultdict

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
    res = si.classify_board(pcb, board_path=b)
    # routed nets: have segments
    seg_nets = defaultdict(int)
    for s in pcb.segments:
        seg_nets[s.net_id] += 1
    counts = {'AGGRESSOR': 0, 'VICTIM': 0, 'NEUTRAL': 0}
    routed_counts = {'AGGRESSOR': 0, 'VICTIM': 0, 'NEUTRAL': 0}
    for nid, info in res.items():
        if nid == -1:
            continue
        counts[info['class']] += 1
        if nid in seg_nets:
            routed_counts[info['class']] += 1
    print(f"{b:<55} all={counts}  ROUTED={routed_counts}")
