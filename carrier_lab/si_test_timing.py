import sys, time
sys.path.insert(0, 'py_router'); sys.path.insert(0, 'rust_router')
import env_knobs
env_knobs.refresh()
from kicad_parser import parse_kicad_pcb
from routing_config import GridRouteConfig
import si_classes as si
import si_enforce

# Time the adaptive computation on haasoscope (200 victims, biggest)
pcb = parse_kicad_pcb('carrier_lab/si_corpus_ab/haasoscope_chain3/off_a.kicad_pcb')
classes = si.classify_board(pcb, board_path='carrier_lab/si_corpus_ab/haasoscope_chain3/off_a.kicad_pcb')
config = GridRouteConfig(layers=['F.Cu','In1.Cu','In2.Cu','In3.Cu','In4.Cu','B.Cu'])
victims = [nid for nid,i in classes.items() if nid!=-1 and i['class']=='VICTIM']

t0 = time.time()
# First call builds trees
arr = si_enforce.compute_victim_si_field(pcb, config, victims[0], classes=classes)
t1 = time.time()
print(f'first victim (tree build): {t1-t0:.3f}s rows={len(arr)}')
# Subsequent calls use cached trees
t0 = time.time()
for nid in victims[1:]:
    arr = si_enforce.compute_victim_si_field(pcb, config, nid, classes=classes)
t1 = time.time()
print(f'{len(victims)-1} victims: {t1-t0:.3f}s ({((t1-t0)/(len(victims)-1))*1000:.1f}ms each)')
