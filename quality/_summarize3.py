
import json

def load(p):
    with open(p) as f:
        return json.load(f)

baseline = ['d1','d1_fixed2','fanout_output2','routed_output','rp2350_fpga_eensy_prePlane','orangecrab_ext_pll']
metric_names = list(load(f'/home/austin/krt_work/quality/out/json/d1.json')['sub_scores'].keys())

print("=== BASELINE per-board sub-scores ===")
print("board " + " ".join(f"{mn:>12}" for mn in metric_names))
for b in baseline:
    d = load(f'/home/austin/krt_work/quality/out/json/{b}.json')
    row = [f"{d['sub_scores'][mn]['sub_score'] if d['sub_scores'][mn]['sub_score'] is not None else -1:>12.2f}" for mn in metric_names]
    print(f"{b} " + " ".join(row))

print("\n=== CORPUS table data (segments, vias, routed nets) ===")
corpus = ['orangecrab_ext_pll','rp2350_fpga_eensy_prePlane','lvds_converter_dualclk_gnd','d1','d1_fixed2','routed_output','fanout_output2','fanout_output1','fanout_starting_point','qfn_fanned_out','qfn_diffpair_escape','qfn_interior_pads','qfn_underpad_coupling']
for b in corpus:
    d = load(f'/home/austin/krt_work/quality/out/corpus/{b}.json')
    print(f"{b}: segs={d['n_segments']} vias={d['n_vias']} routed_nets={d['n_routed_nets']} final={d['final_score']}")
