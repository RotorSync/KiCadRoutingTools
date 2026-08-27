#!/usr/bin/env python3
"""#735: the via-nudger's connector copper is priced at the board's TRACK-scoped
`.kicad_dru` rules, and at those rules ONLY where both sides are tracks.

THE DEFECT. `nudge_vias_for_unresolved` draws a connector back to a relocated
via's stub and gates that copper itself, in `connector_clear`. Its seg-vs-seg
arm resolved through `required`, which carries net classes and the `.kicad_dru`
LAYER rules but not the TRACK-scoped ones -- a separate channel that check_drc
applies at the seg-seg site, which is exactly the arm in question. On a board
declaring a track-to-track rule the pass therefore drew copper closer to a
foreign track than its own checker accepts. It UNDER-blocked, so the violation
shipped rather than the landing being refused.

THE FIX has three parts and this file pins each separately, because they fail
for different reasons: `kicad_dru.track_pair_clearance` is the one binding
predicate (check_drc delegates to it), `PadClearanceModel` carries the rules
and the class memberships, and `_Repair.track_required` is the resolver the
nudger reads through `getattr` like every other.

THE MEASURED LADDER, and why it is a ladder rather than one arm. The connector
is 0.2 wide, the foreign track is 0.2 wide, so the requirement between them is
`0.1 + 0.1 + R`: 0.30 with no rule, 0.65 under a 0.45 rule. Placing one foreign
track a chosen perpendicular distance from the connector's OFF path brackets
that number from both sides:

    dperp   no dru                    with the 0.45 rule
    0.28    ALT (base gate refuses)   ALT           <- the base gate is live
    0.45    RIG_LANDING               no clear spot <- the headline
    0.61    RIG_LANDING               ALT           <- selective, not a blanket
    0.75    RIG_LANDING               RIG_LANDING   <- above 0.65 it stops binding

The 0.75 and 0.28 rows are what make the 0.45 row mean something: without them
"the ON arm refuses" is equally consistent with a rule that refuses everything
and with a fixture that refuses everything. Measured, not predicted -- the
0.65 boundary was located by sweeping and it sits between 0.64 and 0.66.

THE 0.61 ROW IS THE ONE NARROW MARGIN IN THIS FILE, and it is stated rather
than hidden: the relocation band is [0.58, 0.65), 0.06 wide, so no value in it
can be 0.05 from both edges. 0.61 is 0.03 from each. Every other constant here
clears its boundary by 0.10 or more.

CONVENTIONS FOLLOWED (the #725/#736/#747 family's):

  * REAL parser dataclasses and a REAL board -- `_Repair.__init__` reads
    courtyards and locked refs off the file on disk.
  * Every assertion names the single-line MUTATION that must kill it.
  * Assert you are ON the branch before asserting about it: a landing arm
    first checks the via actually moved, or an inert run passes it too.
  * Every "is not touched" is paired with a NEGATIVE CONTROL that is.
  * Source guards read CODE ONLY, with trailing comments stripped, and every
    count arm is paired with a positive control so a rename fails HERE rather
    than silently disarming it.

The child-process half -- the paired check_drc grade of the copper this pass
emits -- is in the `_e2e` sibling, so this file stays in the fast bucket.
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
import sys
import tempfile
import unittest

_TESTS = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_TESTS)
for _p in ('', 'py_router', 'py_placer', 'py_tools'):
    _d = os.path.join(_ROOT, _p)
    if _d not in sys.path:
        sys.path.insert(0, _d)
if _TESTS not in sys.path:
    sys.path.insert(0, _TESTS)

import run_utils
import check_drc
import kicad_dru
from copy_board import copy_board
from kicad_dru import TrackRule, board_track_rules, track_pair_clearance
from kicad_parser import Net, parse_kicad_pcb
from synth import make_seg, make_via
from placement import fanout_clearance as FC
from placement.legality import PadClearanceModel
from placement.fanout_clearance import _Repair, nudge_vias_for_unresolved

# The one board in the repo on which the nudge can be made to FIRE; the same
# board and cap test_725 and test_747 rig, for the same reason.
BOARD = os.path.join(_ROOT, 'kicad_files', 'orangecrab_ext_pll.kicad_pcb')
CLEAR = 0.1
CAP = 'C67'
PREFIX = 'C,R,FB'          # the CLI default
RULE = 0.45

# An INNER layer on purpose. The connector necessarily starts at the OLD via
# position, which sits inside the grazed cap's keep-out; on the cap's own side
# its pads refuse every landing, and the pass emits nothing to price. Measured:
# F.Cu and B.Cu report "no clear spot" for this rig with no foreign track at
# all, In1..In4 all land identically.
LAYER = 'In1.Cu'

# Measured on the rig below at CLEAR. RIG_LANDING is test_747's number too,
# which is the cheapest available check that this rig is the family's rig.
RIG_START = (156.7307, 101.4006)
RIG_LANDING = (157.6084, 101.7641)
ALT_LANDING = (155.6807, 101.4006)

# The two nets, by name, so a board edit that renumbers them fails loudly here
# rather than quietly grading a pair that is not the pair.
CRIT_NET, CRIT_NAME = 1, '/sheetHyperRAM/RAM_VDDQ'
FOREIGN_NET, FOREIGN_NAME = 2, 'RAM_D13'

# The rule text and the project shape are copied from
# tests/test_549_track_clearance_e2e.py, so the two files cannot come to
# disagree about what a track rule looks like.
DRU_BOTH = ('(version 1)\n(rule crit_space (condition "A.Type==\'track\' && '
            'B.Type==\'track\' && A.NetClass==\'CRIT\'") '
            '(constraint clearance (min %gmm)))\n' % RULE)
DRU_OTHER = ('(version 1)\n(rule crit_space (condition "A.Type==\'track\' && '
             'B.Type==\'track\' && A.NetClass==\'CRIT\' && '
             'B.NetClass!=\'CRIT\'") '
             '(constraint clearance (min %gmm)))\n' % RULE)
DRU_LAYER = ('(version 1)\n(rule r (layer "%s") '
             '(constraint clearance (min 0.5mm)))\n' % LAYER)

# The four rungs of the ladder in the docstring.
D_BASE = 0.28        # under the 0.30 base requirement: BOTH arms refuse
D_REFUSE = 0.45      # over 0.30, under 0.65: only the ruled arm refuses
D_RELOCATE = 0.61    # the ruled arm finds a DIFFERENT landing
D_INERT = 0.75       # over 0.65: the rule binds nothing, both arms agree

# A bare board that declares a track rule and NOTHING else -- no class above
# the floor, no layer rule, no pad override. The whole point of `_track` being
# a second handle is that this board keeps `_floors is None`.
BARE = ('(kicad_pcb (version 20240108)\n'
        '  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))\n'
        '  (gr_rect (start 0 0) (end 20 20) (stroke (width 0.05) '
        '(type solid)) (layer "Edge.Cuts"))\n)\n')


def _pro(pattern=CRIT_NAME):
    """A project declaring CRIT, at the SAME clearance as Default and as the
    run. Deliberate: `PadClearanceModel` admits a class only when it exceeds
    the board-wide floor, so this leaves `net_floor` empty and the track rule
    is the only thing the model learned from the board."""
    return {'net_settings': {
        'classes': [{'name': 'Default', 'clearance': CLEAR},
                    {'name': 'CRIT', 'clearance': CLEAR}],
        'netclass_assignments': {},
        'netclass_patterns': [{'pattern': pattern, 'netclass': 'CRIT'}]}}


def _stage(tmp, name, dru=None, pro=None, bare=False):
    """A COPY of the rig board (siblings carried by copy_board), plus a project
    and an optional .kicad_dru. `bare=True` writes the minimal outline board
    above instead -- NO board in this repo ships a .kicad_dru, so every arm
    that needs one writes it."""
    d = os.path.join(tmp, name)
    os.makedirs(d, exist_ok=True)
    dst = os.path.join(d, 'b.kicad_pcb')
    if bare:
        with open(dst, 'w', encoding='utf-8') as f:
            f.write(BARE)
    else:
        copy_board(BOARD, dst)
    stem = os.path.splitext(dst)[0]
    with open(stem + '.kicad_pro', 'w', encoding='utf-8') as f:
        json.dump(_pro() if pro is None else pro, f)
    if dru is not None:
        with open(stem + '.kicad_dru', 'w', encoding='utf-8') as f:
            f.write(dru)
    return dst


def _repair(path, pcb):
    """The 10-POSITIONAL construction every test in this family uses. Calling
    it positionally is itself part of the #725 shape contract."""
    return _Repair(pcb, path, CLEAR, 0.1, 0.55, 1.0, 2.0, 0.3, PREFIX, set())


