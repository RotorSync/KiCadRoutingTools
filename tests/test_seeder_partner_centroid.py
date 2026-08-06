#!/usr/bin/env python3
"""_partner_centroid votes once per (partner footprint, net), not per pad.

Run-7 root cause for the backwards 27R pair: the centroid averaged PADS, so a
partner with duplicated pins outvoted one with a single pin. A USB-C
receptacle's doubled A6/B6 DP pads pulled the series resistors 2:1 toward the
connector; R7 seated 15.3mm from the U1 face the intent named ("against U1's
west face"), and the run spent an iteration re-seating the pair by hand.
"""
import os
import sys
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

sys.path.insert(0, os.path.join(ROOT, 'py_placer'))  # placement split
sys.path.insert(0, os.path.join(ROOT, 'py_router'))  # placement split
sys.path.insert(0, os.path.join(ROOT, 'py_tools'))  # placement split
from placement.seeder import _partner_centroid  # noqa: E402


def _part(nets, pads):
    """pads: list of (x, y, net_id)."""
    return SimpleNamespace(nets=set(nets), pad_globals=lambda: list(pads))


def test_duplicated_pads_no_longer_outvote():
    # J1: USB-C style, TWO pads on net 1 (A6+B6 mirror). U1: ONE pad on net 1.
    state = SimpleNamespace(
        parts={
            'R7': _part([1], [(20.0, 5.0, 1)]),
            'J1': _part([1], [(0.0, 0.0, 1), (0.0, 10.0, 1)]),
            'U1': _part([1], [(10.0, 5.0, 1)]),
        },
        net_refs={1: ('R7', 'J1', 'U1')})
    c = _partner_centroid(state, 'R7', placed={'J1', 'U1'})
    assert c is not None
    x, y = c
    # One vote each: J1 votes (0, 5), U1 votes (10, 5) -> centroid (5, 5).
    # Per-pad voting gave ((0+0+10)/3, (0+10+5)/3) = (3.33, 5): biased 2:1
    # toward the connector.
    assert abs(x - 5.0) < 1e-9, (
        f"expected x=5.0 (one vote per partner), got {x:.3f} -- per-pad "
        f"voting bias is back")
    assert abs(y - 5.0) < 1e-9
    print("  PASS: duplicated pads collapse to one vote per partner")


def test_multi_net_partner_still_votes_per_net():
    """A partner sharing TWO nets with the part legitimately votes twice --
    once per net -- because each net is its own pull."""
    state = SimpleNamespace(
        parts={
            'C1': _part([1, 2], [(0.0, 0.0, 1), (0.0, 1.0, 2)]),
            'U1': _part([1, 2], [(4.0, 0.0, 1), (8.0, 0.0, 2)]),
        },
        net_refs={1: ('C1', 'U1'), 2: ('C1', 'U1')})
    x, y = _partner_centroid(state, 'C1', placed={'U1'})
    assert abs(x - 6.0) < 1e-9, f"two per-net votes (4+8)/2=6, got {x}"
    print("  PASS: one vote per (partner, net), so shared nets each pull")


def test_fanout_and_empty_cases_unchanged():
    state = SimpleNamespace(
        parts={'R1': _part([1], [(0.0, 0.0, 1)]),
               'U1': _part([1], [(5.0, 0.0, 1)])},
        net_refs={1: tuple(f'P{i}' for i in range(30))})  # >max_fanout
    assert _partner_centroid(state, 'R1', placed={'U1'}) is None
    assert _partner_centroid(state, 'MISSING', placed=set()) is None
    print("  PASS: fanout exclusion and missing-ref cases unchanged")


if __name__ == '__main__':
    fns = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
    for fn in fns:
        print(f"--- {fn.__name__}")
        fn()
    print("ALL PASS")
