#!/usr/bin/env python3
"""#756: the via-nudge's DRILL-to-drill floors must be the board's, not two
hand-copied literals.

`nudge_vias_for_unresolved` spelled them as function-locals::

    H2H_VIA = 0.2    # JLC via-hole to via-hole floor
    H2H_PAD = 0.45   # JLC via-hole to pad-hole floor

hand-copied from `fab_tiers` and reading nothing -- not the board's declared
`min_hole_to_hole`, not `--fab-tier`, not a `--fab-overrides` file. Meanwhile
`check_drc` grades this exact geometry at `max(0.20, min_hole_to_hole)`:
`_pin_up` (check_drc.py:3686) raises the one knob and BOTH drill arms add it
(via/via :2605, pad/via :2667).

THE VIA/VIA GATE WAS THE ONE FLOOR IN THAT FUNCTION SITTING STRICTLY BELOW
WHAT ITS OWN CHECKER GRADES IT AT. The other three are fine and stay put, and
saying which is which is the whole argument:

    gate                     nudger                  check_drc          verdict
    via copper <-> NPTH      clearance + vr          clearance          equal
    connector copper <-> NPTH  npth_clr + hw         max(npth_clr, lc)  under, #617's
                                                                        measured
                                                                        exception
    via-drill <-> pad-drill  vdr + prad + 0.45       max(0.20, decl)    OVER by 0.25
    via-drill <-> via-drill  vdr + ovdr + 0.20       max(0.20, decl)    UNDER  <-- #756

WHAT #756's OWN ISSUE GETS WRONG, corrected here rather than repeated. It says
"`H2H_VIA` has no test that keys on it anywhere in the repo". False, and
`tests/test_750_...py:52-64` already records the identical claim as one nobody
should restore: that file carries ~10 threshold assertions built on the
mirrored constant and a behavioural control, and `tests/mutate_750.py`'s
`h2h-via-gate-deleted` row is measured KILLED. What is true is narrower -- no
assertion keys on it IN test_732, whose rig parks two vias 0.28mm apart on the
SAME net so H2H_VIA alone decides the landing, but which asserts `moves[0][0]`,
the PRE-move x. So the arms below are not the first H2H_VIA assertions; they
are the first BOARD-DERIVED ones.

THE #617 DOCTRINE DOES NOT COVER THIS GATE, and that is checkable rather than
arguable. `obstacle_map.resolve_hole_clearance` names this function by name as
an all-or-nothing repair whose floor must not be raised -- but its rig
(`test_617:_mover_board`) stages ONE via, so `if ov is v: continue` fires and
the via/via gate never executes there; its one drilled pad is an NPTH whose
H2H_PAD requirement (0.70) no declaration at or below 0.45 can move; and the
project it stages declares `min_hole_clearance`, a different key. Grepping
tests/ for `min_hole_to_hole` finds it in no nudger harness at all. Pinned by
`TestEveryExistingNudgerRigIsUnmoved`.

MEASURED BEFORE THE ENGINE CHANGED, and the numbers are in the PR body:

    max_shift 0.6 (shipped)   every row KEPT its move and connector; only the
                              landing moved -- drill gap 0.2100 -> 0.2615,
                              0.2400 -> 0.2659
    max_shift 0.15 (squeezed) ONE row lost its repair, the 0.2100 one, whose
                              flat landing check_drc FLAGS (overlap 0.0400
                              against its 0.0125 tolerance)

So the only repair the raise costs is one that shipped flagged copper.

DISCLOSED, and asserted below rather than only stated: check_drc forgives 5%
(`hole_to_hole_clearance * 0.05`, :2603/:2664), so a landing in [0.95*D, D) is
refused here and graded clean there. That band is this repo's grading margin,
not a fab rule -- kicad-cli enforces min_hole_to_hole with no forgiveness.

Conventions (from #725/#731/#732/#733/#737/#750 and CLAUDE.md): REAL parser
dataclasses; every assertion names the single-line MUTATION that must kill it,
with the count the battery measured; assert you are ON the branch before
asserting about it; every refusal paired with an acceptance that still happens.
The battery ships as `tests/mutate_756.py`.

MUTATION BATTERY, 22 rows across TWO targets: 20 killed, 2 SURVIVED and both
expected, 0 broken. The counts make the `# MUTATION:` notes checkable rather
than decorative, so they are recorded here and the battery itself ships as
`tests/mutate_756.py`:

    pad-floor-reads-the-VIA-fab-key                  20 assertions
    via-floor-reads-the-PAD-fab-key                  18
    h2h-via-gate-deleted                             12  + perturbs test_750
    resolver-never-reads-the-board                   10  THE DEFECT
    resolver-hard-wired-to-the-old-literals          10  the source-guard evasion
    resolver-drops-the-fab-wrap                       4
    bga-pad-arm-adopts-the-nudgers-045                3  the refused tidy-up
    resolver-drops-the-fab-wrap-on-the-PAD-arm-only   3
    resolver-drops-the-fab-wrap-on-the-VIA-arm-only   2
    fallback-passed-instead-of-None                   2
    h2h-pad-gate-deleted                              2  (test_737/test_730)
    bga-never-reads-the-board                         2
    cwd-probe-guard-dropped                           1
    raised-disclosure-deleted                         1
    below-fab-disclosure-deleted                      1
    disclosure-fires-at-the-packaged-default-too      1
    assignment-DUPLICATED-above-the-early-return      1  (test_750's shape half)
    bga-via-arm-reverted-to-the-flat-constant         1
    bga-pad-arm-reverted-to-the-flat-constant         1
    bga-drops-the-fab-wrap                            1
    assignment-moved-back-above-the-early-return      0  SURVIVED, expected:
                                                         a genuine move is inert
    layer-count-forced-to-the-multilayer-bucket       0  SURVIVED, expected:
                                                         0.20/0.45 in all four
                                                         fab cells

THE BATTERY FOUND FOUR DEFECTS IN THIS FILE AND IN ITSELF, corrected in place
rather than quietly fixed, because that is what the counts are evidence of:

  * THREE bga rows reported BROKEN -- `bga_fanout/__init__.py` is CRLF while
    `fanout_clearance.py` is LF, and the runner reads with `newline=''` so the
    restore is byte-identical. A multi-line anchor written with LF therefore
    matched zero times. The runner now translates the anchor to the target's
    own ending; BROKEN keeps meaning "stale anchor".
  * `bga-drops-the-fab-wrap` SURVIVED: `manage_vias` hands `board_floor`
    HOLE_TO_HOLE_CLEARANCE as its FALLBACK, so `_h2h_decl` can only fall below
    0.20 when a board declares below it -- and no arm did. The raise-only wrap
    was untested. `test_a_bga_board_declaring_BELOW_the_fab_floor_is_floored_up`
    is that arm and is the only thing that kills the row.
  * `bga-via-arm-reverted-to-the-flat-constant` SURVIVED: the bga rig drove a
    via-in-pad candidate against a foreign PAD drill, so of that function's TWO
    drill arms only the capsule one was ever exercised.
    `test_the_via_to_via_drill_arm_is_board_derived_TOO` is that arm.
  * `assignment-moved-back-above-the-early-return` was KILLED against its
    expectation -- because the row INSERTED a second assignment rather than
    moving the one that is there, which is a different mutation than its name
    claimed. Split into a genuine move (inert, expected survivor) and a
    duplication (killed by test_750's shape half), both kept.

Runs in-process in ~20 s; the only shelling out is one `git ls-files`.

    python3 tests/test_756_fanout_clearance_drill_floors.py
"""
from __future__ import annotations

