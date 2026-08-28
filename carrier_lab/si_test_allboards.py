import sys
sys.path.insert(0, 'py_router'); sys.path.insert(0, 'rust_router')
import env_knobs
env_knobs.refresh()
from kicad_parser import parse_kicad_pcb
from routing_config import GridRouteConfig
import si_classes as si
import si_enforce
import numpy as np

boards = [
  ('watchy',  'carrier_lab/si_corpus_ab/watchy_chain2/off_a.kicad_pcb', ['F.Cu','In1.Cu','In2.Cu','B.Cu']),
  ('ulx3s',   'carrier_lab/si_corpus_ab/ulx3s_chain3/off_a.kicad_pcb', ['F.Cu','In1.Cu','In2.Cu','B.Cu']),
  ('haas',    'carrier_lab/si_corpus_ab/haasoscope_chain3/off_a.kicad_pcb', ['F.Cu','In1.Cu','In2.Cu','In3.Cu','In4.Cu','B.Cu']),
  ('tigard',  'carrier_lab/si_corpus_ab/tigard_chain2/off_a.kicad_pcb', ['F.Cu','In1.Cu','In2.Cu','B.Cu']),
  ('kitdev',  'carrier_lab/si_corpus_ab/kitdev_chain2/off_a.kicad_pcb', ['F.Cu','In1.Cu','In2.Cu','B.Cu']),
  ('glasgow', 'carrier_lab/si_corpus_ab/glasgow_chain2/off_a.kicad_pcb', ['F.Cu','In1.Cu','In2.Cu','B.Cu']),
  ('sonde',   'carrier_lab/si_corpus_ab/sonde_u_chain2/off_a.kicad_pcb', ['F.Cu','B.Cu']),
  ('interf',  'carrier_lab/si_corpus_ab/interf_u_chain2/off_a.kicad_pcb', ['F.Cu','B.Cu']),
]
for name, b, layers in boards:
    try:
        pcb = parse_kicad_pcb(b)
        classes = si.classify_board(pcb, board_path=b)
        config = GridRouteConfig(layers=layers)
        victims = [nid for nid,i in classes.items() if nid!=-1 and i['class']=='VICTIM']
        n_err = 0
        n_rows = 0
        for nid in victims:
            try:
                arr = si_enforce.compute_victim_si_field(pcb, config, nid, classes=classes)
                n_rows += len(arr)
            except Exception as e:
                n_err += 1
                if n_err <= 2:
                    print(f'  ERR {classes[nid]["name"]}: {e}')
        print(f'{name}: victims={len(victims)} errors={n_err} total_rows={n_rows}')
    except Exception as e:
        print(f'{name}: BOARD ERR {e}')
