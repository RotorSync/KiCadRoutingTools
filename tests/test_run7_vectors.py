"""Run-7 A1: conflict-derived candidate vectors (structural no-op)."""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


class TestConflictOffsetVectors(unittest.TestCase):
    def test_healthy_board_mints_nothing(self):
        """The no-op guarantee is STRUCTURAL: no conflicts, no vectors."""
        from kicad_parser import parse_kicad_pcb
        from pose_score import make_state
        from placement import reconstruct as R
        b = os.path.join(ROOT, 'kicad_files', 'tigard.kicad_pcb')
        st = make_state(parse_kicad_pcb(b), b, clearance=0.09)
        self.assertEqual(R.conflict_offset_vectors(st), [])

    def test_damaged_board_with_clustered_offsets_mints(self):
        """The tigard swap input carries 20+ conflict pairs; offsets above
        the 2mm intra-pile floor that agree within tolerance must cluster
        into candidate vectors (coarse, gate-refereed downstream)."""
        b = os.path.join(ROOT, 'wk', 'b2', 'tigard__swap', 'd0',
                         'perturbed.kicad_pcb')
        if not os.path.exists(b):
            self.skipTest('swap corpus board not present')
        from kicad_parser import parse_kicad_pcb
        from pose_score import make_state
        from placement import reconstruct as R
        st = make_state(parse_kicad_pcb(b), b, clearance=0.09)
        vecs = R.conflict_offset_vectors(st)
        self.assertTrue(vecs)
        for d in vecs:
            self.assertGreaterEqual(d['support'], 3)


if __name__ == '__main__':
    unittest.main()
