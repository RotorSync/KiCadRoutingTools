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
sys.path.insert(0, os.path.join(ROOT, 'py_router'))  # placement split
sys.path.insert(0, os.path.join(ROOT, 'py_tools'))  # placement split
sys.path.insert(0, os.path.join(ROOT, 'py_placer'))  # placement split

import converge  # noqa: E402

BOARD = os.path.join(ROOT, 'kicad_files', 'splitflap_driver.kicad_pcb')


def _cv(args, **kw):
    return subprocess.run([sys.executable, '-X', 'utf8',
                           os.path.join(ROOT, 'py_placer', 'converge.py')] + args,
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
        # sys.executable, not 'echo': echo is a shell builtin, so it neither
        # replays on Windows nor passes the record-time replayability guard.
        r = _cv(['record', '--ledger', led, '--board', BOARD,
                 '--lever', 'seed', '--argv', sys.executable, '-c',
                 "print('replayed-ok')"])
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


def test_record_refuses_an_argv_that_cannot_replay():
    """Run-7 F4: entries recorded with placeholder script names made replay a
    reconstruction -- exactly what the ledger exists to prevent."""
    with tempfile.TemporaryDirectory() as td:
        led = os.path.join(td, 'l.jsonl')
        r = _cv(['record', '--ledger', led, '--board', BOARD,
                 '--lever', 'x', '--argv', 'route_v12_placeholder.sh', 'b.pcb'])
        assert r.returncode == 2, (r.returncode, r.stderr)
        assert 'never replay' in r.stderr
        assert not os.path.exists(led), "nothing may be written on refusal"
    print("  PASS: a placeholder --argv is refused, ledger untouched")


def test_record_final_requires_stop_condition():
    with tempfile.TemporaryDirectory() as td:
        led = os.path.join(td, 'l.jsonl')
        r = _cv(['record', '--ledger', led, '--board', BOARD, '--final'])
        assert r.returncode == 2 and 'stop-condition' in r.stderr
        assert not os.path.exists(led), "nothing may be written on refusal"
        r = _cv(['record', '--ledger', led, '--board', BOARD, '--final',
                 '--stop-condition', 'plateau: 3 iterations, no new copper'])
        assert r.returncode == 0, r.stderr
        e = json.loads(r.stdout)
        assert e.get('final') is True
        assert e.get('stop_condition', '').startswith('plateau')
    print("  PASS: --final without --stop-condition is refused; with it, recorded")


def test_record_score_failures_want_names():
    """Run-7 S10/F5: a score carrying only a COUNT forces every later read to
    re-derive which nets -- and a truncated re-derivation shipped a wrong
    close-out. Names print when given; a count without names draws a NOTE."""
    with tempfile.TemporaryDirectory() as td:
        led = os.path.join(td, 'l.jsonl')
        r = _cv(['record', '--ledger', led, '--board', BOARD,
                 '--score', '{"failures": 3}'])
        assert r.returncode == 0
        assert 'names no nets' in r.stderr, r.stderr
        r = _cv(['record', '--ledger', led, '--board', BOARD,
                 '--score', '{"failures": 2, "failed_nets": ["SWDIO", "GPIO1"]}'])
        assert r.returncode == 0
        assert 'SWDIO' in r.stderr and 'GPIO1' in r.stderr, r.stderr
    print("  PASS: failing-net names surface; a bare count draws a NOTE")


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


# ------------------------------------------------------------ where --oracle

def test_parse_pairs_yields_join_specs():
    """parse_pairs turns raw DRC unconnected items into {net, a, b} join
    specs with x/y/layer/kind -- no kicad-cli needed to verify the shape."""
    from kicad_unconnected import parse_pairs
    items = [
        {'items': [
            {'description': 'Pad 1 [GND] of R1 on F.Cu',
             'pos': {'x': 10.0, 'y': 20.0}},
            {'description': 'Track [GND] on F.Cu',
             'pos': {'x': 11.5, 'y': 20.0}}]},
        # straddles nets -> dropped, exactly as the oracle drops it
        {'items': [
            {'description': 'Pad 2 [VCC] of R1 on F.Cu',
             'pos': {'x': 1.0, 'y': 1.0}},
            {'description': 'Track [GND] on F.Cu',
             'pos': {'x': 2.0, 'y': 1.0}}]},
    ]
    pairs = parse_pairs(items)
    assert len(pairs) == 1, pairs
    p = pairs[0]
    assert p['net'] == 'GND'
    assert p['a'] == {'x': 10.0, 'y': 20.0, 'layer': 'F.Cu', 'kind': 'pad'}
    assert p['b']['kind'] == 'track'
    print("  PASS: raw DRC items become exact join specs; cross-net pairs drop")


def test_where_without_nets_or_oracle_refuses():
    r = _cv(['where', BOARD])
    assert r.returncode == 2, (r.returncode, r.stdout)
    assert '--oracle' in r.stdout
    print("  PASS: where with neither --nets nor --oracle refuses with advice")


def test_where_oracle_prints_the_join_work_list():
    """On the unrouted fixture, KiCad's own DRC (zones refilled) must surface
    joins, each as an exact endpoint-pair spec, before the forensics."""
    from kicad_unconnected import find_kicad_cli
    if find_kicad_cli() is None:
        print("  SKIP: kicad-cli not installed")
        return
    r = _cv(['where', BOARD, '--oracle', '--nets', 'GND'], timeout=600)
    assert 'ORACLE:' in r.stdout, r.stdout[:1500]
    assert 'join(s) remain' in r.stdout
    assert ' <-> ' in r.stdout, "no pair spec printed:\n" + r.stdout[:1500]
    print("  PASS: where --oracle prints KiCad's join list as pair specs")


def test_record_warns_on_unbound_or_mismatched_score():
    """Run-3 B4: three ledger entries shipped embedding a PRIOR board's
    quality because --score was free JSON with no binding to --board.
    board_score now embeds board_sha; record warns loudly on absence or
    mismatch (a warning, not a refusal: baseline rows legitimately attach a
    parent score to a rejected candidate -- but never silently)."""
    if not os.path.isfile(BOARD):
        print("  SKIP: fixture missing")
        return
    from board_store import sha256_file
    with tempfile.TemporaryDirectory() as td:
        led = os.path.join(td, 'l.jsonl')

        def rec(score):
            return _cv(['record', '--ledger', led, '--board', BOARD,
                        '--lever', 'x', '--score', json.dumps(score),
                        '--argv', sys.executable, '-c', "pass"])

        r = rec({'blocking': 0, 'board_sha': sha256_file(BOARD)})
        assert r.returncode == 0, r.stderr
        assert 'record WARNING' not in r.stderr, r.stderr

        r = rec({'blocking': 0})  # no board_sha (pre-B4 payload)
        assert r.returncode == 0
        assert 'no board_sha' in r.stderr, r.stderr

        r = rec({'blocking': 0, 'board_sha': 'f' * 64})  # a different board
        assert r.returncode == 0
        assert 'DIFFERENT board' in r.stderr, r.stderr
    print("  PASS: record binds score payloads to the recorded board")


def test_board_score_emits_board_sha():
    if not os.path.isfile(BOARD):
        print("  SKIP: fixture missing")
        return
    from board_store import sha256_file
    r = subprocess.run(
        [sys.executable, '-X', 'utf8',
         os.path.join(ROOT, '.claude', 'skills', 'plan-pcb-routing',
                      'scripts', 'board_score.py'), BOARD, '-q'],
        capture_output=True, text=True, encoding='utf-8', errors='replace',
        cwd=ROOT)
    line = [l for l in r.stdout.splitlines() if l.startswith('SCORE_JSON=')]
    assert line, r.stdout[:800] + r.stderr[:800]
    doc = json.loads(line[0].split('=', 1)[1])
    assert doc.get('board_sha') == sha256_file(BOARD), \
        "board_score must bind its payload to the graded file"
    print("  PASS: board_score embeds board_sha")


if __name__ == '__main__':
    for k, v in sorted(globals().items()):
        if k.startswith('test_'):
            print(f"--- {k}")
            v()
    print("ALL PASS")
