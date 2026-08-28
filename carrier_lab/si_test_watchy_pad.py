import sys
sys.path.insert(0, 'py_router'); sys.path.insert(0, 'rust_router')
import env_knobs
env_knobs.refresh()
from kicad_parser import parse_kicad_pcb
from routing_config import GridRouteConfig
import si_classes as si
import si_enforce
import numpy as np

# watchy: which victim net connects to the U4 +3V3 pad at (82.68, 85.31)?
# Actually +3V3 is an AGGRESSOR on watchy. The victim that got disconnected
# at mid was a victim whose pad is near +3V3 copper. Find victims with pads
# near (82.68, 85.31).
pcb = parse_kicad_pcb('carrier_lab/si_corpus_ab/watchy_chain2/off_a.kicad_pcb')
classes = si.classify_board(pcb, board_path='carrier_lab/si_corpus_ab/watchy_chain2/off_a.kicad_pcb')
config = GridRouteConfig(layers=['F.Cu','In1.Cu','In2.Cu','B.Cu'])
# Find all victim pads near (82.68, 85.31)
target = (82.68, 85.31)
for nid, inf in classes.items():
    if nid == -1 or inf['class'] != 'VICTIM':
        continue
    for p in pcb.pads_by_net.get(nid, []):
        d = ((p.global_x-target[0])**2 + (p.global_y-target[1])**2)**0.5
        if d < 3.0:
            print(f'victim {inf["name"]} pad at ({p.global_x:.2f},{p.global_y:.2f}) dist={d:.2f} layers={p.layers}')
