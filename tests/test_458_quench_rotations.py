"""
Tests for quench rotation semantics (issue #458).

--no-rotate gated the nudge phase's rotation candidates and nothing else. A
same-footprint swap exchanges FULL poses, rotation included, so a 0-degree
part could still come out at 90 degrees under a flag documented as "disable
rotation moves". Swaps are now restricted to pairs that already share a
rotation when rotations are off, which also keeps the exchange
rotation-neutral: with identical rot-0 courtyards each part's rectangle is
then a pure translate of the other's, so the occupied-space invariance that
lets swaps skip candidate_valid is strengthened rather than weakened.

The discriminating pair is test_no_rotate_blocks_a_rotation_exchanging_swap
and test_no_rotate_still_allows_equal_rotation_swaps: same board, same
knobs, same improving swap, differing only in the two parts' seed angles.
"""

import math
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kicad_parser import parse_kicad_pcb
from placement.quench import quench

INTERF_U = os.path.join(os.path.dirname(__file__), '..', 'kicad_files',
                        'interf_u_unrouted.kicad_pcb')


def _cap_footprint(ref, x, y, net_id, net_name, rot=0.0):
    """Movable 2-pad part at an explicit rotation; pad 1 carries the net.

    Square pads on purpose. For a footprint without a courtyard the local
    bbox is derived from the pads, so oblong pads at different footprint
    rotations can produce different rot-0 bounds, and the swap identity guard
    would then reject the pair before any of this code runs, making every
    assertion below vacuously true."""
    return f'''\t(footprint "test:CAP2P"
\t\t(layer "F.Cu")
\t\t(uuid "fp-{ref}")
\t\t(at {x} {y} {rot})
\t\t(property "Reference" "{ref}"
\t\t\t(at 0 0)
\t\t)
\t\t(pad "1" smd rect
\t\t\t(at -0.5 0 {rot})
\t\t\t(size 0.8 0.8)
\t\t\t(layers "F.Cu")
\t\t\t(net {net_id} "{net_name}")
\t\t\t(uuid "p1-{ref}")
\t\t)
\t\t(pad "2" smd rect
\t\t\t(at 0.5 0 {rot})
\t\t\t(size 0.8 0.8)
\t\t\t(layers "F.Cu")
\t\t\t(uuid "p2-{ref}")
\t\t)
\t)
'''


def _anchor_footprint(ref, x, y, net_id, net_name):
    """Locked single-pad net anchor (a connector pin the airwire pulls at)."""
    return f'''\t(footprint "test:PIN"
\t\t(layer "F.Cu")
\t\t(locked yes)
\t\t(uuid "fp-{ref}")
\t\t(at {x} {y})
\t\t(property "Reference" "{ref}"
\t\t\t(at 0 0)
\t\t)
\t\t(pad "1" smd rect
\t\t\t(at 0 0)
\t\t\t(size 1 1)
\t\t\t(layers "F.Cu")
\t\t\t(net {net_id} "{net_name}")
\t\t\t(uuid "p1-{ref}")
\t\t)
\t)
'''


def _swap_board(d, c1_rot=0.0, c2_rot=90.0):
    """Two movable same-footprint parts, C1@(150,100) on net NA and
    C2@(150+d,100) on net NB, with locked anchors J1 (net NA) to the right of
    both and J2 (net NB) to the left. C1's net pulls it right and C2's pulls
    it left, so swapping them shortens both airwires by d."""
    body = '(kicad_pcb\n\t(version 20241229)\n'
    body += '\t(net 0 "")\n\t(net 1 "NA")\n\t(net 2 "NB")\n'
    body += '''\t(gr_rect
\t\t(start 100 80)
\t\t(end 200 120)
\t\t(stroke
\t\t\t(width 0.1)
\t\t\t(type solid)
\t\t)
\t\t(layer "Edge.Cuts")
\t\t(uuid "edge1")
\t)
'''
    body += _cap_footprint('C1', 150.0, 100.0, 1, 'NA', c1_rot)
    body += _cap_footprint('C2', 150.0 + d, 100.0, 2, 'NB', c2_rot)
    body += _anchor_footprint('J1', 150.0 + d + 20.0, 100.0, 1, 'NA')
    body += _anchor_footprint('J2', 130.0, 100.0, 2, 'NB')
    body += ')\n'
    fd, path = tempfile.mkstemp(suffix='.kicad_pcb')
    with os.fdopen(fd, 'w') as f:
        f.write(body)
    return path


