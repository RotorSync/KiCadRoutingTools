import sys
sys.path.insert(0, 'py_router'); sys.path.insert(0, 'rust_router')
import env_knobs
env_knobs.refresh()
from kicad_parser import parse_kicad_pcb
from routing_config import GridRouteConfig
import si_classes as si
import si_enforce
import numpy as np

# Carrier step6 input state: after planes + diff pairs. Use the d2 state.
# The chain uses /tmp/si_tune2_tuned2/routed_d2.kicad_pcb but that's gone.
# Use carrier_lab/d2.kicad_pcb (the d2 state from the chain).
for b in ['carrier_lab/d2.kicad_pcb', 'carrier_lab/p2.kicad_pcb']:
    try:
        pcb = parse_kicad_pcb(b)
        classes = si.classify_board(pcb, board_path=b)
        config = GridRouteConfig(layers=['F.Cu','In1.Cu','In2.Cu','In3.Cu','In4.Cu','B.Cu'])
        victims = [nid for nid,i in classes.items() if nid!=-1 and i['class']=='VICTIM']
        radii = []
        for nid in victims:
            r = si_enforce._adaptive_radius_for_net(pcb, classes, nid)
            radii.append(r)
        radii = np.array(radii)
        print(f'{b}: n_victims={len(victims)} radius mean={radii.mean():.2f} med={np.median(radii):.2f} '
              f'frac<=0.8={(radii<=0.8).mean()*100:.0f}% frac>=1.2={(radii>=1.2).mean()*100:.0f}%')
    except Exception as e:
        print(f'{b}: ERR {e}')
