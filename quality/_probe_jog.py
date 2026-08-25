
import sys, os
sys.path.insert(0, '/home/austin/krt_work/quality')
sys.path.insert(0, '/home/austin/krt_work/py_router')
sys.path.insert(0, '/home/austin/krt_work/rust_router')
from kicad_parser import parse_kicad_pcb
import geometry as G

def analyze(path):
    pcb = parse_kicad_pcb(path)
    by_net_layer = G.group_segments_by_net_layer(pcb.segments)
    total_jc = 0
    total_ex = 0
    total_len = 0.0
    total_bends = 0
    per_net = {}
    for nid, layers in by_net_layer.items():
        njc = 0; nex = 0; nl = 0.0; nb = 0
        for layer, segs in layers.items():
            widths = [s.width for s in segs]
            width = max(widths) if widths else 0.25
            window = max(2.0, 8.0 * width)
            for poly in G.chain_segments(segs):
                nb += G.polyline_bends(poly)
                nl += G.polyline_length(poly)
                jc = G.detect_jog_chains(poly, window)
                njc += len(jc)
                m = G.minimal_octilinear_bends(poly)
                nex += max(0, G.polyline_bends(poly) - m)
        total_jc += njc; total_ex += nex; total_len += nl; total_bends += nb
        per_net[nid] = (njc, nex, nl)
    return {
        'board': os.path.basename(path),
        'len_mm': round(total_len,2),
        'bends': total_bends,
        'jog_chains': total_jc,
        'excess_bends': total_ex,
        'jc_per_mm': round(total_jc/total_len,4) if total_len else 0,
        'ex_per_mm': round(total_ex/total_len,4) if total_len else 0,
        'ex_per_conn': round(total_ex/len(by_net_layer),2) if by_net_layer else 0,
    }

boards = [
  '/home/austin/krt_work/carrier_lab/d1.kicad_pcb',
  '/home/austin/krt_work/carrier_lab/d1_fixed2.kicad_pcb',
  '/home/austin/krt_work/kicad_files/fanout_output2.kicad_pcb',
  '/home/austin/krt_work/kicad_files/routed_output.kicad_pcb',
  '/home/austin/krt_work/kicad_files/rp2350_fpga_eensy_prePlane.kicad_pcb',
  '/home/austin/krt_work/kicad_files/orangecrab_ext_pll.kicad_pcb',
]
for b in boards:
    print(analyze(b))
