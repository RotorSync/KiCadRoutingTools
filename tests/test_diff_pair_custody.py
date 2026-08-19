#!/usr/bin/env python3
"""Unit tests for diff_pair_custody: per-pair failure classification, custody
recording, and the per-pair JSON report builder (pure Python - no routing).

    python3 tests/test_diff_pair_custody.py
"""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'py_router'))  # #522
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'py_tools'))  # #522

from diff_pair_custody import (classify_diff_pair_failure, record_casualty,
                               record_pair_diag, build_pair_reports)

CHECKS = []


def check(name, ok):
    CHECKS.append((name, ok))
    print(f"  {'PASS' if ok else 'FAIL'}: {name}")


def test_classifier():
    c = classify_diff_pair_failure
    check("None -> endpoint-resolution-failure",
          c(None) == 'endpoint-resolution-failure')
    check("polarity_skip denied -> polarity-swap-denied",
          c({'failed': True, 'polarity_skip': True,
             'polarity_swap_denied': True}) == 'polarity-swap-denied')
    check("polarity_skip allowed -> polarity-mismatch",
          c({'failed': True, 'polarity_skip': True}) == 'polarity-mismatch')
    check("pn_crossing -> pn-crossing",
          c({'failed': True, 'pn_crossing': True, 'iterations': 3}) == 'pn-crossing')
    check("connector_graze -> connector-graze",
          c({'failed': True, 'connector_graze': True, 'graze_net_id': 7,
             'iterations': 3}) == 'connector-graze')
    check("probe_blocked -> no-escape-path",
          c({'probe_blocked': True, 'blocked_at': 'target'}) == 'no-escape-path')
    check("setback (0 iters + cells) -> no-escape-path",
          c({'failed': True, 'iterations': 0, 'iterations_forward': 0,
             'iterations_backward': 0,
             'blocked_cells_forward': [(1, 2, 0)]}) == 'no-escape-path')
    # #346: PoseRouter search exhaustion must carry its own reason code.
    check("search exhausted -> pose-router-failure",
          c({'failed': True, 'iterations': 12345, 'iterations_forward': 6000,
             'iterations_backward': 6345}) == 'pose-router-failure')
    check("failed, nothing else -> no-route",
          c({'failed': True, 'iterations': 0}) == 'no-route')


def _fake_state():
    return SimpleNamespace(routed_results={}, diff_pair_single_ended_nets={},
                           pair_diagnostics={}, casualty_custody={})


def test_custody_recording():
    st = _fake_state()
    saved = {'new_segments': [], 'new_vias': []}
    record_casualty(st, 10, saved, [10, 11], True)
    check("custody keyed by every ripped id",
          set(st.casualty_custody) == {10, 11})
    check("custody tuple carries saved copper",
          st.casualty_custody[10][1] is saved)
    # A later rip overwrites with the newest complete geometry.
    saved2 = {'new_segments': [], 'new_vias': []}
    record_casualty(st, 10, saved2, [10, 11], True)
    check("later rip overwrites custody",
          st.casualty_custody[11][1] is saved2)
    # None saved_result (rip refused) never recorded.
    record_casualty(st, 20, None, [20], False)
    check("None saved_result not recorded", 20 not in st.casualty_custody)


