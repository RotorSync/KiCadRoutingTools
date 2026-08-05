"""Run-4 reconstruct fixes F1/F3/F5, on the archived swap corpus.

Board-only assertions throughout: nearest-slot is checked against the
tool's OWN proposals, never against ground truth.
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

SWAP = os.path.join(ROOT, 'wk', 'b2', 'tigard__swap', 'd0',
                    'perturbed.kicad_pcb')


def _state():
    from kicad_parser import parse_kicad_pcb
    from pose_score import make_state
    return make_state(parse_kicad_pcb(SWAP), SWAP, clearance=0.09)


def run_reconstruct(board, out, *extra):
    env = dict(os.environ, PYTHONPATH=ROOT, PYTHONIOENCODING='utf-8')
    return subprocess.run(
        [sys.executable, '-X', 'utf8',
         os.path.join(ROOT, 'place_reconstruct.py'), board, out,
         '--clearance', '0.09', *extra],
        capture_output=True, text=True, env=env, cwd=ROOT)


def _summary(stdout):
    line = [l for l in stdout.splitlines() if l.startswith('JSON_SUMMARY:')]
    return json.loads(line[0].split('JSON_SUMMARY:', 1)[1])


class TestF1NearestSlot(unittest.TestCase):
    def test_moved_pattern_parts_take_their_nearest_slot(self):
        """Run 3 shipped the two repaired holes CROSSED (~40mm from home
        each): zero-net parts have no net-anchor cost, both corners cost the
        same, and HiGHS picked arbitrarily. The tiebreak makes each moved
        pattern part take the slot NEAREST its input pose -- asserted
        against the tool's own fit_proposals, no ground truth read."""
        if not os.path.exists(SWAP):
            self.skipTest('swap corpus board not present')
        from kicad_parser import parse_kicad_pcb
        with tempfile.TemporaryDirectory() as td:
            out = os.path.join(td, 'r.kicad_pcb')
            r = run_reconstruct(SWAP, out)
            self.assertEqual(r.returncode, 0, r.stdout[-800:] + r.stderr[-400:])
            doc = _summary(r.stdout)
            before = parse_kicad_pcb(SWAP).footprints
            after = parse_kicad_pcb(out).footprints
            checked = 0
            for ref, slots in (doc.get('fit_proposals') or {}).items():
                if ref not in doc.get('assign_moved', []):
                    continue
                bx, by = before[ref].x, before[ref].y
                ax, ay = after[ref].x, after[ref].y
                d_taken = math.hypot(ax - bx, ay - by)
                for (sx, sy) in slots:
                    self.assertLessEqual(
                        d_taken, math.hypot(sx - bx, sy - by) + 1e-3,
                        f"{ref} took a slot farther than an alternative")
                checked += 1
            self.assertGreaterEqual(checked, 2, doc.get('fit_proposals'))


class TestF3VectorSupportAndPrune(unittest.TestCase):
    def test_singleton_vector_is_dropped(self):
        if not os.path.exists(SWAP):
            self.skipTest('swap corpus board not present')
        from placement import reconstruct
        st = _state()
        ref = sorted(st.parts)[0]
        p = st.parts[ref]
        # one ref, one proposal 10mm away -> a singleton vector: dropped
        vecs = reconstruct.rigid_vectors(st, {ref: [(p.x + 10.0, p.y)]})
        self.assertEqual(vecs, [])

    def test_two_ref_support_is_kept(self):
        if not os.path.exists(SWAP):
            self.skipTest('swap corpus board not present')
        from placement import reconstruct
        st = _state()
        r1, r2 = sorted(st.parts)[:2]
        p1, p2 = st.parts[r1], st.parts[r2]
        vecs = reconstruct.rigid_vectors(
            st, {r1: [(p1.x + 10.0, p1.y + 4.0)],
                 r2: [(p2.x + 10.0, p2.y + 4.0)]})
        self.assertEqual(len(vecs), 1)

    def test_prune_reverts_a_move_the_global_gate_cannot_see(self):
        """Manufacture a mis-move: drag one part 20mm off its pose (legal,
        conflict-free spot not required -- the tuple's hpwl term does the
        judging) and let prune_assignment restore it."""
        if not os.path.exists(SWAP):
            self.skipTest('swap corpus board not present')
        from placement import reconstruct
        st = _state()
        # pick a small netted part; move it far away
        ref = next(r for r in sorted(st.parts)
                   if st.parts[r].pin_count >= 2 and not st.parts[r].locked)
        p = st.parts[ref]
        old = {ref: (p.x, p.y, p.rot)}
        st.apply_move(ref, p.x + 20.0, p.y, p.rot)
        pruned = reconstruct.prune_assignment(st, old)
        self.assertEqual(pruned, [ref])
        self.assertAlmostEqual(st.parts[ref].x, old[ref][0], places=6)

    def test_prune_keeps_a_good_move(self):
        if not os.path.exists(SWAP):
            self.skipTest('swap corpus board not present')
        from placement import reconstruct
        st = _state()
        ref = next(r for r in sorted(st.parts)
                   if st.parts[r].pin_count >= 2 and not st.parts[r].locked)
        p = st.parts[ref]
        # 'old' pose 20mm away = the CURRENT pose is the good one; prune must
        # not revert to the worse old pose
        old = {ref: (p.x + 20.0, p.y, p.rot)}
        pruned = reconstruct.prune_assignment(st, old)
        self.assertEqual(pruned, [])
        self.assertAlmostEqual(st.parts[ref].x, p.x, places=6)


class TestF5FullCensus(unittest.TestCase):
    def test_worst_n_zero_lists_every_pair(self):
        if not os.path.exists(SWAP):
            self.skipTest('swap corpus board not present')
        from kicad_parser import parse_kicad_pcb
        from placement.legality import grade_pad_legality
        g = grade_pad_legality(parse_kicad_pcb(SWAP), 0.09, worst_n=0)
        self.assertEqual(len(g['worst']), g['pad_conflicts'])
        self.assertGreater(g['pad_conflicts'], 10,
                           'fixture: the swap board carries 20 pairs')

    def test_repair_census_reports_all(self):
        if not os.path.exists(SWAP):
            self.skipTest('swap corpus board not present')
        env = dict(os.environ, PYTHONPATH=ROOT, PYTHONIOENCODING='utf-8')
        with tempfile.TemporaryDirectory() as td:
            intent = os.path.join(td, 'i.json')
            with open(intent, 'w', encoding='utf-8') as f:
                f.write('{"schema": 1, "kind": "floorplan-intent"}\n')
            r = subprocess.run(
                [sys.executable, '-X', 'utf8',
                 os.path.join(ROOT, 'place_seed.py'), SWAP,
                 os.path.join(td, 'o.kicad_pcb'), '--intent', intent,
                 '--repair', '--dry-run', '--clearance', '0.09'],
                capture_output=True, text=True, env=env, cwd=ROOT)
            self.assertIn('Repair census:', r.stdout)
            self.assertIn('all listed', r.stdout)


if __name__ == '__main__':
    unittest.main()
