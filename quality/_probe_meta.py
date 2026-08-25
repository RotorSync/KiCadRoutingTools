import glob, os
from kicad_parser import parse_kicad_pcb

boards = ['kicad_files/routed_output.kicad_pcb', 'kicad_files/orangecrab_ext_pll.kicad_pcb',
          'kicad_files/rp2350_fpga_eensy_prePlane.kicad_pcb', 'kicad_files/fanout_output2.kicad_pcb',
          'kicad_files/lvds_converter_dualclk_gnd.kicad_pcb']
for b in boards:
    pcb = parse_kicad_pcb(b)
    cnt = 0
    samples = []
    for f in pcb.footprints.values():
        for p in f.pads:
            if p.pinfunction or p.pintype:
                cnt += 1
                if len(samples) < 10:
                    samples.append((f.reference, p.pad_number, p.net_name, p.pinfunction, p.pintype))
    print('==', b, 'pads with meta:', cnt)
    for s in samples:
        print('   ', s)
