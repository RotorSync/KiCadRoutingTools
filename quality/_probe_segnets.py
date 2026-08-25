import sys
sys.path.insert(0, '/home/austin/krt_work/py_router')
sys.path.insert(0, '/home/austin/krt_work/rust_router')
from kicad_parser import parse_kicad_pcb
from collections import defaultdict

boards = ['carrier_lab/d1.kicad_pcb', 'kicad_files/routed_output.kicad_pcb',
          'kicad_files/orangecrab_ext_pll.kicad_pcb', 'kicad_files/rp2350_fpga_eensy_prePlane.kicad_pcb',
          'kicad_files/fanout_output2.kicad_pcb', 'kicad_files/lvds_converter_dualclk_gnd.kicad_pcb']
for b in boards:
    pcb = parse_kicad_pcb(b)
    by_net = defaultdict(int)
    for s in pcb.segments:
        by_net[s.net_id] += 1
    print('==', b)
    # nets with segments, sorted by segment count desc
    rows = []
    for nid, cnt in by_net.items():
        name = pcb.nets[nid].name if nid in pcb.nets else str(nid)
        rows.append((cnt, nid, name))
    rows.sort(reverse=True)
    for cnt, nid, name in rows[:40]:
        print(f'   {cnt:5d}  {name}')
    print(f'   ... total routed nets: {len(rows)}')
