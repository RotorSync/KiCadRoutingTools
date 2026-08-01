#!/usr/bin/env python3
"""`broken` must ship a WORK LIST, not just a count (Step 9, 9.1a-ii).

`unrouted` is actionable from names alone -- the net has no copper, route it.
A `broken` net is not: acting on it needs WHICH net, HOW MANY pieces, and WHERE
the stranded pads are. All three were already parsed out of check_connected's
output and then dropped on the floor, so `blocking_by.broken: N` was a number
with nothing behind it.

Measured consequence on a real run: the loop drove `unrouted` 4 -> 0 using
`net_widths`' per-net detail as its model, and left `broken` at 14 across two
iterations because it had no equivalent to work from.
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, '.claude', 'skills', 'plan-pcb-routing', 'scripts'))

import board_score as bs                                        # noqa: E402

# A check_connected report with one clean net, one unrouted net and two broken
# nets of different severity -- including a stranded pad carrying a REF, which is
# what lets a caller tell a do-not-fit part from a real defect.
SAMPLE = """Found 1330 segments, 108 vias, 210 pads, 1 zones
Checking 41 routed nets
FOUND 3 ISSUES:
  Unrouted nets (1):
    QSPI_SD1 (2 pads)

  Connectivity issues (2):

  FLASH_CS (net 2):
    Segments: 9, Vias: 0, Pads: 3
    Disconnected components: 2
    Disconnected pads:
      (120.49, 78.50) on F.Cu [R1]

  GND (net 4):
    Segments: 618, Vias: 38, Pads: 55
    Disconnected components: 5
    Disconnected pads:
      (137.72, 66.05) on F.Cu [SW1]
      (146.00, 70.50) on F.Cu [J2]
      ... and 15 more
"""


def _parse(monkey_output):
    """Drive score_connectivity against a canned check_connected report."""
    real = bs.run_tool
    bs.run_tool = lambda root, tool, board, *a, **k: (1, monkey_output)
    try:
        return bs.score_connectivity('.', 'board.kicad_pcb')
    finally:
        bs.run_tool = real


def test_broken_carries_names_pieces_and_stranded_pads():
    conn = _parse(SAMPLE)
    d = conn['broken_detail']
    assert set(d) == {'FLASH_CS', 'GND'}, \
        f'broken nets must be named, got {sorted(d)}'
    assert d['GND']['components'] == 5 and d['GND']['joins_needed'] == 4
    assert d['FLASH_CS']['components'] == 2 and d['FLASH_CS']['joins_needed'] == 1
    print('  PASS: broken nets carry names and piece counts')


def test_joins_needed_sums_to_the_blocking_count():
    """The list must be COMPLETE -- otherwise sorting by it hides work."""
    conn = _parse(SAMPLE)
    total = sum(v['joins_needed'] for v in conn['broken_detail'].values())
    assert total == conn['broken'], \
        f'joins_needed sums to {total} but blocking_by.broken is {conn["broken"]}'
    print(f'  PASS: joins_needed sums to blocking_by.broken ({total})')


def test_stranded_pads_carry_the_ref_that_classifies_the_break():
    """A break on a DNF part and a break on the MCU are the same NUMBER and
    completely different work. The ref is the only thing that separates them."""
    conn = _parse(SAMPLE)
    fc = conn['broken_detail']['FLASH_CS']['stranded_pads']
    assert fc and fc[0]['ref'] == 'R1', f'stranded pad ref missing: {fc}'
    assert fc[0]['layer'] == 'F.Cu' and abs(fc[0]['x'] - 120.49) < 1e-9
    gnd = {p['ref'] for p in conn['broken_detail']['GND']['stranded_pads']}
    assert gnd == {'SW1', 'J2'}, f'expected SW1/J2, got {gnd}'
    print('  PASS: stranded pads carry ref, layer and coordinates')


def test_unrouted_names_are_separate_from_broken_names():
    """connectivity_nets is the UNION, so it cannot drive either lever."""
    conn = _parse(SAMPLE)
    assert conn['unrouted_net_names'] == ['QSPI_SD1']
    assert 'QSPI_SD1' not in conn['broken_detail']
    assert set(conn['nets']) == {'QSPI_SD1', 'FLASH_CS', 'GND'}
    print('  PASS: unrouted and broken names are separable')


def test_a_fully_connected_board_reports_empty_lists_not_missing_keys():
    conn = _parse('ALL NETS FULLY CONNECTED\n')
    assert conn['broken'] == 0 and conn['unrouted'] == 0
    print('  PASS: a clean board is clean')


if __name__ == '__main__':
    for k, v in sorted(globals().items()):
        if k.startswith('test_'):
            print(f'--- {k}')
            v()
    print('ALL PASS')
