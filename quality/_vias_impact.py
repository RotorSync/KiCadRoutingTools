import sys, json, os
sys.path.insert(0, "/home/austin/krt_work/py_router")
sys.path.insert(0, "/home/austin/krt_work/quality")
from kicad_parser import parse_kicad_pcb
import geometry as G

boards = [
    ("carrier_lab/d1.kicad_pcb", "d1"),
    ("carrier_lab/d1_fixed2.kicad_pcb", "d1_fixed2"),
    ("kicad_files/fanout_output2.kicad_pcb", "fanout_output2"),
    ("kicad_files/routed_output.kicad_pcb", "routed_output"),
    ("kicad_files/rp2350_fpga_eensy_prePlane.kicad_pcb", "rp2350_fpga_eensy_prePlane"),
    ("kicad_files/orangecrab_ext_pll.kicad_pcb", "orangecrab_ext_pll"),
]
print(f"{'board':<24} {'total':>5} {'routed':>6} {'nets':>4} {'old':>7} {'new':>7}")
for path, tag in boards:
    pcb = parse_kicad_pcb(path)
    by_net_layer = G.group_segments_by_net_layer(pcb.segments)
    routed_nets = set(by_net_layer.keys())
    vias_by_net = G.group_vias_by_net(pcb.vias)
    total = len(pcb.vias)
    rv = sum(len(v) for nid,v in vias_by_net.items() if nid in routed_nets)
    nnets = len(routed_nets)
    old = total/nnets if nnets else 0
    new = rv/nnets if nnets else 0
    print(f"{tag:<24} {total:>5} {rv:>6} {nnets:>4} {old:>7.4f} {new:>7.4f}")
