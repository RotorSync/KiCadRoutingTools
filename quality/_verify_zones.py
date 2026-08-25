"""Spot-check the zone-aware stubs fix on d1 (plane-carrying board).

Prints the polygon-hit result for each dangling endpoint and for synthetic
points inside/outside the GND plane zone, to confirm _point_in_polygon works.
"""
import sys
sys.path.insert(0, "/home/austin/krt_work/py_router")
sys.path.insert(0, "/home/austin/krt_work/quality")
from kicad_parser import parse_kicad_pcb
import score

pcb = parse_kicad_pcb("/home/austin/krt_work/carrier_lab/d1.kicad_pcb")
st = score.metric_stubs(pcb)
print("stubs value:", st["value"])
print("dangling endpoints:", st["dangling_endpoints"])

# Build zone index same as metric_stubs
from collections import defaultdict
zone_index = defaultdict(list)
for z in pcb.zones:
    if z.net_id != 0:
        zone_index[(z.net_id, z.layer)].append(z.polygon)

# Spot-check each dangling endpoint: print polygon hit result
for (x, y) in st["dangling_endpoints"]:
    # find net/layer of this endpoint
    for s in pcb.segments:
        for ep in ((s.start_x, s.start_y), (s.end_x, s.end_y)):
            if abs(ep[0]-x) < 0.001 and abs(ep[1]-y) < 0.001:
                nid = s.net_id; layer = s.layer
                break
        else:
            continue
        break
    hit = score._point_in_polygon(x, y, zone_index[(nid, layer)][0]) if zone_index[(nid, layer)] else False
    print(f"  endpoint ({x},{y}) net={nid} layer={layer} in_zone={hit}")

# Demonstrate a real hit: point inside GND zone on In1.Cu
gnd_poly = zone_index[(2, "In1.Cu")][0]
print("GND In1.Cu polygon:", gnd_poly)
for pt in [(50, 50), (92, 60), (200, 200)]:
    print(f"  point {pt} in GND zone: {score._point_in_polygon(pt[0], pt[1], gnd_poly)}")
