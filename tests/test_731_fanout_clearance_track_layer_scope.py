"""#731: the fanout-clearance repair pass must grade a cap pad against a track
only when the two SHARE A REAL COPPER LAYER -- the same predicate check_drc
gates on -- instead of against an 'F'/'B' board SIDE that files every
non-B layer under 'F'.

The collapse was wrong in both directions. It ADDED pairs an F-side cap pad can
never touch (928 of them on orangecrab at --clearance 0.1, 0.2464mm of phantom
seed graze against 0.0000mm of real; R17/R18/R5's entire bill), and it DROPPED
the B.Cu and inner-layer tracks a THROUGH-HOLE cap pad really does share copper
with. Neither is academic: on rp2350 -- a board that is DRC-clean as it ships --
the old pass moved C19 to clear a phantom and put its GND pad 0.145mm inside a
real F.Cu track, so the step introduced a PAD-SEGMENT violation.

Conventions this file follows (from #697/#725 and CLAUDE.md):

  * REAL parser dataclasses and REAL boards. `_Repair.__init__` reads courtyards
    and locked refs from the file on disk, so even a synthetic case needs a file.
  * Every assertion names the single-line MUTATION that must kill it.
  * Assert you are ON the branch before asserting about it -- each test spies the
    value its branch keys on, with a detail string saying why.
  * A removal test needs a NEGATIVE CONTROL, or it passes just as well on a build
    that deleted the whole channel. Every "is not charged" here is paired with an
    "and this one still is".

Nothing here shells out or drives a CLI: it runs entirely in-process in ~25 s.
`run_all.is_integration` classifies by grepping the SOURCE for `run_utils` and
the name of the standard sub-process module, and this file NAMES those markers
in prose (including in this sentence), which is how the first version of the
#725 file got bucketed as integration and silently skipped under `--fast`.
Hence the explicit opt-out below.
"""
from __future__ import annotations

RUN_ALL_FAST_OK = True

import copy
import inspect
import os
import sys
import unittest

_TESTS = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_TESTS)
for _p in ('', 'py_router', 'py_placer', 'py_tools'):
    _d = os.path.join(_ROOT, _p)
    if _d not in sys.path:
        sys.path.insert(0, _d)
if _TESTS not in sys.path:
    sys.path.insert(0, _TESTS)

from kicad_parser import parse_kicad_pcb
from synth import make_seg
import check_drc
from check_drc import check_pad_segment_overlap, pad_copper_layers
from placement.legality import PadClearanceModel
from placement.fanout_clearance import (_Cap, _OFF_LAYER, _Repair,
                                        repair_fanout_clearance)

BOARD = os.path.join(_ROOT, 'kicad_files', 'orangecrab_ext_pll.kicad_pcb')
INERT = os.path.join(_ROOT, 'kicad_files',
                     'rp2350_fpga_eensy_prePlane.kicad_pcb')
CLEAR = 0.1

# R17 is an F-side 0402 near a BGA with two copper pads (net 69 / net 70) and
# two paste-only apertures that _Cap's copper filter drops. Its ENTIRE seed
# graze before #731 was phantom In1.Cu track pairs.
CAP = 'R17'
PHANTOM_CAPS = ('R17', 'R18', 'R5')
FOREIGN_NET = 2          # neither 69 nor 70
TRACK_W = 0.1            # half width 0.05, so keep-out = 0.05 + CLEAR
BITE = 0.05              # how far inside the keep-out band the track sits

# The cap prefix the CLI defaults to. NOT 'C': with 'C' alone the movable set is
# 51 caps and R17/R18/R5 -- the three this issue is about -- never appear.
PREFIX = 'C,R,FB'

# Measured on the stock boards at CLEAR, before #731 (see the PR body).
PRE_FIX_OFF_LAYER_PAIRS = 928       # orangecrab, same-net pairs included
PRE_FIX_PHANTOM_MM = 0.246393       # orangecrab seed graze, all of it phantom
PRE_FIX_INERT_OFF_PAIRS = 2342      # rp2350
BOTH_GRADED = 618                   # orangecrab pairs check_drc DOES grade