# step=1000 makes the nudge grid degenerate to the seed point, so any change
# of position comes from the swap block.
SWAP_ONLY = dict(step=1000.0, max_passes=2)


def test_rotation_exchanging_swap_fires_when_rotations_are_allowed():
    """Anti-vacuity for the pair below, and the behaviour --no-rotate has to
    suppress: with rotations allowed the mixed-angle pair does swap, so the
    swap really is improving, in cap, and past the identity guard."""
    path = _swap_board(3, c1_rot=0.0, c2_rot=90.0)
    try:
        result = quench(parse_kicad_pcb(path), path, max_displacement=5.0,
                        allow_rotations=True, **SWAP_ONLY)
        by_ref = {r['reference']: r for r in result}
        assert set(by_ref) == {'C1', 'C2'}, f"fixture is vacuous: {result}"
        assert by_ref['C1']['new_x'] == 153.0, result
        assert by_ref['C2']['new_x'] == 150.0, result
    finally:
        os.unlink(path)


def test_no_rotate_blocks_a_rotation_exchanging_swap():
    """The fix: the same improving, in-cap swap is refused under --no-rotate,
    because accepting it would hand each part the other's rotation."""
    path = _swap_board(3, c1_rot=0.0, c2_rot=90.0)
    try:
        result = quench(parse_kicad_pcb(path), path, max_displacement=5.0,
                        allow_rotations=False, **SWAP_ONLY)
        assert result == [], \
            f"--no-rotate must not accept a swap that rotates both parts: " \
            f"{result}"
    finally:
        os.unlink(path)


def test_no_rotate_still_allows_equal_rotation_swaps():
    """--no-rotate is not a blanket swap kill. A same-angle pair still swaps,
    and the exchange is rotation-neutral, which is why it remains safe to
    skip candidate_valid: the occupied rectangles are unchanged."""
    path = _swap_board(3, c1_rot=90.0, c2_rot=90.0)
    try:
        result = quench(parse_kicad_pcb(path), path, max_displacement=5.0,
                        allow_rotations=False, **SWAP_ONLY)
        by_ref = {r['reference']: r for r in result}
        assert set(by_ref) == {'C1', 'C2'}, result
        assert by_ref['C1']['new_x'] == 153.0, result
        assert by_ref['C2']['new_x'] == 150.0, result
        assert by_ref['C1']['new_rotation'] == 90.0, result
        assert by_ref['C2']['new_rotation'] == 90.0, result
    finally:
        os.unlink(path)


def test_no_rotate_changes_no_rotation_on_a_real_board():
    """Contract check on a real board: a full --no-rotate run, nudges and
    swaps together, returns no part at an angle other than its seed. This one
    is a contract test, not a bug reproduction: whether it would have failed
    before the fix depends on whether a mixed-angle swap happens to be
    improving on this particular board."""
    pcb = parse_kicad_pcb(INTERF_U)
    seed = {ref: fp.rotation % 360 for ref, fp in pcb.footprints.items()}
    result = quench(pcb, INTERF_U, max_displacement=3.0, step=1.0,
                    max_passes=3, allow_rotations=False)
    assert result, "fixture should still produce improving moves"
    for placement in result:
        ref = placement['reference']
        assert placement['new_rotation'] == seed[ref], \
            f"{ref} rotated from {seed[ref]} to " \
            f"{placement['new_rotation']} under --no-rotate"


TESTS = [
    test_rotation_exchanging_swap_fires_when_rotations_are_allowed,
    test_no_rotate_blocks_a_rotation_exchanging_swap,
    test_no_rotate_still_allows_equal_rotation_swaps,
    test_no_rotate_changes_no_rotation_on_a_real_board,
]


if __name__ == '__main__':
    for fn in TESTS:
        fn()
    print("ALL PASS")
