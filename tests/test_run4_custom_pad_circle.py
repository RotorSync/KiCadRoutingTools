"""Run-4 B6: custom-pad circle primitives must carry clearance-grade geometry.

Run 3's DRC verifier lens found a 0.0884mm-vs-0.09 track-to-pad graze that
kicad-cli flagged and check_drc missed: the pad's copper is a gr_circle
PRIMITIVE, and the fixed inscribed 32-gon under-reached the true circle by
~2.4um at r=0.5 -- more than the 1.6um defect. The primitive path now uses
adaptive tessellation (1um sagitta, capped 64 vertices).

Both directions are pinned: a real sub-floor gap is flagged, and a gap
safely above the floor is NOT (the polygon stays inscribed, so tightening
it cannot manufacture phantom violations).
"""

import math
import os
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
# #522: the engine lives in py_router/, the placer in py_placer/ --
# ROOT alone does not make `import kicad_parser` work.
for _p522 in ('py_router', 'py_placer'):
    _d522 = os.path.join(ROOT, _p522)
    if _d522 not in sys.path:
        sys.path.insert(0, _d522)
# #522 reorg + skill merge: the engine moved to py_router/, the placer to
# py_placer/, and board_score.py into the placement-and-routing skill. Tests
# that shell out to or import them need those roots on sys.path.
for _p in ('py_router', 'py_placer',
           os.path.join('.claude', 'skills', 'plan-pcb-placement-and-routing',
                        'scripts')):
    _d = os.path.join(ROOT, _p)
    if _d not in sys.path:
        sys.path.insert(0, _d)

RUN3_BOARD = os.path.join(ROOT, 'wk', 'run3', 'final2.kicad_pcb')


def board_with_gap(gap_mm: float) -> str:
    """A custom pad whose copper is ONE gr_circle primitive (r=0.5) beside a
    1x1 rect pad on another net, true edge-to-edge gap ``gap_mm``."""
    # circle copper edge at x = 50 + 0.5; rect pad left edge at that + gap
    rect_cx = 50 + 0.5 + gap_mm + 0.5
    return (
        '(kicad_pcb (version 20221018) (generator pcbnew)\n'
        '  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))\n'
        '  (net 0 "") (net 1 "A") (net 2 "B")\n'
        '  (gr_rect (start 40 40) (end 70 60) (stroke (width 0.1) (type default))'
        ' (layer "Edge.Cuts"))\n'
        '  (footprint "t:JP" (layer "F.Cu") (at 50 50)\n'
        '    (property "Reference" "JP1" (at 0 0) (layer "F.SilkS"))\n'
        '    (pad "1" smd custom (at 0 0) (size 0.1 0.1) (layers "F.Cu")'
        ' (net 1 "A")\n'
        '      (options (clearance outline) (anchor circle))\n'
        '      (primitives (gr_circle (center 0 0) (end 0.45 0)'
        ' (width 0.1) (fill yes)))))\n'
        f'  (footprint "t:R" (layer "F.Cu") (at {rect_cx:.4f} 50)\n'
        '    (property "Reference" "R1" (at 0 0) (layer "F.SilkS"))\n'
        '    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 2 "B")))\n'
        ')\n')


def run_drc(board_path, *argv):
    env = dict(os.environ, PYTHONPATH=ROOT, PYTHONIOENCODING='utf-8')
    return subprocess.run(
        [sys.executable, '-X', 'utf8', os.path.join(ROOT, 'py_router', 'check_drc.py'),
         board_path, *argv],
        capture_output=True, text=True, env=env, cwd=ROOT)


class TestAdaptivePrimitiveCircle(unittest.TestCase):
    def _drc_at(self, gap):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, 'b.kicad_pcb')
            with open(p, 'w', encoding='utf-8') as f:
                f.write(board_with_gap(gap))
            return run_drc(p, '-c', '0.09', '--clearance-margin', '0',
                           '--max-print', '0')

    def test_sub_floor_gap_is_flagged(self):
        # The run-3 geometry: true gap 0.0884 vs floor 0.09 (1.6um short).
        r = self._drc_at(0.0884)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn('PAD-PAD', r.stdout)

    def test_above_floor_gap_stays_clean(self):
        # Inscribed polygons cannot manufacture phantom grazes: 6um of margin
        # is far above the 1um sagitta budget.
        r = self._drc_at(0.096)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('NO DRC VIOLATIONS', r.stdout)

    def test_polygon_radial_error_budget(self):
        from kicad_parser import _adaptive_circle_n
        # 1um sagitta wherever the 64-vertex cap does not bite (r <= ~0.9)...
        for r in (0.2, 0.5, 0.8):
            n = _adaptive_circle_n(r)
            sagitta = r * (1 - math.cos(math.pi / n))
            self.assertLessEqual(sagitta, 0.001 + 1e-9, (r, n, sagitta))
        # ...and bounded growth past it: at the cap the sagitta is r*0.0012,
        # so a r=1mm jumper-pad circle stays within ~1.3um.
        self.assertLessEqual(_adaptive_circle_n(10.0), 64)
        s1 = 1.0 * (1 - math.cos(math.pi / _adaptive_circle_n(1.0)))
        self.assertLessEqual(s1, 0.0013)


class TestRun3ArchivedBoard(unittest.TestCase):
    def test_the_shipped_graze_is_now_visible(self):
        """wk/run3/final2.kicad_pcb is the archived pre-fix board carrying the
        /ICE_CDONE-vs-JP2.2 0.0884mm graze only kicad-cli saw. It is the
        regression artifact: check_drc at margin 0 must now report it."""
        if not os.path.exists(RUN3_BOARD):
            self.skipTest('run-3 archive board not present')
        r = run_drc(RUN3_BOARD, '-c', '0.09', '--clearance-margin', '0',
                    '--max-print', '0')
        self.assertEqual(r.returncode, 1, r.stdout[-800:])
        self.assertRegex(r.stdout, r'ICE_CDONE.*SWDIO|SWDIO.*ICE_CDONE')


if __name__ == '__main__':
    unittest.main()
