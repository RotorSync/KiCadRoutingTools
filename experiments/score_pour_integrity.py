#!/usr/bin/env python3
"""Score one layer-cost-tuning arm: standard grade (drc/conn/completion via
check_drc+check_connected already run by ab grader separately) PLUS the pour
metric that motivated the experiment: per plane zone, fill connectivity under
each BGA courtyard and over the whole board (connected-to-main %).

Usage: score_lc_arm.py <final_board.kicad_pcb> [label]
"""
import sys, collections
sys.path.insert(0, '/Users/andy/Documents/KiCadRoutingTools')
import numpy as np
from kicad_parser import parse_kicad_pcb
from plane_fill_model import get_zone_model


def bgas(pcb):
    out = []
    for ref, fp in pcb.footprints.items():
        smd = [p for p in fp.pads if p.drill == 0]
        if len(smd) >= 60:
            xs = collections.Counter(round(p.global_x, 2) for p in fp.pads)
            if len(xs) >= 6 and max(xs.values()) >= 6:
                out.append((ref, fp))
    return out


def main():
    path = sys.argv[1]
    label = sys.argv[2] if len(sys.argv) > 2 else path
    pcb = parse_kicad_pcb(path)
    parts = bgas(pcb)
    print(f"[{label}] {path.split('/')[-1]}")
    for z in (getattr(pcb, 'zones', None) or []):
        nname = next((n.name for n in pcb.nets.values() if n.net_id == z.net_id), '?')
        m = get_zone_model(pcb, z)
        if m is None:
            continue
        main_c = m.largest_component()
        # whole-zone connectivity: fraction of filled cells in the main component
        filled = int((m.labels > 0).sum())
        in_main = int((m.labels == main_c).sum()) if main_c else 0
        whole = 100 * in_main / filled if filled else 0
        line = f"  {z.layer:<7} {nname:<10} whole-zone main {whole:3.0f}%"
        for ref, fp in parts:
            xs = [p.global_x for p in fp.pads]; ys = [p.global_y for p in fp.pads]
            tot = fil = con = 0
            for gx in np.linspace(min(xs), max(xs), 25):
                for gy in np.linspace(min(ys), max(ys), 25):
                    c = m.query_component(float(gx), float(gy))
                    if c is None:
                        continue
                    tot += 1
                    if c > 0:
                        fil += 1
                    if main_c and c == main_c:
                        con += 1
            if tot >= 50:
                line += f" | {ref}: fill {100*fil/tot:3.0f}% main {100*con/tot:3.0f}%"
        print(line)


if __name__ == '__main__':
    main()
