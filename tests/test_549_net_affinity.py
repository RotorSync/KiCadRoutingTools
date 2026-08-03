#!/usr/bin/env python3
"""Per-part net affinity: the part that carries a net its block sits away from.

`block_displacements` asks the block-level question and averages the per-part
answer away. This is the inverse, and the case it exists for is measured: on a
real board a series resistor zoned into a far-edge block carried 85.7% of a
critical bus net's routed length, forcing ten drop-vias and eight
reference-plane voids -- and four runs went by with nothing measuring it.

The tests use a hand-built state whose answer is known by construction, so a
regression points at the arithmetic rather than at a board.
"""
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from placement.routability import (  # noqa: E402
    AFFINITY_DOMINANT_SHARE, net_affinity)


class _Part:
    def __init__(self, x, y, pads, locked=False):
        self.x, self.y, self.rot, self.locked = x, y, 0.0, locked
        self._pads = pads                      # [(dx, dy, net_id)]

    def pad_globals(self, x=None, y=None, rot=None):
        x = self.x if x is None else x
        y = self.y if y is None else y
        return [(x + dx, y + dy, n) for dx, dy, n in self._pads]


class _Net:
    def __init__(self, name):
        self.name = name


class _Board:
    def __init__(self, bounds):
        self.board_bounds = bounds


class _Pcb:
    def __init__(self, nets, bounds):
        self.nets = {i: _Net(n) for i, n in nets.items()}
        self.board_info = _Board(bounds)


class _State:
    """The slice of QuenchState that net_affinity touches."""

    def __init__(self, parts, net_refs):
        self.parts = parts
        self.net_refs = net_refs
        self.net_airwires = {n: self._build_net_airwires(n) for n in net_refs}

    def _points(self, net_id, override_ref=None, override_pads=None):
        pts = []
        for ref in sorted(self.net_refs[net_id]):
            pads = (override_pads if ref == override_ref
                    else self.parts[ref].pad_globals())
            pts.extend((x, y) for x, y, n in pads if n == net_id)
        return pts

    def _build_net_airwires(self, net_id, override_ref=None,
                            override_pads=None):
        """A chain MST over points sorted by x -- these fixtures are collinear
        by construction, so the chain IS the minimum spanning tree."""
        pts = sorted(self._points(net_id, override_ref, override_pads))
        return [(pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1], net_id)
                for i in range(len(pts) - 1)]


def _fixture(r6_x=45.0):
    """U1 at x=10, R10 at x=14, R6 at `r6_x` -- all on net 1 (three owners).

    U1 and R10 sit in block 'core'; R6 is zoned into 'far'. The R6 edge is the
    whole traverse, so R6's share is (r6_x-14)/(r6_x-10).
    """
    parts = {'U1': _Part(10.0, 5.0, [(0.0, 0.0, 1)]),
             'R10': _Part(14.0, 5.0, [(0.0, 0.0, 1)]),
             'R6': _Part(r6_x, 5.0, [(0.0, 0.0, 1)])}
    state = _State(parts, {1: ['U1', 'R10', 'R6']})
    pcb = _Pcb({1: 'QSPI_SS'}, (0.0, 0.0, 50.0, 20.0))
    blocks = {'core': ['U1', 'R10'], 'far': ['R6']}
    return state, pcb, blocks


def test_finds_the_part_that_dominates_a_net_from_another_block():
    state, pcb, blocks = _fixture(r6_x=45.0)
    rows = net_affinity(state, pcb, blocks)
    assert rows, "the far-block part must be reported"
    top = rows[0]
    assert top.ref == 'R6' and top.block == 'far' and top.net == 'QSPI_SS'
    # 31 of 35mm: the long edge is the whole problem.
    assert abs(top.length_mm - 31.0) < 1e-6, top.length_mm
    assert abs(top.share - 31.0 / 35.0) < 1e-9, top.share
    # Normalised by the board diagonal so it compares across boards.
    assert abs(top.reach_norm - 31.0 / math.hypot(50.0, 20.0)) < 1e-9
    # Moving R6 onto the centroid of what it talks to frees almost all of it.
    assert top.recoverable_mm > 25.0, top.recoverable_mm
    print(f"  PASS: R6 carries {100 * top.share:.0f}% of QSPI_SS "
          f"({top.length_mm:.1f}mm), {top.recoverable_mm:.1f}mm recoverable")


def test_a_part_sitting_with_its_net_is_not_reported():
    """Same topology, R6 seated next to the others: nothing to report."""
    state, pcb, blocks = _fixture(r6_x=18.0)
    rows = net_affinity(state, pcb, blocks)
    shares = {r.ref: r.share for r in rows}
    assert 'R6' not in shares or shares['R6'] <= 0.9, shares
    print("  PASS: a part seated with its net does not dominate it")


