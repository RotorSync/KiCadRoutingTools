import json, sys
boards = [
    ("carrier_lab/d1.kicad_pcb", "quality/out/json/d1.json"),
    ("carrier_lab/d1_fixed2.kicad_pcb", "quality/out/json/d1_fixed2.json"),
    ("kicad_files/fanout_output2.kicad_pcb", "quality/out/json/fanout_output2.json"),
    ("kicad_files/routed_output.kicad_pcb", "quality/out/json/routed_output.json"),
    ("kicad_files/rp2350_fpga_eensy_prePlane.kicad_pcb", "quality/out/json/rp2350_fpga_eensy_prePlane.json"),
    ("kicad_files/orangecrab_ext_pll.kicad_pcb", "quality/out/json/orangecrab_ext_pll.json"),
]
metrics = ["bends","off_angle","vias","pad_entry","fragmentation","parallel","channel","layer_direction","stubs"]
print("=== FINAL SCORES ===")
for name, path in boards:
    d = json.load(open(path))
    print(f"{name}: {d['final_score']}")
print()
print("=== SUB-SCORES (metric: raw / sub) ===")
for name, path in boards:
    d = json.load(open(path))
    print(f"--- {name} ---")
    for m in metrics:
        ss = d['sub_scores'][m]
        print(f"  {m:<16} raw={ss['raw']}  sub={ss['sub_score']}")
print()
print("=== MEAN SUB-SCORES ACROSS BOARDS ===")
from collections import defaultdict
acc = defaultdict(list)
for name, path in boards:
    d = json.load(open(path))
    for m in metrics:
        acc[m].append(d['sub_scores'][m]['sub_score'])
for m in metrics:
    vals = [v for v in acc[m] if v is not None]
    print(f"  {m:<16} mean={sum(vals)/len(vals):.1f}")
