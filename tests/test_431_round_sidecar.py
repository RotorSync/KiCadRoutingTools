"""Per-round sidecars make `place_route_loop`'s work dir a real chain (#431).

The boards alone are NOT one. A REJECTED `loop_round2.kicad_pcb` sits between
rounds 1 and 3 in both name and mtime order, so anything that walks the
directory animates it as though it had been kept, and shows a "before" board the
round was never derived from. `cur_file` only advances on ACCEPT, so round N's
parent is the last accepted board, not N-1.

Observed on a real 3-round run (lvds_converter_dualclk forced into congestion):

    round 0  accepted   parent None        failures 5
    round 1  REJECTED   parent round0      failures 6
    round 2  accepted   parent round0   <- NOT round1
    round 3  REJECTED   parent round2

`metrics_from_summary` is covered here too: it was split out of `run_route` so a
renderer can caption a recorded round from its log with the SAME arithmetic
rather than a second implementation that drifts.
"""

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import place_route_loop as L  # noqa: E402
from kicad_parser import parse_kicad_pcb  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOARD = os.path.join(ROOT, 'kicad_files', 'lvds_converter_dualclk.kicad_pcb')

# One merged JSON_SUMMARY, shaped like route.py's.
SUMMARY = {
    'failed_single': ['/A'],
    'failed_multipoint': [{'net_name': '/B', 'failed_pads': []}],
    'multipoint_pads_total': 5, 'multipoint_pads_connected': 3,
    'total_iterations': 1234, 'total_vias': 7,
    'blockers': [{'net': '/A', 'stage': 'phase3',
                  'blocked_by': [{'net': 'GND'}, {'net': '+3V3'}]}],
    'pad_pairs_connected': 9, 'pad_pairs_total': 11,
}


def test_metrics_from_summary_matches_the_loops_own_arithmetic():
    m = L.metrics_from_summary(SUMMARY)
    # 1 failed single + (5 - 3) multipoint pad deficit
    assert m['failures'] == 3, m
    assert m['failed_nets'] == ['/A', '/B'], "multipoint dicts must flatten to names"
    assert m['blockers'] == ['+3V3', 'GND'], "sorted, de-duped, from the #409 key"
    assert (m['iterations'], m['vias']) == (1234, 7)
    assert (m['pad_pairs_connected'], m['pad_pairs_total']) == (9, 11)


def test_empty_blockers_list_does_not_regress_to_log_scraping():
    """An explicit [] means "structured emitter, nothing left to attribute".
    Falling back to the regex there would scrape transient blocks of nets that
    later routed -- the exact bug the #409 key was added to fix."""
    s = dict(SUMMARY, blockers=[])
    noisy = "  1. /MD1: 46 (31.7%) foo\n  2. /MD2: 12 (8.0%) bar\n"
    assert L.metrics_from_summary(s, noisy)['blockers'] == []


def test_pre_409_logs_still_fall_back_to_the_regex():
    s = {k: v for k, v in SUMMARY.items() if k != 'blockers'}
    got = L.metrics_from_summary(s, "  1. /MD1: 46 (31.7%) foo\n")
    assert got['blockers'] == ['/MD1']


def test_run_route_is_now_a_one_line_caller():
    """If someone re-inlines the body, the renderer and the loop start drifting."""
    import inspect
    src = inspect.getsource(L.run_route)
    # arg list left open: #549 added extra_targets and the pin broke
    assert 'return metrics_from_summary(summary, log' in src
    assert 'failed_multipoint' not in src, "the parsing belongs in the pure function"