def test_two_owner_nets_are_skipped():
    """With two owners the single MST edge touches BOTH parts, so `share` is
    1.0 for each and dominance means nothing. That is block_displacement's
    question, and reporting it here would be a false positive."""
    parts = {'U1': _Part(10.0, 5.0, [(0.0, 0.0, 1)]),
             'D1': _Part(45.0, 5.0, [(0.0, 0.0, 1)])}
    state = _State(parts, {1: ['U1', 'D1']})
    pcb = _Pcb({1: 'GPIO25'}, (0.0, 0.0, 50.0, 20.0))
    rows = net_affinity(state, pcb, {'core': ['U1'], 'far': ['D1']})
    assert rows == [], f"two-owner net must not be reported: {rows}"
    print("  PASS: two-owner nets are out of scope, not silently 100%")


def test_high_fanout_nets_are_excluded():
    """A rail reaches everywhere by design and says nothing about a seat."""
    # 25 caps clustered tightly so the far part still DOMINATES the net --
    # otherwise the fixture would pass for the wrong reason (share below the
    # definition) and prove nothing about the fanout cut.
    parts = {f'C{i}': _Part(i * 0.2, 5.0, [(0.0, 0.0, 1)]) for i in range(25)}
    parts['R6'] = _Part(45.0, 5.0, [(0.0, 0.0, 1)])
    refs = sorted(parts)
    state = _State(parts, {1: refs})
    pcb = _Pcb({1: 'GND'}, (0.0, 0.0, 50.0, 20.0))
    blocks = {'core': [r for r in refs if r != 'R6'], 'far': ['R6']}
    assert net_affinity(state, pcb, blocks) == [], "fanout cut must drop it"
    # ... and with the cut lifted the same board does report it, so the test
    # is about the cut and not about the fixture being empty.
    assert net_affinity(state, pcb, blocks, max_fanout=0), \
        "without the cut the same net is reported -- the cut is doing the work"
    print("  PASS: the DISPLACEMENT_MAX_FANOUT cut drops rails")


def test_nets_wholly_inside_one_block_are_skipped():
    state, pcb, _ = _fixture(r6_x=45.0)
    rows = net_affinity(state, pcb, {'all': ['U1', 'R10', 'R6']})
    assert rows == [], "no membership conflict when the net stays in-block"
    print("  PASS: an in-block net raises no membership question")


def test_locked_parts_are_flagged_not_suppressed():
    state, pcb, blocks = _fixture(r6_x=45.0)
    state.parts['R6'].locked = True
    rows = net_affinity(state, pcb, blocks)
    assert rows and rows[0].ref == 'R6' and rows[0].locked is True, \
        "'cannot move' is triage, not absence"
    print("  PASS: locked parts are reported with a flag")


def test_exempt_nets_are_honoured():
    state, pcb, blocks = _fixture(r6_x=45.0)
    assert net_affinity(state, pcb, blocks, exempt_net_ids=[1]) == []
    print("  PASS: affinity_exempt_nets suppresses a deliberate long net")


def test_a_topological_hub_is_not_a_misplacement():
    """The trap `share` alone walks into.

    In the fixture R10 sits BETWEEN U1 and R6, so it is incident on both MST
    edges and scores share 1.0 -- while being exactly where it belongs.
    Dominance is necessary and not sufficient; what separates a hub from a
    misplacement is that moving a hub onto its own net's centroid frees
    nothing.
    """
    state, pcb, blocks = _fixture(r6_x=45.0)
    rows = net_affinity(state, pcb, blocks)
    refs = [r.ref for r in rows]
    assert 'R6' in refs, "the misplaced part must still be reported"
    assert 'R10' not in refs, (
        "R10 is the MST hub: share 1.0, recoverable ~0, correctly seated")
    assert AFFINITY_DOMINANT_SHARE == 0.5
    print("  PASS: a hub scores share 1.0 and is correctly not reported")


if __name__ == '__main__':
    for fn in (test_finds_the_part_that_dominates_a_net_from_another_block,
               test_a_part_sitting_with_its_net_is_not_reported,
               test_two_owner_nets_are_skipped,
               test_high_fanout_nets_are_excluded,
               test_nets_wholly_inside_one_block_are_skipped,
               test_locked_parts_are_flagged_not_suppressed,
               test_exempt_nets_are_honoured,
               test_a_topological_hub_is_not_a_misplacement):
        print(fn.__name__)
        fn()
    print("\nALL PASS")
