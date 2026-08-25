
import sys, os
sys.path.insert(0, '/home/austin/krt_work/quality')
sys.path.insert(0, '/home/austin/krt_work/py_router')
sys.path.insert(0, '/home/austin/krt_work/rust_router')
from kicad_parser import parse_kicad_pcb
import geometry as G

pcb = parse_kicad_pcb('/home/austin/krt_work/carrier_lab/d1.kicad_pcb')
by_net_layer = G.group_segments_by_net_layer(pcb.segments)

# Choose two traces: net 371 F.Cu (15-bend cluster) and net 372 F.Cu (8-bend cluster)
targets = [(371, 'F.Cu'), (372, 'F.Cu')]
for (nid, layer) in targets:
    segs = by_net_layer[nid][layer]
    widths = [s.width for s in segs]
    width = max(widths)
    window = max(2.0, 8.0 * width)
    print(f"===== net {nid} layer {layer} (trace width {width}mm, window {window:.3f}mm) =====")
    for poly in G.chain_segments(segs):
        plen = G.polyline_length(poly)
        nb = G.polyline_bends(poly)
        m = G.minimal_octilinear_bends(poly)
        excess = max(0, nb - m)
        jc = G.detect_jog_chains(poly, window)
        if not jc:
            continue
        print(f"  polyline len={plen:.3f}mm bends={nb} minimal={m} excess={excess}")
        print(f"    endpoints: ({poly[0][0]:.3f},{poly[0][1]:.3f}) -> ({poly[-1][0]:.3f},{poly[-1][1]:.3f})")
        pos = G.polyline_arc_positions(poly)
        bidx = G.polyline_bend_indices(poly)
        print(f"    bend indices={bidx}")
        for cluster in jc:
            print(f"    CLUSTER ({len(cluster)} bends):")
            for (i, a) in cluster:
                print(f"      vertex {i}: arc={a:.3f}mm coord=({poly[i][0]:.3f},{poly[i][1]:.3f})")
        print()
    print()
