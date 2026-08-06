#!/usr/bin/env python3
"""A custom pad's copper is its primitives, not a box around its anchor.

The parser stores a CUSTOM pad as a symmetric box about its anchor, sized to
enclose the real primitives. That is deliberate and safe for clearance work
(it over-blocks), and wrong for CONNECTIVITY, because the anchor is frequently
a corner of the copper rather than its centre.

Measured on a recorded board: a MOSFET tab whose real copper is 4.57 x 5.01mm
was modelled as 9.13 x 10.01mm -- four times the area -- with the box centre
3.4mm away from the copper centre. The connectivity graph then credited any
track passing within min(size)/4 = 2.28mm of the ANCHOR as landing on the pad,
and that disc sat largely in empty space. Two islands of one net each reached
the phantom disc, so the net graded CONNECTED with its copper in two pieces --
and the router skipped it as already routed. A silent open.

The exact-distance path was always right; it uses the polygons. Only the credit
disc was derived from the box. Now it comes from the primitives when they are
there.

Run: python3 -X utf8 tests/test_run8_custom_pad_credit.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault('KRT_NO_BANNER', '1')

from check_connected import _pad_credit_disc                   # noqa: E402

FAILURES = []


def check(name, cond, detail=''):
    print(f'  {"PASS" if cond else "FAIL"}  {name}'
          + (f'\n        {detail}' if not cond and detail else ''))
    if not cond:
        FAILURES.append(name)


class FakePad:
    def __init__(self, x, y, sx, sy, polygons=None):
        self.global_x, self.global_y = x, y
        self.size_x, self.size_y = sx, sy
        self.polygons = polygons or []


def main():
    print('an ordinary pad is unchanged')
    p = FakePad(10.0, 20.0, 1.0, 2.0)
    cx, cy, r = _pad_credit_disc(p)
    check('centre is the pad position', (cx, cy) == (10.0, 20.0))
    check('reach is a quarter of the short side', abs(r - 0.25) < 1e-9, str(r))

    print('a degenerate pad falls back to a small disc')
    cx, cy, r = _pad_credit_disc(FakePad(0.0, 0.0, 0.0, 0.0))
    check('no size gives a tiny reach, not zero', 0 < r <= 0.05, str(r))

    print('a custom pad uses its real primitives')
    # The measured shape: copper 4.57 x 5.01 whose centre is offset from the
    # anchor by (-2.28, -2.5); the parser's box would be 9.13 x 10.01.
    poly = [[(94.407, 78.963), (98.977, 78.963),
             (98.977, 83.973), (94.407, 83.973)]]
    custom = FakePad(98.972, 83.968, 9.13, 10.01, poly)
    cx, cy, r = _pad_credit_disc(custom)
    check('the disc moves onto the real copper',
          abs(cx - 96.692) < 0.01 and abs(cy - 81.468) < 0.01,
          f'({cx}, {cy})')
    check('...which is far from the anchor it used to sit on',
          abs(cx - custom.global_x) > 2.0)
    check('the disc shrinks to the real copper\'s quarter-width',
          abs(r - 4.57 / 4) < 0.01, str(r))
    check('...roughly half what the box gave',
          r < min(custom.size_x, custom.size_y) / 4)

    print('a point in the phantom region is no longer near the disc')
    import math
    # A point just past the anchor, well outside the real copper.
    ghost = (100.5, 85.5)
    old_c, old_r = (custom.global_x, custom.global_y), \
        min(custom.size_x, custom.size_y) / 4
    was_credited = math.hypot(ghost[0] - old_c[0], ghost[1] - old_c[1]) <= old_r
    now_credited = math.hypot(ghost[0] - cx, ghost[1] - cy) <= r
    check('the old box credited it', was_credited)
    check('the real copper does not', not now_credited)

    print()
    if FAILURES:
        print(f'FAIL: {len(FAILURES)} check(s): {", ".join(FAILURES)}')
        return 1
    print('OK')
    return 0


if __name__ == '__main__':
    sys.exit(main())