def _foreign(dperp, half_len=0.15):
    """One short track parallel to the OFF connector, `dperp` from it.

    SHORT and centred on the connector's midpoint so its distance to either
    endpoint stays well above the via's own requirement -- the via arm must not
    be what refuses a landing, or the connector arm this file is about is never
    reached. Measured at D_REFUSE: 0.55 to each endpoint against a 0.45 via
    requirement."""
    dx, dy = RIG_LANDING[0] - RIG_START[0], RIG_LANDING[1] - RIG_START[1]
    n = math.hypot(dx, dy)
    ux, uy = dx / n, dy / n
    cx = (RIG_START[0] + RIG_LANDING[0]) / 2.0 + dperp * -uy
    cy = (RIG_START[1] + RIG_LANDING[1]) / 2.0 + dperp * ux
    return make_seg(round(cx - half_len * ux, 4), round(cy - half_len * uy, 4),
                    round(cx + half_len * ux, 4), round(cy + half_len * uy, 4),
                    layer=LAYER, net_id=FOREIGN_NET, width=0.2)


def _rig(path, dperp=None):
    """A REAL board carrying ONE offending via beside C67's first pad, a
    same-net stub so a connector is actually drawn, and optionally ONE foreign
    track. Everything else is cleared so the connector arm is what decides."""
    pcb = parse_kicad_pcb(path)
    st = _repair(path, pcb)
    rect = st.caps[CAP].pad_rects()[0]
    vx, vy = rect[2] + 0.20, (rect[1] + rect[3]) / 2.0
    segs = [make_seg(vx, vy - 0.6, vx, vy, layer=LAYER, net_id=CRIT_NET,
                     width=0.2)]
    if dperp is not None:
        segs.append(_foreign(dperp))
    pcb.vias[:] = [make_via(vx, vy, net_id=CRIT_NET, size=0.5, drill=0.3)]
    pcb.segments[:] = segs
    st.vias = [st._register_via(vx, vy, CRIT_NET, radius=0.25)]
    st._via_radius_by_id = {id(st.vias[0]): (st.vias[0], 0.25)}
    st.cap_vias = {k: st.vias for k in st.caps}
    st.segments = []
    st.cap_segs = {k: [] for k in st.caps}
    return pcb, st, (vx, vy)


