"""Run-6 body-overlap channel (the assembly gate's primitive).

The calibration contract: the BLOCKING channel (cross-footprint pad
intersection) reads ZERO pairs on every healthy in-repo board, and catches
the run-5 shipped defect (C14 stacked on R14, same-net pads intersecting).
Advisory channels (fab/courtyard) are labeled, never blocking.
"""

import glob
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

sys.path.insert(0, os.path.join(ROOT, 'py_placer'))  # placement split
sys.path.insert(0, os.path.join(ROOT, 'py_router'))  # placement split
sys.path.insert(0, os.path.join(ROOT, 'py_tools'))  # placement split
FINAL5 = os.path.join(ROOT, 'wk', 'run5', 'final5.kicad_pcb')
HUMAN = os.path.join(ROOT, 'wk', 'run2', 'original', 'tigard_v10.kicad_pcb')


def _grade(board, **kw):
    from kicad_parser import parse_kicad_pcb
    from placement.legality import grade_body_overlap
    return grade_body_overlap(parse_kicad_pcb(board), 0.09,
                              pcb_file=board, **kw)


class TestCorpusCalibration(unittest.TestCase):
    def test_all_healthy_boards_grade_zero_blocking(self):
        """THE calibration gate: pad_intersection must be 0 on every corpus
        board, or the channel may not gate anywhere (run-6 invariant)."""
        boards = sorted(glob.glob(os.path.join(ROOT, 'kicad_files',
                                               '*.kicad_pcb')))
        self.assertGreaterEqual(len(boards), 30)
        bad = []
        for b in boards:
            try:
                g = _grade(b)
            except Exception as e:
                bad.append((os.path.basename(b), f'ERROR {e}'))
                continue
            if g['blocking']:
                bad.append((os.path.basename(b),
                            [(p.a, p.b) for p in g['blocking_pairs']]))
        self.assertEqual(bad, [], f'blocking pairs on healthy boards: {bad}')


class TestKnownDefect(unittest.TestCase):
    def test_run5_deliverable_is_caught(self):
        """The board run 5 shipped: C14 stacked on R14 must be BLOCKING via
        pad_intersection (same-net copper physically intersecting -- the
        channel pair_shortfall's same-net skip is blind to), with courtyard
        and fab advisory entries for the same pair."""
        if not os.path.exists(FINAL5):
            self.skipTest('run-5 deliverable not present')
        g = _grade(FINAL5)
        self.assertEqual(g['blocking'], 1)
        p = g['blocking_pairs'][0]
        self.assertEqual((p.a, p.b, p.kind),
                         ('C14', 'R14', 'pad_intersection'))
        kinds = {q.kind for q in g['pairs'] if (q.a, q.b) == ('C14', 'R14')}
        self.assertEqual(kinds, {'pad_intersection', 'courtyard', 'fab'})

    def test_human_board_calibrates_clean(self):
        """The human tigard: 0 blocking; its two by-design courtyard pairs
        (mount holes under connector shells) waive by marker class."""
        if not os.path.exists(HUMAN):
            self.skipTest('human board not present')
        g = _grade(HUMAN)
        self.assertEqual(g['blocking'], 0)
        waived = {(p.a, p.b): p.waiver for p in g['pairs'] if p.waived}
        self.assertEqual(waived.get(('H4', 'J2')), 'marker_class')
        self.assertEqual(waived.get(('H3', 'J7')), 'marker_class')


