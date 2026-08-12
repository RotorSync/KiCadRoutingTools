#!/usr/bin/env python3
"""
Regression test for issue #625: the #529 dynamic-iteration extension had a
per-SEARCH ceiling but no AGGREGATE bound, so a board with many hopeless nets
multiplied million-iteration failed searches across retries, rip-up rounds and
rescue rungs into hours (core64_logic: 950 extended failures, ~590M extension
iterations, killed at the 3 h cap in every cloud arm).

This test proves the waste ledger:
  1. A fresh ledger still grants extension kwargs (the #529 win is preserved).
  2. FAILED searches are charged their overrun; crossing the per-net budget
     makes THAT net's later searches static while other nets keep extending.
  3. Crossing the run-total budget makes EVERY net static.
  4. reset_dynamic_waste_ledger() re-arms (the GUI's long-lived process).
  5. Caps set to 0 (env) restore the old unbounded behavior.
  6. Successful searches (iters <= base) are never charged, and probe-scale
     budgets (<= 10k) still never extend.

Run:
    python3 tests/test_625_dynamic_waste_ledger.py
"""

import os
import sys

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(TESTS_DIR)
sys.path.insert(0, ROOT_DIR)
sys.path.insert(0, os.path.join(ROOT_DIR, 'py_router'))  # #522
sys.path.insert(0, os.path.join(ROOT_DIR, 'rust_router'))

from dataclasses import replace

import env_knobs
import single_ended_routing as ser
from routing_config import GridRouteConfig


def _set_caps(per_net, total):
    os.environ['KICAD_DYNAMIC_WASTE_PER_NET'] = str(per_net)
    os.environ['KICAD_DYNAMIC_WASTE_TOTAL'] = str(total)
    env_knobs.refresh()


def main():
    os.environ.pop('KICAD_DYNAMIC_ITERATIONS', None)
    _set_caps(3_000_000, 30_000_000)
    ser.reset_dynamic_waste_ledger()
    cfg = GridRouteConfig(layers=['F.Cu', 'B.Cu'], max_iterations=200_000)

    # 1. Fresh ledger extends.
    base, kw = ser._dynamic_iterations(cfg, 42)
    assert base == 200_000 and kw.get('max_iterations_ceiling') == \
        ser.DYNAMIC_ITERATIONS_CEILING, (base, kw)

    # 2. Per-net budget: below it extends, above it goes static; foreign
    #    nets unaffected.
    ser._charge_dynamic_waste(42, 1_200_000, 200_000)      # 1.0M waste
    assert ser._dynamic_iterations(cfg, 42)[1], "below cap must extend"
    ser._charge_dynamic_waste(42, 2_500_000, 200_000)      # 3.3M total
    assert ser._dynamic_iterations(cfg, 42)[1] == {}, "over cap must be static"
    assert ser._dynamic_iterations(cfg, 43)[1], "other nets unaffected"

    # 3. Run-total budget stops every net.
    for n in range(100, 115):
        ser._charge_dynamic_waste(n, 2_200_000, 200_000)   # +30M
    assert ser._dynamic_iterations(cfg, 999)[1] == {}, "total cap is global"

    # 4. Reset re-arms.
    ser.reset_dynamic_waste_ledger()
    assert ser._dynamic_iterations(cfg, 42)[1], "reset must re-arm"

    # 5. Caps = 0 -> unbounded (old behavior).
    _set_caps(0, 0)
    ser._charge_dynamic_waste(42, 9_000_000, 200_000)
    assert ser._dynamic_iterations(cfg, 42)[1], "caps=0 means unlimited"
    _set_caps(3_000_000, 30_000_000)

    # 6. No charge for successes; probe-scale budgets never extend.
    ser.reset_dynamic_waste_ledger()
    ser._charge_dynamic_waste(50, 150_000, 200_000)
    assert ser._dyn_waste['total'] == 0, "successes are never charged"
    small = replace(cfg, max_iterations=10_000)
    assert ser._dynamic_iterations(small, 42) == (10_000, {})

    print("PASS: #625 dynamic-iteration waste ledger")


if __name__ == '__main__':
    main()