def test_pair_reports():
    st = _fake_state()
    mk = lambda p, n, pn, nn: SimpleNamespace(p_net_id=p, n_net_id=n,
                                              p_net_name=pn, n_net_name=nn)
    pairs = [
        ('A', mk(1, 2, '/A_P', '/A_N')),    # coupled, audit clean
        ('B', mk(3, 4, '/B_P', '/B_N')),    # coupled, N member incomplete
        ('C', mk(5, 6, '/C_P', '/C_N')),    # failed (congestion)
        ('D', mk(7, 8, '/D_P', '/D_N')),    # deferred (electrically short)
        ('E', mk(9, 10, '/E_P', '/E_N')),   # partial: coupled + peeled terminals
    ]
    # E's shared result is a real coupled route (the trunk-credit
    # discriminator reads is_diff_pair + new_segments; see
    # test_partial_trunk_kept_keeps_credit /
    # test_partial_no_trunk_demotes for both directions of that rule).
    _e_trunk = {'is_diff_pair': True, 'new_segments': [SimpleNamespace()],
                'new_vias': []}
    st.routed_results = {1: {}, 2: {}, 3: {}, 4: {}, 9: _e_trunk, 10: _e_trunk}
    st.diff_pair_single_ended_nets = {7: '/D_P', 8: '/D_N', 9: '/E_P', 10: '/E_N'}
    record_pair_diag(st, 'C', outcome='failed', reason='congestion',
                     stage='initial-route', blocking_nets=['X'])
    record_pair_diag(st, 'D', outcome='deferred', reason='electrically-short',
                     stage='pre-route')
    audit = {'A': {'p': True, 'n': True}, 'B': {'p': True, 'n': False},
             'C': {'p': False, 'n': False}, 'D': {'p': False, 'n': False},
             'E': {'p': True, 'n': True}}
    reps = {r['pair']: r for r in build_pair_reports(
        st, pairs, audit, skipped_fanout=[('F', '/F_P', '/F_N')])}

    check("A coupled, no mismatch",
          reps['A']['outcome'] == 'coupled'
          and not reps['A']['member_audit_mismatch']
          and reps['A']['failure_reason'] is None)
    # #602: a contradicted 'coupled' claim is demoted to 'incomplete' in the
    # record itself -- one pair report must not say 'coupled' next to its own
    # list of disconnected member pads.
    check("B coupled but N incomplete -> mismatch flagged, outcome demoted",
          reps['B']['outcome'] == 'incomplete'
          and reps['B']['member_audit_mismatch']
          and reps['B']['incomplete_members'] == ['/B_N'])
    check("C failed with reason+stage+blockers",
          reps['C']['outcome'] == 'failed'
          and reps['C']['failure_reason'] == 'congestion'
          and reps['C']['failure_stage'] == 'initial-route'
          and reps['C']['blocking_nets'] == ['X'])
    check("D deferred, electrically-short",
          reps['D']['outcome'] == 'deferred'
          and reps['D']['failure_reason'] == 'electrically-short')
    check("E coupled+peeled -> partial",
          reps['E']['outcome'] == 'partial')
    check("E partial with trunk -> not demoted",
          not reps['E']['member_audit_mismatch']
          and not reps['E']['no_coupled_trunk'])
    check("skipped fanout pair reported",
          reps['F']['outcome'] == 'skipped'
          and reps['F']['failure_reason'] == 'fanout-self-overlap')
    check("failed pair without diag record -> reason 'unknown'",
          build_pair_reports(
              _fake_state(), [('Z', mk(1, 2, '/Z_P', '/Z_N'))],
              {'Z': {'p': False, 'n': False}})[0]['failure_reason'] == 'unknown')
    import json
    check("reports JSON-serializable",
          bool(json.dumps(list(reps.values()))))


def _mk_pair(p, n, pn, nn):
    return SimpleNamespace(p_net_id=p, n_net_id=n, p_net_name=pn,
                           n_net_name=nn)


def _reports_with_output(st, pairs, audit):
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        reps = {r['pair']: r for r in build_pair_reports(st, pairs, audit)}
    return reps, buf.getvalue()


def test_partial_trunk_kept_keeps_credit():
    """Direction 1 (maintainer's tigard counterexample, unit-level): a
    'partial' pair that KEPT coupled trunk copper keeps its
    routed_diff_pairs credit -- even with BOTH members incomplete. The
    deferred pads (tigard /USB_D's redundant J1 row) close in the designed
    single-ended follow-up: expected step-level state, not false success;
    incomplete members are already counted as pad deficit at chain level."""
    st = _fake_state()
    trunk = {'is_diff_pair': True, 'new_segments': [SimpleNamespace()],
             'new_vias': []}
    st.routed_results = {1: trunk, 2: trunk}
    st.diff_pair_single_ended_nets = {1: '/G_P', 2: '/G_N'}
    reps, out = _reports_with_output(
        st, [('G', _mk_pair(1, 2, '/G_P', '/G_N'))],
        {'G': {'p': False, 'n': False}})
    check("partial + trunk kept + both members incomplete -> credit KEPT",
          reps['G']['outcome'] == 'partial'
          and not reps['G']['member_audit_mismatch']
          and not reps['G']['no_coupled_trunk'])
    check("kept pair prints no demotion line", 'DEMOTED' not in out)


