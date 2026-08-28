import sys
sys.path.insert(0, 'py_router'); sys.path.insert(0, 'rust_router')
import env_knobs
env_knobs.refresh()
from kicad_parser import parse_kicad_pcb
import si_classes as si
import si_enforce
import numpy as np

# watchy mid: +3V3 U4 pad at (82.68, 85.31) disconnected
# Check what aggressor copper is near that pad on the step-a state
pcb = parse_kicad_pcb('carrier_lab/si_corpus_ab/watchy_chain2/off_a.kicad_pcb')
classes = si.classify_board(pcb, board_path='carrier_lab/si_corpus_ab/watchy_chain2/off_a.kicad_pcb')
# Find the U4 +3V3 pad
for p in pcb.pads_by_net.get(0, []):
    pass
# Find +3V3 net id
name2id = {inf['name']: nid for nid, inf in classes.items() if nid != -1}
v33 = name2id.get('+3V3')
print('+3V3 net id:', v33, 'class:', classes[v33]['class'] if v33 else None)
# Aggressor segments near (82.68, 85.31)
target = (82.68, 85.31)
print('Aggressor segments within 2mm of U4 +3V3 pad:')
for s in pcb.segments:
    if s.net_id in classes and classes[s.net_id]['class'] == 'AGGRESSOR':
        # distance from target to segment
        import math
        # point-segment distance
        px, py = target
        ax, ay = s.start_x, s.start_y
        bx, by = s.end_x, s.end_y
        dx, dy = bx-ax, by-ay
        L2 = dx*dx+dy*dy
        if L2 < 1e-12:
            d = math.hypot(px-ax, py-ay)
        else:
            t = max(0, min(1, ((px-ax)*dx+(py-ay)*dy)/L2))
            cx, cy = ax+t*dx, ay+t*dy
            d = math.hypot(px-cx, py-cy)
        if d < 2.0:
            print(f'  {classes[s.net_id]["name"]:<20} layer={s.layer} dist={d:.2f} seg=({s.start_x:.1f},{s.start_y:.1f})-({s.end_x:.1f},{s.end_y:.1f})')
