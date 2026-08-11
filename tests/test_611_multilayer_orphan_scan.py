#!/usr/bin/env python3
"""#611 (#609 follow-up): the orphan-island scan must cover EVERY poured layer.

A multi-layer plane is analysed in ONE call with plane_layer = the first
poured layer, and the fill-model orphan scan only gathered that layer's
components -- so a pad-less island cut off on any OTHER poured layer was
structurally invisible: no region, no dropped tally, and the caller printed
"Zone is fully connected" while KiCad flagged 'Missing connection between
items'. Same story for a small KEPT island below the 25 mm^2 join bar on the
primary layer: the bar `continue`d it with no trace.

Report-only: none of these become join-eligible regions -- routed copper is
byte-identical; only the tallies/prints change.
"""
import io
import contextlib
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'py_router'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from synth import make_pad, make_seg, make_via, make_pcb, make_net
from kicad_parser import BoardInfo, Zone
from routing_config import GridRouteConfig
from plane_region_connector import find_disconnected_zone_regions

GND, SIG = 1, 2
BOUNDS = (0.5, 0.5, 39.5, 19.5)
_failures = []


def check(label, cond):
    print(f"{'PASS' if cond else 'FAIL'}: {label}")
    if not cond:
        _failures.append(label)


def build(cut_fcu=False, fcu_stub=False, fcu_via=False, bcu_pocket=False):
    """40x20 board, GND poured on BOTH B.Cu and F.Cu; THT GND pads on the
    left anchor both pours. Options:
      cut_fcu:    a SIG track cuts the F.Cu pour in half (B.Cu stays whole),
                  stranding a large pad-less island on the NON-primary layer
      fcu_stub:   a GND track fragment on the stranded F.Cu island (KiCad
                  KEEPS the island and flags it)
      fcu_via:    a GND via on the stranded F.Cu island -- it reaches the
                  intact B.Cu pour, so the island is genuinely connected
      bcu_pocket: a small SIG ring on B.Cu (the PRIMARY layer) enclosing a
                  GND stub -- a kept island well below the 25 mm^2 join bar
    """
    bi = BoardInfo(layers={0: 'F.Cu', 31: 'B.Cu'},
                   copper_layers=['F.Cu', 'B.Cu'],
                   board_bounds=(0.0, 0.0, 40.0, 20.0))
    pads = [make_pad(GND, 5.0, 5.0, ref='J1', num='1', net_name='GND',
                     size_x=1.0, size_y=1.0, layers=('*.Cu',), drill=0.8,
                     pad_type='thru_hole'),
            make_pad(GND, 5.0, 15.0, ref='J2', num='1', net_name='GND',
                     size_x=1.0, size_y=1.0, layers=('*.Cu',), drill=0.8,
                     pad_type='thru_hole')]
    sig = [make_pad(SIG, 20.0, 0.5, ref='U1', num='1', net_name='/SIG',
                    layers=('F.Cu',)),
           make_pad(SIG, 20.0, 19.5, ref='U2', num='1', net_name='/SIG',
                    layers=('F.Cu',))]
    segs = []
    vias = []
    if cut_fcu:
        segs.append(make_seg(20.0, 0.5, 20.0, 19.5, layer='F.Cu',
                             net_id=SIG, width=0.5))
    if fcu_stub:
        segs.append(make_seg(30.0, 9.0, 34.0, 9.0, layer='F.Cu',
                             net_id=GND, width=0.25))
    if fcu_via:
        vias.append(make_via(30.0, 10.0, net_id=GND))
    if bcu_pocket:
        # SIG ring on B.Cu around (32,10); the pour inside it is a GND
        # island of roughly 3x3 mm -- far below the 25 mm^2 join bar.
        for (x1, y1, x2, y2) in ((30, 8, 34, 8), (34, 8, 34, 12),
                                 (34, 12, 30, 12), (30, 12, 30, 8)):
            segs.append(make_seg(float(x1), float(y1), float(x2), float(y2),
                                 layer='B.Cu', net_id=SIG, width=0.5))
        segs.append(make_seg(31.5, 10.0, 32.5, 10.0, layer='B.Cu',
                             net_id=GND, width=0.3))
    zones = [Zone(net_id=GND, net_name='GND', layer=lyr,
                  polygon=[(0.5, 0.5), (39.5, 0.5), (39.5, 19.5), (0.5, 19.5)],
                  clearance=0.3, min_thickness=0.25, island_removal_mode=0)
             for lyr in ('B.Cu', 'F.Cu')]
    return make_pcb(nets={GND: make_net(GND, 'GND'), SIG: make_net(SIG, '/SIG')},
                    pads_by_net={GND: pads, SIG: sig}, board_info=bi,
                    segments=segs, vias=vias, zones=zones)


def regions(pcb):
    cfg = GridRouteConfig(grid_step=0.1, track_width=0.25, clearance=0.2,
                          layers=['F.Cu', 'B.Cu'])
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ra, rc, cp, ri = find_disconnected_zone_regions(
            GND, 'B.Cu', BOUNDS, pcb, cfg, 0.3, 0.5,
            ['F.Cu', 'B.Cu'], {'B.Cu', 'F.Cu'}, False)
    dropped = getattr(find_disconnected_zone_regions, '_last_dropped', (0, 0.0))
    kept = getattr(find_disconnected_zone_regions, '_last_kept_unjoined',
                   (0, 0.0, (), ()))
    return len(ra), dropped, kept[:3]


# --- Control: both pours intact -> connected, nothing tallied ---------------
n, (dn, dmm2), (kn, kmm2, klay) = regions(build())
check("intact 2-layer pour: 1 region, no tallies, no false alarm",
      n == 1 and dn == 0 and kn == 0)

# --- Bare island on the NON-primary layer: invisible before #611 ------------
n, (dn, dmm2), (kn, kmm2, klay) = regions(build(cut_fcu=True))
check("bare island cut off on F.Cu (non-primary): still not a region",
      n == 1 and kn == 0)
check("bare island cut off on F.Cu: REPORTED in the dropped tally",
      dn == 1 and dmm2 > 100.0)

# --- Kept island on the NON-primary layer: KiCad flags it -------------------
n, (dn, dmm2), (kn, kmm2, klay) = regions(build(cut_fcu=True, fcu_stub=True))
check("F.Cu island with a GND track: kept-unjoined tally names the layer",
      n == 1 and dn == 0 and kn == 1 and kmm2 > 100.0 and klay == ('F.Cu',))

# --- Stitching via makes it genuinely connected: no alarm at all ------------
n, (dn, dmm2), (kn, kmm2, klay) = regions(build(cut_fcu=True, fcu_via=True))
check("F.Cu island with a via to the intact B.Cu pour: no tallies",
      n == 1 and dn == 0 and kn == 0)

# --- Small kept island BELOW the join bar on the PRIMARY layer --------------
n, (dn, dmm2), (kn, kmm2, klay) = regions(build(bcu_pocket=True))
check("sub-bar kept island on B.Cu: tallied (was silently skipped)",
      n == 1 and kn == 1 and 1.0 <= kmm2 < 25.0 and klay == ('B.Cu',))
check("sub-bar kept island on B.Cu: never join-eligible (copper unchanged)",
      n == 1)

print()
if _failures:
    print(f"FAILED ({len(_failures)}): " + ", ".join(_failures))
    sys.exit(1)
print("All #611 multi-layer orphan-scan checks passed")