RUN_ALL_FAST_OK = True
RUN_ALL_TIMEOUT = 900

import contextlib
import inspect
import io
import json
import math
import os
import shutil
import sys
import tempfile
import unittest
from types import SimpleNamespace

_TESTS = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_TESTS)
for _p in ('', 'py_router', 'py_placer', 'py_tools'):
    _d = os.path.join(_ROOT, _p)
    if _d not in sys.path:
        sys.path.insert(0, _d)
if _TESTS not in sys.path:
    sys.path.insert(0, _TESTS)

import run_utils
import routing_defaults as defaults
from fab_tiers import fab_floor_min
from kicad_parser import BoardInfo, Segment, parse_kicad_pcb
from list_nets import read_design_rules
from placement import fanout_clearance as FC
from placement.fanout_clearance import (nudge_vias_for_unresolved,
                                        repair_fanout_clearance,
                                        resolve_drill_floors)
from synth import make_pcb, make_via

# --- the rig ---------------------------------------------------------------
CLEAR = 0.1
VIA = (1.0, 1.4)
BAR = (0.2, 0.9, 1.8, 1.1, 2)      # a FOREIGN cap pad the via grazes
DRILL_R = 0.15                     # both vias carry synth's default 0.3 drill

# The fab floors, IMPORTED rather than mirrored -- saying so because a reader
# who believes they are mirrored will look for a source guard that should not
# exist. #756 is precisely the change that made them importable: before it they
# were function-local literals and three test files hand-copied them.
FAB = fab_floor_min(2)
FAB_VIA, FAB_PAD = FAB['hole_to_hole'], FAB['pad_hole_to_hole']

# The landing the rig's sweep reaches when nothing constrains it. Pinned here
# because several arms below are stated relative to it; if the sweep order or
# step ever changes this is the one number to re-derive.
FREE_LANDING = (1.0707, 1.4707)


def _bi(bounds=(0.0, 0.0, 4.0, 4.0), layers=('F.Cu', 'B.Cu')):
    return BoardInfo(layers={}, copper_layers=list(layers),
                     board_bounds=bounds)


def _projectless():
    """The board every rig in the sibling files builds: real BoardInfo, no
    `source_path`, so the resolver takes the fab floors without a disk read."""
    return make_pcb(board_info=_bi(), source_path='')


class _FakeCap:
    """Minimal stand-in for _Cap, the #370 B3 harness shape."""

    def __init__(self, rects):
        self._rects = list(rects)
        self.side = 'F'
        self.x = self.y = self.rot = 0.0

    def pad_rects(self, x=None, y=None, rot=None):
        return self._rects


class _FakeSt:
    """Minimal stand-in for _Repair: one cap, permanently 'unresolved'."""

    def __init__(self, rects):
        self.caps = {'C1': _FakeCap(rects)}
        self.vias = []

    def graze_penalty(self, ref, cap, x, y, rot):
        return 1.0


def _project(d, **rules):
    with open(os.path.join(d, 'b.kicad_pro'), 'w', encoding='utf-8') as f:
        json.dump({'board': {'design_settings': {'rules': rules}}}, f)


def _stub_board(tmp, name, **rules):
    """A bare board file plus a sibling project declaring `rules`. Only the
    PROJECT has to exist on disk -- the PCBData is synthetic."""
    d = os.path.join(tmp, name)
    os.makedirs(d, exist_ok=True)
    pcb = os.path.join(d, 'b.kicad_pcb')
    with open(pcb, 'w', encoding='utf-8') as f:
        f.write('(kicad_pcb (version 20240108))\n')
    if rules:
        _project(d, **rules)
    return pcb


