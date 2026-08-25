from kicad_parser import parse_kicad_pcb
pcb = parse_kicad_pcb('carrier_lab/d1.kicad_pcb')
print('d1 footprints:', len(pcb.footprints))
cnt = 0
rows = []
for f in pcb.footprints.values():
    for p in f.pads:
        if p.pinfunction or p.pintype:
            cnt += 1
            if len(rows) < 25:
                rows.append((f.reference, p.pad_number, p.net_name, p.pinfunction, p.pintype))
print('pads with meta:', cnt)
for r in rows:
    print(r)
