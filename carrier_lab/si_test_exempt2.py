import sys
sys.path.insert(0, 'py_router'); sys.path.insert(0, 'rust_router')
import env_knobs
env_knobs.refresh()
from kicad_parser import parse_kicad_pcb
from routing_config import GridRouteConfig
import si_classes as si
import si_enforce
import numpy as np

pcb = parse_kicad_pcb('carrier_lab/si_corpus_ab/watchy_chain2/off_a.kicad_pcb')
classes = si.classify_board(pcb, board_path='carrier_lab/si_corpus_ab/watchy_chain2/off_a.kicad_pcb')
config = GridRouteConfig(layers=['F.Cu','In1.Cu','In2.Cu','B.Cu'])
name2id = {inf['name']: nid for nid, inf in classes.items() if nid != -1}
sda = name2id['SDA']
cells = si_enforce._own_pad_exempt_cells(pcb, config, sda)
print('exempt cells:', len(cells))
# Check if (827,841) is in cells
print('(827,841) in cells:', ((cells[:,0]==827)&(cells[:,1]==841)).any())
# Show the SDA pads
for p in pcb.pads_by_net.get(sda, []):
    print(f'  pad ({p.global_x:.2f},{p.global_y:.2f}) size=({p.size_x},{p.size_y}) grid=({round(p.global_x/0.1)},{round(p.global_y/0.1)})')
