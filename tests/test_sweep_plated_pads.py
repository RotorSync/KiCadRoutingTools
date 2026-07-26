#!/usr/bin/env python3
"""Issue #494: the fill-aware re-check sweep must not skip plated pads.

The sweep ("pad(s) reported tapped but still floating -- forcing a via")
iterates res['disconnected_pads'] and then skipped plated-through pads on
the premise "plated barrels are already plane-tied by the fill". Every pad
reaching that guard has JUST been reported still floating by the fill-aware
check, so the premise is false for exactly the population the pass looks at.
Split from #492, where the same contradiction was measured in the gate tap.

Two independent blocks, as in #492 -- fixing either alone leaves the pass
inert on plated pads:
  1. the `pad_is_plated_through` skip;
  2. the launch-layer resolution, which required a CONCRETE copper layer, so
     a '*.Cu' through-hole pad resolved to None and was skipped anyway.

Third: custody was per PAD, not per PAD-LAYER. check_net_connectivity lists
a plated pad once per copper layer, so a layer-less key let any one entry
veto a pad that was genuinely joined on the layer just repaired.

NPTH holes stay skipped -- no copper to tap at all (#328).

    python3 tests/test_sweep_plated_pads.py
"""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from route_disconnected_planes import (plane_tap_launch_layers,  # noqa: E402
                                       pad_still_floating)

LAYERS = ['F.Cu', 'In1.Cu', 'In2.Cu', 'B.Cu']
ZONES = ['In2.Cu']


def _pad(layers, drill=0.0, pad_type='smd'):
    return SimpleNamespace(layers=list(layers), drill=drill,
                           pad_type=pad_type)


def _placed(x, y, ref, layers, drill=0.0, pad_type='smd'):
    return SimpleNamespace(global_x=x, global_y=y, component_ref=ref,
                           layers=list(layers), drill=drill,
                           pad_type=pad_type)


def check(label, got, want):
    if got != want:
        print(f"FAIL: {label}: got {got}, want {want}")
        return 1
    print(f"  ok: {label} -> {got}")
    return 0


def main():
    fails = 0

    # --- launch layers ---------------------------------------------------
    fails += check("smd launches from its own layer",
                   plane_tap_launch_layers(_pad(['F.Cu', 'F.Mask']),
                                           ZONES, LAYERS),
                   ['F.Cu'])

    # THE #494 CASE: a plated barrel. Previously skipped twice over -- by the
    # plated guard, and by '*.Cu' failing the concrete-layer filter.
    fails += check("plated '*.Cu' prefers the net's zone layer",
                   plane_tap_launch_layers(
                       _pad(['*.Cu', '*.Mask'], drill=0.8,
                            pad_type='thru_hole'), ZONES, LAYERS),
                   ['In2.Cu'])

    # Multi-layer pour: all of them, in routing-layer order, so repeated
    # runs try the same layer first (determinism).
    fails += check("plated, multi-layer pour, routing order",
                   plane_tap_launch_layers(
                       _pad(['*.Cu'], drill=0.8, pad_type='thru_hole'),
                       ['B.Cu', 'F.Cu'], LAYERS),
                   ['F.Cu', 'B.Cu'])

    # Zone on no routing layer: still a real barrel, so offer every layer
    # rather than giving up.
    fails += check("plated, zone off routing layers -> all",
                   plane_tap_launch_layers(
                       _pad(['*.Cu'], drill=0.8, pad_type='thru_hole'),
                       ['Inner9.Cu'], LAYERS),
                   LAYERS)

    # NPTH has NO copper (#328) even though its layer list names '*.Cu'.
    fails += check("npth stays skipped",
                   plane_tap_launch_layers(
                       _pad(['*.Cu', '*.Mask'], drill=3.2,
                            pad_type='np_thru_hole'), ZONES, LAYERS),
                   [])

    fails += check("smd on a non-routing layer contributes nothing",
                   plane_tap_launch_layers(_pad(['In7.Cu']), ZONES, LAYERS),
                   [])

    # --- custody, per PAD-LAYER -----------------------------------------
    thru = _placed(131.11, 125.75, 'J4', ['*.Cu', '*.Mask'], drill=0.8,
                   pad_type='thru_hole')
    fails += check("plated: another layer must not veto the repaired layer",
                   pad_still_floating(
                       [(131.11, 125.75, 'F.Cu', 'J4')], thru, 'B.Cu'),
                   False)
    fails += check("plated: repaired layer still floating -> undo",
                   pad_still_floating(
                       [(131.11, 125.75, 'B.Cu', 'J4')], thru, 'B.Cu'),
                   True)

    smd = _placed(10.0, 20.0, 'C6', ['F.Cu'])
    fails += check("smd: own layer still floating -> undo",
                   pad_still_floating(
                       [(10.0, 20.0, 'F.Cu', 'C6')], smd, 'F.Cu'),
                   True)
    fails += check("smd: cleared -> keep",
                   pad_still_floating([], smd, 'F.Cu'), False)
    fails += check("a different component does not match",
                   pad_still_floating(
                       [(10.0, 20.0, 'F.Cu', 'C7')], smd, 'F.Cu'),
                   False)

    if fails:
        print(f"\n{fails} FAILURE(S)")
        return 1
    print("\nPASS: sweep plated pads (launch layers + custody)")
    return 0


if __name__ == '__main__':
    sys.exit(main())
