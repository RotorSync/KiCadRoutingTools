"""check_assembly.py CLI contract (run-6 commit 2)."""

import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

FINAL5 = os.path.join(ROOT, 'wk', 'run5', 'final5.kicad_pcb')
HUMAN = os.path.join(ROOT, 'wk', 'run2', 'original', 'tigard_v10.kicad_pcb')


def _run(*argv):
    env = dict(os.environ, PYTHONPATH=ROOT, PYTHONIOENCODING='utf-8',
               KRT_NO_BANNER='1')
    return subprocess.run(
        [sys.executable, '-X', 'utf8',
         os.path.join(ROOT, 'py_tools', 'check_assembly.py'), *argv],
        capture_output=True, text=True, env=env, cwd=ROOT)


class TestCheckAssemblyCLI(unittest.TestCase):
    def test_defect_board_exits_4_with_json(self):
        if not os.path.exists(FINAL5):
            self.skipTest('run-5 deliverable not present')
        with tempfile.TemporaryDirectory() as td:
            jp = os.path.join(td, 'a.json')
            r = _run(FINAL5, '--clearance', '0.09', '--json', jp)
            self.assertEqual(r.returncode, 4, r.stdout[-400:])
            self.assertIn('NOT BUILDABLE', r.stdout)
            doc = json.load(open(jp, encoding='utf-8'))
            self.assertEqual(doc['blocking'], 1)
            self.assertEqual(doc['blocking_pairs'][0]['a'], 'C14')
            self.assertEqual(doc['blocking_pairs'][0]['b'], 'R14')

    def test_clean_board_exits_0(self):
        if not os.path.exists(HUMAN):
            self.skipTest('human board not present')
        r = _run(HUMAN, '--clearance', '0.09')
        self.assertEqual(r.returncode, 0, r.stdout[-400:])
        self.assertIn('buildable (blocking 0)', r.stdout)

    def test_baseline_marks_new_pairs_only(self):
        """The loop currency: pre-existing advisory pairs are not NEW."""
        if not os.path.exists(FINAL5):
            self.skipTest('run-5 deliverable not present')
        base = os.path.join(ROOT, 'wk', 'b2', 'tigard__swap', 'd0',
                            'perturbed.kicad_pcb')
        if not os.path.exists(base):
            self.skipTest('swap corpus board not present')
        with tempfile.TemporaryDirectory() as td:
            jp = os.path.join(td, 'a.json')
            r = _run(FINAL5, '--clearance', '0.09', '--baseline', base,
                     '--json', jp)
            self.assertEqual(r.returncode, 4)
            doc = json.load(open(jp, encoding='utf-8'))
            news = {(q['a'], q['b'], q['kind'])
                    for q in doc['new_advisory_pairs']}
            self.assertEqual(news, {('C14', 'R14', 'courtyard'),
                                    ('C14', 'R14', 'fab')})

    def test_bad_board_exits_2(self):
        r = _run(os.path.join(ROOT, 'no_such_board.kicad_pcb'))
        self.assertEqual(r.returncode, 2)


if __name__ == '__main__':
    unittest.main()


class TestRenderBodyChannel(unittest.TestCase):
    def test_render_checklist_names_the_stack(self):
        """The render JSON's b_body_overlap_pairs must name the stacked
        pair (run 5's render said b_overlap_pairs=[] while C14 sat on R14
        -- the key carried the wrong channel)."""
        if not os.path.exists(FINAL5):
            self.skipTest('run-5 deliverable not present')
        env = dict(os.environ, PYTHONPATH=ROOT, PYTHONIOENCODING='utf-8',
                   KRT_NO_BANNER='1')
        with tempfile.TemporaryDirectory() as td:
            jp = os.path.join(td, 'r.json')
            r = subprocess.run(
                [sys.executable, '-X', 'utf8',
                 os.path.join(ROOT, 'py_tools', 'render_placement.py'), FINAL5,
                 '--json-out', jp, '-o', os.path.join(td, 'r.png'),
                 '--size', '320', '--supersample', '1'],
                capture_output=True, text=True, env=env, cwd=ROOT)
            self.assertEqual(r.returncode, 0, r.stderr[-400:])
            doc = json.load(open(jp, encoding='utf-8'))
            cl = doc['checklist']
            self.assertIn(['C14', 'R14'], cl['b_body_overlap_pairs'])
            self.assertIn('b_pad_clearance_pairs', cl)
