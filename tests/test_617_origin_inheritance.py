#!/usr/bin/env python3
"""#617 follow-up: the declared copper-to-hole floor must survive a CHAIN.

`resolve_hole_clearance` used to read only `design_settings.rules`. That value
is not durable: every DRC writeback clamps it down to the clearance the step
routed at, so from step 2 onward the author's declaration is gone from the only
place the engine looked -- silently, because the relaxed value is a perfectly
plausible number.

Measured on a tigard pour+route chain whose project declared 0.25:

    step   rules.min_hole_clearance   fab_floor_origin.min_hole_clearance
    in     0.25                       (none yet)
    pour   0.15                       0.25
    out    0.1375                     0.25

so the route step read 0.15 -- below the 0.20 fab floor -- and every #616/#617
site went inert on a board that had asked for 0.25.

The original survives in `kicad_routing_tools.fab_floor_origin`, seeded on the
first writeback and carried down with the project (the record
`_fab_floor_disclosure` already compares against). This pins that the engine
prefers the LARGER of the two, and that it stays raise-only.

Standalone, no pytest:  python3 tests/test_617_origin_inheritance.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                '..', 'py_router'))

import obstacle_map                                              # noqa: E402
from obstacle_map import resolve_hole_clearance                   # noqa: E402
from fix_kicad_drc_settings import declared_fab_floor             # noqa: E402

_checks = 0
_fails = 0


def check(cond, label):
    global _checks, _fails
    _checks += 1
    if cond:
        print(f"  PASS  {label}")
    else:
        _fails += 1
        print(f"  FAIL  {label}")


class _PCB:
    """Minimal stand-in: the helper only reads `source_path`."""

    def __init__(self, path):
        self.source_path = path


def _stage(tmp, name, rules_value, origin_value):
    """Write a board + sibling project with the given rule / origin values."""
    pcb = os.path.join(tmp, name + '.kicad_pcb')
    with open(pcb, 'w', encoding='utf-8') as fh:
        fh.write('(kicad_pcb (version 20240108))\n')
    proj = {"board": {"design_settings": {"rules": {}}}}
    if rules_value is not None:
        proj["board"]["design_settings"]["rules"]["min_hole_clearance"] = rules_value
    if origin_value is not None:
        proj["kicad_routing_tools"] = {
            "fab_floor_origin": {"min_hole_clearance": origin_value}}
    with open(os.path.join(tmp, name + '.kicad_pro'), 'w', encoding='utf-8') as fh:
        json.dump(proj, fh)
    return pcb


def _resolve(pcb):
    """Resolve with the per-path caches cleared, so each case is independent."""
    obstacle_map._HOLE_CLR_CACHE.clear()
    obstacle_map._HOLE_CLR_ANNOUNCED.clear()
    obstacle_map._HOLE_CLR_ORIGIN.clear()
    return resolve_hole_clearance(_PCB(pcb), None)


def main():
    with tempfile.TemporaryDirectory() as tmp:
        print("declared_fab_floor -- the reader")
        p = _stage(tmp, 'origin', 0.15, 0.25)
        check(declared_fab_floor(p, 'min_hole_clearance') == 0.25,
              "reads fab_floor_origin off the sibling project")
        check(declared_fab_floor(p, 'min_track_width') is None,
              "absent key -> None (not 0.0, which would read as 'declared none')")
        p_noorigin = _stage(tmp, 'noorigin', 0.25, None)
        check(declared_fab_floor(p_noorigin, 'min_hole_clearance') is None,
              "no origin recorded -> None (rules IS the original at step 0)")
        check(declared_fab_floor(os.path.join(tmp, 'nope.kicad_pcb'),
                                 'min_hole_clearance') is None,
              "no project file -> None, no raise")

        print("\nresolve_hole_clearance -- THE CHAIN CASE (the bug)")
        # Exactly the measured tigard chain: pour relaxed the rule to 0.15,
        # origin still carries the 0.25 the author declared.
        p_chain = _stage(tmp, 'chain', 0.15, 0.25)
        v = _resolve(p_chain)
        check(v == 0.25,
              f"step-2 board (rules 0.15, origin 0.25) resolves 0.25, not 0.15 (got {v})")
        check(p_chain in obstacle_map._HOLE_CLR_ORIGIN,
              "and it is RECORDED as origin-derived, so the banner can say so")

        print("\nraise-only -- the guarantee that keeps this inert elsewhere")
        p_step0 = _stage(tmp, 'step0', 0.25, None)
        check(_resolve(p_step0) == 0.25,
              "step-0 board with no origin is unchanged (0.25 from rules)")
        p_lower = _stage(tmp, 'lower', 0.25, 0.15)
        v = _resolve(p_lower)
        check(v == 0.25,
              f"an origin BELOW the rule never lowers the rule (got {v})")
        check(p_lower not in obstacle_map._HOLE_CLR_ORIGIN,
              "and that case is not flagged origin-derived")
        p_silent = _stage(tmp, 'silent', 0.15, 0.15)
        check(_resolve(p_silent) == 0.15,
              "a board under the fab floor on both sources stays under it")
        p_none = _stage(tmp, 'none', None, None)
        check(_resolve(p_none) == 0.0,
              "a board declaring nothing resolves 0.0 (declares none)")

        print("\nan explicit config override still wins and stops the read")

        class _Cfg:
            hole_clearance = 0.40

        obstacle_map._HOLE_CLR_CACHE.clear()
        check(resolve_hole_clearance(_PCB(p_chain), _Cfg()) == 0.40,
              "config.hole_clearance outranks both board sources")

    print(f"\n{_checks - _fails}/{_checks} checks passed")
    return 1 if _fails else 0


if __name__ == '__main__':
    sys.exit(main())
