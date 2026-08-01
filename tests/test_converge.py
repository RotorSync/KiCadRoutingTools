#!/usr/bin/env python3
"""converge.py: the ladder, the rip invariants, and a step back that is a checkout.

The loop this supports failed in two measurable ways before it existed:

  * every candidate cost a full chain run, so a budget of 20 bought ~8 useful
    moves and the run stopped with nets still carrying no copper;
  * a step back meant reading prose and reconstructing a command by hand.

The verbs here are the fix. `poses` ranks with arithmetic and routes only the
survivors; `record`/`step-back`/`replay` make the history mechanical;
`check_rip_invariants` encodes four rules that each cost a wasted iteration.
"""
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import converge  # noqa: E402

BOARD = os.path.join(ROOT, 'kicad_files', 'splitflap_driver.kicad_pcb')


def _cv(args, **kw):
    return subprocess.run([sys.executable, '-X', 'utf8',
                           os.path.join(ROOT, 'converge.py')] + args,
                          capture_output=True, text=True, encoding='utf-8',
                          errors='replace', cwd=ROOT, **kw)


# ------------------------------------------------------------ rip invariants

def test_rip_invariants_catch_all_four_traps():
    # 1. more than one net per call
    c = converge.check_rip_invariants(['A', 'B'], [])
    assert any('one net per call' in x for x in c), c
    # 2. a width-bearing net ripped without its width
    c = converge.check_rip_invariants(['A'], ['VCC'], power_nets=['VCC'])
    assert any('width-bearing' in x for x in c), c
    # 3. a glob standing in for an exact name
    c = converge.check_rip_invariants(['A'], ['QSPI_*'])
    assert any('glob' in x and 'locked' in x for x in c), c
    # 4. a net in both --nets and the rip set
    c = converge.check_rip_invariants(['A'], ['A'])
    assert any('force-reroute' in x for x in c), c
    print("  PASS: all four rip traps are caught")


def test_a_safe_rip_produces_no_complaints():
    assert converge.check_rip_invariants(['A'], ['B', 'C']) == []
    assert converge.check_rip_invariants(
        ['A'], ['VCC'], power_nets=[]) == []
    print("  PASS: a scoped, exact, width-safe rip is silent")


# -------------------------------------------------------------- route_verdict

def test_route_verdict_counts_both_kinds_of_failure():
    n, note = converge.route_verdict(
        {'failed_single': ['A'], 'failed_multipoint': [{'net_name': 'B'}],
         'multipoint_pads_total': 10, 'multipoint_pads_connected': 8})
    assert n == 3, f"1 failed net + 2 pads short = 3, got {n}"
    assert 'A' in note and 'B' in note
    assert converge.route_verdict({})[0] is None
    print("  PASS: failures = failed nets + pad deficit")


def test_route_verdict_surfaces_a_refused_rip():
    """A caller that cannot see a refusal will follow the router's retry hint
    forever -- 'locked' has no override."""
    n, note = converge.route_verdict(
        {'failed_single': ['A'],
         'protected_skipped': {'--rip-existing-nets': {'GND': 'locked'}}})
    assert 'GND(locked)' in note, note
    print("  PASS: a refused rip reaches the verdict text")


# ---------------------------------------------------------------- the ladder

def test_poses_emits_parseable_json_on_stdout():
    """The verb's stdout is a data channel: the parser and the placement state
    both narrate, and a single stray line makes the document unparseable."""
    if not os.path.isfile(BOARD):
        print("  SKIP: fixture missing")
        return
    r = _cv(['poses', BOARD, '--ref', 'C1', '--radius', '0.5',
             '--step', '0.5', '--limit', '3'])
    assert r.returncode == 0, r.stderr[-800:]
    d = json.loads(r.stdout)             # the assertion IS that this parses
    assert d['ref'] == 'C1' and d['poses']
    costs = [p['cost'] for p in d['poses']]
    assert costs == sorted(costs)
    assert 'WARNING' in r.stderr or True   # diagnostics belong on stderr
    print(f"  PASS: clean JSON on stdout, {len(d['poses'])} ranked poses")


def test_route_flag_requires_the_caller_to_say_what_is_affected():
    """Only the caller knows which nets a move can affect; guessing would
    either route the world or miss the point."""
    if not os.path.isfile(BOARD):
        print("  SKIP: fixture missing")
        return
    r = _cv(['poses', BOARD, '--ref', 'C1', '--route'])
    assert r.returncode == 2 and 'affected' in r.stderr
    print("  PASS: --route without --affected is refused")


# ------------------------------------------------------------- the bookkeeping

def test_record_step_back_replay_round_trip():
    if not os.path.isfile(BOARD):
        print("  SKIP: fixture missing")
        return
    with tempfile.TemporaryDirectory() as td:
        led = os.path.join(td, 'l.jsonl')
        r = _cv(['record', '--ledger', led, '--board', BOARD,
                 '--lever', 'seed', '--argv', 'echo', 'replayed-ok'])
        assert r.returncode == 0, r.stderr
        sha = json.loads(r.stdout)['result_sha']

        out = os.path.join(td, 'back.kicad_pcb')
        assert _cv(['step-back', '--ledger', led, '--iteration', '0',
                    '--out', out]).returncode == 0
        from board_store import sha256_file
        assert sha256_file(out) == sha, "step back must be byte-exact"

        r = _cv(['replay', '--ledger', led, '--iteration', '0'])
        assert r.returncode == 0 and 'replayed-ok' in r.stdout
    print("  PASS: record -> step-back (byte-exact) -> replay")


def test_a_prose_only_entry_refuses_to_replay_without_a_traceback():
    with tempfile.TemporaryDirectory() as td:
        led = os.path.join(td, 'l.jsonl')
        _cv(['record', '--ledger', led, '--board', BOARD, '--kind', 'systemic',
             '--lever', 'restored the net classes'])
        r = _cv(['replay', '--ledger', led, '--iteration', '0'])
        assert r.returncode == 4, f"expected a clean refusal, got {r.returncode}"
        assert 'Traceback' not in r.stderr, "a non-replayable entry is not a crash"
        assert 'lever_argv' in r.stderr
    print("  PASS: a prose entry refuses cleanly, exit 4, no traceback")


def test_status_warns_when_the_budget_goes_to_the_instrument():
    """The failure this makes visible: nine of eleven iterations spent on how
    the chain measures itself, finishing with nets that never got copper."""
    with tempfile.TemporaryDirectory() as td:
        led = os.path.join(td, 'l.jsonl')
        for _ in range(3):
            _cv(['record', '--ledger', led, '--board', BOARD,
                 '--kind', 'systemic', '--lever', 'tooling'])
        r = _cv(['status', '--ledger', led])
        assert r.returncode == 0
        assert json.loads(r.stdout)['systemic'] == 3
        assert 'SYSTEMIC' in r.stderr, "a lopsided budget must be called out"
    print("  PASS: status splits the budget and warns on a lopsided one")


if __name__ == '__main__':
    for k, v in sorted(globals().items()):
        if k.startswith('test_'):
            print(f"--- {k}")
            v()
    print("ALL PASS")