def _repair(path, pcb=None, clearance=CLEAR, prefix=PREFIX):
    """The 10-POSITIONAL construction every test in this family uses."""
    return _Repair(pcb if pcb is not None else parse_kicad_pcb(path), path,
                   clearance, 0.1, 0.55, 1.0, 2.0, 0.3, prefix, set())


def _routing_layers(pcb):
    """check_drc's own layer list for the overlap checks."""
    return [l for l in (pcb.board_info.copper_layers or [])
            if l.endswith('.Cu')]


def _as_through(pad, layers=('*.Cu', '*.Mask')):
    """Convert a copper pad in place into a plated through-hole pad."""
    pad.layers = list(layers)
    pad.pad_type = 'thru_hole'
    pad.drill = 0.3
    return pad


def _copper_pads(fp):
    """The pads _Cap keeps, in _Cap's own order (its loose endswith filter)."""
    return [p for p in fp.pads
            if any(str(l).endswith('.Cu') for l in p.layers)]


class TestTheTruthTable(unittest.TestCase):
    """One real cap, one synthetic track, every (pad kind, track layer) cell --
    graded by the pass and by check_drc, and required to agree."""

    def _case(self, layer, through=False, pad_index=0, net=FOREIGN_NET):
        """Build R17 with exactly one foreign track biting `BITE` into the
        keep-out band of `pad_index`, and return (seg_penalty, drc, st, pcb)."""
        pcb = parse_kicad_pcb(BOARD)
        fp = pcb.footprints[CAP]
        pads = _copper_pads(fp)
        if through:
            for p in pads:
                _as_through(p)
        target = pads[pad_index]
        x0, x1 = target.global_x - target.size_x / 2, target.global_x + target.size_x / 2
        y0 = target.global_y - target.size_y / 2
        # A horizontal track below the pad rect: distance = keepout - BITE.
        keepout = TRACK_W / 2 + CLEAR
        ty = y0 - keepout + BITE
        seg = make_seg(x0 - 1.0, ty, x1 + 1.0, ty,
                       layer=layer, net_id=net, width=TRACK_W)
        pcb.segments = [seg]
        pcb.vias = []
        st = _repair(BOARD, pcb=pcb)
        cap = st.caps[CAP]
        pen = st.seg_penalty(CAP, cap, cap.x, cap.y, cap.rot)
        drc = check_pad_segment_overlap(target, seg, CLEAR,
                                        _routing_layers(pcb),
                                        clearance_margin=0.0)
        return pen, drc, st, pcb

    def test_an_INNER_layer_track_is_not_charged_against_an_F_side_SMD_pad(self):
        """The whole issue, on one cap: an In1.Cu track cannot touch an F.Cu
        pad, and check_drc grades no such pair."""
        for layer in ('In1.Cu', 'In2.Cu', 'In3.Cu', 'In4.Cu'):
            pen, drc, st, _ = self._case(layer)
            # ON THE BRANCH: the cap really is F-side and really is scoped,
            # or "not charged" could be true for an unrelated reason.
            self.assertEqual(st.caps[CAP].side, 'F')
            self.assertIsNotNone(st._cap_pad_layers(st.caps[CAP]))
            self.assertFalse(drc[0], '%s: check_drc changed its mind' % layer)
            self.assertAlmostEqual(pen, 0.0, places=9,
                                   msg='%s track charged %.6f against an F.Cu '
                                       'pad' % (layer, pen))
    # MUTATION: revert the prune to `if seg[6] != cap.side: continue` -> every
    # arm charges BITE (0.05).

    def test_the_SAME_geometry_on_F_Cu_IS_still_charged(self):
        """The negative control. Without it, a build that deleted track grading
        outright would satisfy the test above perfectly."""
        pen, drc, st, _ = self._case('F.Cu')
        # ON THE BRANCH: the track must actually be in the pruned list, or
        # "charged" is being decided somewhere other than the layer gate.
        self.assertTrue(st.cap_segs[CAP],
                        'the F.Cu track was pruned away -- the layer gate is '
                        'dropping copper the cap really does share')
        self.assertTrue(drc[0])
        self.assertAlmostEqual(pen, BITE, places=9)
        self.assertAlmostEqual(pen, drc[1], places=6,
                               msg='the pass and check_drc disagree on the '
                                   'overlap of a pair they both grade')
    # MUTATION: make `_seg_shares` return False always -> this goes red while
    # the inner-layer test above stays green.

    def test_a_THROUGH_HOLE_cap_pad_KEEPS_the_B_Cu_track(self):
        """The mirror. A through-hole pad's copper spans every layer, so the
        side collapse DROPPED the B.Cu track it really does share -- an
        UNDER-block, which is the direction that ships a violation."""
        pen, drc, st, _ = self._case('B.Cu', through=True)
        cap = st.caps[CAP]
        # ON THE BRANCH: the pad must span all copper and the cap must be
        # F-side, or this is not the cross-side case at all.
        self.assertEqual(cap.side, 'F')
        self.assertEqual(len(cap.pad_layers[0]), 6,
                         'the through-hole pad did not resolve every copper '
                         'layer: %r' % (cap.pad_layers[0],))
        self.assertTrue(drc[0], 'check_drc does not grade this pair either -- '
                                'the fixture is wrong, not the engine')
        self.assertAlmostEqual(pen, BITE, places=9,
                               msg='the B.Cu track was dropped for a pad whose '
                                   'copper spans it')
    # MUTATION: keep the side test as an AND alongside the layer test
    # (`if self._seg_side(layer) != cap.side or ...`) -> the B.Cu track is
    # dropped again and this reads 0.0, while the two tests above stay green.

    def test_every_cell_agrees_with_check_pad_segment_overlap(self):
        """Parity with the authority, over the whole 2 x 4 table."""
        seen_true = seen_false = 0
        for through in (False, True):
            for layer in ('F.Cu', 'In1.Cu', 'In2.Cu', 'B.Cu'):
                pen, drc, _, _ = self._case(layer, through=through)
                cell = '%s pad vs %s' % ('THT' if through else 'SMD', layer)
                self.assertEqual(pen > 1e-9, bool(drc[0]),
                                 '%s: pass says %.6f, check_drc says %r'
                                 % (cell, pen, drc[0]))
                if drc[0]:
                    seen_true += 1
                    self.assertAlmostEqual(pen, drc[1], places=6, msg=cell)
                else:
                    seen_false += 1
        # ON THE BRANCH: the table must contain both verdicts, or agreement is
        # trivial (all-clear agrees with all-clear).
        self.assertGreater(seen_true, 0)
        self.assertGreater(seen_false, 0)
    # MUTATION: build `pad_layers` from `frozenset(p.layers)` instead of
    # `pad_copper_layers(p, board_copper)` -> a `*.Cu` pad resolves to
    # {'*.Cu'}, matches no seg.layer, and all four THT rows read 0.0.

    def test_a_MIXED_pad_cap_scopes_PER_PAD_not_by_the_UNION(self):
        """The prune keeps the UNION of a cap's pad layers (a superset, so it
        can never drop a chargeable pair); the GRADING must then apply the
        exact per-pad set. A cap with one through-hole pad beside one F.Cu SMD
        pad is the only shape where the two answers differ."""
        pcb = parse_kicad_pcb(BOARD)
        pads = _copper_pads(pcb.footprints[CAP])
        _as_through(pads[0])                      # pad 0 spans all copper
        target = pads[1]                          # pad 1 stays F.Cu only
        x0, x1 = target.global_x - target.size_x / 2, target.global_x + target.size_x / 2
        keepout = TRACK_W / 2 + CLEAR
        ty = target.global_y - target.size_y / 2 - keepout + BITE
        seg = make_seg(x0 - 1.0, ty, x1 + 1.0, ty,
                       layer='B.Cu', net_id=FOREIGN_NET, width=TRACK_W)
        pcb.segments = [seg]
        pcb.vias = []
        st = _repair(BOARD, pcb=pcb)
        cap = st.caps[CAP]
        # ON THE BRANCH: the two pads must really differ, or per-pad and union
        # are the same answer and this test cannot tell them apart.
        self.assertEqual(len(cap.pad_layers[0]), 6)
        self.assertEqual(sorted(cap.pad_layers[1]), ['F.Cu'])
        rl = _routing_layers(pcb)
        d_tht = check_pad_segment_overlap(pads[0], seg, CLEAR, rl,
                                          clearance_margin=0.0)
        d_smd = check_pad_segment_overlap(target, seg, CLEAR, rl,
                                          clearance_margin=0.0)
        # check_drc grades the THT pad and not the SMD one; the pass must bill
        # exactly the same total.
        self.assertTrue(d_tht[0])
        self.assertFalse(d_smd[0])
        pen = st.seg_penalty(CAP, cap, cap.x, cap.y, cap.rot)
        self.assertAlmostEqual(pen, d_tht[1], places=6,
                               msg='billed %.6f; the union would bill the SMD '
                                   'pad too' % pen)
        # ...and the off-layer cell really is the sentinel, not a small number.
        rows = st._seg_effs(CAP, cap)
        self.assertEqual(rows[1][0], _OFF_LAYER)
        self.assertNotEqual(rows[0][0], _OFF_LAYER)
    # MUTATION: grade with `union(cap.pad_layers)` instead of
    # `cap.pad_layers[i]` -> the SMD pad is billed too and the total rises.


