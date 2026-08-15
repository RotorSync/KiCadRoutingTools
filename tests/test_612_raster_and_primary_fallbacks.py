#!/usr/bin/env python3
"""#612: multi-layer blind spots deferred from #611, now closed.

1. The RASTER fallback discovery (no fill models) was primary-layer-only:
   pad-less islands on other poured layers were invisible, filler-erased
   islands were never counted, and the kept tally was never set so the #611
   per-layer follow-up joins could not fire.
2. Fill-model discovery was gated on the PRIMARY layer's model building --
   an unmodelable first layer dropped the whole net to the raster path even
   when every other poured layer modeled fine. repair_planes now prefers a
   modeled layer as primary.

Run directly (no pytest needed, matching the rest of tests/):
    python3 tests/test_612_raster_and_primary_fallbacks.py
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
import plane_fill_model as PFM

GND, SIG = 1, 2
BOUNDS = (0.5, 0.5, 39.5, 19.5)
_failures = []


def check(label, cond):
    print(f"{'PASS' if cond else 'FAIL'}: {label}")
    if not cond:
        _failures.append(label)


def build(cut_fcu=False, fcu_stub=False, cut_bcu=False):
    """Same board family as test_611: GND poured on B.Cu + F.Cu, THT GND
    pads on the left. cut_fcu/cut_bcu slice the respective pour in half;
    fcu_stub puts a GND track fragment on the stranded F.Cu island."""
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
    if cut_fcu:
        segs.append(make_seg(20.0, 0.5, 20.0, 19.5, layer='F.Cu',
                             net_id=SIG, width=0.5))
    if cut_bcu:
        segs.append(make_seg(20.0, 0.5, 20.0, 19.5, layer='B.Cu',
                             net_id=SIG, width=0.5))
    if fcu_stub:
        segs.append(make_seg(30.0, 9.0, 34.0, 9.0, layer='F.Cu',
                             net_id=GND, width=0.25))
    zones = [Zone(net_id=GND, net_name='GND', layer=lyr,
                  polygon=[(0.5, 0.5), (39.5, 0.5), (39.5, 19.5), (0.5, 19.5)],
                  clearance=0.3, min_thickness=0.25, island_removal_mode=0)
             for lyr in ('B.Cu', 'F.Cu')]
    return make_pcb(nets={GND: make_net(GND, 'GND'), SIG: make_net(SIG, '/SIG')},
                    pads_by_net={GND: pads, SIG: sig}, board_info=bi,
                    segments=segs, zones=zones)


def regions(pcb, primary='B.Cu'):
    cfg = GridRouteConfig(grid_step=0.1, track_width=0.25, clearance=0.2,
                          layers=['F.Cu', 'B.Cu'])
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ra, rc, cp, ri = find_disconnected_zone_regions(
            GND, primary, BOUNDS, pcb, cfg, 0.3, 0.5,
            ['F.Cu', 'B.Cu'], {'B.Cu', 'F.Cu'}, False)
    dropped = getattr(find_disconnected_zone_regions, '_last_dropped', (0, 0.0))
    kept = getattr(find_disconnected_zone_regions, '_last_kept_unjoined',
                   (0, 0.0, (), ()))
    return len(ra), dropped, kept[:3]


_orig_gfm = PFM.get_fill_models


def _no_models(pcb_data, net_id):
    return {}


def _no_bcu_model(pcb_data, net_id):
    out = dict(_orig_gfm(pcb_data, net_id))
    out.pop('B.Cu', None)
    return out


# =========================================================================
# 1. Raster fallback (fill models unavailable)
# =========================================================================
PFM.get_fill_models = _no_models
try:
    # Control: intact pours -> connected, no tallies, no false alarms.
    n, (dn, dmm2), (kn, kmm2, klay) = regions(build())
    check("raster control: 1 region, no tallies", n == 1 and dn == 0 and kn == 0)

    # Bare island on the PRIMARY layer: was appended nowhere and counted
    # nowhere -> "fully connected". Now counted as filler-erased.
    n, (dn, dmm2), (kn, kmm2, klay) = regions(build(cut_bcu=True))
    check("raster: bare island on B.Cu (primary) lands in the dropped tally",
          dn == 1 and dmm2 > 100.0 and kn == 0)

    # Bare island on a NON-primary layer: the whole-path #611 blind spot.
    n, (dn, dmm2), (kn, kmm2, klay) = regions(build(cut_fcu=True))
    check("raster: bare island on F.Cu (non-primary) in the dropped tally",
          dn == 1 and dmm2 > 100.0 and kn == 0)

    # Kept island on a NON-primary layer: tally set -> follow-up joins fire.
    n, (dn, dmm2), (kn, kmm2, klay) = regions(build(cut_fcu=True,
                                                    fcu_stub=True))
    check("raster: kept island on F.Cu lands in the kept tally",
          kn == 1 and kmm2 > 100.0 and klay == ('F.Cu',) and dn == 0)

    # End to end without any fill model: primary call tallies, follow-up
    # call (F.Cu primary, also raster) joins, island connected after.
    pcb = build(cut_fcu=True, fcu_stub=True)
    from repair_planes import repair_planes as _repair
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        _repair('', '', ['GND', 'GND'], ['B.Cu', 'F.Cu'], pcb_data=pcb,
                return_results=True, repair_pads=False,
                routing_layers=['F.Cu', 'B.Cu'])
    out = buf.getvalue()
    check("raster repair: follow-up join fires without fill models",
          '#611 follow-up join with F.Cu primary' in out)
    check("raster repair: the join routed",
          'All 1 route(s) succeeded' in out)
    n, (dn, dmm2), (kn, kmm2, klay) = regions(pcb)
    check("raster repair: island connected afterwards",
          n == 1 and dn == 0 and kn == 0)
finally:
    PFM.get_fill_models = _orig_gfm

# =========================================================================
# 2. Primary layer's model unavailable -> pick a modeled layer as primary
# =========================================================================
PFM.get_fill_models = _no_bcu_model
try:
    pcb = build(cut_fcu=True, fcu_stub=True)
    from repair_planes import repair_planes as _repair
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        _repair('', '', ['GND', 'GND'], ['B.Cu', 'F.Cu'], pcb_data=pcb,
                return_results=True, repair_pads=False,
                routing_layers=['F.Cu', 'B.Cu'])
    out = buf.getvalue()
    check("gap 7: unmodelable B.Cu -> F.Cu promoted to primary",
          '#612: B.Cu has no usable fill model' in out
          and 'using F.Cu as the primary analysis layer' in out)
    # With F.Cu primary the kept island is an ordinary fill-model orphan
    # region: joined directly, no follow-up needed.
    check("gap 7: fill-model discovery ran (not the raster fallback)",
          'Region discovery from fill model:' in out)
    check("gap 7: the island was joined",
          'All 1 route(s) succeeded' in out)
finally:
    PFM.get_fill_models = _orig_gfm

print()
if _failures:
    print(f"FAILED ({len(_failures)}): " + ", ".join(_failures))
    sys.exit(1)
print("All #612 raster/primary-fallback checks passed")