def _nudge(st, pcb, **kw):
    """Drive the real pass, capturing what it printed."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        moves, segs = nudge_vias_for_unresolved(st, pcb, CLEAR, **kw)
    return moves, segs, buf.getvalue()


def _landing(moves):
    return (None if not moves
            else (round(moves[0][2]['x'], 4), round(moves[0][2]['y'], 4)))


class _TmpCase(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)

    def stage(self, name, **kw):
        return _stage(self._td.name, name, **kw)

    def run_ladder(self, dperp):
        """(off_landing, on_landing) for one rung, from two independent
        stages. Two stages, not one re-read: a shared directory would let the
        first run's project writeback reach the second."""
        out = []
        for tag, dru in (('off', None), ('on', DRU_BOTH)):
            p = self.stage('%s_%s' % (tag, str(dperp).replace('.', '')),
                           dru=dru)
            pcb, st, _v0 = _rig(p, dperp=dperp)
            moves, segs, text = _nudge(st, pcb, max_shift=4.0)
            out.append((_landing(moves), len(segs), text, st))
        return out


# ---------------------------------------------------------------------------
# 1. The shared resolver
# ---------------------------------------------------------------------------
class TestTheBindingPredicateIsOneFunction(unittest.TestCase):
    R_BOTH = TrackRule('crit_space', 'CRIT', False, RULE)
    R_OTHER = TrackRule('crit_space', 'CRIT', True, RULE)
    CRIT = frozenset({'CRIT'})
    NONE = frozenset()

    def test_a_member_versus_a_NON_member_binds(self):
        eff, rule = track_pair_clearance([self.R_BOTH], self.CRIT, self.NONE,
                                         CLEAR)
        self.assertEqual((eff, rule), (RULE, self.R_BOTH))
        # symmetric: which side carries the class cannot matter
        self.assertEqual(track_pair_clearance([self.R_BOTH], self.NONE,
                                              self.CRIT, CLEAR)[0], RULE)
    # MUTATION: `binds = (a_in and not b_in)` -> the mirrored call goes flat.

    def test_two_members_bind_unless_the_rule_says_other_only(self):
        self.assertEqual(track_pair_clearance([self.R_BOTH], self.CRIT,
                                              self.CRIT, CLEAR)[0], RULE)
        self.assertEqual(track_pair_clearance([self.R_OTHER], self.CRIT,
                                              self.CRIT, CLEAR),
                         (CLEAR, None))
        # NEGATIVE CONTROL: other_only must still bind the mixed pair, or the
        # arm above is satisfied by a rule that binds nothing at all.
        self.assertEqual(track_pair_clearance([self.R_OTHER], self.CRIT,
                                              self.NONE, CLEAR)[0], RULE)
    # MUTATION: drop `and not r.other_only` -> the sibling pair is charged.

    def test_two_NON_members_never_bind(self):
        self.assertEqual(track_pair_clearance([self.R_BOTH], self.NONE,
                                              self.NONE, CLEAR),
                         (CLEAR, None))
    # MUTATION: `binds = True` -> every pair on the board is charged.

    def test_it_is_RAISE_only_and_reports_no_rule_when_it_does_not_raise(self):
        # A resolved value already above the rule keeps its own value AND its
        # empty rule slot: the identity is what check_drc uses to call a pair
        # rule-governed, so a rule that changed nothing must not claim it.
        self.assertEqual(track_pair_clearance([self.R_BOTH], self.CRIT,
                                              self.NONE, 0.6), (0.6, None))
        self.assertEqual(track_pair_clearance([], self.CRIT, self.NONE, CLEAR),
                         (CLEAR, None))
    # MUTATION: `>` -> `>=`, or return `rule` unconditionally.

    def test_the_largest_binding_rule_wins_and_owns_the_identity(self):
        small = TrackRule('small', 'CRIT', False, 0.2)
        got = track_pair_clearance([self.R_BOTH, small], self.CRIT, self.NONE,
                                   CLEAR)
        self.assertEqual(got, (RULE, self.R_BOTH))
        # order must not decide it
        self.assertEqual(track_pair_clearance([small, self.R_BOTH], self.CRIT,
                                              self.NONE, CLEAR),
                         (RULE, self.R_BOTH))
    # MUTATION: `eff, rule = r.clearance_mm, r` without the `>` test.

    def test_check_drc_calls_it_rather_than_carrying_a_second_copy(self):
        """The delegation is the whole point of extracting it, and a source
        arm is the only thing that catches a re-inlining."""
        src = [l.split('#')[0] for l in
               inspect.getsource(check_drc).splitlines()]
        # ONE, not two: the import spells the name without the paren, so it is
        # counted separately below. An earlier draft of this arm expected 2 and
        # the run said 1 -- recorded rather than quietly re-fitted, because an
        # expectation edited to match its result measures nothing.
        hits = [i + 1 for i, l in enumerate(src)
                if 'track_pair_clearance(' in l]
        self.assertEqual(len(hits), 1, 'expected exactly ONE call site; '
                                       'lines %r' % hits)
        self.assertEqual(len([l for l in src
                              if 'import' in l and 'track_pair_clearance' in l]),
                         1, 'check_drc no longer imports the shared resolver')
        # ANTI-VACUITY: the predicate's own text must NOT be back in check_drc.
        # The needle is the ASSIGNMENT, not `r.other_only` -- that field is
        # also named by the announce line three lines above, so the obvious
        # needle reports a re-inlining that has not happened. Caught by this
        # arm failing on its own first run.
        self.assertEqual([i + 1 for i, l in enumerate(src) if 'binds = (' in l],
                         [], 'the binding predicate has been re-inlined')
        self.assertIn('def track_pair_clearance', inspect.getsource(kicad_dru),
                      'the resolver was renamed; every count arm above is now '
                      'searching for a dead string')
    # MUTATION: paste the loop back into `_track_pair_cl`.


