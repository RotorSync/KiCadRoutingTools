import sys, os
sys.path.insert(0, 'py_router'); sys.path.insert(0, 'rust_router')
os.environ['KICAD_SI_ADAPTIVE'] = '1'
import env_knobs
env_knobs.refresh()
from kicad_parser import parse_kicad_pcb
from routing_config import GridRouteConfig
import si_classes as si
import si_enforce
import numpy as np

# For each board, compute the adaptive radius distribution on the step-a state
# (what stamp-time sees) and report the median/mean radius.
boards = [
  ('watchy', 'carrier_lab/si_corpus_ab/watchy_chain2/off_a.kicad_pcb', ['F.Cu','In1.Cu','In2.Cu','B.Cu']),
  ('ulx3s',  'carrier_lab/si_corpus_ab/ulx3s_chain3/off_a.kicad_pcb', ['F.Cu','In1.Cu','In2.Cu','B.Cu']),
  ('haas',   'carrier_lab/si_corpus_ab/haasoscope_chain3/off_a.kicad_pcb', ['F.Cu','In1.Cu','In2.Cu','In3.Cu','In4.Cu','B.Cu']),
  ('tigard', 'carrier_lab/si_corpus_ab/tigard_chain2/off_a.kicad_pcb', ['F.Cu','In1.Cu','In2.Cu','B.Cu']),
  ('kitdev', 'carrier_lab/si_corpus_ab/kitdev_chain2/off_a.kicad_pcb', ['F.Cu','In1.Cu','In2.Cu','B.Cu']),
  ('glasgow','carrier_lab/si_corpus_ab/glasgow_chain2/off_a.kicad_pcb', ['F.Cu','In1.Cu','In2.Cu','B.Cu']),
  ('sonde',  'carrier_lab/si_corpus_ab/sonde_u_chain2/off_a.kicad_pcb', ['F.Cu','B.Cu']),
]
for name, b, layers in boards:
    pcb = parse_kicad_pcb(b)
    classes = si.classify_board(pcb, board_path=b)
    config = GridRouteConfig(layers=layers)
    victims = [nid for nid,i in classes.items() if nid!=-1 and i['class']=='VICTIM']
    radii = []
    fracs = []
    for nid in victims:
        r = si_enforce._adaptive_radius_for_net(pcb, classes, nid)
        radii.append(r)
        dists = si_enforce._pad_to_aggressor_distances(pcb, classes, nid)
        if dists:
            fracs.append(sum(1 for d in dists if d <= 1.0)/len(dists))
    radii = np.array(radii)
    print(f'{name}: n_victims={len(victims)} radius mean={radii.mean():.2f} med={np.median(radii):.2f} '
          f'frac<=0.8={(radii<=0.8).mean()*100:.0f}% frac>=1.2={(radii>=1.2).mean()*100:.0f}% '
          f'frac_close_mean={np.mean(fracs)*100:.1f}%')