def _nudge_rig(path, neighbour_offset):
    """The via the cap grazes, plus a SAME-NET neighbour parked directly above.

    Same net on purpose: the copper term (`ov.net_id != v.net_id`) then
    short-circuits and the via/via DRILL gate ALONE decides how far up the
    mover may go -- the shape tests/test_732's `_rig` uses for the same reason.
    The neighbour sits 0.8mm+ from the bar at every offset tried, so it is
    never itself an offender and exactly one via moves.
    """
    v = make_via(VIA[0], VIA[1], net_id=3)
    nb = make_via(VIA[0], VIA[1] + neighbour_offset, net_id=3)
    stub = Segment(start_x=VIA[0], start_y=VIA[1] - 0.2, end_x=VIA[0],
                   end_y=VIA[1], width=0.2, layer='F.Cu', net_id=3)
    pcb = make_pcb(board_info=_bi(), vias=[v, nb], segments=[stub],
                   footprints={'C1': SimpleNamespace(layer='F.Cu', pads=[])},
                   pads_by_net={}, source_path=path, zones=[])
    return v, nb, pcb


def _nudge(path, neighbour_offset, max_shift=0.6):
    """Returns (moves, segs, landing_or_None, drill_gap_or_None, printed)."""
    v, nb, pcb = _nudge_rig(path, neighbour_offset)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        moves, segs = nudge_vias_for_unresolved(_FakeSt([BAR]), pcb, CLEAR,
                                                max_shift=max_shift)
    if not moves:
        return moves, segs, None, None, buf.getvalue()
    gap = math.hypot(v.x - nb.x, v.y - nb.y) - 2 * DRILL_R
    return moves, segs, (v.x, v.y), gap, buf.getvalue()


# Neighbour offsets whose FREE-landing drill gap is the named value. Derived
# from FREE_LANDING, not guessed: gap = hypot(0.0707, D - 0.0707) - 0.30.
OFFSET_FOR_GAP = {0.21: 0.5758, 0.24: 0.6061, 0.26: 0.6262, 0.29: 0.6564}


