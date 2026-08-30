#!/usr/bin/env python3
"""Regression test: beautify_stub_repair Phase-3 safe-spur-trim must judge each
candidate against the EVOLVING board state, not a stale pre-loop snapshot.

The bug: cur_segs / rem_ids / pcb_data.segments were built once before the Phase-3
loop and never refreshed on acceptance, so a later candidate's connectivity gate
and chain walk saw the copper that earlier trims had already removed. Two
individually-safe trims on one net -- e.g. redundant parallel paths to a pad left
by rip-up/retry -- could each pass against the original board and together remove
a through-path.

Construction (the redundant-path case):
  pad Q at (0,0)
  A:(0,0)->(1,0)
  B:(1,0)->(2,0)          [through-path segment]
  T1:(1,0)->(1,-1)        [dangling tail at junction (1,0)]
  T2:(2,0)->(2,-1)        [dangling tail at junction (2,0)]

Initial chains (walk from each dangling end):
  Chain((1,-1)): T1 -> (1,0) has A,B,T1 = 3 -> branch. Chain = [T1].
  Chain((2,-1)): T2 -> (2,0) has B,T2 = 2 -> continue -> B -> (1,0) has A,B,T1
    = 3 -> branch. Chain = [T2, B].

OLD logic (stale snapshot): removes [T1] then [T2,B] -- both pass against the
original board. B -- a through-path segment -- is removed.

FIXED logic (evolving state): removes [T1], then re-walks (2,-1) against the
evolved board {A,B,T2}: T2 -> (2,0) has B,T2 = 2 -> B -> (1,0) has A,B = 2 ->
A -> Q = anchor -> 'anchor' chain -> skipped. Only [T1] is removed; B survives.

Run: python3 tests/test_stub_repair_evolving_gate.py
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, 'py_router'))

from kicad_parser import Segment, PCBData, BoardInfo
from routing_config import GridRouteConfig
from beautify_stub_repair import beautify_stub_repair


def _seg(x1, y1, x2, y2, nid=1):
    return Segment(start_x=x1, start_y=y1, end_x=x2, end_y=y2,
                   width=0.25, layer='F.Cu', net_id=nid)


class _Pad:
    pass


def _make_pcb(segs):
    pads = []
    p = _Pad()
    p.global_x = 0.0
    p.global_y = 0.0
    p.size_x = 0.5
    p.size_y = 0.5
    p.net_id = 1
    p.layers = ['F.Cu', 'B.Cu']
    p.pad_number = '1'
    p.component_ref = 'U1'
    p.net_name = 'N1'
    p.shape = 'rect'
    p.drill = 0
    p.hole_x = None
    p.hole_y = None
    p.pad_type = 'smd'
    p.local_x = 0.0
    p.local_y = 0.0
    p.rect_rotation = 0
    p.castellated = False
    p.local_clearance = 0
    p.pinfunction = ''
    p.pintype = ''
    pads.append(p)
    bi = BoardInfo(copper_layers=['F.Cu', 'B.Cu'],
                   layers={0: 'F.Cu', 1: 'B.Cu'},
                   board_bounds=None,
                   stackup=[])
    net = type('N', (), {'net_id': 1, 'name': 'N1', 'pads': pads})()
    return PCBData(board_info=bi, nets={1: net}, footprints={},
                   vias=[], segments=segs, pads_by_net={1: pads})


def _removed_sigs(segs):
    """Return {(x1,y1,x2,y2)} normalized (min endpoint first) for removed segs."""
    out = set()
    for s in segs:
        a = (round(s.start_x, 3), round(s.start_y, 3))
        b = (round(s.end_x, 3), round(s.end_y, 3))
        out.add(tuple(sorted([a, b])))
    return out


def main():
    fails = []

    def check(name, cond, detail=""):
        print(("  PASS " if cond else "  FAIL ") + name + (f"  {detail}" if detail else ""))
        if not cond:
            fails.append(name)

    # The redundant-path construction.
    segs = [
        _seg(0, 0, 1, 0),      # A: Q -> J1
        _seg(1, 0, 2, 0),      # B: J1 -> J2 (through-path)
        _seg(1, 0, 1, -1),     # T1: tail at J1
        _seg(2, 0, 2, -1),     # T2: tail at J2
    ]
    pcb = _make_pcb(segs)
    cfg = GridRouteConfig(clearance=0.5, board_edge_clearance=0.0)
    removed, added = beautify_stub_repair(pcb, config=cfg,
                                          scope_net_ids=None,
                                          skip_net_ids=set())
    rem_sigs = _removed_sigs(removed)

    # The through-path segment B:(1,0)->(2,0) must NOT be removed: after T1 is
    # trimmed first, B is on the surviving path to the pad and the re-walk of
    # T2's endpoint sees an 'anchor' chain (skipped). The stale-snapshot logic
    # removed B along with T2.
    check("through-path segment B survives",
          ((1.0, 0.0), (2.0, 0.0)) not in rem_sigs,
          f"removed={sorted(rem_sigs)}")

    # At most one of the two tails is trimmed (the first one processed); the
    # second becomes an anchor chain in the evolved state and is skipped.
    tails = {((1.0, 0.0), (1.0, -1.0)), ((2.0, 0.0), (2.0, -1.0))}
    removed_tails = tails & rem_sigs
    check("at most one tail trimmed",
          len(removed_tails) <= 1,
          f"removed_tails={removed_tails}")

    # Connectivity must be preserved: pad Q still connected after the pass.
    from check_connected import check_net_connectivity as cnc
    rem_ids = {id(s) for s in removed}
    kept = [s for s in pcb.segments if id(s) not in rem_ids]
    kept.extend(added)
    res = cnc(1, [s for s in kept if s.net_id == 1], [],
              pcb.pads_by_net.get(1, []), None, pcb_data=pcb)
    check("pad stays connected",
          res['connected'] and res['num_components'] == 1,
          f"connected={res['connected']} comp={res['num_components']}")

    print(f"{len(fails)} failure(s)")
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(main())
