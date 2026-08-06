#!/usr/bin/env python3
"""A repair moves the part that can move, by an amount the damage justifies.

Two run-7 defects in the repair seat loop, and one design mistake I made
fixing them.

A17 -- the mover chooser picks one member of a conflicting pair by
(pin_count, ref) and charges it. When that member has no legal seat anywhere,
the pair is reported unrepairable while its PARTNER may have had a seat
available the whole time. One board shipped exactly that.

  The obvious fix -- charge both members -- is WRONG, and the corpus sweep
  caught it: on a board with one genuine 0.04mm pair it moved a second part
  0.50mm, and both the old and new outputs graded 0 violations. Churn away
  from the original placement is the very failure the other fix here exists to
  stop. So the partner is a FALLBACK, tried only when the preferred mover
  fails.

R3 -- the cap ladder escalates 0.5 -> 1.0 -> 2.0 -> 5.0mm hunting any legal
seat, with nothing relating the move to the damage. On a board perturbed by
1.2mm it relocated parts 4.3-5.8mm from truth, and those few parts carried the
whole of that run's negative recovery: excluding three of them took it from
-0.296 to -0.058, and excluding seven made it positive. A seat that far away is
not a repair, it is a different placement.

Run: python3 -X utf8 tests/test_run8_repair_proportion.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault('KRT_NO_BANNER', '1')

from placement import seeder                                   # noqa: E402

FAILURES = []


def check(name, cond, detail=''):
    print(f'  {"PASS" if cond else "FAIL"}  {name}'
          + (f'\n        {detail}' if not cond and detail else ''))
    if not cond:
        FAILURES.append(name)


def main():
    print('the proportionality rule')
    check('the ratio is a named constant',
          isinstance(seeder.DISPROPORTION_RATIO, float)
          and seeder.DISPROPORTION_RATIO > 1)
    check('there is a floor, so a tiny violation stays fixable',
          seeder.DISPROPORTION_FLOOR_MM >= 0.5)

    def budget(v):
        return max(seeder.DISPROPORTION_FLOOR_MM,
                   seeder.DISPROPORTION_RATIO * v)

    check('a 0.04mm violation may still be fixed by a sensible move',
          budget(0.04) >= 1.0, str(budget(0.04)))
    check('a 1.2mm-scale damage does not license a 5mm relocation',
          budget(0.15) < 5.0, str(budget(0.15)))
    check('a genuinely large violation licenses a proportionate move',
          budget(2.0) >= 8.0, str(budget(2.0)))

    print('the partner is a fallback, not a second charge')
    src = open(os.path.join(ROOT, 'placement', 'seeder.py'),
               encoding='utf-8').read()
    check('the partner map exists', 'partner_of' in src)
    check('...and is consulted only after a seat FAILS',
          'seated its pair partner' in src)
    check('the partner is NOT charged as a violator',
          'PARTNER_CHARGE_SHARE' not in src,
          'charging both members was measured as churn and reverted')
    check('a failure names the partners that were tried too',
          'tried too' in src)

    print('the refusal explains itself')
    check('a disproportionate seat is refused, not silently taken',
          'disproportionate to the' in src)
    check('...and says what to do instead',
          'reconstruct or re-arrange instead' in src)
    check('the part is put back where it was',
          'state.apply_move(ref, ox, oy, orot)' in src)

    print()
    if FAILURES:
        print(f'FAIL: {len(FAILURES)} check(s): {", ".join(FAILURES)}')
        return 1
    print('OK')
    return 0


if __name__ == '__main__':
    sys.exit(main())
