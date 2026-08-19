"""Run-6 backlog fixes: qfn auto-detect ranking, bga_fanout None guard."""

import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

POUR = os.path.join(ROOT, 'wk', 'run5', 's1_pour.kicad_pcb')


def _run(script, *argv):
    env = dict(os.environ, PYTHONPATH=ROOT, PYTHONIOENCODING='utf-8',
               KRT_NO_BANNER='1')
    return subprocess.run(
        [sys.executable, '-X', 'utf8', os.path.join(ROOT, script), *argv],
        capture_output=True, text=True, env=env, cwd=ROOT)


class TestQfnAutoDetect(unittest.TestCase):
    def test_ranking_prefers_the_real_qfn(self):
        """File order used to pick J1 (a rect-pad USB-C the geometric
        fallback classifies QFN) over the 64-pin U3."""
        if not os.path.exists(POUR):
            self.skipTest('run-5 chain board not present')
        r = _run('qfn_fanout.py', POUR, '-o', os.devnull,
                 '--clearance', '0.09', '--width', '0.127')
        self.assertIn('Auto-detected QFN/QFP component: U3', r.stdout)


class TestBgaFanoutGuard(unittest.TestCase):
    def test_qfn_ref_does_not_crash(self):
        """bga_fanout -c <QFN>: analyze_bga_grid returns None for perimeter
        packages and the plane-drop pass crashed on grid.pitch_x."""
        if not os.path.exists(POUR):
            self.skipTest('run-5 chain board not present')
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            r = _run('bga_fanout.py', POUR, '-o',
                     os.path.join(td, 'x.kicad_pcb'), '-c', 'U3',
                     '--clearance', '0.09')
            self.assertNotIn('Traceback', r.stderr)
            self.assertNotIn('AttributeError', r.stderr)
            self.assertIn('qfn_fanout.py', r.stdout)


if __name__ == '__main__':
    unittest.main()
