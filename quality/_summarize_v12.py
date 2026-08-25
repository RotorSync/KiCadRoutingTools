
import json, os, glob

def load(path):
    with open(path) as f:
        return json.load(f)

# Baseline boards
baseline = [
  'd1', 'd1_fixed2', 'fanout_output2', 'routed_output',
  'rp2350_fpga_eensy_prePlane', 'orangecrab_ext_pll'
]
print("=== BASELINE final scores ===")
base_scores = {}
for b in baseline:
    d = load(f'/home/austin/krt_work/quality/out/json/{b}.json')
    base_scores[b] = d['final_score']
    print(f"{b}: {d['final_score']}")

# mean sub-scores across baseline
print("\n=== BASELINE mean sub-scores (ranked) ===")
metric_names = list(load(f'/home/austin/krt_work/quality/out/json/d1.json')['sub_scores'].keys())
means = {}
for mn in metric_names:
    vals = []
    for b in baseline:
        d = load(f'/home/austin/krt_work/quality/out/json/{b}.json')
        ss = d['sub_scores'][mn]['sub_score']
        if ss is not None:
            vals.append(ss)
    means[mn] = sum(vals)/len(vals) if vals else None
for mn in sorted(means, key=lambda k: means[k]):
    print(f"{mn}: {means[mn]:.2f}")
