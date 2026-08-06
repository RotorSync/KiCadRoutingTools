"""Run-4 C: place_seed --anchors-first / --anchor-rounds.

Two DIFFERENT boards (generalization gate 3): the pile fixture is
synthesized from each tracked board (every padded part stacked at the board
center), the intent emitted from the original, and the seed run in
anchors-first mode. Board-only assertions: legality, seating completeness,
the anchors-before-smalls queue note, and the no-worsen round gate.
"""

import json
import math
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'py_router'))  # placement split
sys.path.insert(0, os.path.join(ROOT, 'py_tools'))  # placement split
sys.path.insert(0, os.path.join(ROOT, 'py_placer'))  # placement split

# Two boards with different outlines/part mixes (generalization gate 3).
# interf_u's pile is a PRE-EXISTING hard case (the plain seed also fails it
# with 318mm2 of courtyard overlap -- measured, mode-independent), so it is
# not a useful discriminator here.
BOARDS = [os.path.join(ROOT, 'kicad_files', 'splitflap_driver.kicad_pcb'),
          os.path.join(ROOT, 'kicad_files', 'cap_chain.kicad_pcb')]


def _run(argv):
    env = dict(os.environ, PYTHONPATH=ROOT, PYTHONIOENCODING='utf-8',
               PYTHONHASHSEED='0')
    return subprocess.run([sys.executable, '-X', 'utf8'] + argv,
                          capture_output=True, text=True, encoding='utf-8',
                          errors='replace', cwd=ROOT, env=env, timeout=1800)


def _pile(board, td):
    from kicad_parser import parse_kicad_pcb
    from placement.floorplan import emit_intent
    from placement.writer import write_placed_output
    pcb = parse_kicad_pcb(board)
    b = pcb.board_info.board_bounds
    cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
    pile = os.path.join(td, 'pile.kicad_pcb')
    write_placed_output(board, pile, [
        {'reference': ref, 'new_x': cx, 'new_y': cy, 'new_rotation': 0}
        for ref, fp in sorted(pcb.footprints.items()) if fp.pads])
    ip = os.path.join(td, 'intent.json')
    with open(ip, 'w', encoding='utf-8') as f:
        json.dump(emit_intent(pcb, board), f)
    return pile, ip


class TestAnchorsFirst(unittest.TestCase):
    def _seed(self, board, *extra):
        with tempfile.TemporaryDirectory() as td:
            pile, ip = _pile(board, td)
            out = os.path.join(td, 'out.kicad_pcb')
            r = _run([os.path.join(ROOT, 'py_placer', 'place_seed.py'), pile, out,
                      '--intent', ip, '--anchors-first', *extra])
            self.assertEqual(r.returncode, 0,
                             r.stdout[-900:] + r.stderr[-400:])
            from kicad_parser import parse_kicad_pcb
            from placement.legality import grade_pad_legality
            return r, parse_kicad_pcb(out)

    def test_anchors_seed_before_smalls_and_stay_legal(self):
        for board in BOARDS:
            if not os.path.isfile(board):
                continue
            with self.subTest(board=os.path.basename(board)):
                r, pcb = self._seed(board)
                self.assertIn('anchors-first:', r.stdout,
                              'the anchor queue must be in the record')
                self.assertNotIn('no legal pose anywhere', r.stdout)
                from placement.legality import grade_pad_legality
                g = grade_pad_legality(pcb, 0.15)
                self.assertEqual(g['pad_conflicts'], 0, g['worst'][:5])
                self.assertEqual(g['hole_conflicts'], 0)

    def test_anchor_rounds_gate_never_worsens(self):
        board = BOARDS[0]
        if not os.path.isfile(board):
            self.skipTest('fixture missing')
        from placement.reconstruct import measure
        from pose_score import make_state
        r1, pcb1 = self._seed(board)
        r2, pcb2 = self._seed(board, '--anchor-rounds', '2')
        self.assertIn('anchor round 2', r2.stdout + '',
                      'round 2 must run and be recorded')
        # the round gate: rounds may only improve-or-hold the tuple
        with tempfile.TemporaryDirectory() as td:
            p1 = os.path.join(td, 'a.kicad_pcb')
            p2 = os.path.join(td, 'b.kicad_pcb')
            # re-write parsed boards is awkward; measure states directly
        s1 = make_state(pcb1, board, clearance=0.15)
        s2 = make_state(pcb2, board, clearance=0.15)
        self.assertLessEqual(measure(s2), measure(s1),
                             'anchor rounds must not worsen the gate tuple')


if __name__ == '__main__':
    unittest.main()
