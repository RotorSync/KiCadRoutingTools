import sys
sys.path.insert(0, 'py_router'); sys.path.insert(0, 'rust_router')
import env_knobs
env_knobs.refresh()
from kicad_parser import parse_kicad_pcb
from routing_config import GridRouteConfig
import si_classes as si
import si_enforce
import numpy as np

pcb = parse_kicad_pcb('carrier_lab/si_corpus_ab/ulx3s_chain3/off_a.kicad_pcb')
classes = si.classify_board(pcb, board_path='carrier_lab/si_corpus_ab/ulx3s_chain3/off_a.kicad_pcb')
config = GridRouteConfig(layers=['F.Cu','In1.Cu','In2.Cu','B.Cu'])
victims = [nid for nid,i in classes.items() if nid!=-1 and i['class']=='VICTIM']
# Show per-net radius for all victims, sorted
rows = []
for nid in victims:
    r = si_enforce._adaptive_radius_for_net(pcb, classes, nid)
    dists = si_enforce._pad_to_aggressor_distances(pcb, classes, nid)
    frac = sum(1 for d in dists if d <= 1.0)/len(dists) if dists else 0
    rows.append((classes[nid]['name'], r, frac, len(dists)))
rows.sort(key=lambda x: -x[1])
for name, r, frac, npads in rows:
    print(f'  {name:<30} R={r:.2f} frac_close={frac*100:.0f}% pads={npads}')
