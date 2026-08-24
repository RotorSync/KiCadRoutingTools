"""#737: the relocated via's COPPER must be tested against an NPTH hole.

`nudge_vias_for_unresolved` has two sibling validators, written to mirror each
other. `connector_clear` tests the connector TRACK's copper against copper-less
drill holes; `valid_via_pos` had NO equivalent for the via it relocates. Its
only drilled-pad gate is drill-to-drill at the JLC `H2H_PAD` floor, which
measures the DRILL -- so it covered the annular ring only while

    ring = (size - drill) / 2  <=  H2H_PAD - npth_clr

That bound is NOT the constant 0.25 the issue quotes: `npth_clr` is
`max(clearance, NPTH_TO_TRACK_CLEARANCE)`, so it is 0.25 only at clearance
<= 0.20, **0.20 at the shipped `--clearance` 0.25**, and 0 at clearance >= 0.45.
Past it the pass parks ring copper inside a mounting hole's keep-out -- and it
WRITES that via (placement/writer.py on the CLI, the plugin's own pcbnew mirror
in the GUI).

Measured at HEAD before the fix, on the rig below (`--clearance 0.1`, a 1.4/0.3
via boxed so its only escape is +x): the via relocates to (3.4500, 3.0000),
0.0500mm of copper-to-hole-WALL gap where 0.2000 is required, and `check_drc`
grades that board with its own via arm of the same rule:

    VIA-HOLE violations (1):
      Hole:net_0 (MH1.H1) <-> Via:net_1 (copper-to-hole)
        Overlap: 0.050mm    Hole: (4.70,3.00)    Via: (3.45,3.00)

After the fix no candidate validates, `via-nudge: no clear spot` prints, and the
via stays put. A via with real room still relocates.

WHY THE FLOOR IS `npth_clr` AND NOT A BARE `clearance`, decided by measurement
rather than by argument. Both candidates were run over all 27 tracked boards at
`--clearance` 0.25 AND 0.1, comparing placements with coordinates, the
unresolved list, `via_moves`, `new_segments` and stdout: **0 boards differ, on
either arm, at either clearance.** Both are inert everywhere this repo can
measure, so the conservative one ships. Two facts make that cheap:

  * At every shipped default they are the SAME NUMBER. `routing_defaults`
    CLEARANCE is 0.25 and `npth_clr` is `max(clearance, 0.20)`, so on the CLI,
    GUI and animator path `npth_clr == clearance` exactly and this gate IS
    check_drc's `via-hole` arm. The choice bites only below clearance 0.20.
  * `npth_clr >= clearance` always, so the pass can never emit a via its own
    checker then flags -- a one-way invariant a bare `clearance` gives only as
    equality.

NOT A REVERSAL OF #617, and `TestTheFloorIsTheFlatFabFloor` pins that
behaviourally. #617 measured that RAISING this pass's hole floor to a board's
DECLARED `min_hole_clearance` turns 1 move / 1 connector into 0 / 0; #737 ADDS a
missing gate at the flat fab floor and leaves the declared value unread.

DELIBERATELY NOT CLOSED HERE: neither hole gate honours the hole pad's own
`local_clearance`, which check_drc does honour -- that is #730, a wrong VALUE
where this is a missing GATE.

Conventions this file follows (#697/#725/#731/#732/#733 and CLAUDE.md):

  * Every assertion names the single-line MUTATION that must kill it.
  * Assert you are ON THE BRANCH before asserting about it.
  * Every "is refused" is paired with a NEGATIVE CONTROL that still moves.
  * No fixture sits within 0.05mm of the quantity under test. The ONE
    sub-0.05 boundary in the rig is the CAP gate (0.025 either side of
    nx = 3.425); that is the sweep's own 0.05mm radial quantum, it is stated
    here rather than left for a reviewer to find, and it is not the quantity
    any assertion below measures.

A 9-mutation run over the engine gate kills 8. The survivor is named rather
than hidden: `<` -> `<=`. It, and dropping the `- 1e-4`, were both measured to
leave the ENTIRE suite green on behaviour alone -- they decide an exact tie, and
the 0.05mm spiral produces no candidate that sits on one. The 1e-4 is therefore
held by the SOURCE guard in `TestTheFloorIsTheFlatFabFloor` (the two sibling
gates must read as one expression, which is a source property); `<=` is held by
nothing, because any fixture that could catch it would have to sit on the
threshold this file's own convention forbids.

Runs in-process in a couple of seconds; the corpus class shells out once for
`git ls-files`.

    python3 tests/test_737_fanout_clearance_via_hole.py
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
import subprocess
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

import routing_defaults as defaults
from bga_fanout.constants import DEFAULT_VIA_SIZE
from check_drc import _pad_has_no_copper
from kicad_parser import BoardInfo, parse_kicad_pcb
from single_ended_routing import _seg_foreign_hole_dist
from synth import make_pad, make_pcb, make_seg, make_via
from placement import fanout_clearance as FC
from placement.fanout_clearance import nudge_vias_for_unresolved

# --- the rig ---------------------------------------------------------------
CLEAR = 0.1                    # -> npth_clr 0.20; every other requirement 0.10
NPTH_FLOOR = defaults.NPTH_TO_TRACK_CLEARANCE       # 0.20
H2H_PAD = 0.45                 # the function's own literal, mirrored here
V_SIZE, V_DRILL = 1.4, 0.3     # vr 0.70, ring 0.55 -- past the masking bound
VR = V_SIZE / 2.0
H_DRILL = 1.0                  # hr 0.50
HR = H_DRILL / 2.0
MAX_SHIFT = 0.55
VIA0 = (3.0, 3.0)              # the offending via, net 3
LANDING = (3.45, 3.0)          # the ONLY landing the walls admit

# Foreign-net (2) walls that box the search to +x, so the landing is
# deterministic and one-dimensional: T/B admit only |dy| <= 0.10, and L needs
# nx >= 3.425, so the first admissible ring is r = 0.45 -> (3.45, 3.00).
WALL_L = (0.0, 0.0, 2.625, 8.0, 2)
WALL_T = (0.0, 3.90, 8.0, 8.0, 2)
WALL_B = (0.0, 0.0, 8.0, 2.10, 2)
WALLS = [WALL_L, WALL_T, WALL_B]

# Hole x positions. Distance from the landing is XH - 3.45; the gate measures
# to the hole WALL, so the requirement is `floor + VR` on `XH - 3.45 - HR`.
XH_REFUSED_BY_BOTH = 4.70      # wall gap 0.050 -- refused at either floor
XH_SEPARATES_THE_FLOORS = 4.80  # wall gap 0.150 -- refused at 0.20, not at 0.10
XH_MOVES = 4.90                # wall gap 0.250 -- moves at 0.20, not at 0.40


class _FakeCap:
    """Minimal stand-in for _Cap: fixed pad rects, never moves."""

    def __init__(self, rects):
        self._rects = list(rects)
        self.x = self.y = self.rot = 0.0

    def pad_rects(self, x=None, y=None, rot=None):
        return self._rects


class _FakeSt:
    """The duck-typed `st` the #370/#617 harnesses pass: no resolvers at all,
    so every requirement in the nudger falls back to the flat scalar."""

    def __init__(self, rects):
        self.caps = {'C1': _FakeCap(rects)}
        self.vias = []

    def graze_penalty(self, ref, cap, x, y, rot):
        return 1.0          # permanently unresolved, so the offender loop RUNS


def _board(xh, *, hole_net=0, hole_ref='H1REF', source_path=None,
           v_size=V_SIZE, v_drill=V_DRILL, h_drill=H_DRILL):
    bi = BoardInfo(layers={}, copper_layers=['F.Cu', 'B.Cu'],
                   board_bounds=(0.0, 0.0, 8.0, 8.0))
    v = make_via(VIA0[0], VIA0[1], net_id=3, size=v_size, drill=v_drill)
    stub = make_seg(2.0, 3.0, VIA0[0], VIA0[1], width=0.2, layer='F.Cu',
                    net_id=3)
    hole = make_pad(net_id=hole_net, x=xh, y=3.0, ref=hole_ref, num='H1',
                    size_x=h_drill, size_y=h_drill, shape='circle',
                    layers=['F.Mask', 'B.Mask'], drill=h_drill,
                    pad_type='np_thru_hole')
    kw = {} if source_path is None else {'source_path': source_path}
    pcb = make_pcb(board_info=bi, vias=[v], segments=[stub],
                   footprints={'C1': SimpleNamespace(layer='F.Cu', pads=[])},
                   pads_by_net={hole_net: [hole]}, zones=[], **kw)
    return v, pcb


def _nudge(st, pcb, clear=CLEAR, **kw):
    """Drive the real pass, capturing what it printed. The PRINTED OUTPUT is
    half the evidence: a refusal that prints nothing is indistinguishable from
    a pass that never looked (the #732 silent-failure lesson)."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        moves, segs = nudge_vias_for_unresolved(st, pcb, clear, **kw)
    return moves, segs, buf.getvalue()


def _rig(xh, **kw):
    v, pcb = _board(xh, **kw)
    moves, segs, out = _nudge(_FakeSt(WALLS), pcb, max_shift=MAX_SHIFT)
    return v, pcb, moves, segs, out


def _wall_gap(xh, at=LANDING, hr=HR):
    """The copper-edge-to-hole-WALL gap the new gate actually measures."""
    return math.hypot(at[0] - xh, at[1] - 3.0) - hr - VR


class TestARelocatedViaHonoursTheHoleFloor(unittest.TestCase):
    """The headline, both signs."""

    def test_a_via_whose_RING_reaches_into_a_mounting_hole_is_refused(self):
        # ON THE BRANCH 1: the ring must be past the bound the DRILL test
        # masks, or this rig is a statement about some other geometry.
        self.assertGreater((V_SIZE - V_DRILL) / 2.0, H2H_PAD - NPTH_FLOOR,
                           'the via ring does not exceed H2H_PAD - npth_clr, '
                           'so the drill test would reject this landing on '
                           'its own and the new gate is not what is measured')
        # ON THE BRANCH 2: the landing is DRILL-test-legal, so the refusal
        # below is attributable to the new gate and to nothing else.
        d_axis = abs(LANDING[0] - XH_REFUSED_BY_BOTH)
        self.assertGreaterEqual(d_axis, V_DRILL / 2.0 + HR + H2H_PAD + 0.05,
                                'the landing is inside the drill-to-drill '
                                'keep-out, so the drill test refuses it too')
        self.assertLess(_wall_gap(XH_REFUSED_BY_BOTH), NPTH_FLOOR - 0.05,
                        'the landing is not inside the copper-to-hole band')
        v, _pcb, moves, segs, out = _rig(XH_REFUSED_BY_BOTH)
        # The PRINT first: assert it before the counts and no mutation can
        # make the silence be the reported failure.
        self.assertIn('no clear spot', out,
                      'the pass said nothing about a via it declined to move')
        self.assertEqual(moves, [], 'the via was relocated into the hole band')
        self.assertEqual(segs, [], 'connector copper was drawn for no move')
        self.assertEqual((v.x, v.y), VIA0, 'the via was mutated in place')
    # MUTATION: delete the `_seg_foreign_hole_dist` gate in valid_via_pos --
    # the via is parked at (3.4500, 3.0000) with 0.0500mm of copper-to-hole
    # gap and stdout says "moved" (check_drc: VIA-HOLE, overlap 0.050mm).

    def test_a_via_with_real_room_still_moves(self):
        """The over-rejection guard, and the negative control for every
        refusal in this file: the same rig with the hole 0.20mm further off."""
        self.assertGreaterEqual(_wall_gap(XH_MOVES), NPTH_FLOOR + 0.04,
                                'the control landing is not clear of the gate')
        v, _pcb, moves, segs, out = _rig(XH_MOVES)
        self.assertEqual(len(moves), 1,
                         'a via with 0.25mm of copper-to-hole gap was refused '
                         'at a 0.20mm requirement -- the gate over-rejects')
        self.assertAlmostEqual(v.x, LANDING[0], places=4)
        self.assertAlmostEqual(v.y, LANDING[1], places=4)
        self.assertEqual([(s['layer'], s['width']) for s in segs],
                         [('F.Cu', 0.2)])
        self.assertIn('moved', out)
    # MUTATION: `npth_clr + vr` -> `npth_clr + 2 * vr` -- the gate
    # over-rejects and this move disappears.

    def test_the_gate_prices_the_BARREL_not_the_DRILL(self):
        """What separates #737's gate from the drill test beside it."""
        v, _pcb, moves, _segs, _out = _rig(XH_REFUSED_BY_BOTH)
        self.assertGreater(VR, (v.drill or 0.3) / 2.0 + 0.05,
                           'barrel and drill radius are too close for this '
                           'rig to tell the two conventions apart')
        # A drill-priced gate would need only 0.15 + 0.50 + 0.20 = 0.85 of
        # centre distance; the landing has 1.25, so it would accept.
        self.assertGreater(abs(LANDING[0] - XH_REFUSED_BY_BOTH),
                           V_DRILL / 2.0 + HR + NPTH_FLOOR + 0.05,
                           'a drill-priced gate would refuse this landing too')
        self.assertEqual(moves, [])
    # MUTATION: `npth_clr + vr` -> `npth_clr + (v.drill or 0.3) / 2.0` -- the
    # requirement falls to 0.85 of centre distance and the via moves.


class TestTheFloorIsTheFlatFabFloor(unittest.TestCase):
    """Brackets the floor from BOTH sides: above a bare `clearance`, and not
    raised by a board's declared `min_hole_clearance` (the #617 pin)."""

    def test_the_floor_is_not_the_bare_clearance(self):
        # ON THE BRANCH: above clearance 0.20 the two candidates coincide and
        # this arm asserts nothing.
        self.assertLess(CLEAR, NPTH_FLOOR,
                        'at this clearance npth_clr == clearance, so this '
                        'test cannot separate the two conventions')
        gap = _wall_gap(XH_SEPARATES_THE_FLOORS)
        self.assertGreater(gap, CLEAR + 0.04,
                           'the landing is inside the bare-clearance band '
                           'too, so a `clearance` floor would refuse it as '
                           'well and this arm proves nothing')
        self.assertLess(gap, NPTH_FLOOR - 0.04,
                        'the landing is outside the npth_clr band')
        _v, _pcb, moves, segs, out = _rig(XH_SEPARATES_THE_FLOORS)
        self.assertIn('no clear spot', out)
        self.assertEqual((moves, segs), ([], []))
    # MUTATION: `npth_clr + vr` -> `clearance + vr` -- the requirement drops
    # to a 0.10mm wall gap and the via moves to (3.4500, 3.0000).

    def test_a_DECLARED_min_hole_clearance_changes_nothing(self):
        """#617's balance, pinned behaviourally rather than by comment. A
        board declaring 0.40 would refuse this landing if the gate read the
        board; the flat floor accepts it, so the #130 repair is kept."""
        self.assertGreater(_wall_gap(XH_MOVES), NPTH_FLOOR + 0.04,
                           'the landing does not clear the flat floor')
        self.assertLess(_wall_gap(XH_MOVES), 0.40 - 0.04,
                        'the landing clears 0.40 as well, so a board-aware '
                        'gate would accept it and this arm is vacuous')
        with tempfile.TemporaryDirectory() as td:
            pcb_path = os.path.join(td, 'b.kicad_pcb')
            with open(pcb_path, 'w', encoding='utf-8') as f:
                f.write('(kicad_pcb (version 20240108))\n')
            with open(os.path.join(td, 'b.kicad_pro'), 'w',
                      encoding='utf-8') as f:
                json.dump({'board': {'design_settings':
                                     {'rules': {'min_hole_clearance': 0.40}}}},
                          f)
            v, pcb = _board(XH_MOVES, source_path=pcb_path)
            moves, segs, out = _nudge(_FakeSt(WALLS), pcb, max_shift=MAX_SHIFT)
        self.assertEqual(len(moves), 1,
                         'the declared 0.40 reached this gate -- #617 '
                         'measured that raising it costs the repair entirely')
        self.assertAlmostEqual(v.x, LANDING[0], places=4)
        self.assertEqual(len(segs), 1)
        self.assertIn('moved', out)
    # MUTATION: `npth_clr` -> `st.npth_floor` (or a resolve_hole_clearance
    # read) -- this arm refuses while the project-less arm above still moves.

    def test_both_hole_gates_in_the_nudger_spell_the_same_floor(self):
        """A source guard, because the two sibling validators drifting apart
        IS this issue. Reports the offending LINES, never assertIn over the
        whole source (test_732 measured a 393KB failure message)."""
        src = inspect.getsource(FC.nudge_vias_for_unresolved).splitlines()
        calls = [i for i, l in enumerate(src)
                 if '_seg_foreign_hole_dist(' in l
                 and not l.lstrip().startswith('#')]
        self.assertEqual(len(calls), 2,
                         'expected exactly two copper-to-hole gates (the via '
                         'and the connector); found %d at function-relative '
                         'line(s) %s' % (len(calls), [i + 1 for i in calls]))
        bad = [i + 1 for i in calls
               if 'npth_clr' not in ' '.join(src[i:i + 3])]
        self.assertEqual(bad, [],
                         'a hole gate does not spell npth_clr, at '
                         'function-relative line(s) %s' % bad)
        # The tolerance too. This one is pinned HERE and nowhere else, and
        # deliberately so: measured, dropping `- 1e-4` from the via gate leaves
        # the whole suite green, because it only decides an exact tie and no
        # candidate the 0.05mm spiral produces lands on one. It is kept because
        # the two sibling gates must read as one expression, which is a source
        # property, so a source guard is the honest place to hold it.
        eps = [i + 1 for i in calls
               if '1e-4' not in ' '.join(src[i:i + 3])]
        self.assertEqual(eps, [],
                         'a hole gate dropped the 1e-4 the other one carries, '
                         'at function-relative line(s) %s' % eps)
        drift = [i + 1 for i, l in enumerate(src)
                 if ('npth_floor' in l or 'resolve_hole_clearance' in l)
                 and not l.lstrip().startswith('#')]
        self.assertEqual(drift, [],
                         'the nudger reads a BOARD-AWARE hole floor at '
                         'function-relative line(s) %s -- #617 refused that'
                         % drift)
    # MUTATION: re-spell either gate's floor, drop either 1e-4, or add a third
    # gate -- the count, the `bad` list or the `eps` list changes. This arm is
    # the ONLY thing that kills the 1e-4 mutation; `<` -> `<=` is killed by
    # nothing at all, and is left declared rather than papered over, because
    # any fixture that could catch it would sit on the threshold.


class TestTheTwoSiblingValidatorsSeeTheSameHoles(unittest.TestCase):
    """Why the gate calls the shared helper instead of reusing `board_pads`."""

    @staticmethod
    def _board_pads_would_see(pcb, cap_refs):
        """`board_pads`' own filter, rebuilt here so the claim below is
        measured rather than asserted from reading the source."""
        return [p for ps in pcb.pads_by_net.values() for p in ps
                if getattr(p, 'component_ref', None) not in cap_refs]

    def test_an_NPTH_hole_on_a_MOVABLE_CAP_is_still_seen(self):
        """`board_pads` drops the pads of movable caps; the helper keeps them.
        This is the ONE arm that separates the two implementations."""
        v, pcb, moves, segs, out = _rig(XH_REFUSED_BY_BOTH, hole_ref='C1')
        # ON THE BRANCH: the pad really is invisible to a board_pads-based
        # gate, and really is visible to the helper.
        self.assertEqual(self._board_pads_would_see(pcb, {'C1'}), [],
                         'the hole pad is NOT filtered out of board_pads, so '
                         'this rig does not exercise the scope difference')
        self.assertAlmostEqual(
            _seg_foreign_hole_dist(pcb, 3, LANDING[0], LANDING[1],
                                   LANDING[0], LANDING[1]), 0.75, places=4,
            msg='the helper does not see this hole either, so the outcome '
                'below is not about the scope difference')
        self.assertIn('no clear spot', out)
        self.assertEqual((moves, segs), ([], []))
        self.assertEqual((v.x, v.y), VIA0)
    # MUTATION: reimplement the gate inline over `board_pads` (reusing its
    # `cap_` capsule) -- this arm moves while every other arm still passes.

    def test_a_hole_on_the_VIAS_OWN_NET_is_exempt(self):
        """NEGATIVE CONTROL for the net filter. A via on the hole's own net
        legitimately lands there; check_drc's via arm exempts it the same way
        (`if via.net_id == hnet: continue`)."""
        v, pcb, moves, segs, out = _rig(XH_REFUSED_BY_BOTH, hole_net=3)
        self.assertEqual(
            _seg_foreign_hole_dist(pcb, 3, LANDING[0], LANDING[1],
                                   LANDING[0], LANDING[1]), 1e9,
            'the helper still sees this hole, so the exemption under test is '
            'not the reason for the outcome below')
        self.assertEqual(len(moves), 1,
                         'an OWN-NET mounting hole blocked its own net')
        self.assertEqual(len(segs), 1)
        self.assertAlmostEqual(v.x, LANDING[0], places=4)
        self.assertIn('moved', out)
        # POSITIVE CONTROL, identical geometry on a foreign net.
        self.assertEqual(_rig(XH_REFUSED_BY_BOTH)[2], [],
                         'the foreign-net arm moves too, so this rig does not '
                         'separate own-net from foreign-net at all')
    # MUTATION: pass `0` (or drop the net argument) instead of `v.net_id` --
    # a net-tied mounting hole blocks its own net's via and this arm refuses.


class TestTheDrillTestIsUnchanged(unittest.TestCase):
    """#617's own rig, verbatim. The change detector that says the #130 repair
    is kept -- and the measurement that licenses the change."""

    NPTH = (1.0, 0.98)
    ND = 0.2
    BAR = (0.2, 0.9, 1.8, 1.1, 2)

    def _rig617(self):
        bi = BoardInfo(layers={}, copper_layers=['F.Cu', 'B.Cu'],
                       board_bounds=(0.0, 0.0, 3.0, 3.0))
        v = make_via(1.0, 1.4, net_id=3, size=0.5, drill=0.3)
        stub = make_seg(1.0, 1.6, 1.0, 1.4, width=0.2, layer='F.Cu', net_id=3)
        hole = make_pad(net_id=0, x=self.NPTH[0], y=self.NPTH[1], ref='BUS1',
                        num='H1', size_x=self.ND, size_y=self.ND,
                        shape='circle', layers=['F.Mask', 'B.Mask'],
                        drill=self.ND, pad_type='np_thru_hole')
        pcb = make_pcb(board_info=bi, vias=[v], segments=[stub],
                       footprints={'C1': SimpleNamespace(layer='F.Cu',
                                                         pads=[])},
                       pads_by_net={0: [hole]}, zones=[])
        return v, pcb

    def test_the_617_rig_relocates_to_exactly_the_same_place(self):
        v, pcb = self._rig617()
        moves, segs, out = _nudge(_FakeSt([self.BAR]), pcb)
        self.assertEqual((len(moves), len(segs)), (1, 1),
                         'the #130 pad-via repair #617 measured was traded '
                         'away by this change')
        self.assertAlmostEqual(v.x, 1.1148, places=4)
        self.assertAlmostEqual(v.y, 1.6772, places=4)
        self.assertIn('moved', out)
        gap = _seg_foreign_hole_dist(
            pcb, segs[0]['net_id'], segs[0]['start'][0], segs[0]['start'][1],
            segs[0]['end'][0], segs[0]['end'][1]) - segs[0]['width'] / 2.0
        self.assertAlmostEqual(gap, 0.22, places=4,
                               msg='#617 records this connector landing at '
                                   '0.2200mm; it moved')
    # MUTATION: any change to the gate that rejects this landing -- the counts
    # go to (0, 0) and the refusal line appears instead.

    def test_the_new_gate_is_numerically_unreachable_on_a_NORMAL_via(self):
        """Why #617 stays green, as an inequality rather than a hope: on a
        0.5/0.3 via the DRILL test is the stricter of the two, so the new gate
        can never be the binding one there."""
        drill_need = 0.3 / 2.0 + self.ND / 2.0 + H2H_PAD          # 0.70
        gate_need = 0.5 / 2.0 + self.ND / 2.0 + NPTH_FLOOR        # 0.55
        self.assertGreater(drill_need, gate_need + 0.10,
                           'the two requirements are close enough that the '
                           'new gate could become the binding one here')
        v, pcb = self._rig617()
        _nudge(_FakeSt([self.BAR]), pcb)
        d = math.hypot(v.x - self.NPTH[0], v.y - self.NPTH[1])
        self.assertGreater(d, gate_need + 0.10,
                           'the achieved landing has less than 0.10mm of '
                           'headroom on the new gate')
        self.assertAlmostEqual(d, 0.706588, places=5)
    # MUTATION: none kills these -- they are the measurement that licenses the
    # change. Their job is to fail loudly if a later edit moves the landing.


class TestInertOnTheTrackedCorpus(unittest.TestCase):
    """A self-expiring bound. The gate is UNREACHABLE on every board this repo
    tracks, which is why the demonstration above is synthetic on purpose."""

    @staticmethod
    def _tracked():
        out = subprocess.run(['git', 'ls-files', '-z', '*.kicad_pcb'],
                             cwd=_ROOT, capture_output=True, text=True)
        return [os.path.join(_ROOT, p) for p in out.stdout.split('\0')
                if p.endswith('.kicad_pcb')]

    def test_no_tracked_via_is_fat_enough_for_the_gate_to_bind(self):
        boards = self._tracked()
        self.assertGreater(len(boards), 25,
                           'git ls-files returned %d boards -- never glob the '
                           'directory, 11 boards in kicad_files/ alone are '
                           'gitignored build products' % len(boards))
        widest, where, total, with_both = 0.0, None, 0, []
        for b in boards:
            pcb = parse_kicad_pcb(b)
            vias = [v for v in pcb.vias if (v.size or 0) > 0]
            holes = [p for ps in pcb.pads_by_net.values() for p in ps
                     if (getattr(p, 'drill', 0) or 0) > 0
                     and _pad_has_no_copper(p)]
            total += len(vias)
            if not vias:
                continue
            r = max((v.size - (v.drill or 0)) / 2.0 for v in vias)
            if r > widest:
                widest, where = r, os.path.basename(b)
            if holes:
                with_both.append(os.path.basename(b))
        self.assertGreater(total, 500,
                           'only %d sized vias across the corpus -- the bound '
                           'below would be vacuous' % total)
        bound = H2H_PAD - max(defaults.CLEARANCE, NPTH_FLOOR)      # 0.20
        self.assertLess(widest, bound - 0.04,
                        'a tracked board now carries a via ring of %.4f (%s), '
                        'at or near the %.4f bound where this gate starts to '
                        'bind at the shipped --clearance %.2f. The "provably '
                        'inert on the corpus" claim in this PR must be '
                        're-measured.' % (widest, where, bound,
                                          defaults.CLEARANCE))
        # The boards carrying BOTH vias and copper-less holes are the only
        # ones where this gate could ever fire; name them, so a change there
        # is visible rather than silent.
        self.assertEqual(sorted(with_both),
                         ['orangecrab_ext_pll.kicad_pcb',
                          'rp2350_fpga_eensy_prePlane.kicad_pcb'],
                         'the set of boards carrying both vias and '
                         'copper-less holes changed; re-run the corpus A/B')
    # MUTATION: add a via with size - drill > 0.32 to any tracked board -- the
    # inertness claim stops being true and this fails with the board's name.

    def test_every_via_this_pipeline_CREATES_is_below_the_bound(self):
        """The corpus is a snapshot; these are the sizes the tool itself
        emits, and they are what keep the gate inert on boards it produces."""
        bound = H2H_PAD - max(defaults.CLEARANCE, NPTH_FLOOR)
        for name, size, drill in (
                ('BGA_VIA', defaults.BGA_VIA_SIZE, defaults.BGA_VIA_DRILL),
                ('VIA', defaults.VIA_SIZE, defaults.VIA_DRILL),
                ('DEFAULT_VIA_SIZE', DEFAULT_VIA_SIZE, defaults.VIA_DRILL)):
            with self.subTest(name):
                self.assertLess((size - drill) / 2.0, bound - 0.04,
                                '%s (%s/%s) now has a ring at the %.4f bound'
                                % (name, size, drill, bound))
    # MUTATION: raise BGA_VIA_SIZE past 0.62 -- the fanout vias this pass
    # moves start binding and the inertness claim must be restated.


if __name__ == '__main__':
    unittest.main(verbosity=2)
