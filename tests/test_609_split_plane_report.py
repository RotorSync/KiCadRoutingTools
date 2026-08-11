#!/usr/bin/env python3
"""#609: a pour split by routing must not be reported "fully connected".

Routing a signal net across a poured layer islands the pour. The fill-model
region discovery FINDS the island, but under `island_removal_mode` 0 -- the
KiCad default, and what route_planes writes -- it used to discard every
pad-less island wholesale, with no area bar and no per-island judgement. The
region list then held one entry and the caller printed "Zone is fully
connected (N anchors in 1 region)" over a plane it had just found split.

Two distinct island classes have to come out differently, and the old code
collapsed them:

  A. a TRULY BARE island -- KiCad's filler erases it on refill, so strapping
     it would ship copper that is never poured (the duodyne finding). Still
     not joined, but the split is now REPORTED instead of denied.
  B. an island carrying same-net copper (a track/via/pad on it) -- the filler
     KEEPS it and DRC then flags it against the rest of the net. This is the
     real missing-connection case, and it must be joined.
"""
import io
import contextlib
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'py_router'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from synth import make_pad, make_seg, make_pcb, make_net
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


def build(cut=True, stray_track=False, right_pad=False, mode=0):
    """A 40x20 board, GND poured on B.Cu, optionally cut in half by a signal
    track. Both GND pads sit on the LEFT half, so the right half is stranded."""
    bi = BoardInfo(layers={0: 'F.Cu', 31: 'B.Cu'},
                   copper_layers=['F.Cu', 'B.Cu'],
                   board_bounds=(0.0, 0.0, 40.0, 20.0))
    pads = [make_pad(GND, 5.0, 5.0, ref='J1', num='1', net_name='GND',
                     size_x=1.0, size_y=1.0, layers=('B.Cu',), drill=0.8,
                     pad_type='thru_hole'),
            make_pad(GND, 5.0, 15.0, ref='J2', num='1', net_name='GND',
                     size_x=1.0, size_y=1.0, layers=('B.Cu',), drill=0.8,
                     pad_type='thru_hole')]
    if right_pad:
        pads.append(make_pad(GND, 35.0, 10.0, ref='J3', num='1',
                             net_name='GND', size_x=1.0, size_y=1.0,
                             layers=('B.Cu',), drill=0.8,
                             pad_type='thru_hole'))
    sig = [make_pad(SIG, 20.0, 0.5, ref='U1', num='1', net_name='/SIG',
                    layers=('B.Cu',)),
           make_pad(SIG, 20.0, 19.5, ref='U2', num='1', net_name='/SIG',
                    layers=('B.Cu',))]
    segs = []
    if cut:
        segs.append(make_seg(20.0, 0.5, 20.0, 19.5, layer='B.Cu',
                             net_id=SIG, width=0.5))
    if stray_track:
        segs.append(make_seg(30.0, 9.0, 34.0, 9.0, layer='B.Cu',
                             net_id=GND, width=0.25))
    zone = Zone(net_id=GND, net_name='GND', layer='B.Cu',
                polygon=[(0.5, 0.5), (39.5, 0.5), (39.5, 19.5), (0.5, 19.5)],
                clearance=0.3, min_thickness=0.25, island_removal_mode=mode)
    return make_pcb(nets={GND: make_net(GND, 'GND'), SIG: make_net(SIG, '/SIG')},
                    pads_by_net={GND: pads, SIG: sig}, board_info=bi,
                    segments=segs, zones=[zone])


def regions(pcb):
    cfg = GridRouteConfig(grid_step=0.1, track_width=0.25, clearance=0.2,
                          layers=['F.Cu', 'B.Cu'])
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ra, rc, cp, ri = find_disconnected_zone_regions(
            GND, 'B.Cu', BOUNDS, pcb, cfg, 0.3, 0.5,
            ['F.Cu', 'B.Cu'], {'B.Cu'}, False)
    dropped = getattr(find_disconnected_zone_regions, '_last_dropped', (0, 0.0))
    return len(ra), dropped


# --- Control: an intact pour is connected, and reports nothing --------------
n, (drop_n, drop_mm2) = regions(build(cut=False))
check("intact pour: 1 region, nothing dropped, no false alarm",
      n == 1 and drop_n == 0)

# --- A. bare stranded island: not joined, but REPORTED ----------------------
n, (drop_n, drop_mm2) = regions(build())
check("cut pour, bare stranded island: still not joined (filler erases it)",
      n == 1)
check("cut pour, bare stranded island: the split is REPORTED, not denied",
      drop_n == 1 and drop_mm2 > 100.0)

# --- B. island carrying same-net copper: the filler KEEPS it, so JOIN it ----
# This is the case that shipped a real open: KiCad keeps the island and DRC
# flags 'Missing connection between items', while the region pass said the
# zone was whole and did nothing.
n, (drop_n, drop_mm2) = regions(build(stray_track=True))
check("stranded island with a GND track on it -> 2 regions, joins attempted",
      n == 2 and drop_n == 0)

# --- A pad in the stranded half was always caught; keep it that way ---------
n, _ = regions(build(right_pad=True))
check("stranded island with a GND pad -> 2 regions (unchanged)", n == 2)

# --- island_removal_mode 1 keeps every island, so it must always join -------
n, (drop_n, _m) = regions(build(mode=1))
check("mode 1 (never remove islands): bare island still joined",
      n == 2 and drop_n == 0)

print()
if _failures:
    print(f"{len(_failures)} FAILURE(S): " + "; ".join(_failures))
    sys.exit(1)
print("All #609 split-plane checks passed")
