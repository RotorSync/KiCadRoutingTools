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
# With adaptive on
arr_on = si_enforce.compute_victim_si_field(pcb, config, sda, classes=classes)
# With adaptive off (no exemption)
import os
os.environ['KICAD_SI_ADAPTIVE']='0'
env_knobs.refresh()
arr_off = si_enforce.compute_victim_si_field(pcb, config, sda, classes=classes)
print('SDA adaptive rows:', len(arr_on), 'fixed rows:', len(arr_off))
print('exemption removed:', len(arr_off) - len(arr_on), 'cells')
# Check the pad's own cells are gone
for p in pcb.pads_by_net.get(sda, []):
    gx = round(p.global_x/0.1); gy = round(p.global_y/0.1)
    in_on = ((arr_on[:,0]==0)&(arr_on[:,1]==gx)&(arr_on[:,2]==gy)).any()
    in_off = ((arr_off[:,0]==0)&(arr_off[:,1]==gx)&(arr_off[:,2]==gy)).any()
    print(f'  pad ({p.global_x:.2f},{p.global_y:.2f}) cell ({gx},{gy}): in_fixed={in_off} in_adaptive={in_on}')
