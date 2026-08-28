import sys, os
sys.path.insert(0, 'py_router'); sys.path.insert(0, 'rust_router')
os.environ['KICAD_SI_ADAPTIVE'] = '0'
import env_knobs
env_knobs.refresh()
import si_enforce
from kicad_parser import parse_kicad_pcb
from routing_config import GridRouteConfig
import si_classes as si
import numpy as np

pcb = parse_kicad_pcb('carrier_lab/si_corpus_ab/watchy_chain2/off_a.kicad_pcb')
classes = si.classify_board(pcb, board_path='carrier_lab/si_corpus_ab/watchy_chain2/off_a.kicad_pcb')
config = GridRouteConfig(layers=['F.Cu','In1.Cu','In2.Cu','B.Cu'])
victims = [nid for nid,i in classes.items() if nid!=-1 and i['class']=='VICTIM']
# With adaptive off, compute_victim_si_field should use R=0.8/C=0.1
arr = si_enforce.compute_victim_si_field(pcb, config, victims[0], classes=classes)
print('rows:', len(arr), 'maxcost:', arr[:,3].max() if len(arr) else 0)
# Verify the geometry cache key uses (fp, 0.8, 0.1)
geo = si_enforce._get_aggressor_geometry(pcb, config, classes)
print('geo layers:', sorted(geo.keys()))
