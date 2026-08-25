
import json, os

def load(p):
    with open(p) as f:
        return json.load(f)

corpus = [
  'orangecrab_ext_pll', 'rp2350_fpga_eensy_prePlane', 'lvds_converter_dualclk_gnd',
  'd1', 'd1_fixed2', 'routed_output', 'fanout_output2', 'fanout_output1',
  'fanout_starting_point', 'qfn_fanned_out', 'qfn_diffpair_escape',
  'qfn_interior_pads', 'qfn_underpad_coupling'
]
print("=== CORPUS final scores ===")
corp_scores = {}
for b in corpus:
    d = load(f'/home/austin/krt_work/quality/out/corpus/{b}.json')
    corp_scores[b] = d['final_score']
    print(f"{b}: {d['final_score']}")

print("\n=== CORPUS mean sub-scores (ranked) ===")
metric_names = list(load(f'/home/austin/krt_work/quality/out/corpus/d1.json')['sub_scores'].keys())
means = {}
for mn in metric_names:
    vals = []
    for b in corpus:
        d = load(f'/home/austin/krt_work/quality/out/corpus/{b}.json')
        ss = d['sub_scores'][mn]['sub_score']
        if ss is not None:
            vals.append(ss)
    means[mn] = sum(vals)/len(vals) if vals else None
for mn in sorted(means, key=lambda k: means[k]):
    print(f"{mn}: {means[mn]:.2f}")

print("\n=== CORPUS jog_chains raw detail ===")
for b in corpus:
    d = load(f'/home/austin/krt_work/quality/out/corpus/{b}.json')
    jc = d['metrics']['jog_chains']
    print(f"{b}: chains={jc['jog_chains']} excess={jc['excess_bends']} "
          f"jc/mm={jc['jog_chains_per_mm']:.4f} ex/mm={jc['excess_bends_per_mm']:.4f} "
          f"sub={d['sub_scores']['jog_chains']['sub_score']:.2f}")