class TestTheGateIsBoardDerived(unittest.TestCase):
    """The arms #756 exists for. Each pairs a REFUSAL with an acceptance that
    still happens, so none of them can pass on a rig that refuses everything."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)

    def _board(self, name, **rules):
        return _stub_board(self._tmp, name, **rules)

    def test_ON_THE_BRANCH_the_free_landing_is_where_this_file_says(self):
        """Every gap below is stated relative to FREE_LANDING. If the sweep
        moved, the offsets are measuring something else and every arm in this
        class is quietly about a different geometry."""
        _m, _s, land, _g, _o = _nudge(self._board('free'), 5.0)
        self.assertIsNotNone(land, 'the unconstrained rig no longer moves at '
                                   'all; the whole class is inert')
        self.assertAlmostEqual(land[0], FREE_LANDING[0], places=4)
        self.assertAlmostEqual(land[1], FREE_LANDING[1], places=4)
    # MUTATION: none -- this is the measurement that licenses the offsets, not
    # a fix assertion. Its job is to fail loudly if a later edit moves the
    # sweep.

    def test_a_declaring_board_REFUSES_the_landing_the_fab_floor_allowed(self):
        """THE DEFECT, and the fix, in one pair. At the flat 0.20 the pass
        parked the barrel 0.2100 from a neighbour drill on a board declaring
        0.25 -- copper check_drc then flags, since 0.25 - 0.21 = 0.0400 exceeds
        its 0.05*0.25 = 0.0125 tolerance."""
        off = OFFSET_FOR_GAP[0.21]
        # ON THE BRANCH: the free landing really would sit at 0.2100 here.
        self.assertAlmostEqual(
            math.hypot(FREE_LANDING[0] - VIA[0],
                       FREE_LANDING[1] - (VIA[1] + off)) - 2 * DRILL_R,
            0.21, places=4,
            msg='the offset table no longer produces a 0.21 gap')
        moves, segs, land, gap, _o = _nudge(self._board('d21',
                                                        min_hole_to_hole=0.25),
                                            off)
        self.assertEqual((len(moves), len(segs)), (1, 1),
                         'the repair was traded away: raising the floor must '
                         'move the landing, not abandon the via')
        self.assertGreaterEqual(
            gap, 0.25 - 1e-9,
            'the accepted landing is still inside the declared floor')
        self.assertNotAlmostEqual(
            land[1], FREE_LANDING[1], places=4,
            msg='the landing did not move, so the gate did not bind and this '
                'arm is measuring nothing')
    # MUTATION: 6 rows -- every edit that drops the board read, drops the
    # max(), or re-hardcodes the literal. This is the arm the fix exists for.

    def test_the_SAME_board_declaring_nothing_keeps_the_old_landing(self):
        """The negative control. Without it the arm above could be passing on
        a rig that refuses every candidate near the neighbour."""
        moves, segs, land, gap, _o = _nudge(self._board('undeclared'),
                                            OFFSET_FOR_GAP[0.21])
        self.assertEqual((len(moves), len(segs)), (1, 1))
        self.assertAlmostEqual(land[1], FREE_LANDING[1], places=4)
        self.assertAlmostEqual(gap, 0.21, places=4,
                               msg='a board that declares nothing must be '
                                   'byte-identical to the pre-#756 pass')
    # MUTATION: 4 rows -- anything that makes the fab fallback larger, or that
    # reads a declaration where there is none.

    def test_a_declaration_BELOW_the_fab_floor_cannot_lower_it(self):
        """RAISE-ONLY, in the code and not only in the docstring. `board_floor`
        is board-AUTHORITATIVE and hands back a declared 0.10 unwrapped, so
        without the max() this pass would relocate a via to a drill pair no fab
        can punch -- and would do it silently."""
        via, pad, decl, src = resolve_drill_floors(
            make_pcb(board_info=_bi(),
                     source_path=self._board('tiny', min_hole_to_hole=0.10)))
        self.assertEqual((via, pad), (FAB_VIA, FAB_PAD))
        self.assertEqual((decl, src), (0.10, 'board constraint'),
                         'the declaration must still be REPORTED -- the '
                         'disclosure branch keys on it')
    # MUTATION: `max(declared, fab_via)` -> `declared` at either floor. 3 rows.

    def test_and_SAYS_SO_rather_than_relaxing_in_silence(self):
        """A board file cannot lower a fab floor without the transcript saying
        it tried. The qfn/bga convention, and the reason the resolver returns
        `declared` separately from the resolved value."""
        _m, _s, _l, _g, out = _nudge(
            self._board('tiny2', min_hole_to_hole=0.10), 5.0)
        self.assertIn('below the', out)
        self.assertIn('fab hole-to-hole floor', out)
    # MUTATION: the `elif` disclosure branch deleted. 1 row.

    def test_a_raised_board_announces_the_number_it_used(self):
        _m, _s, _l, _g, out = _nudge(
            self._board('loud', min_hole_to_hole=0.25), 5.0)
        self.assertIn("from the board's own min_hole_to_hole", out)
        self.assertIn('0.25', out)
    # MUTATION: the `if` disclosure branch deleted. 1 row.

    def test_a_board_at_EXACTLY_the_fab_floor_says_nothing(self):
        """`> defaults.HOLE_TO_HOLE_CLEARANCE`, not `>=`: a board declaring the
        packaged default has nothing to disclose, and a line on every such run
        would be noise in a last-resort repair most runs never reach."""
        _m, _s, _l, _g, out = _nudge(
            self._board('exact', min_hole_to_hole=0.20), 5.0)
        self.assertNotIn('min_hole_to_hole', out)
    # MUTATION: `>` -> `>=` in the disclosure guard. 1 row.


class TestTheCostOfRaisingIt(unittest.TestCase):
    """The decision gate, kept as a test so the PR's numbers can be re-derived
    rather than quoted. This is the measurement that said the raise was safe to
    ship, and it is the one a reviewer attacking #756 should attack."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)

    def test_at_the_shipped_budget_the_raise_costs_NO_repair(self):
        """0.6mm is `nudge_vias_for_unresolved`'s own default and what
        `repair_fanout_clearance` passes. Across the whole band where the two
        floors disagree, every landing moves and none is abandoned."""
        board = _stub_board(self._tmp, 'ship', min_hole_to_hole=0.25)
        for gap, off in sorted(OFFSET_FOR_GAP.items()):
            moves, segs, _l, got, _o = _nudge(board, off)
            self.assertEqual((len(moves), len(segs)), (1, 1),
                             'gap %.2f: the repair was abandoned at the '
                             'SHIPPED budget' % gap)
            self.assertGreaterEqual(got, 0.25 - 1e-9,
                                    'gap %.2f: accepted below the declared '
                                    'floor' % gap)
    # MUTATION: none kills this -- it is the measurement that licenses the
    # change, not an assertion about it. It fails loudly if a later edit starts
    # costing repairs at the shipped budget.

    def test_squeezed_to_a_quarter_budget_ONE_row_does_lose_its_repair(self):
        """And it is the row whose flat landing check_drc FLAGS. Stated as a
        test because "the raise never costs a repair" would be a false claim
        and the PR must not make it."""
        board = _stub_board(self._tmp, 'squeeze', min_hole_to_hole=0.25)
        lost, kept = [], []
        for gap, off in sorted(OFFSET_FOR_GAP.items()):
            moves, _s, _l, _g, _o = _nudge(board, off, max_shift=0.15)
            (kept if moves else lost).append(gap)
        self.assertEqual(lost, [0.21],
                         'the set of gaps that lose their repair at a 0.15mm '
                         'budget has CHANGED: lost=%r kept=%r' % (lost, kept))
        # ...and that row is a REAL violation, not a forgiven one.
        self.assertGreater(0.25 - 0.21, 0.25 * 0.05,
                           'the lost row is inside check_drc\'s 5% tolerance, '
                           'so the raise now costs a repair the grader would '
                           'have passed -- re-read the #756 trade-off')
    # MUTATION: 2 rows. Also a change detector on the sweep order.

    def test_the_5pct_forgiveness_band_is_REAL_and_is_not_hidden(self):
        """check_drc flags only `overlap > hole_to_hole_clearance * 0.05`
        (:2603, :2664 -- a bare literal, not `_grade_tol`), so on a board
        declaring D a landing in [0.95*D, D) is refused here and graded clean
        there. #756 charges D anyway: that 5% is this repo's grading margin,
        not a fab rule, and kicad-cli enforces min_hole_to_hole with none.

        Asserted rather than only written down, because a disclosure nobody
        can check is a claim like any other."""
        D = 0.25
        self.assertTrue(0.95 * D <= 0.24 < D,
                        'the 0.24 fixture is no longer inside the band this '
                        'arm is about')
        board = _stub_board(self._tmp, 'band', min_hole_to_hole=D)
        # It is refused HERE...
        _m, _s, _l, got, _o = _nudge(board, OFFSET_FOR_GAP[0.24])
        self.assertGreaterEqual(got, D - 1e-9)
        # ...and the flat landing it replaced would have graded CLEAN there.
        self.assertLessEqual(D - 0.24, D * 0.05)
        # The repair survives anyway, at the shipped budget AND squeezed --
        # which is why the band is a disclosure and not a cost.
        for shift in (0.6, 0.15):
            moves, _s2, _l2, _g2, _o2 = _nudge(board, OFFSET_FOR_GAP[0.24],
                                               max_shift=shift)
            self.assertEqual(len(moves), 1,
                             'the band row lost its repair at max_shift %s; '
                             'the #756 disclosure understates the cost'
                             % shift)
    # MUTATION: 1 row (the tolerance expression in check_drc is not mutated
    # here -- this arm mirrors it; TestParityWithTheChecker pins the mirror).


