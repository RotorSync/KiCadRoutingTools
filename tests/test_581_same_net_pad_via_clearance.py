#!/usr/bin/env python3
"""
Gate for issue #581: an ACTIVE (> 0) same-net pad via clearance keeps EVERY
placed via off same-net SMD pads; -1 (unset) AND 0 preserve pre-#581 behavior
exactly.

Invariants:
  1. same_net_pad_via_keepout_cells: empty at -1 and 0 (compat contract),
     covers the pad + margin at > 0; through-hole pads exempt.
  2. .kicad_pro persistence: > 0 round-trips; 0 / -1 never recorded; absent
     reads as -1.
  3. #189 via-in-pad rescue (_place_shrunk_via_in_pad) declines when active.
  4. validate_single_swap declines a swap that needs a pad-centre via.
  5. build_via_obstacle_map resolves an active value from the CONFIG when the
     caller leaves the parameter unset (repair/finalize path), and an explicit
     caller 0 keeps its legacy meaning.
  6. add_same_net_via_clearance stamps the pad keep-out (Phase 3 path).

Run:
    python3 tests/test_581_same_net_pad_via_clearance.py
"""

import json
import os
import sys
import tempfile

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(TESTS_DIR)
sys.path.insert(0, ROOT_DIR)
sys.path.insert(0, os.path.join(ROOT_DIR, 'py_router'))  # #522
sys.path.insert(0, os.path.join(ROOT_DIR, 'py_tools'))  # #522
sys.path.insert(0, os.path.join(ROOT_DIR, 'rust_router'))

from kicad_parser import PCBData, Pad, Net, BoardInfo
from routing_config import GridRouteConfig, GridCoord
from obstacle_map import (same_net_pad_via_keepout_cells,
                          add_same_net_via_clearance)


def _pad(x, y, net_id=1, drill=0.0, size=1.0):
    return Pad(pad_number='1', net_id=net_id, net_name='N1',
               global_x=x, global_y=y, local_x=0, local_y=0,
               size_x=size, size_y=size, shape='rect',
               layers=['F.Cu'] if not drill else ['*.Cu'], drill=drill,
               pad_type='smd' if not drill else 'thru_hole',
               component_ref='U1')


def _pcb(pads):
    bi = BoardInfo(layers={0: 'F.Cu', 31: 'B.Cu'},
                   copper_layers=['F.Cu', 'B.Cu'])
    bi.board_bounds = (0.0, 0.0, 20.0, 20.0)
    by_net = {}
    for p in pads:
        by_net.setdefault(p.net_id, []).append(p)
    return PCBData(board_info=bi, nets={1: Net(1, 'N1'), 2: Net(2, 'N2')},
                   footprints={}, vias=[], segments=[], pads_by_net=by_net)


