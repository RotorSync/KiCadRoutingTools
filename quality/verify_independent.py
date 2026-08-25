#!/usr/bin/env python3
"""Independent re-computation of four score.py metrics for hand-verification.

Written by the supervising agent, sharing no code with score.py: segments are
chained by endpoint coincidence per (net, layer); a joint is a point where
exactly two segments meet; a bend is a joint turning more than 2 degrees; an
off-grid joint turns off any 45-degree multiple by more than 1 degree.
Raw values must match score.py's table for the same board.
"""

import math
import sys
from collections import defaultdict

sys.path.insert(0, "/home/austin/krt_work/py_router")
from kicad_parser import parse_kicad_pcb  # noqa: E402

Q = 0.001  # endpoint coincidence quantum, mm


def qpt(x, y):
    return (round(x / Q), round(y / Q))


def main(path):
    pcb = parse_kicad_pcb(path)
    segs_by = defaultdict(list)
    for s in pcb.segments:
        segs_by[(s.net_id, s.layer)].append(s)
    routed_nets = {n for (n, _l) in segs_by}
    vias_by_net = defaultdict(int)
    for v in pcb.vias:
        if v.net_id in routed_nets:
            vias_by_net[v.net_id] += 1

    total_len = 0.0
    bends = 0
    joints = 0
    off45 = 0
    nsegs = 0
    for (_net, _layer), segs in segs_by.items():
        nsegs += len(segs)
        point_segs = defaultdict(list)
        for s in segs:
            total_len += math.hypot(s.end_x - s.start_x, s.end_y - s.start_y)
            point_segs[qpt(s.start_x, s.start_y)].append(s)
            point_segs[qpt(s.end_x, s.end_y)].append(s)
        for pt, ss in point_segs.items():
            if len(ss) != 2:
                continue  # endpoint or junction, not an interior joint
            a, b = ss
            # direction of each segment AWAY from the shared point
            def outdir(s):
                if qpt(s.start_x, s.start_y) == pt:
                    return math.atan2(s.end_y - s.start_y, s.end_x - s.start_x)
                return math.atan2(s.start_y - s.end_y, s.start_x - s.end_x)
            turn = math.degrees(abs(outdir(a) - outdir(b)))
            turn = min(turn % 360.0, 360.0 - turn % 360.0)
            turn = 180.0 - turn  # deviation from straight continuation
            joints += 1
            if abs(turn) > 2.0:
                bends += 1
            frac = abs(turn) % 45.0
            if min(frac, 45.0 - frac) > 1.0:
                off45 += 1

    nnets = len(routed_nets)
    print(f"board: {path}")
    print(f"segments={nsegs} vias={sum(vias_by_net.values())} routed_nets={nnets}")
    print(f"total trace length = {total_len:.2f} mm, joints={joints}")
    print(f"bends/mm        = {bends}/{total_len:.2f} = {bends/total_len:.4f}")
    print(f"off45 fraction  = {off45}/{joints} = {off45/max(joints,1):.4f}")
    print(f"vias/routed net = {sum(vias_by_net.values())}/{nnets} = {sum(vias_by_net.values())/nnets:.4f}")
    print(f"segments/mm     = {nsegs}/{total_len:.2f} = {nsegs/total_len:.4f}")


if __name__ == "__main__":
    main(sys.argv[1])