class TestParityWithTheChecker(unittest.TestCase):
    """The rule #756 rests on: raise a floor at this site iff check_drc raises
    it. Mirrored here BY LINE, and said out loud to be a mirror."""

    def test_check_drc_raises_hole_to_hole_from_the_same_board_key(self):
        import check_drc
        src = inspect.getsource(check_drc)
        self.assertIn("_pin_up('hole_to_hole_clearance'", src,
                      'check_drc no longer board-derives its hole-to-hole '
                      "floor, so #756's parity argument has expired -- "
                      're-derive it before trusting the resolver')
        self.assertIn("get('min_hole_to_hole')", src,
                      'the key check_drc pins from has changed')
    # MUTATION: none -- a mirror of another module, and a change detector on it.

    def test_the_pad_arm_stays_STRICTER_than_the_checker_deliberately(self):
        """check_drc grades via-drill<->pad-drill at the SAME single
        hole-to-hole value, not at 0.45. So this pass over-blocks that pair by
        0.25mm on every board declaring at or below 0.45 -- which is all of
        them. Deliberate: 0.45 is the JLC pad-hole fab minimum and nothing else
        in the repo enforces it. Pinned so a later "consistency" pass cannot
        quietly level it DOWN and call it parity."""
        self.assertGreater(FAB_PAD, FAB_VIA)
        via, pad, _d, _s = resolve_drill_floors(_projectless())
        self.assertEqual((via, pad), (FAB_VIA, FAB_PAD))
    # MUTATION: `fmin['pad_hole_to_hole']` -> `fmin['hole_to_hole']`. 1 row.


class TestTheResolverItself(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)

    def test_no_project_takes_the_fab_floors_without_reading_the_disk(self):
        """`read_design_rules("")` probes ".kicad_pro" RELATIVE TO THE PROCESS
        CWD, so an empty `source_path` must short-circuit BEFORE the read --
        otherwise a stray file of that name is read as this board's rules.
        Proven by planting exactly that file and cd-ing into it."""
        d = os.path.join(self._tmp, 'cwdtrap')
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, '.kicad_pro'), 'w', encoding='utf-8') as f:
            json.dump({'board': {'design_settings':
                                 {'rules': {'min_hole_to_hole': 9.0}}}}, f)
        cwd = os.getcwd()
        try:
            os.chdir(d)
            self.assertEqual(resolve_drill_floors(_projectless()),
                             (FAB_VIA, FAB_PAD, None, 'fixed default'))
        finally:
            os.chdir(cwd)
    # MUTATION: the `if not path` guard dropped -> this arm goes to 9.0. 1 row.

    def test_a_declaring_project_is_read_and_TAGGED(self):
        p = _stub_board(self._tmp, 'decl', min_hole_to_hole=0.25)
        self.assertEqual(resolve_drill_floors(make_pcb(board_info=_bi(),
                                                       source_path=p)),
                         (0.25, FAB_PAD, 0.25, 'board constraint'))
    # MUTATION: 5 rows.

    def test_a_declaration_above_the_PAD_floor_moves_the_pad_arm_too(self):
        """One board rule behind BOTH numbers, and that is not an
        approximation: min_hole_to_hole governs any two holes, which is why
        `list_nets.effective_floors` floors both of its keys at it. No board
        this repo tracks declares above 0.45, so this arm is the only place the
        pad arm is shown to move at all."""
        p = _stub_board(self._tmp, 'huge', min_hole_to_hole=0.60)
        self.assertEqual(resolve_drill_floors(make_pcb(board_info=_bi(),
                                                       source_path=p))[:2],
                         (0.60, 0.60))
    # MUTATION: `max(declared, fab_pad)` -> `fab_pad`. 1 row.

    def test_a_project_that_declares_nothing_reports_no_declaration(self):
        p = _stub_board(self._tmp, 'silent')
        via, pad, decl, _src = resolve_drill_floors(
            make_pcb(board_info=_bi(), source_path=p))
        self.assertEqual((via, pad, decl), (FAB_VIA, FAB_PAD, None),
                         'a project with no min_hole_to_hole must not report '
                         'a declaration -- the disclosure keys on it')
    # MUTATION: `fallback=None` -> `defaults.HOLE_TO_HOLE_CLEARANCE`, which
    # makes `declared` a fallback comparing against itself. 1 row.

    def test_a_corrupt_project_is_not_a_declaration(self):
        d = os.path.join(self._tmp, 'corrupt')
        os.makedirs(d, exist_ok=True)
        pcb = os.path.join(d, 'b.kicad_pcb')
        with open(pcb, 'w', encoding='utf-8') as f:
            f.write('(kicad_pcb (version 20240108))\n')
        with open(os.path.join(d, 'b.kicad_pro'), 'w', encoding='utf-8') as f:
            f.write('{not json at all')
        via, pad, decl, _s = resolve_drill_floors(
            make_pcb(board_info=_bi(), source_path=pcb))
        self.assertEqual((via, pad, decl), (FAB_VIA, FAB_PAD, None))
    # MUTATION: none -- `board_constraint` swallows its own read errors, so
    # this is a change detector on that contract rather than on #756's code.

    def test_a_duck_typed_pcb_data_does_not_explode(self):
        """The function is reached from harnesses that pass SimpleNamespace,
        and from the GUI with a live board. Neither may raise."""
        self.assertEqual(resolve_drill_floors(SimpleNamespace()),
                         (FAB_VIA, FAB_PAD, None, 'fixed default'))
        self.assertEqual(
            resolve_drill_floors(SimpleNamespace(board_info=None,
                                                 source_path=None)),
            (FAB_VIA, FAB_PAD, None, 'fixed default'))
    # MUTATION: either `getattr` chain replaced by a direct attribute read.
    # 2 rows.

    def test_it_agrees_with_list_nets_effective_floors(self):
        """The independent oracle. `effective_floors` computes the same two
        numbers from the same board key, and #756 deliberately hand-composes
        instead of calling it (it counts copper layers off the FILE, which is 0
        for an unsaved GUI board, and returns no source tag). Pinning them
        EQUAL turns "we mirror the shared rule" from prose into a failing test.

        Run on the two tracked boards that declare anything at all."""
        boards = [b for b in run_utils.corpus_boards()
                  if os.path.exists(os.path.splitext(b)[0] + '.kicad_pro')]
        if not boards:
            print('SKIP: git cannot identify the tracked corpus')
            self.skipTest('no git')
        self.assertGreaterEqual(
            len(boards), 2,
            'the set of tracked boards carrying a sibling project has '
            'SHRUNK to %d; this arm has no witness left' % len(boards))
        for b in boards:
            eff = read_design_rules(b)['effective']
            got = resolve_drill_floors(parse_kicad_pcb(b))[:2]
            self.assertEqual(
                got, (eff['drc_hole_to_hole'], eff['pad_hole_to_hole']),
                '%s: the resolver and effective_floors disagree'
                % os.path.basename(b))
    # MUTATION: 4 rows -- any change to either fab key or to the max().


