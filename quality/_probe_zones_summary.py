import sys
sys.path.insert(0, "/home/austin/krt_work/py_router")
sys.path.insert(0, "/home/austin/krt_work/quality")
from kicad_parser import parse_kicad_pcb
import geometry as G
from collections import defaultdict

def point_in_poly(x, y, poly):
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside

pcb = parse_kicad_pcb(sys.argv[1])
zone_index = defaultdict(list)
for z in pcb.zones:
    if z.net_id == 0:
        continue
    zone_index[(z.net_id, z.layer)].append(z.polygon)

tol = 1e-3
seg_endpoints = []
for s in pcb.segments:
    seg_endpoints.append((s.start_x, s.start_y))
    seg_endpoints.append((s.end_x, s.end_y))
pad_points = []
for pads in pcb.pads_by_net.values():
    for p in pads:
        pad_points.append((p.global_x, p.global_y))
via_points = [(v.x, v.y) for v in pcb.vias]
all_points = seg_endpoints + pad_points + via_points

def count_near(px, py):
    c = 0
    for (qx, qy) in all_points:
        if G.dist(px, py, qx, qy) <= tol:
            c += 1
            if c >= 2:
                return c
    return c

dangling = []
seen = set()
for s in pcb.segments:
    for ep in ((s.start_x, s.start_y), (s.end_x, s.end_y)):
        key = (round(ep[0], 4), round(ep[1], 4))
        if key in seen:
            continue
        seen.add(key)
        nconn = count_near(ep[0], ep[1])
        if nconn < 2:
            dangling.append((ep[0], ep[1], s.net_id, s.layer))

in_zone_count = 0
for (x, y, nid, layer) in dangling:
    for poly in zone_index.get((nid, layer), []):
        if point_in_poly(x, y, poly):
            in_zone_count += 1
            break

print(f"board={sys.argv[1]}")
print(f"  dangling={len(dangling)}  in_zone(same net+layer)={in_zone_count}  remaining={len(dangling)-in_zone_count}")