class TestTheSeedPhantomIsGone(unittest.TestCase):
    """The board-level claim, on the board the issue was measured on."""

    @classmethod
    def setUpClass(cls):
        cls.pcb = parse_kicad_pcb(BOARD)
        cls.st = _repair(BOARD, pcb=cls.pcb)
        cls.rl = _routing_layers(cls.pcb)

    def test_no_pruned_pair_is_one_check_drc_would_IGNORE(self):
        """Every (cap pad, pruned track) pair must be one check_drc grades."""
        st, pcb = self.st, self.pcb
        both = ignored = 0
        for ref, cap in st.caps.items():
            pads = _copper_pads(pcb.footprints[ref])
            self.assertEqual(len(pads), len(cap.pads),
                             '%s: pad list misaligned with _Cap' % ref)
            for p in pads:
                expanded = pad_copper_layers(p, pcb.board_info.copper_layers)
                for t in st.cap_segs[ref]:
                    if t[6] in expanded:
                        both += 1
                    else:
                        ignored += 1
        self.assertEqual(ignored, 0,
                         '%d pruned pair(s) are ones check_drc does not grade '
                         '(was %d before #731)'
                         % (ignored, PRE_FIX_OFF_LAYER_PAIRS))
        # ON THE BRANCH: 0/0 would also satisfy the line above.
        self.assertEqual(both, BOTH_GRADED,
                         'the number of REAL pairs moved: %d, expected %d'
                         % (both, BOTH_GRADED))
    # MUTATION: revert the prune to the side collapse -> `ignored` returns to
    # 928. MUTATION: make `_seg_shares` return False -> `both` collapses to 0.

    def test_R17_R18_R5_have_NO_seed_graze_left_in_ANY_channel(self):
        """Their entire bill was off-layer track graze -- 0.0821mm each -- so
        they were violators, were moved, and were candidates for the via
        nudger, which moves real vias and appends real segments."""
        st = self.st
        for ref in PHANTOM_CAPS:
            cap = st.caps[ref]
            x, y, rot = cap.x, cap.y, cap.rot
            # Named per channel so the test says WHY, not just that it is 0.
            self.assertAlmostEqual(
                st.via_penalty(cap, x, y, rot, st.cap_vias[ref], ref=ref),
                0.0, places=9, msg='%s via graze' % ref)
            self.assertAlmostEqual(st.pad_penalty(ref, cap, x, y, rot),
                                   0.0, places=9, msg='%s pad graze' % ref)
            self.assertAlmostEqual(st.seg_penalty(ref, cap, x, y, rot),
                                   0.0, places=9, msg='%s track graze' % ref)
            self.assertAlmostEqual(st.graze_penalty(ref, cap, x, y, rot),
                                   0.0, places=9, msg='%s total' % ref)
    # MUTATION: revert the prune -> each reads 0.0821.

    def test_the_boards_whole_seed_track_graze_is_zero_and_was_not(self):
        """0.246393mm before #731, every micron of it off-layer: this board has
        NO on-layer seed track graze at all, which is why check_drc reports
        zero PAD-SEGMENT violations on it."""
        st = self.st
        total = sum(st.seg_penalty(r, c, c.x, c.y, c.rot)
                    for r, c in st.caps.items())
        self.assertAlmostEqual(total, 0.0, places=9,
                               msg='%.6f mm of seed track graze remains (was '
                                   '%.6f, all phantom)'
                                   % (total, PRE_FIX_PHANTOM_MM))
        # ON THE BRANCH: there must be tracks in the pruned lists at all.
        self.assertGreater(sum(len(v) for v in st.cap_segs.values()), 0)
    # MUTATION: revert the prune -> the total reads 0.246393.


