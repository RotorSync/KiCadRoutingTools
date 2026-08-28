import sys, os
sys.path.insert(0, 'py_router'); sys.path.insert(0, 'rust_router')
os.environ['KICAD_SI_ADAPTIVE'] = '1'
import env_knobs
env_knobs.refresh()
import si_enforce
from kicad_parser import parse_kicad_pcb
from routing_config import GridRouteConfig
import si_classes as si

boards = [
  ('watchy', 'carrier_lab/si_corpus_ab/watchy_chain2/off_a.kicad_pcb', ['F.Cu','In1.Cu','In2.Cu','B.Cu']),
  ('ulx3s',  'carrier_lab/si_corpus_ab/ulx3s_chain3/off_a.kicad_pcb', ['F.Cu','In1.Cu','In2.Cu','B.Cu']),
  ('haas',   'carrier_lab/si_corpus_ab/haasoscope_chain3/off_a.kicad_pcb', ['F.Cu','In1.Cu','In2.Cu','In3.Cu','In4.Cu','B.Cu']),
]
for name, b, layers in boards:
    pcb = parse_kicad_pcb(b)
    classes = si.classify_board(pcb, board_path=b)
    config = GridRouteConfig(layers=layers)
    victims = [nid for nid,i in classes.items() if nid!=-1 and i['class']=='VICTIM']
    radii = [si_enforce._adaptive_radius_for_net(pcb, classes, nid) for nid in victims]
    costs = [si_enforce._adaptive_cost_for(r) for r in radii]
    print(f'{name}: R mean={sum(radii)/len(radii):.2f} C mean={sum(costs)/len(costs):.3f} '
          f'R range=[{min(radii):.2f},{max(radii):.2f}]')
