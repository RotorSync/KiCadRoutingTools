import sys
sys.path.insert(0, '/home/austin/krt_work/py_router')
sys.path.insert(0, '/home/austin/krt_work/rust_router')
sys.path.insert(0, '/home/austin/krt_work/quality')
from kicad_parser import PCBData, BoardInfo, Net, Segment
import score

def make_board(victim_segs, aggr_segs, copper_layers=('F.Cu','In1.Cu','B.Cu')):
    info = BoardInfo(layers={i: l for i, l in enumerate(copper_layers)},
                     copper_layers=list(copper_layers))
    nets = {1: Net(1, 'UART_TX', []), 2: Net(2, '+5V_SW', [])}
    segs = []
    for (x1,y1,x2,y2,layer) in victim_segs:
        segs.append(Segment(x1,y1,x2,y2,0.2,layer,1))
    for (x1,y1,x2,y2,layer) in aggr_segs:
        segs.append(Segment(x1,y1,x2,y2,0.5,layer,2))
    return PCBData(board_info=info, nets=nets, footprints={}, vias=[],
                   segments=segs, pads_by_net={})

# Case A: UART_TX hugs +5V_SW for 40mm at 0.4mm separation (parallel)
# Case B: UART_TX crosses +5V_SW once perpendicularly
# Case C: UART_TX runs 40mm but 5mm away (outside window)
# Case D: UART_TX on F.Cu, +5V_SW on B.Cu with GND plane on In1.Cu (shielded)
# Case E: UART_TX on F.Cu, +5V_SW on B.Cu with NO plane (2-layer)

caseA = make_board([(0,0,40,0,'F.Cu')], [(0,0.4,40,0.4,'F.Cu')])
caseB = make_board([(0,0,40,0,'F.Cu')], [(20,-5,20,5,'F.Cu')])
caseC = make_board([(0,0,40,0,'F.Cu')], [(0,5,40,5,'F.Cu')])
caseD = make_board([(0,0,40,0,'F.Cu')], [(0,0,40,0,'B.Cu')], ('F.Cu','In1.Cu','B.Cu'))
caseE = make_board([(0,0,40,0,'F.Cu')], [(0,0,40,0,'B.Cu')], ('F.Cu','B.Cu'))

for name, b in [('A hug 40mm@0.4', caseA), ('B cross once', caseB),
                ('C 40mm@5mm away', caseC), ('D shielded by GND plane', caseD),
                ('E broadside no plane', caseE)]:
    sic = score.metric_si_coupling(b)
    print(f'{name:<28} value={sic["value"]:.4f} pairs={sic["n_exposed_pairs"]}')
