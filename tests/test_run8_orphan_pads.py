#!/usr/bin/env python3
"""A pad is not a circle, and a trace end is not a point (run-7 A8).

check_orphan_stubs modelled every pad as a circle of diameter max(w, h) about
its centre and ignored the width of the trace whose endpoint it was testing.
Both approximations are wrong in both directions:

  * a square pad's corner copper lies OUTSIDE that circle, so a trace ending in
    the corner reads as a dead end;
  * a 1.5 x 0.9 pad's circle reaches 0.3mm past the long edges, so a stub that
    really does end in empty copper reads as connected;
  * the endpoint is the CENTRE of the trace's end cap, whose copper extends
    half a track width further -- one measured false orphan sat 0.01mm outside
    a pad its cap overlapped by 0.19mm.

A false orphan is not cosmetic: one drove a net split a watcher had to unpick.

Measured A/B on recorded run-7 boards (before -> after this fix):
    one board's final:      1 orphan  -> 0
    another board's repair: 3 orphans -> 1   (the real one is retained)

Run: python3 -X utf8 tests/test_run8_orphan_pads.py
"""
import math
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from check_orphan_stubs import _endpoint_connected, _point_in_pad  # noqa: E402

FAILURES = []


def check(name, cond, detail=''):
    print(f'  {"PASS" if cond else "FAIL"}  {name}'
          + (f'\n        {detail}' if not cond and detail else ''))
    if not cond:
        FAILURES.append(name)


def rect(cx, cy, w, h, rot=0.0):
    return (cx, cy, w, h, rot, 'rect')


def main():
    print('pad geometry')
    # A 2.5 x 2.5 pad at the origin. Its corner copper is at (1.25, 1.25),
    # distance 1.77 from the centre -- outside the old max(w,h)/2 = 1.25 circle.
    pad = rect(0.0, 0.0, 2.5, 2.5)
    check('a point in the corner copper of a square pad is inside it',
          _point_in_pad(1.2, 1.2, pad, 0.0))
    check('the old circle model would have missed it',
          math.hypot(1.2, 1.2) > 2.5 / 2)
    check('a point beyond the corner is outside',
          not _point_in_pad(1.4, 1.4, pad, 0.0))

    # A 1.5 x 0.9 pad: the old circle over-credited 0.3mm past the long edges.
    narrow = rect(0.0, 0.0, 1.5, 0.9)
    check('a point 0.2mm past the short edge is OUTSIDE the real pad',
          not _point_in_pad(0.0, 0.65, narrow, 0.0))
    check('the old circle model would have called it connected',
          0.65 < 1.5 / 2)
    check('a point inside the real pad is inside',
          _point_in_pad(0.6, 0.3, narrow, 0.0))

    # Rotation: the same pad turned 90 degrees swaps which axis is long.
    turned = rect(0.0, 0.0, 1.5, 0.9, 90.0)
    check('a rotated pad is tested in its own frame',
          _point_in_pad(0.0, 0.65, turned, 0.0)
          and not _point_in_pad(0.65, 0.0, turned, 0.0))

    # Round and oval pads keep their real shapes.
    check('a circular pad stays circular',
          _point_in_pad(0.49, 0.0, (0, 0, 1.0, 1.0, 0, 'circle'), 0.0)
          and not _point_in_pad(0.4, 0.4, (0, 0, 1.0, 1.0, 0, 'circle'), 0.0))
    oval = (0.0, 0.0, 2.0, 1.0, 0.0, 'oval')
    check('an oval pad is a capsule, not its bounding box',
          _point_in_pad(0.9, 0.0, oval, 0.0)
          and not _point_in_pad(0.95, 0.45, oval, 0.0))

    print('end-cap credit')
    # The measured case: a 0.4mm-wide trace ending 0.01mm outside the pad edge.
    pad = rect(0.0, 0.0, 2.5, 2.5)
    endpoint = (1.26, 0.0)                      # 0.01mm past the edge at 1.25
    check('a trace ending just outside a pad, cap overlapping, is connected',
          _endpoint_connected(endpoint, [], vias=[], ph_pads=[],
                              layer_pads=[pad], tol=0.0, end_half_width=0.2))
    check('...and without the cap credit it would read as an orphan',
          not _endpoint_connected(endpoint, [], vias=[], ph_pads=[],
                                  layer_pads=[pad], tol=0.0, end_half_width=0.0))
    # The checker must still find a REAL dead end: far enough that no copper
    # of any kind reaches it.
    check('a genuinely dangling stub is still an orphan',
          not _endpoint_connected((3.0, 3.0), [], vias=[], ph_pads=[],
                                  layer_pads=[pad], tol=0.05,
                                  end_half_width=0.2))
    print('vias')
    check('a via is still tested as a circle',
          _endpoint_connected((0.3, 0.0), [], vias=[(0.0, 0.0, 0.6)],
                              ph_pads=[], layer_pads=[], tol=0.01)
          and not _endpoint_connected((1.0, 0.0), [], vias=[(0.0, 0.0, 0.6)],
                                      ph_pads=[], layer_pads=[], tol=0.01))

    print()
    if FAILURES:
        print(f'FAIL: {len(FAILURES)} check(s): {", ".join(FAILURES)}')
        return 1
    print('OK')
    return 0


if __name__ == '__main__':
    sys.exit(main())
