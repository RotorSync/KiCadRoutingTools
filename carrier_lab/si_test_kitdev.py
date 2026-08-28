import sys, os
sys.path.insert(0, 'py_router'); sys.path.insert(0, 'rust_router')
os.environ['KICAD_SI_ADAPTIVE'] = '1'
import env_knobs
env_knobs.refresh()
from kicad_parser import parse_kicad_pcb
from routing_config import GridRouteConfig
import si_classes as si
import si_enforce

pcb = parse_kicad_pcb('carrier_lab/si_corpus_ab/kitdev_chain2/off_a.kicad_pcb')
classes = si.classify_board(pcb, board_path='carrier_lab/si_corpus_ab/kitdev_chain2/off_a.kicad_pcb')
config = GridRouteConfig(layers=['F.Cu','In1.Cu','In2.Cu','B.Cu'])
victims = [nid for nid,i in classes.items() if nid!=-1 and i['class']=='VICTIM']
print('kitdev victims:', len(victims))
for nid in victims:
    r = si_enforce._adaptive_radius_for_net(pcb, classes, nid)
    print(f'  {classes[nid]["name"]} R={r:.2f}')