class TestTheInertPath(unittest.TestCase):
    """The case a fix built on #725's side-channels would have MISSED.

    rp2350 declares no netclass, no .kicad_dru and no pad override, so
    PadClearanceModel is inert -- and before #731 both layer side-channels were
    gated on it. It is also 6 copper layers and carried 4.6685mm of phantom
    seed graze against 0.0142mm of real, the largest phantom measured anywhere.
    """

    @classmethod
    def setUpClass(cls):
        cls.pcb = parse_kicad_pcb(INERT)
        cls.st = _repair(INERT, pcb=cls.pcb)

    def test_layer_scoping_SURVIVES_a_board_that_declares_nothing(self):
        st = self.st
        # ON THE BRANCH: the model really must be inert, or this passes for the
        # wrong reason -- it would just be testing the active path again.
        self.assertIsNone(st._floors,
                          'rp2350 built a clearance model; this fixture no '
                          'longer tests the inert path')
        self.assertTrue(st._all_cu, 'no copper layers -> scoping switches off')
        for ref, cap in st.caps.items():
            self.assertTrue(cap.pad_layers,
                            '%s has no pad_layers on an inert board' % ref)
            self.assertIsNotNone(st._cap_pad_layers(cap),
                                 '%s: layer scoping switched itself off' % ref)
        off = sum(1 for ref, cap in st.caps.items()
                  for t in st.cap_segs[ref]
                  if not any(t[6] in pl for pl in cap.pad_layers))
        self.assertEqual(off, 0,
                         '%d off-layer pair(s) survived on the inert board '
                         '(was %d)' % (off, PRE_FIX_INERT_OFF_PAIRS))
    # MUTATION: move the `pad_layers` build back inside `if model is not None:`
    # -> pad_layers is [] here and every assertion above fails.
    # MUTATION: re-anchor `_cap_pad_layers` on `pad_floors` -> it returns None
    # on this board and `off` returns to 2342.

    def test_the_model_is_STILL_never_consulted_on_an_inert_board(self):
        """The layer data must come from `board_info.copper_layers`, never from
        `model.board_copper` -- sourcing it from the model would ACTIVATE the
        model on a board that declares nothing."""
        hits = [0]
        orig = PadClearanceModel.pair

        def counting(self, a, b):
            hits[0] += 1
            return orig(self, a, b)
        PadClearanceModel.pair = counting
        try:
            r = repair_fanout_clearance(parse_kicad_pcb(INERT), INERT,
                                        clearance=CLEAR)
        finally:
            PadClearanceModel.pair = orig
        self.assertEqual(hits[0], 0,
                         'the model was consulted %d times on a board that '
                         'declares nothing' % hits[0])
        # ON THE BRANCH: the run must have done something, or 0 calls is
        # trivially true.
        self.assertTrue(r['bga_refs'])
    # MUTATION: source `board_copper` from a `PadClearanceModel.for_board`
    # call instead of `self._all_cu` -> hits > 0.

    def test_an_ON_layer_inert_cell_NEVER_REACHES_THE_RESOLVER(self):
        """On the inert path the eff cell is the tuple's own over-reach, taken
        directly rather than rebuilt as `halves[j] + clearance`.

        The point is the CALL, not the rounding. Rebuilding it routes every
        inert cell through `_pair_or_flat`, which is the funnel the clearance
        model hangs off -- a board that declares nothing would start consulting
        the resolver once per pair. Spied directly, because the arithmetic
        round-trip is float-exact on every cell measured (0 of 1063 on rp2350),
        so an equality check alone cannot tell the two forms apart: a mutation
        replacing this arm left the value assertion GREEN."""
        st = self.st
        calls = []
        orig = _Repair._pair_or_flat

        def spy(self_, fa, fb):
            calls.append((fa, fb))
            return orig(self_, fa, fb)
        _Repair._pair_or_flat = spy
        try:
            st._cap_seg_eff.clear()
            rows_by_ref = {ref: st._seg_effs(ref, cap)
                           for ref, cap in st.caps.items()}
        finally:
            _Repair._pair_or_flat = orig
        self.assertEqual(calls, [],
                         'the resolver was consulted %d time(s) building the '
                         'TRACK eff matrix on an inert board' % len(calls))
        checked = 0
        for ref, cap in st.caps.items():
            rows = rows_by_ref[ref]
            self.assertIsNotNone(rows, '%s got no eff matrix' % ref)
            for i in range(len(cap.pads)):
                for j, t in enumerate(st.cap_segs[ref]):
                    self.assertNotEqual(
                        rows[i][j], _OFF_LAYER,
                        '%s: an off-layer pair survived the prune' % ref)
                    self.assertEqual(rows[i][j], t[5],
                                     '%s pad %d vs track %d: %r != %r'
                                     % (ref, i, j, rows[i][j], t[5]))
                    checked += 1
        # ON THE BRANCH: there must BE inert cells, or "0 calls" is vacuous.
        self.assertGreater(checked, 0, 'no inert pair was examined')
    # MUTATION: replace the `fa is None and floors[j] is None` arm with `False`
    # -> every inert cell falls through to `_pair_or_flat` and `calls` is no
    # longer empty.

    def test_the_inert_boards_recorded_result_is_the_NEW_one(self):
        """Six of rp2350's eight caps were violators purely on phantom. Only
        C18 (0.0106mm on-layer) and C23 (0.0036mm) have anything real; C18 is
        resolved and C23 is the one cap that legitimately remains unresolved."""
        r = repair_fanout_clearance(parse_kicad_pcb(INERT), INERT,
                                    clearance=CLEAR)
        self.assertEqual(len(r['placements']), 1)
        self.assertEqual(sorted(r['unresolved']), ['C23'])
        self.assertEqual(sorted(r['resolved']), ['C18'])
        self.assertEqual(r['required'], [], 'an inert board discloses nothing')
    # MUTATION: any revert of the prune, the unconditional pad_layers, or the
    # `_cap_pad_layers` anchor -> the 7-ref unresolved list comes back.