def test_partial_no_trunk_demotes():
    """Direction 2: a 'partial' pair that kept NO coupled trunk copper at
    all is demoted (member_audit_mismatch -> partial_diff_pairs, off the
    routed_diff_pairs credit list), and the demotion line says why."""
    st = _fake_state()
    # Members carry results, but none is a coupled route with committed
    # segments -- there is no coupled trunk to credit.
    hollow = {'new_segments': [], 'new_vias': []}
    st.routed_results = {3: hollow, 4: hollow}
    st.diff_pair_single_ended_nets = {3: '/H_P', 4: '/H_N'}
    reps, out = _reports_with_output(
        st, [('H', _mk_pair(3, 4, '/H_P', '/H_N'))],
        {'H': {'p': False, 'n': False}})
    check("partial + NO coupled trunk copper -> DEMOTED",
          reps['H']['outcome'] == 'partial'
          and reps['H']['member_audit_mismatch']
          and reps['H']['no_coupled_trunk'])
    check("demotion line names the pair and the reason",
          any('DEMOTED H' in ln and 'no coupled trunk copper kept' in ln
              for ln in out.splitlines()))


def _mk_state(pcb, cfg):
    from routing_state import RoutingState
    return RoutingState(pcb_data=pcb, config=cfg)


def test_reconcile_restore_first():
    """Phase A: a casualty whose corridor stayed free gets its ORIGINAL
    pre-rip copper restored (restore-first, no reroute needed)."""
    from kicad_parser import PCBData, Segment, Net, Pad
    from routing_config import GridRouteConfig, DiffPairNet
    from diff_pair_custody import run_casualty_reconcile

    def _pad(ref, num, x, y, nid, nm):
        return Pad(component_ref=ref, pad_number=num, global_x=x, global_y=y,
                   local_x=0, local_y=0, size_x=0.4, size_y=0.4,
                   shape='circle', layers=['F.Cu'], net_id=nid, net_name=nm)

    cfg = GridRouteConfig()
    cfg.layers = ['F.Cu', 'B.Cu']
    p_id, n_id = 1, 2
    seg_p = Segment(start_x=0, start_y=0, end_x=5, end_y=0, width=0.2,
                    layer='F.Cu', net_id=p_id)
    seg_n = Segment(start_x=0, start_y=0.5, end_x=5, end_y=0.5, width=0.2,
                    layer='F.Cu', net_id=n_id)
    saved = {'new_segments': [seg_p, seg_n], 'new_vias': [], 'iterations': 10,
             'is_diff_pair': True}
    pads = {p_id: [_pad('U1', '1', 0, 0, p_id, '/X_P'),
                   _pad('U2', '1', 5, 0, p_id, '/X_P')],
            n_id: [_pad('U1', '2', 0, 0.5, n_id, '/X_N'),
                   _pad('U2', '2', 5, 0.5, n_id, '/X_N')]}
    pcb = PCBData(board_info=None,
                  nets={p_id: Net(p_id, '/X_P'), n_id: Net(n_id, '/X_N')},
                  footprints={}, vias=[], segments=[], pads_by_net=pads)
    pair = DiffPairNet(base_name='X', p_net_name='/X_P', n_net_name='/X_N',
                       p_net_id=p_id, n_net_id=n_id)
    st = _mk_state(pcb, cfg)
    st.diff_pair_by_net_id = {p_id: ('X', pair), n_id: ('X', pair)}
    st.remaining_net_ids = [p_id, n_id]
    st.casualty_custody = {p_id: (p_id, saved, [p_id, n_id], True),
                           n_id: (p_id, saved, [p_id, n_id], True)}

    summary = run_casualty_reconcile(st)
    check("reconcile attempted the casualty", summary['attempted'] == 1)
    check("casualty restored (Phase A)", summary['restored'] == ['X'])
    check("both members back in routed_results",
          p_id in st.routed_results and n_id in st.routed_results)
    check("original copper back in pcb_data",
          any(s.net_id == p_id for s in pcb.segments)
          and any(s.net_id == n_id for s in pcb.segments))
    check("restored result back in write-list", saved in st.results)
    check("diag notes restored-original",
          st.pair_diagnostics.get('X', {}).get('casualty') == 'restored-original')


