#!/usr/bin/env python3
"""Castellated landings are retracted to the pad's inner reach (run-6 fix 1.7).

The defect this guards (test-board run 5, journal + audit item 1.7): a
castellated pad's center sits ON the outline, so the router's landing there is
graded SEGMENT-BOARD-EDGE -- and run 5 hand-trimmed the same two landings
(y60.52/y80.48) every convergence lap. compute_castellated_landing_retract
pulls each such endpoint back ALONG its segment to the first point clearing
`edge_clearance + width/2 + margin`, capped at the pad rect so the landing
stays on pad copper; joints (two same-net ends at the point) are left alone.
The property itself must survive BOTH parse paths (text asserted here;
pcbnew's GetProperty twin is source-inspected).
"""
import os
import re
import sys

_TESTS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_TESTS)
for p in (ROOT, os.path.join(ROOT, 'py_router'),  # #522 layout
          os.path.join(ROOT, 'py_tools'), _TESTS):
    if p not in sys.path:
        sys.path.insert(0, p)

from synth import make_pad, make_seg, make_net, make_pcb  # noqa: E402
from kicad_parser import BoardInfo, Footprint  # noqa: E402
from pcb_modification import compute_castellated_landing_retract  # noqa: E402

BOARD = os.path.join(ROOT, 'kicad_files', 'splitflap_driver.kicad_pcb')


def _pcb(castellated=True, joint=False):
    """10x10 board; net-1 pad centered ON the right edge (x=10), a track
    landing on the pad center from the left."""
    pad = make_pad(net_id=1, x=10.0, y=5.0, size_x=1.0, size_y=0.6,
                   ref='J1', num='1', net_name='SIG', castellated=castellated)
    fp = Footprint(reference='J1', footprint_name='t:cast', x=10.0, y=5.0,
                   rotation=0.0, layer='F.Cu', pads=[pad])
    segs = [make_seg(7.0, 5.0, 10.0, 5.0, net_id=1, width=0.2)]
    if joint:
        segs.append(make_seg(10.0, 5.0, 10.0, 4.8, net_id=1, width=0.2))
    bi = BoardInfo(layers={0: 'F.Cu', 31: 'B.Cu'},
                   copper_layers=['F.Cu', 'B.Cu'],
                   board_bounds=(0.0, 0.0, 10.0, 10.0))
    return make_pcb(nets={1: make_net(1, 'SIG')}, segments=segs,
                    footprints={'J1': fp}, pads_by_net={1: [pad]},
                    board_info=bi)


def test_landing_is_pulled_to_the_inner_reach():
    delta = compute_castellated_landing_retract(_pcb(), 0.3)
    assert len(delta.moves) == 1, delta.moves
    s, end, nx, ny = delta.moves[0]
    assert end == 'end' and ny == 5.0, delta.moves[0]
    # required = 0.3 + 0.1 + 0.02 = 0.42 from the x=10 edge, sampled at 0.01
    assert 9.55 <= nx <= 9.59, f"expected ~9.58, got {nx}"
    # and the landing stays on pad copper (pad spans x 9.5..10.5)
    assert nx >= 9.5
    print(f"  PASS: landing pulled to x={nx} (clears 0.42, still on the pad)")


def test_without_the_property_nothing_moves():
    assert compute_castellated_landing_retract(_pcb(castellated=False), 0.3).is_empty
    print("  PASS: a non-castellated edge pad is not touched")


def test_a_joint_inside_the_pad_is_left_alone():
    delta = compute_castellated_landing_retract(_pcb(joint=True), 0.3)
    assert delta.is_empty, \
        f"moving one side of a joint would open it: {delta.moves}"
    print("  PASS: a two-segment joint at the landing is left alone")


def test_cap_at_the_pad_rect_when_clearing_is_impossible():
    # Edge rule so large the required distance can't be met inside the pad:
    # best effort = the pad's inner reach (x=9.5), never past it.
    delta = compute_castellated_landing_retract(_pcb(), 2.0)
    assert len(delta.moves) == 1
    _, _, nx, _ = delta.moves[0]
    assert abs(nx - 9.5) < 0.02, f"cap must be the pad's inner edge, got {nx}"
    print("  PASS: impossible clearance caps at the pad's inner reach")


def test_text_parser_reads_pad_prop_castellated():
    if not os.path.isfile(BOARD):
        print("  SKIP: fixture missing")
        return
    import tempfile
    import shutil
    from kicad_parser import parse_kicad_pcb
    with tempfile.TemporaryDirectory() as td:
        staged = os.path.join(td, 'b.kicad_pcb')
        shutil.copyfile(BOARD, staged)
        txt = open(staged, encoding='utf-8').read()
        # Mark the FIRST pad castellated, the way KiCad writes the property.
        txt = re.sub(r'(\(pad\s+"[^"]+"\s+\w+\s+\w+\s*\n)',
                     r'\1\t\t\t(property pad_prop_castellated)\n',
                     txt, count=1)
        open(staged, 'w', encoding='utf-8').write(txt)
        pcb = parse_kicad_pcb(staged)
        flags = [p.castellated for fp in pcb.footprints.values()
                 for p in fp.pads]
        assert sum(flags) == 1, f"expected exactly 1 castellated pad, got {sum(flags)}"
    print("  PASS: text parser sets pad.castellated from the property")


def test_live_board_path_has_the_parity_twin():
    """The live-board build path must carry pad.castellated too, or the retract
    is a no-op in the GUI while the CLI applies it.

    IPC branch: kipy's Pad proto has NO pad-property field (the SWIG path read
    PAD_PROP_CASTELLATED off the pcbnew pad), so build_pcb_data_from_board
    resolves it from the .kicad_pcb via read_castellated_pads_from_kicad_pcb --
    the same fallback this builder already uses for pinfunction/pintype and the
    keepout rule areas. Source inspection, the repo's accepted style for a path
    that needs a running KiCad."""
    src = open(os.path.join(ROOT, 'py_router', 'kicad_parser.py'),
               encoding='utf-8').read()
    assert 'def read_castellated_pads_from_kicad_pcb' in src, \
        "the IPC path needs a castellated reader (kipy exposes no pad property)"
    build = src[src.index('def build_pcb_data_from_board'):]
    assert 'read_castellated_pads_from_kicad_pcb' in build, \
        "build_pcb_data_from_board must resolve the castellated property"
    assert 'pad_obj.castellated = True' in build
    print("  PASS: live-board parse path carries the same flag")


if __name__ == '__main__':
    for k, v in sorted(globals().items()):
        if k.startswith('test_'):
            print(f"--- {k}")
            v()
    print("ALL PASS")