class TestTheOffSwitches(unittest.TestCase):
    """The three ways layer scoping must decline to act."""

    def _one_track_board(self, layer, net=FOREIGN_NET):
        pcb = parse_kicad_pcb(BOARD)
        pads = _copper_pads(pcb.footprints[CAP])
        target = pads[0]
        x0, x1 = target.global_x - target.size_x / 2, target.global_x + target.size_x / 2
        keepout = TRACK_W / 2 + CLEAR
        ty = target.global_y - target.size_y / 2 - keepout + BITE
        pcb.segments = [make_seg(x0 - 1.0, ty, x1 + 1.0, ty, layer=layer,
                                 net_id=net, width=TRACK_W)]
        pcb.vias = []
        return pcb

    def test_an_INJECTED_tuple_with_an_UNRECOGNISED_layer_is_still_graded(self):
        """A tuple a test injects carries whatever it likes in the layer slot --
        `tests/test_fanout_clearance.py` injects the cap's own 'F'/'B' side.
        An UNKNOWN layer must be CHARGED: over-blocking is the only direction
        that can never ship a violation, and this is the arm that replaced
        #725's `sl is None` fallback."""
        pcb = self._one_track_board('F.Cu')
        st = _repair(BOARD, pcb=pcb)
        cap = st.caps[CAP]
        real = st.cap_segs[CAP][0]
        for slot in ('F', 'B', None, 'Nonsense.Cu'):
            injected = real[:6] + (slot,)
            st.cap_segs[CAP] = [injected]
            st._cap_seg_eff.pop(CAP, None)
            # ON THE BRANCH: the slot really is not a board copper layer.
            self.assertNotIn(slot, st._all_cu)
            pen = st.seg_penalty(CAP, cap, cap.x, cap.y, cap.rot)
            self.assertGreater(pen, 1e-6,
                               'an injected tuple with layer slot %r was '
                               'silently dropped' % (slot,))
    # MUTATION: drop the `layer not in self._all_cu` arm of `_seg_shares` ->
    # every injected tuple grades 0 and
    # test_fanout_clearance::test_seed_track_graze_is_resolved goes red too.

    def test_an_INJECTED_OFF_LAYER_tuple_is_NOT_charged(self):
        """The GRADING skip, tested independently of the prune. With the prune
        working, no real board can put an off-layer pair in front of
        `_seg_shortfalls` -- so without this test a mutation that deletes the
        per-pad gate is caught by nothing."""
        pcb = self._one_track_board('F.Cu')
        st = _repair(BOARD, pcb=pcb)
        cap = st.caps[CAP]
        real = st.cap_segs[CAP][0]
        # ON THE BRANCH: the same tuple on its real layer IS charged.
        self.assertGreater(st.seg_penalty(CAP, cap, cap.x, cap.y, cap.rot), 1e-6)
        st.cap_segs[CAP] = [real[:6] + ('In1.Cu',)]
        st._cap_seg_eff.pop(CAP, None)
        self.assertIn('In1.Cu', st._all_cu)
        self.assertNotIn('In1.Cu', cap.pad_layers[0])
        pen = st.seg_penalty(CAP, cap, cap.x, cap.y, cap.rot)
        self.assertAlmostEqual(pen, 0.0, places=9,
                               msg='an off-layer tuple was charged %.6f' % pen)
    # MUTATION: delete the `_seg_shares` branch in `_seg_effs` -> this reads
    # BITE (0.05) while every board-level test stays green.

    def test_a_board_with_NO_copper_layers_falls_back_to_the_SIDE_collapse(self):
        """`pad_copper_layers(p, [])` resolves nothing for a `*.Cu` pad, so
        every pad would read copper-less and every pair would take the
        off-layer path -- an UNDER-block, which ships violations. Scoping
        switches off wholesale. It must fall back to the OLD side test, NOT to
        keeping every segment on the board: check_drc still layer-scopes such a
        board (its routing_layers falls back to the segment-derived set), so
        the placer must stay on the over-block side."""
        pcb = parse_kicad_pcb(BOARD)
        pcb.board_info.copper_layers = []
        st = _repair(BOARD, pcb=pcb)
        self.assertEqual(st._all_cu, frozenset())
        cap = st.caps[CAP]
        self.assertIsNone(st._cap_pad_layers(cap),
                          'layer scoping stayed on with no copper layers')
        # ON THE BRANCH: the board must carry tracks on BOTH sides, or "did not
        # keep everything" cannot be distinguished from "kept everything".
        sides = {st._seg_side(t[6]) for t in st.segments}
        self.assertEqual(sides, {'F', 'B'})
        kept = st.cap_segs[CAP]
        self.assertTrue(kept, 'the fallback dropped every track')
        self.assertTrue(all(st._seg_side(t[6]) == cap.side for t in kept),
                        'a track from the OTHER side survived: the fallback '
                        'stopped filtering instead of using the side test')
        self.assertLess(len(kept), len(st.segments),
                        'the fallback kept every segment on the board')
    # MUTATION: make the `_union is None` arm keep every segment -> the last
    # two assertions fail.