def test_sidecar_records_the_parent_not_the_previous_round():
    """The load-bearing field. Round 2 follows a REJECTED round 1, so its parent
    is round 0 -- which is also the board its ghosts/arrows must be drawn against."""
    d = tempfile.mkdtemp()
    try:
        L.write_round_sidecar(d, 0, board='loop_round0.kicad_pcb',
                              routed='loop_round0_routed.kicad_pcb',
                              parent=None, accepted=True, metrics={'failures': 5})
        L.write_round_sidecar(d, 1, board='loop_round1.kicad_pcb',
                              routed='loop_round1_routed.kicad_pcb',
                              parent='loop_round0.kicad_pcb', accepted=False,
                              metrics={'failures': 6})
        L.write_round_sidecar(d, 2, board='loop_round2.kicad_pcb',
                              routed='loop_round2_routed.kicad_pcb',
                              parent='loop_round0.kicad_pcb', accepted=True,
                              metrics={'failures': 3})
        docs = {}
        for n in (0, 1, 2):
            with open(os.path.join(d, f'loop_round{n}.json'), encoding='utf-8') as f:
                docs[n] = json.load(f)
        assert docs[2]['parent'] == 'loop_round0.kicad_pcb', \
            "round 2 must point past the rejected round 1"
        assert docs[1]['accepted'] is False and docs[2]['accepted'] is True
        assert docs[0]['parent'] is None
        assert all(x['schema'] == 1 for x in docs.values())
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_screened_round_is_distinguishable_from_a_crash():
    """A screened round writes no board, so without a record a consumer sees a
    gap in the numbering and cannot tell "screened" from "the tool died"."""
    d = tempfile.mkdtemp()
    try:
        L.write_round_sidecar(d, 4, board=None, routed=None,
                              parent='loop_round2.kicad_pcb', accepted=False,
                              screened=True, targets=['R1', 'C2'])
        with open(os.path.join(d, 'loop_round4.json'), encoding='utf-8') as f:
            doc = json.load(f)
        assert doc['screened'] is True and doc['board'] is None
        assert doc['targets'] == ['C2', 'R1'], "targets must be sorted (#457)"
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_sidecar_is_deterministic():
    """It feeds a renderer and a movie; unsorted keys would make two identical
    runs produce different artifacts (#457)."""
    d = tempfile.mkdtemp()
    try:
        kw = dict(board='b.kicad_pcb', routed='r.kicad_pcb', parent='p.kicad_pcb',
                  accepted=True, targets=['R2', 'R1'],
                  groups={'sheet:b': ['Z1'], 'sheet:a': ['C2', 'C1']},
                  metrics={'failures': 1})
        L.write_round_sidecar(d, 1, **kw)
        a = open(os.path.join(d, 'loop_round1.json'), encoding='utf-8').read()
        L.write_round_sidecar(d, 1, **kw)
        b = open(os.path.join(d, 'loop_round1.json'), encoding='utf-8').read()
        assert a == b
        doc = json.loads(a)
        assert doc['targets'] == ['R1', 'R2']
        assert list(doc['groups']) == ['sheet:a', 'sheet:b']
        assert doc['groups']['sheet:a'] == ['C1', 'C2']
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_moves_carry_both_poses_so_a_tween_has_a_source():
    """quench() returns only the DESTINATION pose. Without the source read off
    the parent board there is nothing to animate from and no arrow to draw."""
    pcb = parse_kicad_pcb(BOARD)
    ref = sorted(pcb.footprints)[0]
    fp = pcb.footprints[ref]
    moves = L.moves_from_placements(
        pcb, [{'reference': ref, 'new_x': fp.x + 2.5, 'new_y': fp.y - 1.0,
               'new_rotation': 90.0},
              {'reference': 'NOSUCHPART', 'new_x': 0, 'new_y': 0,
               'new_rotation': 0}])
    assert len(moves) == 1, "an unknown ref must be dropped, not crash"
    m = moves[0]
    assert m['reference'] == ref
    assert abs(m['from'][0] - fp.x) < 1e-6 and abs(m['from'][1] - fp.y) < 1e-6
    assert abs(m['to'][0] - (fp.x + 2.5)) < 1e-6
    assert m['to'][2] == 90.0


def test_sidecar_failure_never_loses_a_round():
    """Best-effort: an unwritable work dir must not abort a routing round that
    cost minutes of A*."""
    got = L.write_round_sidecar(os.path.join(tempfile.gettempdir(), 'no', 'such', 'dir'),
                                1, board='b', routed='r', parent='p', accepted=True)
    assert got == ''


TESTS = [
    test_metrics_from_summary_matches_the_loops_own_arithmetic,
    test_empty_blockers_list_does_not_regress_to_log_scraping,
    test_pre_409_logs_still_fall_back_to_the_regex,
    test_run_route_is_now_a_one_line_caller,
    test_sidecar_records_the_parent_not_the_previous_round,
    test_screened_round_is_distinguishable_from_a_crash,
    test_sidecar_is_deterministic,
    test_moves_carry_both_poses_so_a_tween_has_a_source,
    test_sidecar_failure_never_loses_a_round,
]


if __name__ == '__main__':
    for t in TESTS:
        print(f"--- {t.__name__}")
        t()
    print("ALL PASS")
