import sys
sys.path.insert(0, 'py_router'); sys.path.insert(0, 'rust_router')
from kicad_parser import parse_kicad_pcb
import si_classes as si
from collections import Counter

fixed = parse_kicad_pcb('carrier_lab/si_corpus_rescore/ulx3s/on_v.kicad_pcb')
adapt = parse_kicad_pcb('carrier_lab/si_corpus_adaptive/ulx3s/ad_v.kicad_pcb')
fc = si.classify_board(fixed, board_path='carrier_lab/si_corpus_rescore/ulx3s/on_v.kicad_pcb')
ac = si.classify_board(adapt, board_path='carrier_lab/si_corpus_adaptive/ulx3s/ad_v.kicad_pcb')
fseg = [s for s in fixed.segments if s.net_id in fc and fc[s.net_id]['class']=='VICTIM']
aseg = [s for s in adapt.segments if s.net_id in ac and ac[s.net_id]['class']=='VICTIM']
print(f'fixed victim segs: {len(fseg)}, adaptive victim segs: {len(aseg)}')
print(f'fixed total segs: {len(fixed.segments)}, adaptive total segs: {len(adapt.segments)}')
fn = Counter(s.net_id for s in fseg)
an = Counter(s.net_id for s in aseg)
print('fixed victim nets with copper:', len(fn))
print('adaptive victim nets with copper:', len(an))
# Which nets differ?
for nid in set(fn) | set(an):
    if fn.get(nid,0) != an.get(nid,0):
        name = fc.get(nid,{}).get('name', ac.get(nid,{}).get('name','?'))
        print(f'  {name}: fixed={fn.get(nid,0)} adaptive={an.get(nid,0)}')