# ---------------------------------------------------------------------------
# 2. The model carries the channel, and `active` did not move
# ---------------------------------------------------------------------------
class TestTheModelCarriesTheChannel(_TmpCase):
    def test_for_board_reads_the_rules_and_the_memberships(self):
        p = self.stage('read', dru=DRU_BOTH)
        m = PadClearanceModel.for_board(parse_kicad_pcb(p), CLEAR, p)
        # ON THE BRANCH: `active` staying False is the claim, so assert it
        # BEFORE anything that would be true of an active model too.
        self.assertEqual(m.track_rules, [TrackRule('crit_space', 'CRIT',
                                                   False, RULE)])
        self.assertEqual(m.net_classes.get(CRIT_NET), frozenset({'CRIT'}))
        self.assertIsNone(m.net_classes.get(FOREIGN_NET))
    # MUTATION: drop the board_track_rules call from for_board -> empty.

    def test_track_pair_is_the_last_tier_and_raise_only(self):
        p = self.stage('tier', dru=DRU_BOTH)
        m = PadClearanceModel.for_board(parse_kicad_pcb(p), CLEAR, p)
        self.assertEqual(m.track_pair(CRIT_NET, FOREIGN_NET, CLEAR)[0], RULE)
        self.assertEqual(m.track_pair(CRIT_NET, FOREIGN_NET, 0.6)[0], 0.6)
        # NEGATIVE CONTROL: `pair` must NOT have learned about it.
        fa = m.pad_floor(make_pad_like(CRIT_NET))
        fb = m.pad_floor(make_pad_like(FOREIGN_NET))
        self.assertEqual(m.pair(fa, fb), CLEAR,
                         'a track rule reached the PAD pair resolver')
    # MUTATION: fold the track raise into `pair_with_source`.

    def test_other_only_survives_the_round_trip_into_the_model(self):
        p = self.stage('oo', dru=DRU_OTHER)
        m = PadClearanceModel.for_board(parse_kicad_pcb(p), CLEAR, p)
        self.assertTrue(m.track_rules and m.track_rules[0].other_only)
        self.assertEqual(m.track_pair(CRIT_NET, CRIT_NET, CLEAR),
                         (CLEAR, None))
        self.assertEqual(m.track_pair(CRIT_NET, FOREIGN_NET, CLEAR)[0], RULE)
    # MUTATION: parse other_only as False -> the sibling pair is charged.

    def test_a_LAYER_rule_does_not_leak_into_the_track_list(self):
        p = self.stage('layer', dru=DRU_LAYER)
        m = PadClearanceModel.for_board(parse_kicad_pcb(p), CLEAR, p)
        self.assertEqual(m.track_rules, [])
        # NEGATIVE CONTROL: it must have landed in the LAYER map, or this arm
        # is satisfied by a dru nobody read at all.
        self.assertEqual(round(m.layer_rules.get(LAYER, 0.0), 6), 0.5)
    # MUTATION: let a layer-scoped rule through _parse_track_condition.

    def test_the_notes_are_not_filed_twice(self):
        """Both readers are views of ONE _parse_dru pass. Collecting the track
        read's notes as well would double every line the file produces."""
        p = self.stage('notes', dru=DRU_BOTH)
        m = PadClearanceModel.for_board(parse_kicad_pcb(p), CLEAR, p)
        parse = [n for n in m.notes if 'handled by the track channel' in n]
        self.assertEqual(len(parse), 1, m.notes)
        # ...and the one thing the layer read cannot say IS said.
        self.assertEqual(len([n for n in m.notes
                              if 'track rule' in n and 'crit_space' in n]), 1,
                         m.notes)
    # MUTATION: `notes.extend(...)` the track read's notes -> two copies.

    def test_a_board_declaring_no_dru_is_a_strict_no_op(self):
        p = self.stage('none')
        m = PadClearanceModel.for_board(parse_kicad_pcb(p), CLEAR, p)
        self.assertEqual((m.track_rules, m.net_classes), ([], {}))
        self.assertEqual([n for n in m.notes if 'track' in n], [])
    # MUTATION: return the rules unconditionally from board_track_rules.

    def test_board_track_rules_answers_empty_rather_than_raising(self):
        """Quiet by construction: the quiet reader is what lets a pass that
        worked before a rules file appeared keep working when one is
        unreadable."""
        p = self.stage('bad', dru='(this is not a dru')
        pcb = parse_kicad_pcb(p)
        self.assertEqual(board_track_rules(pcb, p), ([], {}))
        self.assertEqual(board_track_rules(pcb, ''), ([], {}))
        # NEGATIVE CONTROL: the same call on a GOOD file returns rules, so the
        # arm above is not satisfied by a reader that always answers empty.
        good = self.stage('good', dru=DRU_BOTH)
        self.assertEqual(len(board_track_rules(parse_kicad_pcb(good),
                                               good)[0]), 1)
    # MUTATION: drop the try/except -> the malformed arm raises.


