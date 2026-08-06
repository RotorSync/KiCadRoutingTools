"""Run-5 suspect-and-derive emit guard (commit 2), on the archived swap corpus.

The observation branch of emit_intent used to BLESS every overhanging part
with its damaged pose (run-4 limit 3: SW1's wrong overhang became a declared
allowance). The guard: an overhanging part that is pad-legality-implicated or
sits where the board's own rigid pattern vectors say a displaced pose lands
is a SUSPECT -- it gets a class-only declaration (no edge invented) and the
oob budget is withheld rather than baked from a damaged board. Healthy
boards bless byte-identically; mount-hole observation entries are suppressed
outright (the pattern fit owns hole positions, run-4 limit 3).
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

SWAP = os.path.join(ROOT, 'wk', 'b2', 'tigard__swap', 'd0',
                    'perturbed.kicad_pcb')
HEALTHY = os.path.join(ROOT, 'kicad_files', 'tigard.kicad_pcb')


def emit(board, out, *extra):
    env = dict(os.environ, PYTHONPATH=ROOT, PYTHONIOENCODING='utf-8',
               KRT_NO_BANNER='1')
    return subprocess.run(
        [sys.executable, '-X', 'utf8',
         os.path.join(ROOT, 'check_floorplan.py'), board,
         '--emit-intent', out, *extra],
        capture_output=True, text=True, env=env, cwd=ROOT)


class TestSuspectAndDerive(unittest.TestCase):
    def _swap_intent(self, td):
        ip = os.path.join(td, 'auto.json')
        r = emit(SWAP, ip, '--declare-classes')
        self.assertEqual(r.returncode, 0, r.stdout[-600:] + r.stderr[-400:])
        return json.load(open(ip, encoding='utf-8'))

    def test_displaced_overhang_becomes_class_only_suspect(self):
        """SW1 (displaced, parked overhanging a WRONG edge) must emit with
        its class and NO edge -- the note names the suspicion, the
        derivation/exchange rungs decide. Board-only: the suspicion comes
        from the board's own pattern vectors, never from ground truth."""
        if not os.path.exists(SWAP):
            self.skipTest('swap corpus board not present')
        with tempfile.TemporaryDirectory() as td:
            doc = self._swap_intent(td)
            by_ref = {c['ref']: c for c in doc['edge_connectors']}
            self.assertIn('SW1', by_ref)
            e = by_ref['SW1']
            self.assertNotIn('edge', e, 'a suspect pose must not become a '
                                        'declared edge')
            self.assertEqual(e.get('class'), 'edge_actuator')
            self.assertIn('SUSPECT', e.get('note') or '')

    def test_mount_hole_observation_entries_suppressed(self):
        """H1 (a displaced mounting hole, overhanging) must not appear at
        all: hole positions belong to the R2 pattern fit, and recording the
        damaged overhang as an allowance poisoned run 4's intent."""
        if not os.path.exists(SWAP):
            self.skipTest('swap corpus board not present')
        with tempfile.TemporaryDirectory() as td:
            doc = self._swap_intent(td)
            refs = {c['ref'] for c in doc['edge_connectors']}
            self.assertNotIn('H1', refs)

    def test_budget_withheld_when_suspects_exist(self):
        """A damaged board must not bake its own oob count as the budget --
        the repaired board re-emits the honest number instead."""
        if not os.path.exists(SWAP):
            self.skipTest('swap corpus board not present')
        with tempfile.TemporaryDirectory() as td:
            doc = self._swap_intent(td)
            self.assertNotIn('oob_count', doc.get('legality_budget') or {})

    def test_healthy_board_blesses_and_bakes(self):
        """The healthy in-repo tigard: no suspects (both signals are
        board-only and read ~0 there), the budget bakes oob_count as
        before, and the corner mounting holes emit no observation entries
        (the one documented healthy-board change)."""
        with tempfile.TemporaryDirectory() as td:
            ip = os.path.join(td, 'i.json')
            r = emit(HEALTHY, ip)
            self.assertEqual(r.returncode, 0,
                             r.stdout[-600:] + r.stderr[-400:])
            doc = json.load(open(ip, encoding='utf-8'))
            for c in doc['edge_connectors']:
                self.assertNotIn('SUSPECT', c.get('note') or '', c['ref'])
            self.assertIn('oob_count', doc.get('legality_budget') or {})
            refs = {c['ref'] for c in doc['edge_connectors']}
            self.assertFalse(refs & {'H1', 'H2', 'H3', 'H4'},
                             'mount-hole observation entries must be gone')

    def test_emit_is_deterministic(self):
        if not os.path.exists(SWAP):
            self.skipTest('swap corpus board not present')
        with tempfile.TemporaryDirectory() as td:
            a = os.path.join(td, 'a.json')
            b = os.path.join(td, 'b.json')
            self.assertEqual(emit(SWAP, a, '--declare-classes').returncode, 0)
            self.assertEqual(emit(SWAP, b, '--declare-classes').returncode, 0)
            self.assertEqual(open(a, encoding='utf-8').read(),
                             open(b, encoding='utf-8').read())


class TestEdgePrefReceptaclesOnly(unittest.TestCase):
    def test_actuator_suspect_gets_no_edge_objective(self):
        """place_reconstruct's edge_pref must contain the edge-less
        RECEPTACLE (J1: the edge metric IS its objective) and must NOT
        contain the suspect ACTUATOR (SW1: no seat claim -- pinning it to
        an edge would re-manufacture the damage; the exchange stage owns
        it)."""
        if not os.path.exists(SWAP):
            self.skipTest('swap corpus board not present')
        env = dict(os.environ, PYTHONPATH=ROOT, PYTHONIOENCODING='utf-8',
                   KRT_NO_BANNER='1')
        with tempfile.TemporaryDirectory() as td:
            ip = os.path.join(td, 'auto.json')
            r0 = emit(SWAP, ip, '--declare-classes')
            self.assertEqual(r0.returncode, 0)
            r = subprocess.run(
                [sys.executable, '-X', 'utf8',
                 os.path.join(ROOT, 'place_reconstruct.py'), SWAP,
                 os.path.join(td, 'r.kicad_pcb'), '--clearance', '0.09',
                 '--intent', ip, '--dry-run'],
                capture_output=True, text=True, env=env, cwd=ROOT)
            self.assertEqual(r.returncode, 0,
                             r.stdout[-800:] + r.stderr[-400:])
            line = [l for l in r.stdout.splitlines()
                    if l.startswith('JSON_SUMMARY:')]
            doc = json.loads(line[0].split('JSON_SUMMARY:', 1)[1])
            pref = doc.get('edge_pref', [])
            self.assertIn('J1', pref)
            self.assertNotIn('SW1', pref)
            # the suspect actuator still keeps its band allowance (charging
            # its observed overhang would let the ILP 'improve' the tuple by
            # pulling a false-positive suspect off a correct seat)
            self.assertIn('SW1', doc.get('edge_bands', {}))


if __name__ == '__main__':
    unittest.main()
