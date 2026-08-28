import sys
sys.path.insert(0, 'py_router'); sys.path.insert(0, 'rust_router')
import env_knobs
env_knobs.refresh()
from kicad_parser import parse_kicad_pcb
from routing_config import GridRouteConfig
import si_classes as si
import si_enforce

for b, layers in [
  ('carrier_lab/si_corpus_ab/watchy_chain2/off_a.kicad_pcb', ['F.Cu','In1.Cu','In2.Cu','B.Cu']),
  ('carrier_lab/si_corpus_ab/ulx3s_chain3/off_a.kicad_pcb', ['F.Cu','In1.Cu','In2.Cu','B.Cu']),
  ('carrier_lab/si_corpus_ab/haasoscope_chain3/off_a.kicad_pcb', ['F.Cu','In1.Cu','In2.Cu','In3.Cu','In4.Cu','B.Cu']),
]:
    pcb = parse_kicad_pcb(b)
    classes = si.classify_board(pcb, board_path=b)
    config = GridRouteConfig(layers=layers)
    victims = [nid for nid,i in classes.items() if nid!=-1 and i['class']=='VICTIM']
    print(f'== {b.split("/")[-1]} victims={len(victims)}')
    for nid in victims[:8]:
        r = si_enforce._adaptive_radius_for_net(pcb, classes, nid)
        c = si_enforce._adaptive_cost_for_net(pcb, classes, nid)
        arr = si_enforce.compute_victim_si_field(pcb, config, nid, classes=classes)
        print(f'   {classes[nid]["name"]:<30} R={r:.2f} C={c:.3f} rows={len(arr)}')
