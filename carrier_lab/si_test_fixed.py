import sys, os
sys.path.insert(0, 'py_router'); sys.path.insert(0, 'rust_router')
os.environ['KICAD_SI_ADAPTIVE'] = '0'
import env_knobs
env_knobs.refresh()
import si_enforce
print('SI_ADAPTIVE:', si_enforce._adaptive_enabled())
from kicad_parser import parse_kicad_pcb
from routing_config import GridRouteConfig
import si_classes as si

pcb = parse_kicad_pcb('carrier_lab/si_corpus_ab/watchy_chain2/off_a.kicad_pcb')
classes = si.classify_board(pcb, board_path='carrier_lab/si_corpus_ab/watchy_chain2/off_a.kicad_pcb')
config = GridRouteConfig(layers=['F.Cu','In1.Cu','In2.Cu','B.Cu'])
victims = [nid for nid,i in classes.items() if nid!=-1 and i['class']=='VICTIM']
for nid in victims[:4]:
    r = si_enforce._adaptive_radius_for_net(pcb, classes, nid)
    c = si_enforce._adaptive_cost_for_net(pcb, classes, nid)
    print(f'{classes[nid]["name"]:<20} R={r:.2f} C={c:.3f} (fixed: expect R=0.80 C=0.100)')
