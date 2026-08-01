#!/usr/bin/env python3
"""A ripped net must come back with the GEOMETRY it had, not just the width.

`route_disconnected_planes` rips blocking nets to reach boxed-in plane pads, and
then re-routes them. That re-route already preserved per-net WIDTH (#513 item 5).
It did not preserve anything else, because `batch_route` takes ONE layer list per
call and the reconnect passed every casualty through a single call using the
run's global routing layers.

The consequence, measured: a net the caller had pinned to one layer -- a crystal
leg under a zero-via rule -- was re-routed onto the opposite layer with 5 vias.
No CLI flag could prevent it, because the parameters did not exist on the
function at all: `track_width_floor`, `layer_costs`, `via_cost`, `impedance`,
`ordering_strategy` and `length_match_groups` are all in `batch_route`'s
signature and none of them was in `route_planes`'.

The fix batches the reconnect by allowed-layer-set, which needs no change to the
A*: each group is one `batch_route` call with its own `layers`, and a
single-layer group cannot emit a via by construction -- the same guarantee
`route.py --layers F.Cu` already gives a caller who can reach the command line.
"""
import inspect
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import route_disconnected_planes as rdp  # noqa: E402
from route_disconnected_planes import reconnect_batches  # noqa: E402


def test_no_restrictions_is_exactly_one_batch():
    """A board that declares nothing must behave as before: one call."""
    g = reconnect_batches(['A', 'B', 'C'], None, ['F.Cu', 'B.Cu'])
    assert g == {('F.Cu', 'B.Cu'): ['A', 'B', 'C']}, g
    assert reconnect_batches(['A'], {}, ['F.Cu']) == {('F.Cu',): ['A']}
    print("  PASS: undeclared nets stay in one default batch")


def test_a_pinned_net_gets_its_own_batch():
    g = reconnect_batches(['XTAL', 'SIG'], {'XTAL': ['F.Cu']}, ['F.Cu', 'B.Cu'])
    assert g[('F.Cu',)] == ['XTAL'], g
    assert g[('F.Cu', 'B.Cu')] == ['SIG'], g
    print("  PASS: a pinned net is routed in its own single-layer batch")


def test_nets_sharing_a_restriction_share_a_batch():
    """Grouping, not one-call-per-net: a bus pinned to a layer routes together,
    so its members can still see each other's copper."""
    g = reconnect_batches(['D0', 'D1', 'OTHER'],
                          {'D0': ['F.Cu'], 'D1': ['F.Cu']}, ['F.Cu', 'B.Cu'])
    assert g[('F.Cu',)] == ['D0', 'D1'], g
    assert len(g) == 2, g
    print("  PASS: a pinned bus routes as one batch, not one call per net")


def test_a_single_layer_batch_cannot_produce_a_via():
    """The guarantee is structural, not a heuristic: with one layer in the list
    there is no second layer for a via to reach."""
    g = reconnect_batches(['XTAL'], {'XTAL': ['F.Cu']}, ['F.Cu', 'B.Cu'])
    ((layers, names),) = g.items()
    assert len(layers) == 1 and names == ['XTAL'], g
    print("  PASS: single-layer batch -> a via has nowhere to go")


def test_the_parameters_now_exist_on_the_function():
    """The original defect was not a dropped argument -- it was a missing
    parameter. Nothing the caller passed could reach the re-route."""
    params = inspect.signature(rdp.route_planes).parameters
    for p in ('net_layers', 'track_width_floor'):
        assert p in params, f"route_planes still cannot accept {p}"
    print("  PASS: net_layers and track_width_floor reach route_planes")


def test_the_flags_are_on_the_cli():
    r = subprocess.run([sys.executable, '-X', 'utf8',
                        os.path.join(ROOT, 'route_disconnected_planes.py'), '--help'],
                       capture_output=True, text=True, encoding='utf-8',
                       errors='replace', cwd=ROOT)
    assert r.returncode == 0
    for flag in ('--net-layers', '--track-width-floor'):
        assert flag in r.stdout, f"{flag} missing from --help"
    print("  PASS: --net-layers and --track-width-floor are on the CLI")


def test_the_reconnect_actually_uses_the_batches():
    """Guard against the helper existing but the call site ignoring it."""
    src = inspect.getsource(rdp.route_planes)
    assert 'reconnect_batches(' in src, "route_planes no longer batches the reconnect"
    i = src.index('reconnect_batches(')
    tail = src[i:i + 2500]
    assert 'for _glay, _gnames in _groups.items():' in tail, \
        "the batches are computed but not iterated"
    assert 'layers=list(_glay)' in tail, \
        "each batch must route with ITS OWN layer list"
    assert 'track_width_floor=track_width_floor' in tail, \
        "the floor must be forwarded into the reconnect"
    print("  PASS: each batch routes with its own layers and carries the floor")


if __name__ == '__main__':
    for k, v in sorted(globals().items()):
        if k.startswith('test_'):
            print(f"--- {k}")
            v()
    print("ALL PASS")