def main():
    fails = []

    def check(name, cond):
        print(f"  {'PASS' if cond else 'FAIL'}: {name}")
        if not cond:
            fails.append(name)

    # -- 1: keep-out cells activation contract --------------------------------
    print("1: same_net_pad_via_keepout_cells activation")
    pcb = _pcb([_pad(10, 10)])
    for v, expect_cells in ((-1.0, False), (0.0, False), (0.3, True)):
        cfg = GridRouteConfig(layers=['F.Cu', 'B.Cu'], via_size=0.5,
                              clearance=0.2)
        cfg.same_net_pad_clearance = v
        cells = same_net_pad_via_keepout_cells(pcb, 1, cfg)
        check(f"snpc={v}: {'cells' if expect_cells else 'EMPTY'}",
              bool(len(cells)) == expect_cells)
    cfg = GridRouteConfig(layers=['F.Cu', 'B.Cu'], via_size=0.5, clearance=0.2)
    cfg.same_net_pad_clearance = 0.3
    cells = {tuple(r) for r in
             same_net_pad_via_keepout_cells(pcb, 1, cfg).tolist()}
    coord = GridCoord(cfg.grid_step)
    check("pad centre blocked", coord.to_grid(10, 10) in cells)
    # edge of margin: pad half 0.5 + via/2 0.25 + clr 0.3 = 1.05 from centre
    check("cell just inside margin blocked", coord.to_grid(10, 11.0) in cells)
    check("cell well outside margin free", coord.to_grid(10, 11.5) not in cells)
    th = _pcb([_pad(10, 10, drill=0.8)])
    check("through-hole pad exempt",
          len(same_net_pad_via_keepout_cells(th, 1, cfg)) == 0)

    # -- 2: persistence -------------------------------------------------------
    print("2: .kicad_pro persistence")
    from protected_nets import (persist_same_net_pad_clearance,
                                read_same_net_pad_clearance)
    with tempfile.TemporaryDirectory() as td:
        pro = os.path.join(td, 'b.kicad_pro')
        with open(pro, 'w') as f:
            json.dump({}, f)
        check("0 not recorded",
              not persist_same_net_pad_clearance(pro, 0.0, verbose=False))
        check("-1 not recorded",
              not persist_same_net_pad_clearance(pro, -1.0, verbose=False))
        check("absent reads -1", read_same_net_pad_clearance(pro) == -1.0)
        check("0.3 recorded",
              persist_same_net_pad_clearance(pro, 0.3, verbose=False))
        check("0.3 read back", read_same_net_pad_clearance(pro) == 0.3)

    # -- 3: #189 rescue gate --------------------------------------------------
    # With #581 active the IN-PAD arm is forbidden; the #535 off-pad escape
    # rung is the compliant rescue. Accept either outcome: None, or an
    # OFF-pad via (with its escape stub) that itself honors the clearance.
    print("3: via-in-pad rescue is #581-compliant when active")
    from single_ended_routing import _place_shrunk_via_in_pad
    import math as _math
    cfg_on = GridRouteConfig(layers=['F.Cu', 'B.Cu'])
    cfg_on.same_net_pad_clearance = 0.3
    res = _place_shrunk_via_in_pad(_pad(10, 10), None, cfg_on, pcb, 1,
                                   GridCoord(0.1), ['F.Cu', 'B.Cu'])
    if res is None:
        check("rescue declined (no compliant escape)", True)
    else:
        _via, _cell, _pli, _stub = res
        # pad is 1.0mm square at (10,10): via edge to pad edge >= 0.3
        _edge_d = (max(abs(_via.x - 10.0), abs(_via.y - 10.0)) - 0.5
                   - _via.size / 2.0)
        check("escape via keeps the same-net pad clearance",
              _edge_d >= 0.3 - 1e-6)
        check("escape ships its pad->via stub", bool(_stub))
    # In-pad arm stays forbidden either way: the via must never sit ON the pad
    if res is not None:
        check("via is OFF the pad",
              max(abs(res[0].x - 10.0), abs(res[0].y - 10.0)) > 0.5)

    # -- 4: swap validator gate ----------------------------------------------
    print("4: pad-via swap declined when active")
    from stub_layer_switching import StubInfo, validate_single_swap
    from kicad_parser import Segment
    stub_seg = Segment(start_x=10, start_y=10, end_x=11, end_y=10,
                       width=0.2, layer='F.Cu', net_id=1)
    stub = StubInfo(net_id=1, x=11, y=10, layer='F.Cu', segments=[stub_seg],
                    pad_x=10, pad_y=10, has_pad_via=False)  # needs a pad via
    pcb4 = _pcb([_pad(10, 10)])
    pcb4.segments.append(stub_seg)
    ok, why = validate_single_swap(stub, 'B.Cu', {}, pcb4, cfg_on)
    check("swap declined with #581 reason", not ok and '581' in why)
    cfg_off = GridRouteConfig(layers=['F.Cu', 'B.Cu'])
    ok_off, _ = validate_single_swap(stub, 'B.Cu', {}, pcb4, cfg_off)
    check("swap allowed when inactive", ok_off)

    # -- 5: build_via_obstacle_map config resolution --------------------------
    print("5: build_via_obstacle_map resolves from config")
    from plane_obstacle_builder import build_via_obstacle_map
    coord = GridCoord(cfg_on.grid_step)
    gx, gy = coord.to_grid(10, 10)
    m_on = build_via_obstacle_map(pcb, cfg_on, 1, verbose=False)  # param unset
    check("target pad blocked via config value", m_on.is_via_blocked(gx, gy))
    m_off = build_via_obstacle_map(pcb, cfg_off, 1, verbose=False)
    check("inactive config leaves target pad open",
          not m_off.is_via_blocked(gx, gy))
    # explicit legacy 0 still blocks (stitching semantics preserved)
    m_zero = build_via_obstacle_map(pcb, cfg_off, 1, verbose=False,
                                    same_net_pad_clearance=0.0)
    check("explicit 0 keeps legacy blocking", m_zero.is_via_blocked(gx, gy))

    # -- 6: Phase 3 path (add_same_net_via_clearance) -------------------------
    print("6: add_same_net_via_clearance stamps the keep-out")
    from obstacle_map import GridObstacleMap
    obs = GridObstacleMap(2)
    add_same_net_via_clearance(obs, pcb, 1, cfg_on)
    check("pad centre via-blocked", obs.is_via_blocked(gx, gy))
    obs2 = GridObstacleMap(2)
    add_same_net_via_clearance(obs2, pcb, 1, cfg_off)
    check("inactive: pad centre open", not obs2.is_via_blocked(gx, gy))

    print()
    if fails:
        print(f"FAILED: {len(fails)} check(s): {fails}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == '__main__':
    sys.exit(main())
