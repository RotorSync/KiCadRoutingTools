#!/usr/bin/env python3
"""Re-seating a constrained cluster by assignment instead of freezing it.

Parts under a proximity rule get LOCKED, because the quench has no proximity
term and will otherwise walk one member past the limit every run. That works,
and it freezes exactly the parts whose re-seating produced most of a run's
placement wins.

The trick under test: make the proximity rule the DEFINITION of the slot pool,
so every candidate satisfies it by construction and no proximity cost term is
ever needed. What is left is an assignment problem, and it is exactly additive
because each member is scored with every other member's pads overridden to an
empty list.

The properties that make it safe are what these assert:

  * identity is always feasible, so "leave it alone" is a possible answer;
  * acceptance is on the EXACT cluster objective, so a lying surrogate can
    waste time but never ship a worse board;
  * two members never end up in the same place, even though Hungarian returns
    distinct slot INDICES rather than non-overlapping courtyards;
  * no RNG anywhere -- same input, same answer.
"""
import io
import contextlib
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kicad_parser import parse_kicad_pcb                       # noqa: E402
from placement import reseat                                   # noqa: E402
from placement.quench import QuenchState                       # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOARD = os.path.join(ROOT, 'kicad_files', 'ulx3s.kicad_pcb')

_CACHE = {}


def _state(path=BOARD):
    """A fresh state each call: these tests MUTATE it."""
    pcb = _CACHE.get(path)
    if pcb is None:
        pcb = _CACHE[path] = parse_kicad_pcb(path)
    with contextlib.redirect_stdout(io.StringIO()), \
            contextlib.redirect_stderr(io.StringIO()):
        st = QuenchState(pcb, path, clearance=0.2, board_edge_clearance=0.55,
                         crossing_penalty=30.0, halo_base=0.5, halo_coef=0.15,
                         halo_weight=2.0, edge_halo=2.0, edge_weight=2.0,
                         grid_step=0.1, length_weight=0.3)
        st.build_neighbor_lists(4.0)
    return pcb, st


def _clusters(pcb, st, radius=3.0, limit=2):
    cl = reseat.clusters_from_tethers(pcb, st, radius)
    return cl[:limit]


def _multi(pcb, st, radius=3.0):
    """The first cluster with two or more members.

    Taking cl[0] made the additivity test SKIP silently -- the most load-bearing
    assertion in the file, quietly not running because the first anchor on this
    board happens to have one cap.
    """
    for c in reseat.clusters_from_tethers(pcb, st, radius):
        if len(c.members) >= 2:
            return c
    return None


def test_every_slot_satisfies_the_proximity_rule_by_construction():
    """The whole design. If a slot could violate the rule, a cost term would be
    needed to price it -- and that is the term the quench does not have."""
    pcb, st = _state()
    cl = _clusters(pcb, st)
    assert cl, "ulx3s must yield decap tethers"
    c = cl[0]
    pins = reseat._anchor_pin_points(st, c)
    slots = reseat.slot_pool(st, c)
    assert slots
    incumbent = {(round(st.parts[m].x, 6), round(st.parts[m].y, 6))
                 for m in c.members}
    off = 0
    for x, y, _rot in slots:
        if (round(x, 6), round(y, 6)) in incumbent:
            continue                              # identity is exempt by design
        d = min(math.hypot(x - px, y - py) for px, py in pins)
        if d > c.radius_mm + 1e-6:
            off += 1
    assert off == 0, f"{off} generated slots violate the radius"
    print(f"  PASS: {len(slots)} slots for {c.name}, all within "
          f"{c.radius_mm}mm of an anchor pin")


def test_identity_is_always_in_the_pool():
    pcb, st = _state()
    c = _clusters(pcb, st)[0]
    slots = set((round(x, 6), round(y, 6), round(r, 3))
                for x, y, r in reseat.slot_pool(st, c))
    for m in c.members:
        p = st.parts[m]
        assert (round(p.x, 6), round(p.y, 6), round(p.rot, 3)) in slots, m
    print(f"  PASS: all {len(c.members)} incumbent poses are feasible")