class TestShapeAndClassification(unittest.TestCase):

    def test_the_Cap_constructor_takes_board_copper_POSITIONALLY(self):
        """`board_copper` must default to None, NOT to `()`.

        With `()` the fallback to the model's own copper list never fires, so a
        3-positional `_Cap(fp, lb, model)` -- which `test_725` uses -- resolves
        `pad_copper_layers(p, [])`. That still resolves a CONCRETE `F.Cu` pad,
        which is why a naive fixture cannot see it: only a `*.Cu` pad collapses
        to the empty set, and an empty set is the "copper-less, grade flat"
        marker. So the board would silently stop scoping its through-hole pads
        while every visible signal looked fine. Measured: with a plain
        signature check plus a 4-positional construction, that mutation
        SURVIVED the whole suite."""
        names = list(inspect.signature(_Cap.__init__).parameters)
        self.assertEqual(names[1:5],
                         ['fp', 'courtyard_local', 'model', 'board_copper'])
        self.assertIsNone(
            inspect.signature(_Cap.__init__).parameters['board_copper'].default,
            'board_copper defaults to something other than None, so the '
            'fallback to model.board_copper cannot fire')
        pcb = parse_kicad_pcb(BOARD)
        fp = copy.deepcopy(pcb.footprints[CAP])
        for p in _copper_pads(fp):
            _as_through(p)                      # now a `*.Cu` pad
        cu = pcb.board_info.copper_layers
        # (a) 4-positional, no model at all: layers still resolve.
        cap = _Cap(fp, (-0.5, -0.5, 0.5, 0.5), None, cu)
        self.assertEqual(cap.pad_floors, [])
        self.assertEqual(len(cap.pad_layers), len(cap.pads))
        self.assertEqual(len(cap.pad_layers[0]), len(cu))
        # (b) 3-positional with only a model, the shape test_725 uses: the
        # fallback to model.board_copper must carry the layers.
        model = PadClearanceModel.for_board(pcb, CLEAR, BOARD)
        cap3 = _Cap(fp, (-0.5, -0.5, 0.5, 0.5), model)
        self.assertEqual(len(cap3.pad_layers[0]), len(cu),
                         'a 3-positional _Cap resolved %r for a *.Cu pad -- '
                         'board_copper is shadowing the model fallback'
                         % (cap3.pad_layers[0],))
    # MUTATION: `board_copper=()` -> arm (b) resolves the empty set.
    # MUTATION: gate the pad_layers build on `model is not None` -> arm (a) has
    # no pad_layers. MUTATION: rename or reorder the parameter -> the name list
    # differs.

    def test_the_segment_tuple_is_still_SEVEN_wide_and_carries_a_LAYER(self):
        """#731 replaced element 6 rather than widening the tuple, because
        `_seg_shortfalls` unpacks it positionally in two places and
        test_725's TestShapeContract pins the width."""
        st = _repair(BOARD)
        self.assertTrue(st.segments)
        for t in st.segments[:50]:
            self.assertEqual(len(t), 7)
            self.assertIn(t[6], st._all_cu)
        self.assertFalse(hasattr(st, '_seg_layer_by_id'),
                         'the companion layer map is back -- there are two '
                         'answers to "what layer is this track on" again')
    # MUTATION: put `side` back in element 6 -> `assertIn(t[6], _all_cu)` fails.

    def test_this_file_is_collected_as_a_FAST_test(self):
        """This file names the integration markers in its own prose, which is
        exactly how the #725 file got bucketed as integration and skipped under
        --fast (commit 6c2096c6)."""
        import run_all
        self.assertFalse(run_all.is_integration(os.path.abspath(__file__)))
        found = [os.path.basename(f) for f in run_all.discover(['731'])]
        self.assertIn(os.path.basename(__file__), found)
    # MUTATION: delete the `RUN_ALL_FAST_OK = True` line -> is_integration
    # returns True and the file is skipped under --fast.


if __name__ == '__main__':
    unittest.main(verbosity=2)