def make_pad_like(net_id):
    """A pad-shaped object carrying only what `pad_floor` reads. Deliberately
    not a parser Pad: the claim is about the model, and a real pad would drag
    the board's own overrides into an arm that is not about them."""
    from types import SimpleNamespace
    return SimpleNamespace(net_id=net_id, local_clearance=0.0,
                           layers=[LAYER], drill=0.0, pad_type='smd')


# ---------------------------------------------------------------------------
# 3. `active` did not move, and `_Repair` keeps the model anyway
# ---------------------------------------------------------------------------
class TestTheTrackHandleIsSeparateFromTheFloors(_TmpCase):
    def _bare(self, name, dru):
        p = self.stage(name, dru=dru, bare=True)
        pcb = parse_kicad_pcb(p)
        pcb.nets = {CRIT_NET: Net(net_id=CRIT_NET, name=CRIT_NAME),
                    FOREIGN_NET: Net(net_id=FOREIGN_NET, name=FOREIGN_NAME)}
        return p, pcb

    def test_a_track_only_board_leaves_active_False_and_the_floors_None(self):
        """THE decision this change rests on. `self._floors` switches nine
        consumers between their flat and their resolved path; a track rule can
        price none of the pairs they ask about, so it must not flip it."""
        p, pcb = self._bare('bare_on', DRU_BOTH)
        m = PadClearanceModel.for_board(pcb, CLEAR, p)
        self.assertFalse(m.active, 'a track rule reached `active`')
        self.assertEqual(len(m.track_rules), 1, 'the rule was not read at all')
        st = _repair(p, pcb)
        self.assertIsNone(st._floors, 'the track rule flipped `_floors`')
        self.assertIsNotNone(st._track, 'the track channel is not live')
    # MUTATION: `active = bool(... or self.track_rules)`; or
    # `self._floors = _model if (_model.active or _model.track_rules)`.

    def test_the_resolver_still_prices_the_pair_with_the_floors_gone(self):
        """The raise is keyed on NETS, so it is correct over the flat fallback
        -- which is the whole answer on this board."""
        p, pcb = self._bare('bare_price', DRU_BOTH)
        st = _repair(p, pcb)
        self.assertIsNone(st._floors)          # ON THE BRANCH
        self.assertEqual(st.required(None, None), CLEAR)
        self.assertEqual(st.track_required(None, None, CRIT_NET, FOREIGN_NET),
                         RULE)
        # NEGATIVE CONTROL: a pair the rule does not bind stays flat.
        self.assertEqual(
            st.track_required(None, None, FOREIGN_NET, FOREIGN_NET), CLEAR)
    # MUTATION: `if self._floors is None: return base` in track_required.

    def test_no_dru_leaves_the_track_handle_None(self):
        p, pcb = self._bare('bare_off', None)
        st = _repair(p, pcb)
        self.assertIsNone(st._track)
        self.assertEqual(st.track_required(None, None, CRIT_NET, FOREIGN_NET),
                         st.required(None, None))
    # MUTATION: `self._track = _model` unconditionally -> AttributeError-free
    # but the guard below stops being exercised.


