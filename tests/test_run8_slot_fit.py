#!/usr/bin/env python3
"""The hole-pattern fit, on the patterns real boards actually use.

Two defects, both of which made the fit silently find nothing.

PER-AXIS CONFORMANCE. The grouping demanded `abs(inset_x - inset_y) <= 2*tol`
-- a SQUARE inset. A hole 7.62mm in from one edge and 1.27mm from the other is
just an imperial layout, and it never conformed, so the whole fit returned {}
and the ladder's first vector source produced nothing. Conformance is now
per-axis: the two insets must agree across HOLES, not with each other.

AT-HOME SURVIVORS. A hole already sitting on the fitted pattern was offered
every free corner as a candidate. That is the degeneracy that once shipped two
mounting holes at each other's corners -- mechanically equivalent, and worth
~0.16 of recovery. Its conformance still over-determines the fit; it is simply
not a candidate for being moved.

Run: python3 -X utf8 tests/test_run8_slot_fit.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault('KRT_NO_BANNER', '1')

from placement import reconstruct                              # noqa: E402

FAILURES = []


def check(name, cond, detail=''):
    print(f'  {"PASS" if cond else "FAIL"}  {name}'
          + (f'\n        {detail}' if not cond and detail else ''))
    if not cond:
        FAILURES.append(name)


class P:
    def __init__(self, x, y, tht=True):
        self.x, self.y, self.has_tht = x, y, tht


class S:
    """The slice of a placement state the fit reads."""
    def __init__(self, board, parts):
        self.board, self.parts = board, parts


class T:
    def __init__(self, zero_net, locked=()):
        self.zero_net, self.locked = set(zero_net), set(locked)


BOARD = (0.0, 0.0, 100.0, 80.0)


def main():
    print('a SQUARE inset pattern still fits (the case that always worked)')
    parts = {'H1': P(3.0, 3.0), 'H2': P(97.0, 3.0), 'H3': P(3.0, 77.0),
             'H4': P(50.0, 40.0)}          # H4 displaced into the middle
    got = reconstruct.fit_corner_insets(S(BOARD, parts), T(parts))
    check('the displaced hole gets the free corner', 'H4' in got, str(got))
    if 'H4' in got:
        check('...at the fitted inset', (97.0, 77.0) in got['H4'], str(got['H4']))

    print('an ASYMMETRIC inset pattern now fits too')
    # 7.62 in x, 1.27 in y: an ordinary imperial layout, and square-inset
    # conformance rejected it outright.
    parts = {'H1': P(7.62, 1.27), 'H2': P(92.38, 1.27), 'H3': P(7.62, 78.73),
             'H4': P(50.0, 40.0)}
    got = reconstruct.fit_corner_insets(S(BOARD, parts), T(parts))
    check('the fit is no longer blind to a rectangular inset', 'H4' in got,
          str(got))
    if 'H4' in got:
        check('...and proposes the remaining corner at BOTH insets',
              (92.38, 78.73) in got['H4'], str(got['H4']))

    print('a hole already at home is not offered somewhere else')
    parts = {'H1': P(3.0, 3.0), 'H2': P(97.0, 3.0), 'H3': P(3.0, 77.0),
             'H4': P(97.0, 77.0)}          # the complete, correct pattern
    got = reconstruct.fit_corner_insets(S(BOARD, parts), T(parts))
    check('a complete pattern proposes nothing at all', not got, str(got))

    print('a locked hole is never proposed')
    parts = {'H1': P(3.0, 3.0), 'H2': P(97.0, 3.0), 'H3': P(3.0, 77.0),
             'H4': P(50.0, 40.0)}
    got = reconstruct.fit_corner_insets(S(BOARD, parts), T(parts, locked={'H4'}))
    check('the locked displaced hole stays where it is', 'H4' not in got,
          str(got))

    print('too little evidence proposes nothing')
    parts = {'H1': P(3.0, 3.0), 'H2': P(50.0, 40.0)}
    got = reconstruct.fit_corner_insets(S(BOARD, parts), T(parts))
    check('one survivor does not over-determine a pattern', not got, str(got))

    print()
    if FAILURES:
        print(f'FAIL: {len(FAILURES)} check(s): {", ".join(FAILURES)}')
        return 1
    print('OK')
    return 0


if __name__ == '__main__':
    sys.exit(main())
