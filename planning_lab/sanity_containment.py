"""Sanity check: containment of a polyline against itself should be ~100%."""
import sys, math
sys.path.insert(0, "/home/austin/krt_work/planning_lab")
from measure_corridor_quality import sample_containment, point_polyline_dist, polyline_len

# straight line from (0,0) to (10,0)
pts = [(0,0),(10,0)]
segs = [(0,0,10,0)]
print("self-containment straight:", sample_containment(segs, pts, 0.5))

# L-shaped polyline vs its own segments
pts2 = [(0,0),(5,0),(5,5)]
segs2 = [(0,0,5,0),(5,0,5,5)]
print("self-containment L:", sample_containment(segs2, pts2, 0.5))

# offset line by 1mm -> should be ~0 at W=0.5
segs3 = [(0,1,10,1)]
print("offset 1mm at W=0.5:", sample_containment(segs3, pts, 0.5))
print("offset 1mm at W=2:", sample_containment(segs3, pts, 2.0))

# point-seg distance checks
print("dist (5,0)->(0,0)-(10,0):", point_polyline_dist(5,0,pts))
print("dist (5,3)->(0,0)-(10,0):", point_polyline_dist(5,3,pts))