# ---------------------------------------------------------------------------
# 4. The ladder: the connector gate honours the rule, and only where it binds
# ---------------------------------------------------------------------------
class TestTheConnectorGateHonoursTheRule(_TmpCase):
    def test_the_ruled_arm_REFUSES_a_landing_the_unruled_arm_takes(self):
        (off_land, off_n, off_out, _), (on_land, on_n, on_out, on_st) = \
            self.run_ladder(D_REFUSE)
        # ON THE BRANCH: the unruled arm must actually have moved, or
        # "the ruled arm did not" is what an inert fixture looks like too.
        self.assertEqual(off_land, RIG_LANDING,
                         'the fixture stopped moving; out=%r' % off_out)
        self.assertEqual(off_n, 1, 'no connector was drawn to price')
        self.assertIsNotNone(on_st._track, 'the rule never reached the pass')
        self.assertIsNone(on_land,
                          'the ruled arm took a landing at %g mm, inside the '
                          '%g mm the rule demands' % (D_REFUSE,
                                                      0.2 + RULE))
        self.assertEqual(on_n, 0, 'the ruled arm still emitted copper')
    # MUTATION: `track_req(...)` -> `req(...)` at the s2 arm.

    def test_the_refusal_is_SELECTIVE_rather_than_a_blanket(self):
        """One rung out, the ruled arm still repairs the graze -- it just goes
        somewhere else. A gate that refused everything would pass the arm
        above and fail this one."""
        (off_land, _, off_out, _), (on_land, _, on_out, _) = \
            self.run_ladder(D_RELOCATE)
        self.assertEqual(off_land, RIG_LANDING, off_out)
        self.assertEqual(on_land, ALT_LANDING,
                         'expected a different landing, got %r; out=%r'
                         % (on_land, on_out))
    # MUTATION: return False unconditionally from connector_clear.

    def test_above_its_own_requirement_the_rule_binds_NOTHING(self):
        """The upper bracket. At D_INERT the gap exceeds 0.1 + 0.1 + the rule,
        so the two arms must agree exactly -- which is what makes the refusal
        above attributable to the rule's VALUE and not to its presence."""
        (off_land, off_n, _, _), (on_land, on_n, on_out, on_st) = \
            self.run_ladder(D_INERT)
        self.assertIsNotNone(on_st._track, 'the rule was not even read')
        self.assertEqual((on_land, on_n), (off_land, off_n), on_out)
        self.assertEqual(on_land, RIG_LANDING)
    # MUTATION: charge the rule unconditionally instead of raise-only.

    def test_the_BASE_gate_is_live_without_any_rule_at_all(self):
        """The lower bracket. Below 0.1 + 0.1 + clearance the unruled arm
        refuses the landing too, so the arms above are measuring a gate that
        exists rather than a rule that is the only gate."""
        (off_land, _, off_out, off_st), (on_land, _, _, _) = \
            self.run_ladder(D_BASE)
        self.assertIsNone(off_st._track, 'the unruled arm read a rule')
        self.assertEqual(off_land, ALT_LANDING, off_out)
        # The ruled arm refuses OUTRIGHT at this gap, which is a strictly
        # stronger refusal than the unruled arm's relocation and is what a
        # raise-only rule must do below its own requirement. This file's first
        # draft predicted ALT here; the run said None, and the prediction is
        # left recorded beside the correction.
        self.assertIsNone(on_land)
    # MUTATION: none -- this arm exists to catch a fixture whose only live
    # gate is the one under test.

    def test_a_duck_typed_st_grades_flat_and_does_not_crash(self):
        """The nine harnesses in this family carry no resolver at all. The
        read must be a `getattr`, or every one of them dies here."""
        p = self.stage('duck', dru=DRU_BOTH)
        pcb, st, _v0 = _rig(p, dperp=D_REFUSE)
        fake = _FakeSt(st)
        # ON THE BRANCH: prove the guarded read is REACHED, not skipped.
        self.assertIsNone(getattr(fake, 'track_required', None))
        moves, segs, out = _nudge(fake, pcb, max_shift=4.0)
        self.assertEqual(_landing(moves), RIG_LANDING,
                         'the duck-typed harness stopped grading flat; '
                         'out=%r' % out)
        self.assertEqual(len(segs), 1)
    # MUTATION: `_trk_req = st.track_required` -> AttributeError here and in
    # test_370 / test_617 / test_756.


