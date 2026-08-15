#!/usr/bin/env python3
"""#610: --impedance was silently overridden by the --track-width floor.

With --track-width OMITTED, impedance-solved widths must floor at the fab
tier's track minimum (impedance_width_floor), not the resolved default track
width -- and any clamp that still fires must land in clamp_report so the
engines can surface it in JSON_SUMMARY.

Run directly (no pytest needed, matching the rest of tests/):
    python3 tests/test_610_impedance_width_floor.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'py_router'))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'py_tools'))

import impedance as I                                          # noqa: E402
from fab_tiers import (fab_floors, set_default_fab_tier,       # noqa: E402
                       get_default_fab_tier)

FAILURES = []


def check(cond, label, detail=""):
    if cond:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}  {detail}")
        FAILURES.append(label)


def close(a, b, tol):
    return abs(a - b) <= tol


# The reporter's #607/#610 stackup shape: 6-layer, thin prepreg over a thick
# core, where the asymmetric-stripline fix makes inner widths NARROW (0.13mm
# class) and every solved width falls under the 0.3mm default track width.
def make_pcb():
    class SL:
        def __init__(self, name, ltype, thick, eps=0.0):
            self.name, self.layer_type, self.thickness = name, ltype, thick
            self.epsilon_r, self.loss_tangent, self.material = eps, 0.0, ''

    class BI:
        stackup = [SL('F.Cu', 'copper', 0.035), SL('p1', 'prepreg', 0.1, 4.3),
                   SL('In1.Cu', 'copper', 0.0152), SL('c1', 'core', 0.55, 4.3),
                   SL('In2.Cu', 'copper', 0.0152), SL('p2', 'prepreg', 0.1, 4.3),
                   SL('B.Cu', 'copper', 0.035)]
        copper_layers = ['F.Cu', 'In1.Cu', 'In2.Cu', 'B.Cu']

    class PCB:
        board_info = BI()

    return PCB()


def test_floor_derivation():
    print("impedance_width_floor")
    # Explicit --track-width: honored verbatim, whatever the tier says.
    w, desc = I.impedance_width_floor(0.3, False, 6)
    check(w == 0.3, "explicit width is the floor, verbatim")
    check("--track-width" in desc, "explicit floor names the flag", desc)

    prev = get_default_fab_tier()
    try:
        set_default_fab_tier('standard')
        w4, desc4 = I.impedance_width_floor(0.3, True, 6)
        check(close(w4, fab_floors(6)['track_width'], 1e-12),
              "omitted width -> multilayer standard-tier track floor",
              f"{w4}")
        check(w4 < 0.1, "multilayer standard floor is sub-0.1mm", f"{w4}")
        check("fab-tier" in desc4, "derived floor names the fab tier", desc4)

        w2, _ = I.impedance_width_floor(0.3, True, 2)
        check(close(w2, fab_floors(2)['track_width'], 1e-12),
              "2-layer boards use the 2-layer floor", f"{w2}")

        set_default_fab_tier('advanced')
        wa, _ = I.impedance_width_floor(0.3, True, 6)
        check(wa < w4, "advanced tier lowers the derived floor",
              f"{wa} vs {w4}")

        # 0 copper layers (no board info) falls back to the multilayer bucket
        # rather than crashing.
        set_default_fab_tier('standard')
        w0, _ = I.impedance_width_floor(0.3, True, 0)
        check(close(w0, fab_floors(4)['track_width'], 1e-12),
              "unknown layer count -> multilayer floor", f"{w0}")
    finally:
        set_default_fab_tier(*prev)


def test_clamp_report_and_floor_effect():
    print("calculate_layer_widths_for_impedance clamp_report / #610 effect")
    pcb = make_pcb()
    layers = ['F.Cu', 'In1.Cu']

    # The pre-#610 behaviour: min_width = the 0.3 default. Every solved width
    # clamps, and clamp_report records [solved, floor] per layer.
    report = {}
    clamped = I.calculate_layer_widths_for_impedance(
        pcb, layers, 90.0, spacing=0.1, is_differential=True,
        min_width=0.3, clamp_report=report)
    check(all(close(v, 0.3, 1e-12) for v in clamped.values()),
          "0.3 floor clamps every solved width on this stackup",
          str(clamped))
    check(set(report) == set(layers),
          "clamp_report names every clamped layer", str(report))
    check(all(solved < floor and close(floor, 0.3, 1e-12)
              for solved, floor in report.values()),
          "clamp_report entries are [solved_mm, floor_mm]", str(report))

    # The #610 fix: the floor derived from the omitted flag (fab tier) lets
    # the impedance request keep its solved widths.
    prev = get_default_fab_tier()
    try:
        set_default_fab_tier('standard')
        floor, desc = I.impedance_width_floor(
            0.3, True, len(pcb.board_info.copper_layers))
    finally:
        set_default_fab_tier(*prev)
    report2 = {}
    solved = I.calculate_layer_widths_for_impedance(
        pcb, layers, 90.0, spacing=0.1, is_differential=True,
        min_width=floor, floor_desc=desc, clamp_report=report2)
    check(all(v < 0.3 for v in solved.values()),
          "fab-tier floor keeps the impedance-solved widths", str(solved))
    check(not report2, "no clamp fires at the fab-tier floor", str(report2))
    # clamp_report values are rounded to 4 decimals for the JSON summary.
    check(all(close(solved[l], report[l][0], 5e-5) for l in layers),
          "kept widths equal the widths the 0.3 floor threw away",
          f"{solved} vs {report}")


def main():
    for fn in (test_floor_derivation, test_clamp_report_and_floor_effect):
        fn()
    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}): " + ", ".join(FAILURES))
        return 1
    print("All #610 impedance-width-floor tests passed.")
    return 0


if __name__ == '__main__':
    sys.exit(main())
