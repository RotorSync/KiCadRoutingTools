#!/usr/bin/env python3
"""The corridor chord kernel, against closed forms.

`foreign_crossings` COUNTS that an airwire pierces a bus corridor. A count is
piecewise constant in pose, so a greedy descent gets no direction from it: a
part can slide halfway out of a corridor and the number never moves. This
kernel measures the LENGTH cut instead, which is piecewise linear in pose and
prices obliqueness for free.

Every case here has an answer derivable by hand, so a failure points at the
clipper rather than at a board.
"""
import math
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'py_placer'))  # placement split
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'py_router'))  # placement split
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'py_tools'))  # placement split

from placement.quench import _CorridorBox, _corridor_cut_np  # noqa: E402

WIDTH = 4.0
NETS = 8


def _box(ax=0.0, ay=0.0, angle_deg=0.0, length=20.0, width=WIDTH, own=()):
    """A corridor from (ax, ay) running `length` at `angle_deg`."""
    th = math.radians(angle_deg)
    skip = np.zeros(NETS, dtype=bool)
    for n in own:
        skip[n] = True
    return _CorridorBox(ax=ax, ay=ay, ux=math.cos(th), uy=math.sin(th),
                        length=length, half_w=width / 2.0, skip=skip)


def _aw(*segs):
    return np.asarray([list(s) for s in segs], dtype=float)


def test_perpendicular_crossing_cuts_exactly_the_width():
    """The base case: straight across a width-4 lane cuts 4mm."""
    box = _box()                                   # x in [0,20], y in [-2,2]
    got = _corridor_cut_np(_aw((10.0, -9.0, 10.0, 9.0, 1)), [box])
    assert abs(got - WIDTH) < 1e-9, got
    print(f"  PASS: perpendicular cut = {got:.3f}mm = the lane width")


def test_oblique_crossing_costs_width_over_sin_theta():
    """w/sin(theta) -- the whole reason for measuring length over counting.

    A count says a 30-degree crossing and a square one are the same event. They
    are not: the shallow one runs twice as far under the bus.
    """
    for deg in (90.0, 60.0, 45.0, 30.0):
        th = math.radians(deg)
        # A long segment through (10, 0) at `deg` to the corridor axis.
        half = 50.0
        seg = (10.0 - half * math.cos(th), -half * math.sin(th),
               10.0 + half * math.cos(th), half * math.sin(th), 1)
        got = _corridor_cut_np(_aw(seg), [_box()])
        want = WIDTH / math.sin(th)
        assert abs(got - want) < 1e-6, (deg, got, want)
    print("  PASS: oblique cut = w/sin(theta) at 90/60/45/30 degrees")


def test_a_wire_outside_the_corridor_costs_nothing():
    box = _box()
    outside = _aw((10.0, 5.0, 12.0, 9.0, 1),      # above the lane
                  (-5.0, -1.0, -1.0, 1.0, 1),     # before its start
                  (21.0, -1.0, 25.0, 1.0, 1))     # past its end
    assert _corridor_cut_np(outside, [box]) == 0.0
    print("  PASS: outside the rectangle is exactly zero, not epsilon")


def test_the_ends_clip_and_the_term_is_continuous_there():
    """A wire leaving through the END of the lane pays only for what is inside.

    The continuity check is the point: sliding the exit point out of the lane
    must not make the cost jump, or the descent sees a cliff instead of a slope.
    """
    box = _box()
    # Along the axis from x=15 to x=25: 5mm inside, 5mm past the end.
    got = _corridor_cut_np(_aw((15.0, 0.0, 25.0, 0.0, 1)), [box])
    assert abs(got - 5.0) < 1e-9, got
    prev = None
    for x in (18.0, 19.0, 19.9, 19.99, 20.0, 20.01):
        c = _corridor_cut_np(_aw((15.0, 0.0, x, 0.0, 1)), [box])
        want = min(x, 20.0) - 15.0
        assert abs(c - want) < 1e-9, (x, c, want)
        if prev is not None:
            assert c - prev < 1.01, "the term must not jump at the boundary"
        prev = c
    print("  PASS: end-clipped, and continuous across the boundary")


def test_a_wire_wholly_inside_pays_its_whole_length():
    box = _box()
    got = _corridor_cut_np(_aw((4.0, -1.0, 10.0, 1.0, 1)), [box])
    assert abs(got - math.hypot(6.0, 2.0)) < 1e-9, got
    print("  PASS: a wire wholly inside pays its full length")


def test_the_corridors_own_nets_are_free():
    """The bus running down its own lane is the POINT, not a cost."""
    box = _box(own=(3,))
    across = (10.0, -9.0, 10.0, 9.0, 3)
    assert _corridor_cut_np(_aw(across), [box]) == 0.0
    # ... and the identical geometry on a foreign net is charged, so the test
    # is about ownership and not about the geometry being degenerate.
    foreign = (10.0, -9.0, 10.0, 9.0, 1)
    assert abs(_corridor_cut_np(_aw(foreign), [box]) - WIDTH) < 1e-9
    print("  PASS: own nets are free; the same wire on a foreign net is not")