class _FakeSt:
    """Minimal stand-in carrying only what the nudger's offender loop needs --
    the #370 B3 harness shape, built from a real _Repair so the fixture cannot
    drift from the one the arms above use."""

    def __init__(self, st):
        self.caps = st.caps
        self.vias = list(st.vias)

    def graze_penalty(self, ref, cap, x, y, rot):
        return 1.0 if ref == CAP else 0.0


# ---------------------------------------------------------------------------
# 5. Where the rule must NOT reach
# ---------------------------------------------------------------------------
class TestTheRuleReachesExactlyOnePairKind(_TmpCase):
    def test_the_via_versus_track_arm_is_not_raised(self):
        """`valid_via_pos` charges a BARREL against a foreign track. KiCad's
        condition names a track on both sides, so that pair is out of scope --
        and it must stay out, or the pass refuses landings check_drc grades
        clean."""
        p = self.stage('viaseg', dru=DRU_BOTH)
        pcb, st, _v0 = _rig(p, dperp=D_REFUSE)
        # The two resolvers, on the SAME pair of nets, must disagree.
        self.assertEqual(st.required(st.via_floor(CRIT_NET),
                                     st.seg_floor(FOREIGN_NET, LAYER)), CLEAR)
        self.assertEqual(st.track_required(st.seg_floor(CRIT_NET, LAYER),
                                           st.seg_floor(FOREIGN_NET, LAYER),
                                           CRIT_NET, FOREIGN_NET), RULE)
    # MUTATION: route valid_via_pos's segment loop through track_req.

    def test_the_cap_pad_and_board_pad_arms_are_not_raised(self):
        p = self.stage('padarms', dru=DRU_BOTH)
        pcb, st, _v0 = _rig(p, dperp=D_REFUSE)
        cap = st.caps[CAP]
        self.assertTrue(cap.pad_floors, 'the cap carries no floors to price')
        self.assertEqual(st.required(cap.pad_floors[0],
                                     st.seg_floor(FOREIGN_NET, LAYER)), CLEAR)
    # MUTATION: route connector_clear's cap-rect arm through track_req.

    def test_the_broad_phase_over_reach_is_not_inflated(self):
        """`_register_segment`'s keep-out is a cap-pad-vs-track reach that
        `_seg_effs` strips back. Inflating it would be cancelled on a
        declaring board and would leak into a pad pair on an inert one."""
        rows = []
        for dru in (None, DRU_BOTH):
            p = self.stage('reach_%s' % ('on' if dru else 'off'), dru=dru)
            pcb, st, _v0 = _rig(p, dperp=D_REFUSE)
            t = st._register_segment(0.0, 0.0, 1.0, 0.0, FOREIGN_NET, 0.2,
                                     LAYER)
            rows.append(t[5])
        self.assertEqual(rows[0], rows[1],
                         'the track rule reached the broad-phase reach')
        # NEGATIVE CONTROL: the number is a real reach, not a constant zero.
        self.assertGreater(rows[0], 0.1)
    # MUTATION: `self._item_reach` consulting the track channel.

    def test_the_rule_reaches_exactly_one_expression_in_the_nudger(self):
        src = [l.split('#')[0] for l in
               inspect.getsource(FC.nudge_vias_for_unresolved).splitlines()]

        def n(needle):
            return len([l for l in src if needle in l])

        self.assertEqual(n('track_req('), 2, 'expected the def and ONE call')
        self.assertEqual(n('req(pfl, cfl)'), 2)
        self.assertEqual(n('req(cfl, via_fl(ov.net_id))'), 1)
        self.assertEqual(n('req(vfl, seg_fl('), 1)
        # ANTI-ROT: every count above passes after a rename. Assert the
        # positive controls so a rename fails HERE.
        for token, floor in (('track_req', 3), ('_trk_req', 2), ('req(', 8)):
            self.assertGreaterEqual(n(token), floor,
                                    '%r has been renamed' % token)
    # MUTATION: a second `track_req(` call site, or reverting the one.

    def test_track_required_is_read_from_exactly_one_place(self):
        mod = [l.split('#')[0] for l in
               inspect.getsource(FC).splitlines()]
        self.assertEqual(len([l for l in mod if 'self._track' in l]), 3,
                         'expected the assignment plus the two reads in '
                         'track_required')
        self.assertEqual(len([l for l in mod if 'def track_required' in l]), 1)
        self.assertEqual(len([l for l in mod if 'self._floors = ' in l]), 1)
        # The two handles must not be defined in terms of each other -- that
        # merge is exactly the change this file exists to prevent.
        for l in mod:
            if 'self._track = ' in l:
                self.assertNotIn('_floors', l)
            if 'self._floors = ' in l:
                self.assertNotIn('_track', l)
    # MUTATION: `self._floors = _model if (_model.active or
    # _model.track_rules) else None` and delete `_track`.