class TestTheLayerCountChoice(unittest.TestCase):

    def test_the_layer_count_cannot_change_either_floor_TODAY(self):
        """Not a wiring, a CHANGE DETECTOR. `_FAB_FLOORS` carries 0.20/0.45 in
        all four cells and an override file only REPLACES keys already present,
        so `fab_floor_min(n)[k]` is independent of n for both keys under every
        tier. The resolver therefore picks a bucket it cannot currently be
        wrong about -- and this arm is what makes a future per-layer hole floor
        a failure instead of a silent mis-bucket."""
        for k in ('hole_to_hole', 'pad_hole_to_hole'):
            self.assertEqual(fab_floor_min(2)[k], fab_floor_min(4)[k],
                             '%s now differs by layer bucket; re-read '
                             "resolve_drill_floors' layer-count choice" % k)
    # MUTATION: none -- it guards a fab_tiers table this change does not touch.

    def test_a_board_with_no_readable_layers_still_resolves(self):
        """`_layer_floors` maps 0 to the 2-layer bucket (`(n or 2) <= 2`), so
        an empty copper list is the same answer rather than a KeyError."""
        self.assertEqual(
            resolve_drill_floors(make_pcb(board_info=_bi(layers=()),
                                          source_path='')),
            (FAB_VIA, FAB_PAD, None, 'fixed default'))
    # MUTATION: `or []` dropped from the copper-layer read. 1 row.


class TestEveryExistingNudgerRigIsUnmoved(unittest.TestCase):
    """#617's doctrine is cited against this change; its rig cannot reach the
    gate. Asserted rather than argued."""

    def test_no_nudger_harness_declares_min_hole_to_hole(self):
        """So every one of them resolves to the fab floors and is
        byte-identical under #756. If a future harness starts declaring it,
        this fails and its author has to look at what moved."""
        names = ('test_370_tierb_fixes.py',
                 'test_617_placement_fanout_hole_clearance.py',
                 'test_725_fanout_clearance_pad_floors.py',
                 'test_730_fanout_clearance_npth_local_clearance.py',
                 'test_732_fanout_clearance_via_radius.py',
                 'test_733_fanout_clearance_edge_margin.py',
                 'test_736_fanout_clearance_regrade_view.py',
                 'test_737_fanout_clearance_via_hole.py',
                 'test_741_via_nudge_tenting.py',
                 'test_746_fanout_clearance_resolved_credit.py',
                 'test_750_fanout_clearance_via_drill.py')
        present = [n for n in names if os.path.exists(os.path.join(_TESTS, n))]
        self.assertGreaterEqual(len(present), 10,
                                'the nudger harness set has shrunk to %d; '
                                'this arm is no longer about the row it names'
                                % len(present))
        declaring = [n for n in present
                     if 'min_hole_to_hole' in open(os.path.join(_TESTS, n),
                                                   encoding='utf-8').read()]
        self.assertEqual(declaring, [],
                         'a nudger harness now declares min_hole_to_hole: %r. '
                         "#756's claim that every existing rig is unmoved has "
                         'EXPIRED -- re-run them and record what changed'
                         % declaring)
    # MUTATION: none -- a claim about the OTHER files, and a change detector.

    def test_the_617_rig_cannot_reach_the_via_to_via_gate_at_all(self):
        """One via, so `if ov is v: continue` fires and the loop body never
        runs. This is why #617's measurement -- real, and re-derived under #730
        -- is silent about H2H_VIA."""
        src = inspect.getsource(FC.nudge_vias_for_unresolved)
        self.assertIn('if ov is v:', src,
                      'the self-skip is gone, so a single-via board now DOES '
                      "reach the via/via gate and #617's rig is no longer "
                      'blind to it')
    # MUTATION: the `if ov is v: continue` skip deleted -> killed here and by
    # every arm that builds a one-via rig. 1 row.


