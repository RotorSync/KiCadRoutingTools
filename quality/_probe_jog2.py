
import sys, os
sys.path.insert(0, '/home/austin/krt_work/quality')
sys.path.insert(0, '/home/austin/krt_work/py_router')
sys.path.insert(0, '/home/austin/krt_work/rust_router')
from kicad_parser import parse_kicad_pcb
import geometry as G

def analyze(path):
    pcb = parse_kicad_pcb(path)
    by_net_layer = G.group_segments_by_net_layer(pcb.segments)
    total_jc = 0; total_ex = 0; total_len = 0.0; total_bends = 0
    for nid, layers in by_net_layer.items():
        for layer, segs in layers.items():
            widths = [s.width for s in segs]
            width = max(widths) if widths else 0.25
            window = max(2.0, 8.0 * width)
            for poly in G.chain_segments(segs):
                total_bends += G.polyline_bends(poly)
                total_len += G.polyline_length(poly)
                total_jc += len(G.detect_jog_chains(poly, window))
                m = G.minimal_octilinear_bends(poly)
                total_ex += max(0, G.polyline_bends(poly) - m)
    return {
        'board': os.path.basename(path),
        'len_mm': round(total_len,2),
        'bends': total_bends,
        'jog_chains': total_jc,
        'excess_bends': total_ex,
        'jc_per_mm': round(total_jc/total_len,4) if total_len else 0,
        'ex_per_mm': round(total_ex/total_len,4) if total_len else 0,
    }

boards = [
  '/home/austin/krt_work/kicad_files/fanout_output1.kicad_pcb',
  '/home/austin/krt_work/kicad_files/fanout_starting_point.kicad_pcb',
  '/home/austin/krt_work/kicad_files/lvds_converter_dualclk_gnd.kicad_pcb',
  '/home/austin/krt_work/kicad_files/qfn_diffpair_escape.kicad_pcb',
  '/home/austin/krt_work/kicad_files/qfn_fanned_out.kicad_pcb',
  '/home/austin/krt_work/kicad_files/qfn_interior_pads.kicad_pcb',
  '/home/austin/krt_work/kicad_files/qfn_underpad_coupling.kicad_pcb',
]
for b in boards:
    print(analyze(b))