def test_skipped_nets_are_free_and_out_of_range_ids_do_not_alias():
    """A net id past the lookup must not read some other net's slot."""
    box = _box(own=(2,))
    assert _corridor_cut_np(_aw((10.0, -9.0, 10.0, 9.0, 2)), [box]) == 0.0
    # id NETS+2 is out of range; clamped indexing would land on the last slot.
    got = _corridor_cut_np(_aw((10.0, -9.0, 10.0, 9.0, NETS + 2)), [box])
    assert got == 0.0, f"an unknown net id must be dropped, not aliased: {got}"
    print("  PASS: out-of-range net ids are dropped, never aliased")


def test_a_rotated_corridor_gives_the_same_answer_as_an_axis_aligned_one():
    """Rotation is an isometry, so the cut is invariant under it. This is what
    says the local-frame transform is right and not merely self-consistent."""
    for deg in (0.0, 17.0, 45.0, 90.0, 133.0, 180.0, 271.0):
        th = math.radians(deg)
        c, s = math.cos(th), math.sin(th)
        box = _box(angle_deg=deg)
        # The perpendicular wire at the same relative spot, rotated with it.
        def rot(x, y):
            return (x * c - y * s, x * s + y * c)
        p1, p2 = rot(10.0, -9.0), rot(10.0, 9.0)
        got = _corridor_cut_np(_aw((p1[0], p1[1], p2[0], p2[1], 1)), [box])
        assert abs(got - WIDTH) < 1e-9, (deg, got)
    print("  PASS: invariant under corridor rotation (0..271 degrees)")


def test_multiple_corridors_sum_and_a_shared_wire_pays_both():
    """Two crossing lanes: a wire through both pays both, by design -- it does
    damage under each bus separately."""
    a = _box(angle_deg=0.0, length=20.0)                 # along x through y=0
    b = _box(ax=10.0, ay=-10.0, angle_deg=90.0, length=20.0)  # along y at x=10
    wire = _aw((10.0, -9.0, 10.0, 9.0, 1))
    # It runs down b's axis for its whole length, and across a's width.
    got = _corridor_cut_np(wire, [a, b])
    assert abs(got - (WIDTH + 18.0)) < 1e-9, got
    print("  PASS: corridors sum independently")


def test_empty_inputs_are_zero_not_a_crash():
    assert _corridor_cut_np(np.zeros((0, 5)), [_box()]) == 0.0
    assert _corridor_cut_np(_aw((0.0, 0.0, 1.0, 1.0, 1)), []) == 0.0
    print("  PASS: empty airwires or no corridors is 0.0")


def test_the_gradient_a_count_does_not_have():
    """The motivating property, asserted rather than asserted-in-prose.

    Walk a wire out of a corridor in 0.25mm steps. `foreign_crossings` would
    report 1, 1, 1, ..., 0 -- a cliff with no slope. The chord must fall
    monotonically and strictly, so every step gives the descent a direction.
    """
    box = _box()
    costs = []
    for y in np.arange(0.0, 2.6, 0.25):
        # A short wire crossing the lower edge: as it rises, less is inside.
        costs.append(_corridor_cut_np(_aw((10.0, y - 2.0, 10.0, y + 2.0, 1)),
                                      [box]))
    assert costs[0] > 0.0
    for lo, hi in zip(costs[1:], costs):
        assert lo <= hi + 1e-12, costs
    assert len(set(round(c, 9) for c in costs)) > 4, (
        f"a piecewise-constant term would show 2 distinct values: {costs}")
    print(f"  PASS: {len(set(round(c, 9) for c in costs))} distinct values "
          "over the walk -- a count would give 2")


if __name__ == '__main__':
    for fn in (test_perpendicular_crossing_cuts_exactly_the_width,
               test_oblique_crossing_costs_width_over_sin_theta,
               test_a_wire_outside_the_corridor_costs_nothing,
               test_the_ends_clip_and_the_term_is_continuous_there,
               test_a_wire_wholly_inside_pays_its_whole_length,
               test_the_corridors_own_nets_are_free,
               test_skipped_nets_are_free_and_out_of_range_ids_do_not_alias,
               test_a_rotated_corridor_gives_the_same_answer_as_an_axis_aligned_one,
               test_multiple_corridors_sum_and_a_shared_wire_pays_both,
               test_empty_inputs_are_zero_not_a_crash,
               test_the_gradient_a_count_does_not_have):
        print(fn.__name__)
        fn()
    print("\nALL PASS")