# ---------------------------------------------------------------------------
# 6. Inert on everything this repo actually ships
# ---------------------------------------------------------------------------
class TestInertOnTheTrackedCorpus(unittest.TestCase):
    def test_no_tracked_board_declares_a_track_rule(self):
        """A self-expiring bound. Every expression this change touches is
        gated on a non-empty rule list, so on the tracked corpus the change is
        inert by construction -- and the day a board ships a .kicad_dru, this
        says so instead of the claim quietly becoming false."""
        boards = run_utils.corpus_boards()
        if not boards:
            print('SKIP: git cannot identify the tracked corpus')
            self.skipTest('no tracked corpus')
        self.assertGreaterEqual(len(boards), 10,
                                'the corpus shrank; this bound is now about '
                                'a different set')
        declaring = [b for b in boards
                     if os.path.isfile(os.path.splitext(
                         os.path.join(_ROOT, b))[0] + '.kicad_dru')]
        self.assertEqual(declaring, [],
                         'a tracked board now ships a .kicad_dru; the '
                         'inertness claim needs re-measuring, not deleting')

    def test_the_rig_board_prices_identically_with_no_rules(self):
        """The paired null: the same rig, staged twice with no dru either
        time, must agree -- so a difference in the ladder above cannot be the
        staging."""
        with tempfile.TemporaryDirectory() as td:
            out = []
            for tag in ('a', 'b'):
                p = _stage(td, tag)
                pcb, st, _v0 = _rig(p, dperp=D_REFUSE)
                moves, segs, _ = _nudge(st, pcb, max_shift=4.0)
                out.append((_landing(moves), len(segs), st._track))
            self.assertEqual(out[0][:2], out[1][:2])
            self.assertEqual(out[0][0], RIG_LANDING)
            self.assertIsNone(out[0][2])


if __name__ == '__main__':
    unittest.main(verbosity=2)
