import sys
sys.path.insert(0, '/home/austin/krt_work/py_router')
sys.path.insert(0, '/home/austin/krt_work/rust_router')
from kicad_parser import parse_kicad_pcb
import geometry as G
import math
pcb = parse_kicad_pcb('/home/austin/krt_work/carrier_lab/d1.kicad_pcb')
segs = [s for s in pcb.segments if s.net_id == 43]
print("VBUS segments:")
for s in segs:
    print(f"  ({s.start_x:.4f},{s.start_y:.4f})->({s.end_x:.4f},{s.end_y:.4f}) layer={s.layer}")
# chain
polys = G.chain_segments(segs)
print("chained polylines:")
for poly in polys:
    print("  ", [(round(x,4),round(y,4)) for x,y in poly])
    print("   length =", round(G.polyline_length(poly),4))
    print("   bends =", G.polyline_bends(poly))
    print("   off_angle_joints =", G.polyline_off_angle_joints(poly))
    # per-joint angles
    for i in range(1, len(poly)-1):
        a = math.degrees(math.atan2(poly[i][1]-poly[i-1][1], poly[i][0]-poly[i-1][0]))%180
        b = math.degrees(math.atan2(poly[i+1][1]-poly[i][1], poly[i+1][0]-poly[i][0]))%180
        turn = G.angle_between_deg(a,b)
        resid = turn%45; resid=min(resid,45-resid)
        print(f"   joint {i}: dir_in={a:.2f} dir_out={b:.2f} turn={turn:.2f} resid={resid:.2f}")