class TestSynthetic(unittest.TestCase):
    BOARD = (
        '(kicad_pcb (version 20221018) (generator pcbnew)\n'
        '  (layers (0 "F.Cu" signal) (31 "B.Cu" signal)'
        ' (44 "Edge.Cuts" user))\n'
        '  (net 0 "") (net 1 "VCC")\n'
        '  (gr_rect (start 0 0) (end 30 30) (stroke (width 0.1)'
        ' (type default)) (layer "Edge.Cuts"))\n'
        '  (footprint "t:A" (layer "{la}") (at 10 10)\n'
        '    (property "Reference" "CA" (at 0 0) (layer "F.SilkS"))\n'
        '    (fp_rect (start -1 -0.6) (end 1 0.6) (stroke (width 0.05)'
        ' (type default)) (layer "{la_pref}.CrtYd"))\n'
        '    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6)'
        ' (layers "{la}") (net 1 "VCC"))\n'
        '    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6)'
        ' (layers "{la}") (net 1 "VCC")))\n'
        '  (footprint "t:B" (layer "{lb}") (at {bx} 10)\n'
        '    (property "Reference" "CB" (at 0 0) (layer "F.SilkS"))\n'
        '    (fp_rect (start -1 -0.6) (end 1 0.6) (stroke (width 0.05)'
        ' (type default)) (layer "{lb_pref}.CrtYd"))\n'
        '    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6)'
        ' (layers "{lb}") (net 1 "VCC"))\n'
        '    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6)'
        ' (layers "{lb}") (net 1 "VCC")))\n'
        ')\n')

    def _board(self, bx, la='F.Cu', lb='F.Cu'):
        text = self.BOARD.format(bx=bx, la=la, lb=lb,
                                 la_pref=la[0], lb_pref=lb[0])
        td = tempfile.mkdtemp()
        path = os.path.join(td, 'b.kicad_pcb')
        with open(path, 'w', encoding='utf-8') as f:
            f.write(text)
        return path

    def test_same_net_stack_is_blocking(self):
        """Two footprints stacked so their SAME-NET pads intersect: the
        short detector skips the pair by design; the assembly channel must
        flag it (the C14/R14 class)."""
        g = _grade(self._board(bx=10.4))
        self.assertEqual(g['blocking'], 1)
        self.assertEqual(g['blocking_pairs'][0].kind, 'pad_intersection')

    def test_opposite_sides_do_not_interact(self):
        """The same XY stack on OPPOSITE board sides is legal (the JP1/SW1
        lesson: side-blind flattening manufactured a phantom)."""
        g = _grade(self._board(bx=10.4, lb='B.Cu'))
        self.assertEqual(g['blocking'], 0)
        self.assertEqual(g['advisory'], 0)

    def test_clear_parts_grade_clean(self):
        g = _grade(self._board(bx=13.0))
        self.assertEqual(g['blocking'], 0)
        self.assertEqual(g['advisory'], 0)

    def test_courtyard_kiss_is_advisory_not_blocking(self):
        """Pads clear, courtyards intersecting: advisory (the corpus
        measured 6 real boards shipping exactly this; it must not block)."""
        g = _grade(self._board(bx=11.8))
        self.assertEqual(g['blocking'], 0)
        self.assertEqual(g['advisory'], 1)
        self.assertEqual(g['advisory_pairs'][0].kind, 'courtyard')

    def test_intent_waiver_labels_the_pair(self):
        g = _grade(self._board(bx=11.8), intent_waivers=[('CA', 'CB')])
        self.assertEqual(g['advisory'], 0)
        waived = [p for p in g['pairs'] if p.waived]
        self.assertEqual(len(waived), 1)
        self.assertEqual(waived[0].waiver, 'intent_declared')


class TestIntentKey(unittest.TestCase):
    def test_overlap_waivers_load_and_validate(self):
        from placement import floorplan
        doc = {'schema': 1, 'kind': 'floorplan-intent',
               'overlap_waivers': [{'pair': ['A1', 'B2'],
                                    'reason': 'shield overhang'}]}
        td = tempfile.mkdtemp()
        p = os.path.join(td, 'i.json')
        with open(p, 'w', encoding='utf-8') as f:
            json.dump(doc, f)
        intent = floorplan.load_intent(p)
        self.assertEqual(intent.waiver_pairs(), (('A1', 'B2'),))
        doc['overlap_waivers'] = [{'pair': ['only-one']}]
        with open(p, 'w', encoding='utf-8') as f:
            json.dump(doc, f)
        with self.assertRaises(floorplan.IntentError):
            floorplan.load_intent(p)


if __name__ == '__main__':
    unittest.main()