class TestInertOnTheTrackedCorpus(unittest.TestCase):
    """The row's self-expiring bound. #756 is inert on the tracked corpus at
    file poses for THREE independent reasons, and a "0 diffs" run proves
    nothing unless all three are stated: 20 of 22 boards carry no project at
    all, the 2 that do never reach the via-nudge, and none declares above 0.45.

    So this asserts the REASONS, not just the outcome."""

    def setUp(self):
        self.boards = run_utils.corpus_boards()
        if not self.boards:
            print('SKIP: git cannot identify the tracked corpus')
            self.skipTest('no git')
        self.assertGreaterEqual(len(self.boards), 20,
                                'the tracked corpus collapsed to %d boards; '
                                'nothing below is a bound'
                                % len(self.boards))

    def test_only_two_tracked_boards_can_declare_anything(self):
        withpro = [os.path.basename(b) for b in self.boards
                   if os.path.exists(os.path.splitext(b)[0] + '.kicad_pro')]
        self.assertEqual(
            sorted(withpro),
            ['flat_hierarchy.kicad_pcb', 'routed_output.kicad_pcb'],
            'the set of tracked boards carrying a sibling project has '
            'CHANGED: %r. The "inert on the corpus" claim in the #756 PR has '
            'EXPIRED -- re-run the before/after sweep and record the new '
            'numbers' % sorted(withpro))

    def test_no_tracked_board_declares_above_the_PAD_fab_floor(self):
        """Which is why the pad arm has no corpus witness and is demonstrated
        synthetically instead. Said out loud rather than left to be noticed."""
        over = []
        for b in self.boards:
            _via, pad, decl, _s = resolve_drill_floors(parse_kicad_pcb(b))
            if decl is not None and decl > FAB_PAD:
                over.append((os.path.basename(b), decl))
        self.assertEqual(over, [],
                         'a tracked board now declares above the %s pad-hole '
                         'fab floor: %r. The pad arm now HAS a corpus '
                         'witness -- measure it and record it' % (FAB_PAD, over))

    def test_the_via_nudge_emits_nothing_on_any_tracked_board(self):
        """NOT REACHABLE, which is a different claim from NO EFFECT, and the
        PR must not blur them. Measured at the shipped defaults: zero via_moves
        on every tracked board, so the corpus cannot witness this change at
        file poses either way."""
        noisy = []
        for b in self.boards:
            buf = io.StringIO()
            try:
                with contextlib.redirect_stdout(buf):
                    r = repair_fanout_clearance(parse_kicad_pcb(b), b,
                                                clearance=0.2)
            except Exception as e:                            # noqa: BLE001
                noisy.append((os.path.basename(b), 'ERROR', repr(e)[:60]))
                continue
            if r.get('via_moves') or r.get('new_segments'):
                noisy.append((os.path.basename(b), len(r['via_moves']),
                              len(r['new_segments'])))
        self.assertEqual(noisy, [],
                         'a tracked board now reaches the via-nudge at the '
                         'shipped defaults: %r. The "inert on the corpus" '
                         'claim in the #756 PR has EXPIRED -- re-run the '
                         'before/after sweep and record the new numbers'
                         % noisy)
    # MUTATION: none -- a self-expiring corpus bound, not a fix assertion.


