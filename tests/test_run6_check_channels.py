"""check_channels.py / face_lane_ledger contract (run-6 commit 5)."""

import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

sys.path.insert(0, os.path.join(ROOT, 'py_placer'))  # placement split
sys.path.insert(0, os.path.join(ROOT, 'py_router'))  # placement split
sys.path.insert(0, os.path.join(ROOT, 'py_tools'))  # placement split
TIGARD = os.path.join(ROOT, 'kicad_files', 'tigard.kicad_pcb')


class TestLaneLedger(unittest.TestCase):
    def test_ledger_shape_and_report_only_exit(self):
        env = dict(os.environ, PYTHONPATH=ROOT, PYTHONIOENCODING='utf-8',
                   KRT_NO_BANNER='1')
        with tempfile.TemporaryDirectory() as td:
            jp = os.path.join(td, 'c.json')
            r = subprocess.run(
                [sys.executable, '-X', 'utf8',
                 os.path.join(ROOT, 'py_tools', 'check_channels.py'), TIGARD,
                 '--clearance', '0.09', '--track-width', '0.127',
                 '--grid-step', '0.05', '--json', jp],
                capture_output=True, text=True, env=env, cwd=ROOT)
            self.assertEqual(r.returncode, 0, r.stdout[-400:])
            doc = json.load(open(jp, encoding='utf-8'))
            self.assertTrue(doc['taps_not_modeled'],
                            'the v1 limitation must be stated in the output')
            self.assertIn('U3', doc['ledgers'])
            faces = {row['face'] for row in doc['ledgers']['U3']}
            self.assertEqual(faces, {'N', 'S', 'E', 'W'})
            for row in doc['ledgers']['U3']:
                self.assertGreaterEqual(row['supply_finest_grid'],
                                        row['supply_routed_grid'],
                                        'finer grid can only add lanes')
            self.assertTrue(doc['channels'],
                            'anchor channel table must not be empty')

    def test_ledger_library_call(self):
        from kicad_parser import parse_kicad_pcb
        from placement import routability
        pcb = parse_kicad_pcb(TIGARD)
        rows = routability.face_lane_ledger(
            pcb, 'U3', clearance=0.09, track_width=0.127, grid_step=0.05,
            pcb_file=TIGARD)
        self.assertEqual(len(rows), 4)
        self.assertTrue(all(r['taps_not_modeled'] for r in rows))


if __name__ == '__main__':
    unittest.main()
