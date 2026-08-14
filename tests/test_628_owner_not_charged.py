#!/usr/bin/env python3
"""Regression test for the #628 follow-up -- the milled-ring swallow rule must
not fire on the part that OWNS the ring.

#636 made `BoardOutlineGate`'s swallow probe see the milled `board_edge_contours`
as well as `board_cutouts`, which closed a real hole. But `board_edge_contours`
has exactly ONE producer -- `drop_pad_containing_cutouts` -- and it moves a
contour there only when the contour encloses **>= 2 pad centres**. So every
milled contour has a part living inside it BY CONSTRUCTION, and when that part's
courtyard is bigger than the ring (a connector over a milled relief) the probe
called the part board-violating at its own hand-placed seed pose:

    J1 seed violation()                 0.0   -> 0.55
    candidate_valid('J1', 4.45, 20.0)   False -> True

The second line is the damage. A part whose seed reads as violating enters
quench's UNFREEZE branch (`quench.py:686-700`), which accepts any overlap-free
pose whose board term merely DECREASES -- so quench would walk J1 to 0.45mm from
a board edge that requires 0.55mm, exactly what the comment at `quench.py:668-675`
exists to prevent. It also makes the #456 scorecard report a correct hand
placement as `oob_count 1`.

The fix scopes the MILLED half of the probe by ownership (`ring_owner_refs`,
computed once at the seed poses) and threads a `ref` through `rect_blocked` /
`rect_outside_amount`. This test pins all four corners of that:

  * the OWNER is legal over its own relief, and is still refused a sub-clearance
    pose (the unfreeze licence is gone);
  * a NON-owner swallowing a foreign relief is STILL blocked (#636 survives) --
    pinned on the same board and the same gate as the owner case, so the two
    cannot be confused for a board-level opt-out;
  * ownership is PER RING, not per part: J1 owns ring1 and is still blocked on
    ring2, which SW17 owns;
  * a real `cutouts` entry is NEVER owner-skipped, even when it encloses the
    tested part's own pad. A part may not swallow a real hole, owner or not.

`tests/test_628_milled_contour_legality.py` is the other half and must keep
passing unmodified: its SW17 swallows a ring owned by a back-side B1, i.e. a
non-owner case.

    python3 tests/test_628_owner_not_charged.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'py_router'))  # #522
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'py_tools'))  # #522

from kicad_parser import parse_kicad_pcb  # noqa: E402
from placement import legality  # noqa: E402
from placement.legality import BoardOutlineGate  # noqa: E402
from placement.quench import QuenchState  # noqa: E402

fails = []


def check(name, cond):
    print(("  ok  " if cond else " FAIL ") + name)
    if not cond:
        fails.append(name)


# --- API shims -------------------------------------------------------------
# On the UNFIXED gate none of the ownership API exists, so a plain call raises
# and the run aborts before it reaches the substantive checks. These report a
# clean FAIL once per missing piece and fall back to the old call, so a red run
# names the behaviours that regressed rather than dying at import time.
_MISSING = set()


def _api(name, cond):
    if name not in _MISSING:
        _MISSING.add(name)
        check("API: " + name, cond)


def _gate(board_info, footprints):
    """BoardOutlineGate with the ownership precompute enabled."""
    try:
        g = BoardOutlineGate(board_info, MARGIN, footprints=footprints)
    except TypeError:
        _api("BoardOutlineGate takes footprints=", False)
        return BoardOutlineGate(board_info, MARGIN)
    _api("BoardOutlineGate takes footprints=", True)
    return g


def _blocked(gate, rect, ref):
    try:
        v = gate.rect_blocked(rect, ref=ref)
    except TypeError:
        _api("rect_blocked takes ref=", False)
        return gate.rect_blocked(rect)
    _api("rect_blocked takes ref=", True)
    return v


def _amount(gate, rect, ref):
    try:
        v = gate.rect_outside_amount(rect, ref=ref)
    except TypeError:
        _api("rect_outside_amount takes ref=", False)
        return gate.rect_outside_amount(rect)
    _api("rect_outside_amount takes ref=", True)
    return v


def _owner_refs(ring, footprints):
    fn = getattr(legality, 'ring_owner_refs', None)
    if fn is None:
        _api("legality.ring_owner_refs exists", False)
        return None
    _api("legality.ring_owner_refs exists", True)
    return fn(ring, footprints)


# The canonical QuenchState knobs (same set tests/test_628_milled_contour_legality
# and tests/test_456_side_and_outline use); board_edge_clearance is the gate margin.
KNOBS = dict(clearance=0.25, board_edge_clearance=0.55, crossing_penalty=10.0,
             halo_base=0.3, halo_coef=0.15, halo_weight=1.0,
             edge_halo=1.0, edge_weight=1.0, grid_step=0.05)
MARGIN = KNOBS['board_edge_clearance']


def _rect_edge(x1, y1, x2, y2, tag):
    """Four Edge.Cuts gr_lines forming a closed axis-aligned rectangle."""
    pts = [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]
    return "\n".join(
        f' (gr_line (start {pts[i][0]} {pts[i][1]}) '
        f'(end {pts[(i + 1) % 4][0]} {pts[(i + 1) % 4][1]}) '
        f'(layer "Edge.Cuts") (width 0.05) (uuid "{tag}{i}"))' for i in range(4))


# --------------------------------------------------------------------------
# Fixture A -- the pinned repro, plus a second relief with a DIFFERENT owner.
#
# Outline 0..60 x 0..40. Two milled rings:
#   ring1  20..24 x 19..21  owned by J1  (front, connector over a relief)
#   ring2  34..38 x 19..21  owned by SW17 (back)
# Each part sits centred on its own ring with an 8x8mm courtyard, so each
# SWALLOWS its own ring at its seed pose and could swallow the other's by moving
# 14mm along x. The two are on OPPOSITE board sides and are both plain SMD, so
# they share no side and cannot interact through the courtyard-overlap term --
# every verdict below is the board term alone.
#
# The two pads inside each ring are what make drop_pad_containing_cutouts
# reclassify it (predicate: >= 2 enclosed pad centres); without them the rings
# would stay plain cutouts and the swallow probe would fire for a different
# reason entirely (test_fixture_shape below makes that a failure).
# --------------------------------------------------------------------------
BOARD_TWO_RELIEFS = f'''(kicad_pcb
 (version 20241229)
 (net 0 "")
 (net 1 "NA")
 (net 2 "NB")
{_rect_edge(0, 0, 60, 40, 'o')}
{_rect_edge(20, 19, 24, 21, 'm')}
{_rect_edge(34, 19, 38, 21, 'n')}
 (footprint "test:conn" (layer "F.Cu") (at 22 20)
  (property "Reference" "J1" (at 0 -2) (layer "F.SilkS"))
  (fp_rect (start -4 -4) (end 4 4) (layer "F.CrtYd") (uuid "cyJ1"))
  (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "NA") (uuid "pJ1a"))
  (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "NA") (uuid "pJ1b"))
 )
 (footprint "test:sw" (layer "B.Cu") (at 36 20)
  (property "Reference" "SW17" (at 0 -2) (layer "B.SilkS"))
  (fp_rect (start -4 -4) (end 4 4) (layer "B.CrtYd") (uuid "cySW"))
  (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "B.Cu") (net 2 "NB") (uuid "pSWa"))
  (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "B.Cu") (net 2 "NB") (uuid "pSWb"))
 )
)
'''

# --------------------------------------------------------------------------
# Fixture B -- a REAL cutout that encloses ONE of J2's pad centres.
#
# One pad is below drop_pad_containing_cutouts' >= 2 threshold, so the ring stays
# a genuine board_cutouts hole. J2 would be its owner under the milled rule --
# and must be charged anyway. J2's second pad is at +5mm, outside the hole, so
# the footprint straddles it exactly as a part over a milled relief would.
# --------------------------------------------------------------------------
BOARD_REAL_CUTOUT = f'''(kicad_pcb
 (version 20241229)
 (net 0 "")
 (net 1 "NA")
{_rect_edge(0, 0, 60, 40, 'o')}
{_rect_edge(20, 19, 24, 21, 'h')}
 (footprint "test:conn" (layer "F.Cu") (at 22 20)
  (property "Reference" "J2" (at 0 -2) (layer "F.SilkS"))
  (fp_rect (start -4 -4) (end 4 4) (layer "F.CrtYd") (uuid "cyJ2"))
  (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "NA") (uuid "pJ2a"))
  (pad "2" smd rect (at 5 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "NA") (uuid "pJ2b"))
 )
)
'''

J1_SEED = (18.0, 16.0, 26.0, 24.0)      # J1's courtyard at (22, 20): swallows ring1
SW17_SEED = (32.0, 16.0, 40.0, 24.0)    # SW17's courtyard at (36, 20): swallows ring2
RING1 = (20.0, 19.0, 24.0, 21.0)
RING2 = (34.0, 19.0, 38.0, 21.0)
# The pose the unfreeze branch would have walked J1 to: courtyard 8mm wide with
# its LEFT edge 0.45mm from x=0, against a required board_edge_clearance of 0.55.
BADX, BADY = 4.45, 20.0


def _write(text):
    fd, path = tempfile.mkstemp(suffix='.kicad_pcb')
    with os.fdopen(fd, 'w') as f:
        f.write(text)
    return path


# --------------------------------------------------------------------------
# 1. Fixture sanity + the ownership map itself.
# --------------------------------------------------------------------------
def test_fixture_shape_and_owner_map():
    path = _write(BOARD_TWO_RELIEFS)
    try:
        pcb = parse_kicad_pcb(path)
        bi = pcb.board_info
        ec = [c for c in (getattr(bi, 'board_edge_contours', None) or [])
              if len(c) >= 3]
        cut = [c for c in (bi.board_cutouts or []) if len(c) >= 3]
        check("fixture: both interior rings reclassified as MILLED "
              f"(board_edge_contours == 2, got {len(ec)})", len(ec) == 2)
        check(f"fixture: neither is a cutout (board_cutouts == 0, got {len(cut)})",
              len(cut) == 0)
        gate = _gate(bi, pcb.footprints)
        check("fixture: gate active with outline + 2 milled rings",
              gate.active and len(gate.rings) == 3)
        owners = getattr(gate, 'milled_owners', None)
        check("gate exposes milled_owners, one entry per milled ring",
              isinstance(owners, list) and len(owners or []) == 2)
        if owners and len(owners) == 2:
            # Rings keep board_edge_contours order; identify by geometry rather
            # than by index so a parser reorder cannot silently pass this.
            by_x = {min(p[0] for p in c): o
                    for c, o in zip(gate.milled, owners)}
            check(f"ring1 (x0=20) is owned by exactly {{J1}}, got {by_x.get(20.0)}",
                  by_x.get(20.0) == frozenset({'J1'}))
            check(f"ring2 (x0=34) is owned by exactly {{SW17}}, got {by_x.get(34.0)}",
                  by_x.get(34.0) == frozenset({'SW17'}))
        check("fixture: J1's seed courtyard is inside the bbox inset, so ONLY "
              "the ring tests can reject it",
              gate.bbox_outside_amount(J1_SEED) == 0.0)
        check("fixture: J1's seed courtyard really does contain ring1 whole",
              J1_SEED[0] < RING1[0] and J1_SEED[1] < RING1[1]
              and RING1[2] < J1_SEED[2] and RING1[3] < J1_SEED[3])
        # Without footprints the gate cannot know about ownership, and must
        # behave exactly as it did before this change.
        bare = BoardOutlineGate(bi, MARGIN)
        check("no footprints supplied -> every milled ring is ownerless",
              all(o == frozenset()
                  for o in (getattr(bare, 'milled_owners', None) or [])))
    finally:
        os.unlink(path)


# --------------------------------------------------------------------------
# 2. The gate primitives: the owner is exempt, ref=None is unchanged, and the
#    two primitives agree (legality.py:373 / :403-404 invariant).
# --------------------------------------------------------------------------
def test_gate_primitives_scope_by_owner():
    path = _write(BOARD_TWO_RELIEFS)
    try:
        pcb = parse_kicad_pcb(path)
        gate = _gate(pcb.board_info, pcb.footprints)

        check("owner: rect_blocked(J1 seed, ref='J1') is False",
              _blocked(gate, J1_SEED, 'J1') is False)
        amt_owner = _amount(gate, J1_SEED, 'J1')
        check(f"owner: rect_outside_amount(J1 seed, ref='J1') == 0 ({amt_owner})",
              amt_owner == 0.0)
        check("owner: the two primitives agree",
              _blocked(gate, J1_SEED, 'J1') == (amt_owner > 0.0))

        # ref=None must keep charging -- any caller not updated is unchanged.
        check("ref=None: rect_blocked(J1 seed) still True (default unchanged)",
              gate.rect_blocked(J1_SEED) is True)
        check("ref=None: rect_outside_amount(J1 seed) still > 0",
              gate.rect_outside_amount(J1_SEED) > 0.0)

        # #636 survives: a non-owner swallowing a foreign relief is blocked.
        check("non-owner: rect_blocked(J1 seed, ref='SW17') is True",
              _blocked(gate, J1_SEED, 'SW17') is True)
        amt_foreign = _amount(gate, J1_SEED, 'SW17')
        check(f"non-owner: rect_outside_amount > 0 ({amt_foreign})",
              amt_foreign > 0.0)
        check("non-owner: the two primitives agree",
              _blocked(gate, J1_SEED, 'SW17') == (amt_foreign > 0.0))

        # Ownership is PER RING: J1's exemption on ring1 buys it nothing on ring2.
        check("per-ring: rect_blocked(SW17 seed, ref='J1') is True",
              _blocked(gate, SW17_SEED, 'J1') is True)
        check("per-ring: ...and scores a violation",
              _amount(gate, SW17_SEED, 'J1') > 0.0)
        check("per-ring: SW17 IS exempt on its own ring2",
              _blocked(gate, SW17_SEED, 'SW17') is False
              and _amount(gate, SW17_SEED, 'SW17') == 0.0)

        # An unknown ref owns nothing, so it is charged like ref=None.
        check("an unknown ref owns nothing and is charged",
              _blocked(gate, J1_SEED, 'NOSUCHPART') is True)
    finally:
        os.unlink(path)


# --------------------------------------------------------------------------
# 3. THE POINT OF THE TEST: end to end through QuenchState.candidate_valid.
#    The gate primitives alone would not have caught this -- the damage is that
#    a seed reading as violating opens the unfreeze branch.
# --------------------------------------------------------------------------
def test_owner_is_legal_at_seed_and_still_refused_a_subclearance_pose():
    path = _write(BOARD_TWO_RELIEFS)
    try:
        pcb = parse_kicad_pcb(path)
        st = QuenchState(pcb, path, **KNOBS)

        v = st.violation('J1')
        check(f"owner J1 is LEGAL at its hand-placed seed (violation {v})",
              v == 0.0)
        check("owner SW17 is LEGAL at its hand-placed seed",
              st.violation('SW17') == 0.0)

        # The regression itself: with J1 reading as violating, the unfreeze
        # branch accepts any overlap-free pose whose board term decreases, and
        # 4.45 (0.45mm from the edge, 0.55 required) is such a pose.
        bad = (BADX - 4.0, BADY - 4.0, BADX + 4.0, BADY + 4.0)
        check("the bad pose really is sub-clearance "
              f"(rect x0 {bad[0]} < usable x0 {st.usable[0]})",
              bad[0] < st.usable[0])
        check(f"candidate_valid('J1', {BADX}, {BADY}) is REFUSED",
              st.candidate_valid('J1', BADX, BADY, 0.0) is False)
        check("violation() scores that pose as illegal",
              st.violation('J1', BADX, BADY, 0.0) > 0.0)

        # The owner is not frozen either: a small nudge on its own relief is fine.
        check("owner J1 may still be nudged over its own relief",
              st.candidate_valid('J1', 22.4, 20.0, 0.0) is True)

        # #636 survives end to end: J1 may not move onto SW17's relief, and
        # vice versa. Both parts are legal at their seeds, so this is the strict
        # gate rejecting, not the unfreeze branch.
        check("non-owner: candidate_valid('J1', 36, 20) over SW17's relief "
              "is REFUSED", st.candidate_valid('J1', 36.0, 20.0, 0.0) is False)
        check("non-owner: candidate_valid('SW17', 22, 20) over J1's relief "
              "is REFUSED", st.candidate_valid('SW17', 22.0, 20.0, 0.0) is False)
        check("non-owner: violation() scores J1 over ring2 as illegal",
              st.violation('J1', 36.0, 20.0, 0.0) > 0.0)

        # #456 scorecard: a correct hand placement must not grade out-of-board.
        m = st.legality_metrics()
        check(f"scorecard: oob_count == 0 on the hand placement ({m['oob_count']})",
              m['oob_count'] == 0)
        check(f"scorecard: oob_amount == 0 ({m['oob_amount']})",
              m['oob_amount'] == 0.0)
    finally:
        os.unlink(path)


# --------------------------------------------------------------------------
# 4. Real cutouts are UNAFFECTED: a part may never swallow a real hole, owner
#    or not. Fixture B's cutout encloses one of J2's own pad centres, so J2
#    would qualify as its owner under the milled rule.
# --------------------------------------------------------------------------
def test_real_cutout_is_never_owner_skipped():
    path = _write(BOARD_REAL_CUTOUT)
    try:
        pcb = parse_kicad_pcb(path)
        bi = pcb.board_info
        cut = [c for c in (bi.board_cutouts or []) if len(c) >= 3]
        ec = [c for c in (getattr(bi, 'board_edge_contours', None) or [])
              if len(c) >= 3]
        check("fixture: one enclosed pad is below the >= 2 reclassification "
              f"threshold, so it stays a CUTOUT ({len(cut)})", len(cut) == 1)
        check(f"fixture: nothing was milled ({len(ec)})", len(ec) == 0)
        check("fixture: J2 still has both pads after parse",
              len(pcb.footprints['J2'].pads) == 2)
        # J2 WOULD be the owner if this ring were milled -- that is what makes
        # the check below a real statement rather than a vacuous one.
        check("J2 encloses a pad of the cutout ring (it would be its owner)",
              _owner_refs(cut[0], pcb.footprints) == frozenset({'J2'}))

        gate = _gate(bi, pcb.footprints)
        check("gate.milled is empty, so milled_owners is too",
              gate.milled == []
              and (getattr(gate, 'milled_owners', None) or []) == [])
        check("cutout: rect_blocked(J2 seed, ref='J2') is STILL True",
              _blocked(gate, J1_SEED, 'J2') is True)
        amt = _amount(gate, J1_SEED, 'J2')
        check(f"cutout: rect_outside_amount(..., ref='J2') is STILL > 0 ({amt})",
              amt > 0.0)
        check("cutout: the two primitives agree",
              _blocked(gate, J1_SEED, 'J2') == (amt > 0.0))

        st = QuenchState(pcb, path, **KNOBS)
        check("cutout: QuenchState scores J2 over the hole as illegal",
              st.violation('J2') > 0.0)
    finally:
        os.unlink(path)


def run():
    for t in (test_fixture_shape_and_owner_map,
              test_gate_primitives_scope_by_owner,
              test_owner_is_legal_at_seed_and_still_refused_a_subclearance_pose,
              test_real_cutout_is_never_owner_skipped):
        print(f"--- {t.__name__}")
        t()
    print()
    if fails:
        print("FAILED: %d check(s): %s" % (len(fails), fails))
        return 1
    print("All checks passed")
    return 0


if __name__ == '__main__':
    sys.exit(run())