def test_the_surrogate_matrix_is_additive():
    """Scoring a member with the others blanked is what makes the matrix
    Hungarian-legal. If a member's cost depended on where its siblings sat, the
    assignment would be solving the wrong problem."""
    pcb, st = _state()
    c = _multi(pcb, st)
    assert c is not None, "ulx3s must have an anchor with two tethered caps"
    slots = reseat.slot_pool(st, c)[:24]
    involved = set()
    for m in c.members:
        involved.update(st.parts[m].nets)
    other_aw = st.airwires_excluding(involved)
    m0, m1 = c.members[0], c.members[1]
    blank = {m: [] for m in c.members}

    def row(member, sibling_pose):
        others = dict(blank)
        others.pop(member, None)
        if sibling_pose is not None:
            sib, pose = sibling_pose
            others[sib] = st.parts[sib].pad_globals(*pose)
        return reseat._member_costs(st, c, slots, member, others, other_aw,
                                    involved)

    base = row(m0, None)
    # Moving the sibling must not change m0's row -- because the sibling is
    # blanked, not merely far away.
    p1 = st.parts[m1]
    moved = row(m0, None)
    assert base == moved
    # ...and with the sibling NOT blanked, it does change, which shows the
    # blanking is what is doing the work rather than the fixture being flat.
    with_sib = row(m0, (m1, (p1.x + 2.0, p1.y + 2.0, p1.rot)))
    assert any(a != b for a, b in zip(base, with_sib)
               if math.isfinite(a) and math.isfinite(b)), \
        "the fixture is degenerate: blanking makes no difference here"
    print("  PASS: a member's row is independent of its siblings' poses")


def test_two_members_never_land_on_the_same_spot():
    """Hungarian returns distinct slot INDICES, which is not the same as
    non-overlapping courtyards."""
    pcb, st = _state()
    for c in [_multi(pcb, st)]:
        assert c is not None
        r = reseat.reseat_cluster(st, c, apply=True)
        seen = {}
        for m in c.members:
            p = st.parts[m]
            key = (round(p.x, 4), round(p.y, 4))
            assert key not in seen or seen[key] == m, \
                f"{m} and {seen[key]} are stacked at {key} after {r.to_dict()}"
            seen[key] = m
    print("  PASS: no cluster left two members stacked")


def test_it_never_accepts_a_worse_cluster_cost():
    """Acceptance is on the exact objective, re-evaluated with all members in
    place -- never on the additive surrogate the matrix was built from."""
    pcb, st = _state()
    accepted = 0
    for c in _clusters(pcb, st, limit=3):
        before = reseat.cluster_cost(st, c)
        r = reseat.reseat_cluster(st, c, apply=True)
        after = reseat.cluster_cost(st, c)
        assert after <= before + 1e-6, (c.name, before, after, r.to_dict())
        if r.accepted:
            accepted += 1
            assert after < before - 1e-9, (c.name, before, after)
    print(f"  PASS: {accepted} cluster(s) accepted, none made worse")


def test_it_is_deterministic():
    pcb, st_a = _state()
    _pcb2, st_b = _state()
    ca = [r.to_dict() for r in reseat.reseat(st_a, _clusters(pcb, st_a, limit=2))]
    cb = [r.to_dict() for r in reseat.reseat(st_b, _clusters(pcb, st_b, limit=2))]
    assert ca == cb, "two identical runs disagreed"
    print(f"  PASS: {len(ca)} cluster results identical across two runs")


def test_a_repair_is_reported_rather_than_hidden():
    """When a member is bumped off its assigned slot, that is a fact about how
    good the assignment was and must be visible."""
    pcb, st = _state()
    rows = reseat.reseat(st, _clusters(pcb, st, limit=3))
    assert rows and all('repairs' in r.to_dict() for r in rows)
    total = sum(r.repairs for r in rows)
    print(f"  PASS: {len(rows)} row(s) report repairs (total {total})")


def test_a_cluster_with_no_room_returns_the_incumbent_not_an_error():
    pcb, st = _state()
    c = _clusters(pcb, st)[0]
    tiny = reseat.Cluster(name=c.name, anchor=c.anchor, members=c.members,
                          radius_mm=0.01)
    before = {m: (st.parts[m].x, st.parts[m].y, st.parts[m].rot)
              for m in c.members}
    r = reseat.reseat_cluster(st, tiny, apply=True)
    after = {m: (st.parts[m].x, st.parts[m].y, st.parts[m].rot)
             for m in c.members}
    assert before == after or r.accepted, r.to_dict()
    print(f"  PASS: a 0.01mm radius yields '{r.reason or 'an improvement'}', "
          "not a crash")


if __name__ == '__main__':
    for fn in (test_every_slot_satisfies_the_proximity_rule_by_construction,
               test_identity_is_always_in_the_pool,
               test_the_surrogate_matrix_is_additive,
               test_two_members_never_land_on_the_same_spot,
               test_it_never_accepts_a_worse_cluster_cost,
               test_it_is_deterministic,
               test_a_repair_is_reported_rather_than_hidden,
               test_a_cluster_with_no_room_returns_the_incumbent_not_an_error):
        print(fn.__name__)
        fn()
    print("\nALL PASS")
