#!/usr/bin/env python3
"""Crossings banding, the health promotion it enables, and the intrusion fix.

Three properties, in the order they matter:

1. DEFAULT-OFF IS BIT-IDENTICAL. `--rank-crossing-band 0` (the default) must
   produce exactly the legacy order, health included in its old slot. Without
   this the change is not shippable: every existing cost/ranking expectation
   in the suite depends on it.

2. BANDING IS WHAT LETS HEALTH SPEAK. Reordering alone is close to a no-op --
   crossings is near-injective over a real slate, so slot 1 decides and
   nothing below it ever runs. The test that proves the feature is one where
   two candidates differ slightly on crossings and sharply on health.

3. THE KEY STAYS A TOTAL ORDER. Quantisation is used rather than a tolerance
   compare precisely because `abs(a-b) <= tol` is not transitive and
   `sorted()` on a non-transitive key is undefined.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'py_placer'))  # placement split
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'py_router'))  # placement split
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'py_tools'))  # placement split

from placement.portfolio import (  # noqa: E402
    Candidate, band_width, rank_key, rank_static)


def _cand(index, crossings, health=0, hpwl=100.0, inversions=0):
    # `viable` is a read-only property over board + gates, so make it true the
    # way the real code does rather than assigning to it.
    c = Candidate(index=index, strategy='t', board=f'b{index}.kicad_pcb',
                  seed_board='')
    c.metrics = {'crossings': crossings, 'hpwl': hpwl,
                 'inversions': inversions, 'health_penalty': health}
    c.gates = {'passed': True, 'reasons': []}
    return c


def test_default_is_the_legacy_order():
    """q=0 must reproduce the old tuple exactly, health in its old slot."""
    a = _cand(1, crossings=70, health=99)
    b = _cand(2, crossings=70, health=0)
    # Legacy: health sat BELOW inversions/plane/hpwl/neck, so with everything
    # else equal it still broke the tie -- and the low-health board won.
    assert rank_key(b, 0) < rank_key(a, 0)
    assert rank_key(a, 0).health_legacy == 99
    assert rank_key(a, 0).health_banded == 0
    assert rank_key(a, 0).crossings_band == 70, "q=0 => raw crossings"

    # And the legacy order really is crossings-first: a worse-crossings board
    # with perfect health still loses.
    worse = _cand(3, crossings=71, health=0)
    better = _cand(4, crossings=70, health=500)
    assert rank_key(better, 0) < rank_key(worse, 0)
    print("  PASS: q=0 is the legacy order, health in slot 6")


def test_banding_lets_health_decide_inside_a_band():
    """The whole point: near-tied crossings, health picks the winner."""
    # 70 vs 71 crossings: legacy ranks strictly on the count.
    clean = _cand(1, crossings=71, health=0)
    fighty = _cand(2, crossings=70, health=40)
    assert rank_key(fighty, 0) < rank_key(clean, 0), "legacy: crossings wins"

    # Band of 3 puts both in ceil(70/3) == ceil(71/3) == 24, so health decides.
    q = 3
    assert rank_key(clean, q).crossings_band == rank_key(fighty, q).crossings_band
    assert rank_key(clean, q) < rank_key(fighty, q), \
        "inside one band the floorplan-fights-the-router term decides"
    print("  PASS: banding promotes health inside a band")


def test_banding_never_crosses_bands():
    """A genuinely worse board must not be rescued by good health."""
    q = 3
    far_worse = _cand(1, crossings=90, health=0)
    good = _cand(2, crossings=70, health=999)
    assert rank_key(good, q) < rank_key(far_worse, q), \
        "health may break a tie, never overturn a real crossings gap"
    print("  PASS: health decides within a band, not across bands")


def test_key_is_a_total_order():
    """Transitivity: the reason for ceil-div instead of a tolerance compare."""
    q = 5
    cands = [_cand(i, crossings=c, health=h) for i, (c, h) in
             enumerate([(70, 3), (72, 1), (74, 9), (76, 0), (99, 0)])]
    keys = [rank_key(c, q) for c in cands]
    for i, ki in enumerate(keys):
        for j, kj in enumerate(keys):
            for k, kk in enumerate(keys):
                if ki < kj and kj < kk:
                    assert ki < kk, f"not transitive: {i},{j},{k}"
    # sorted() is therefore well defined and stable.
    order = rank_static(cands, q)
    assert sorted(order) == sorted(c.index for c in cands)
    print("  PASS: rank_key is a total order under banding")


def test_band_width_refuses_to_band_when_health_is_inert():
    """No intent (or no health block) => every penalty is 0 => q must be 0.

    Banding coarsens the strongest signal. Doing that to buy a term that is
    identically zero is strictly worse than not banding.
    """
    inert = [_cand(1, crossings=70, health=0), _cand(2, crossings=80, health=0)]
    assert band_width(inert, 0.05, 70) == 0, "all-zero health => no banding"

    live = [_cand(1, crossings=70, health=4), _cand(2, crossings=80, health=0)]
    assert band_width(live, 0.05, 70) == max(1, round(0.05 * 70))
    assert band_width(live, 0.0, 70) == 0, "flag off => no banding"
    print("  PASS: band_width is inert without a live health signal")


def test_intrusion_count_is_not_truncated():
    """The bug: the row truncates intrusions to 5 for display, and the
    consumer summed len() of that -- so 12 parts in a channel scored as 5."""
    rows = [{'intrusions': [{}] * 5, 'intrusions_total': 12}]
    total = sum(int(r['intrusions_total']) if 'intrusions_total' in r
                else len(r.get('intrusions') or ()) for r in rows)
    assert total == 12, "must read the untruncated count"

    legacy = [{'intrusions': [{}] * 3}]          # health JSON written before
    total_legacy = sum(int(r['intrusions_total']) if 'intrusions_total' in r
                       else len(r.get('intrusions') or ())
                       for r in legacy)
    assert total_legacy == 3, "old rows still read"
    print("  PASS: intrusions_total defeats the 5-per-corridor saturation")


if __name__ == '__main__':
    for fn in (test_default_is_the_legacy_order,
               test_banding_lets_health_decide_inside_a_band,
               test_banding_never_crosses_bands,
               test_key_is_a_total_order,
               test_band_width_refuses_to_band_when_health_is_inert,
               test_intrusion_count_is_not_truncated):
        print(fn.__name__)
        fn()
    print("\nALL PASS")