def test_reconcile_refused_then_partial():
    """Phases B/C: restore refused (foreign copper moved into the corridor),
    depth-1 reroute fails -> piece-level partial restore keeps only the
    non-colliding copper (DRC-safe: never a blind restore)."""
    from kicad_parser import PCBData, Segment, Net, Pad
    from routing_config import GridRouteConfig
    from diff_pair_custody import run_casualty_reconcile
    import routing_context
    import single_ended_routing

    def _pad(ref, num, x, y, nid, nm):
        return Pad(component_ref=ref, pad_number=num, global_x=x, global_y=y,
                   local_x=0, local_y=0, size_x=0.4, size_y=0.4,
                   shape='circle', layers=['F.Cu'], net_id=nid, net_name=nm)

    cfg = GridRouteConfig()
    cfg.layers = ['F.Cu', 'B.Cu']
    nid, foreign = 5, 9
    # Saved route: two segments; a foreign track now overlaps the FIRST only
    # (the second is well clear of it).
    seg_a = Segment(start_x=0, start_y=0, end_x=5, end_y=0, width=0.2,
                    layer='F.Cu', net_id=nid)
    seg_b = Segment(start_x=5, start_y=4, end_x=5, end_y=8, width=0.2,
                    layer='F.Cu', net_id=nid)
    saved = {'new_segments': [seg_a, seg_b], 'new_vias': [], 'iterations': 10}
    fseg = Segment(start_x=0, start_y=0, end_x=5, end_y=0, width=0.2,
                   layer='F.Cu', net_id=foreign)
    pcb = PCBData(board_info=None,
                  nets={nid: Net(nid, '/CAS'), foreign: Net(foreign, '/F')},
                  footprints={}, vias=[], segments=[fseg],
                  pads_by_net={nid: [_pad('U1', '1', 5, 4, nid, '/CAS'),
                                     _pad('U2', '1', 5, 8, nid, '/CAS')]})
    st = _mk_state(pcb, cfg)
    st.remaining_net_ids = [nid]
    st.casualty_custody = {nid: (nid, saved, [nid], True)}

    # Depth-1 reroute is exercised but must fail for this scenario; stub the
    # lazy-imported hooks so no real obstacle maps are needed.
    orig_build = routing_context.build_single_ended_obstacles
    orig_route = single_ended_routing.route_net_with_obstacles
    routing_context.build_single_ended_obstacles = lambda *a, **k: (None, [])
    single_ended_routing.route_net_with_obstacles = \
        lambda *a, **k: {'failed': True, 'iterations': 0}
    try:
        summary = run_casualty_reconcile(st)
    finally:
        routing_context.build_single_ended_obstacles = orig_build
        single_ended_routing.route_net_with_obstacles = orig_route

    check("refused restore recorded (#134 sink)",
          nid in st.collision_refused_net_ids)
    check("casualty lands in partial (not restored/rerouted)",
          summary['partial'] == ['/CAS'] and not summary['restored']
          and not summary['rerouted'])
    cas_segs = [s for s in pcb.segments if s.net_id == nid]
    check("only the NON-colliding piece restored",
          len(cas_segs) == 1 and cas_segs[0].start_y == 4
          and cas_segs[0].end_y == 8)
    check("partial result in write-list flagged",
          any(r.get('partial_restore_134') for r in st.results))
    check("net NOT claimed routed", nid not in st.routed_results)


def main():
    print("=" * 60)
    print("diff_pair_custody unit tests")
    print("=" * 60)
    test_classifier()
    test_custody_recording()
    test_pair_reports()
    test_partial_trunk_kept_keeps_credit()
    test_partial_no_trunk_demotes()
    print("-" * 60)
    test_reconcile_restore_first()
    test_reconcile_refused_then_partial()
    failed = [n for n, ok in CHECKS if not ok]
    print("-" * 60)
    print(f"{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
    if failed:
        print("FAILED: " + ", ".join(failed))
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