class TestTheBgaSibling(unittest.TestCase):
    """`bga_fanout.manage_vias`' via_in_pad_conflict had the identical defect,
    named in its own docstring. Closed in the same PR, narrowly."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)

    def _run(self, path, sep):
        from bga_fanout import manage_vias
        from bga_fanout.types import FanoutRoute
        from kicad_parser import Pad

        def pad(x, y, **kw):
            d = dict(pad_number='1', net_id=0, net_name='', global_x=x,
                     global_y=y, local_x=0.0, local_y=0.0, size_x=0.5,
                     size_y=0.5, shape='circle', layers=['F.Cu', 'B.Cu'],
                     drill=0.0, pad_type='smd', component_ref='X1')
            d.update(kw)
            return Pad(**d)

        ball = pad(10.0, 10.0, net_id=7, layers=['F.Cu'], component_ref='U1',
                   pad_number='A1')
        foreign = pad(10.0 + sep, 10.0, drill=0.3, size_x=0.6, size_y=0.6,
                      pad_type='thru_hole', component_ref='U2')
        r = FanoutRoute(pad=ball, pad_pos=(10.0, 10.0), stub_end=(10.5, 10.5),
                        exit_pos=(11.0, 10.5), layer='B.Cu')
        pcb = make_pcb(board_info=_bi((0.0, 0.0, 20.0, 20.0),
                                      ('F.Cu', 'In1.Cu', 'In2.Cu', 'B.Cu')),
                       vias=[], segments=[],
                       pads_by_net={7: [ball], 0: [foreign]},
                       source_path=path, zones=[])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            add, _rm, blocked = manage_vias([r], pcb, 'F.Cu', 0.45, 0.2, 0.1)
        return len(add), len(blocked), buf.getvalue()

    def test_the_verdict_flips_at_the_declared_floor(self):
        """0.2 via drill (r 0.10) against a 0.3 pad drill (r 0.15), so the
        drill-edge gap is `sep - 0.25`. A refusal AND an acceptance at every
        declaration, so no row can pass on a rig that refuses everything."""
        undecl = _stub_board(self._tmp, 'bga_none')
        d25 = _stub_board(self._tmp, 'bga_25', min_hole_to_hole=0.25)
        d30 = _stub_board(self._tmp, 'bga_30', min_hole_to_hole=0.30)
        # gap 0.20 / 0.25 / 0.30  (sep 0.45 / 0.50 / 0.55)
        self.assertEqual([self._run(undecl, s)[0] for s in (0.45, 0.50, 0.55)],
                         [1, 1, 1],
                         'a board declaring nothing must be byte-identical to '
                         'the pre-#756 pass')
        self.assertEqual([self._run(d25, s)[0] for s in (0.45, 0.50, 0.55)],
                         [0, 1, 1])
        self.assertEqual([self._run(d30, s)[0] for s in (0.45, 0.50, 0.55)],
                         [0, 0, 1])
    # MUTATION: 4 rows in the bga target -- `_h2h` reverted to the flat
    # constant at either arm, the max() dropped, the board read dropped.

    def _run_via(self, path, sep):
        """Same shape, but the thing to clear is an existing VIA rather than a
        pad drill -- `via_in_pad_conflict` has TWO arms and the pad rig above
        exercises only one of them.

        The foreign via is deliberately SMALL in copper (0.2 dia) and normal in
        drill (0.3): `would_overlap_existing_via` needs
        0.1 + 0.225 + 0.1 = 0.425 and the drill gate needs 0.25 + h2h, so at
        sep 0.46 the copper gate is satisfied and the DRILL gate alone decides.
        Net 0, not the ball's net, so `find_nearby_via` cannot reuse it.
        """
        from bga_fanout import manage_vias
        from bga_fanout.types import FanoutRoute
        from kicad_parser import Pad

        ball = Pad(pad_number='A1', net_id=7, net_name='', global_x=10.0,
                   global_y=10.0, local_x=0.0, local_y=0.0, size_x=0.5,
                   size_y=0.5, shape='circle', layers=['F.Cu'], drill=0.0,
                   pad_type='smd', component_ref='U1')
        r = FanoutRoute(pad=ball, pad_pos=(10.0, 10.0), stub_end=(10.5, 10.5),
                        exit_pos=(11.0, 10.5), layer='B.Cu')
        ov = make_via(10.0 + sep, 10.0, net_id=0, size=0.2, drill=0.3)
        pcb = make_pcb(board_info=_bi((0.0, 0.0, 20.0, 20.0),
                                      ('F.Cu', 'In1.Cu', 'In2.Cu', 'B.Cu')),
                       vias=[ov], segments=[], pads_by_net={7: [ball]},
                       source_path=path, zones=[])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            add, _rm, _blocked = manage_vias([r], pcb, 'F.Cu', 0.45, 0.2, 0.1)
        return len(add)

    def test_the_via_to_via_drill_arm_is_board_derived_TOO(self):
        """`via_in_pad_conflict` has two arms and #756 moved both. The battery
        caught this one having NO test: reverting it to the flat constant
        survived every arm here, because the rig above only ever presented a
        pad drill. A refusal AND an acceptance, so the rig cannot be one that
        refuses everything."""
        undecl = _stub_board(self._tmp, 'bgav_none')
        d25 = _stub_board(self._tmp, 'bgav_25', min_hole_to_hole=0.25)
        # drill gap at sep 0.46 is 0.46 - (0.1 + 0.15) = 0.21.
        self.assertEqual(self._run_via(undecl, 0.46), 1,
                         'a board declaring nothing must be byte-identical to '
                         'the pre-#756 pass on the via arm too')
        self.assertEqual(self._run_via(d25, 0.46), 0,
                         'the via/via drill arm ignored the declaration')
        self.assertEqual(self._run_via(d25, 0.80), 1)
    # MUTATION: bga-via-arm-reverted-to-the-flat-constant. 1 row -- and this
    # arm is the ONLY thing that kills it.

    def test_a_bga_board_declaring_BELOW_the_fab_floor_is_floored_up(self):
        """The raise-only wrap, which nothing exercised until the battery said
        so: `bga-drops-the-fab-wrap` SURVIVED the first run because
        `manage_vias` hands `board_floor` HOLE_TO_HOLE_CLEARANCE as its
        FALLBACK, so `_h2h_decl` can only fall below 0.20 when a board declares
        below it -- and no arm did. A project declaring 0.10 must still space
        drills at the fab floor, or this pass places holes no fab can punch."""
        tiny = _stub_board(self._tmp, 'bga_tiny', min_hole_to_hole=0.10)
        # gap 0.15 (sep 0.40): under the 0.20 fab floor, over a declared 0.10.
        self.assertEqual(self._run(tiny, 0.40)[0], 0,
                         'a sub-fab declaration lowered the drill spacing; '
                         'the max() against the fab floor is gone')
        # ...and the acceptance that still happens, so this is not a rig that
        # refuses everything.
        self.assertEqual(self._run(tiny, 0.50)[0], 1)
    # MUTATION: bga-drops-the-fab-wrap. 1 row -- and this arm is the ONLY
    # thing that kills it.

    def test_it_did_NOT_adopt_the_nudgers_045_pad_floor(self):
        """The two passes disagree by 0.25mm about the very same
        via-drill<->pad-drill pair -- 0.45 in the via-nudge, the single
        hole-to-hole value here. #756 reconciles neither; unifying them would
        move keep-outs on every fine-pitch BGA via-in-pad escape and needs its
        own before/after. Pinned so nobody 'tidies' it in passing."""
        undecl = _stub_board(self._tmp, 'bga_pad')
        # gap 0.20: refused by 0.45, allowed by the 0.20 fab hole-to-hole.
        self.assertEqual(self._run(undecl, 0.45)[0], 1,
                         'the bga pass now charges the pad-hole floor; that is '
                         'a THIRD change with its own before/after, not a '
                         'tidy-up')
    # MUTATION: `_h2h` -> the pad-hole floor at the capsule arm. 1 row.


if __name__ == '__main__':
    unittest.main(verbosity=2)
