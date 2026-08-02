#!/usr/bin/env python3
"""The #517 IMMEDIATE reconnect must honor --net-layers and the width floor.

The defect this guards (test-board run 5, journal [10]): route_disconnected_planes
has TWO reconnect paths. The end-of-run batch groups casualties by allowed-layer
set (reconnect_batches) and carries track_width_floor; the immediate
_reconnect_ripped_now closure -- the DEFAULT path for rip queues <= 12 -- routed
every casualty across the full layer list in one call with no floor. Measured:
USB_DP, ripped for a plane tap, returned with 2 vias and B.Cu segments despite an
F.Cu --net-layers pin, and the repair's thin reconnects are where a 0.4mm rail's
sub-width segments came from. Its docstring claimed parity it did not have.

This is a source-level pin (precedent: test_json_out_summary.py's inspect-based
check): the closure is not importable, so the assertion slices its block out of
the file text and requires both the batching call and the floor to be present in
the batch_route invocation.
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
SRC = os.path.join(ROOT, 'route_disconnected_planes.py')


def _closure_block():
    lines = open(SRC, encoding='utf-8').read().splitlines(keepends=True)
    start = next(i for i, ln in enumerate(lines)
                 if ln.startswith('    def _reconnect_ripped_now('))
    # The closure body is every following line blank or indented deeper than
    # its own 4-space def line.
    end = start + 1
    while end < len(lines) and (not lines[end].strip()
                                or lines[end].startswith('        ')):
        end += 1
    return ''.join(lines[start:end])


def test_immediate_reconnect_batches_by_layer_and_floors():
    block = _closure_block()
    assert 'reconnect_batches(' in block, \
        "immediate reconnect no longer groups by allowed-layer set"
    assert re.search(r'layers\s*=\s*list\(_glay\)', block), \
        "immediate reconnect does not route each group on its own layer set"
    assert 'track_width_floor=track_width_floor' in block, \
        "immediate reconnect drops the width floor again"
    assert not re.search(r'layers\s*=\s*routing_layers\b', block), \
        "a batch_route call in the immediate reconnect still uses the full layer list"
    print("  PASS: immediate reconnect batches by net_layers and carries the floor")


if __name__ == '__main__':
    test_immediate_reconnect_batches_by_layer_and_floors()
