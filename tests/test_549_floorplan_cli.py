"""
check_floorplan.py: the exit-code contract and the JSON surface (issue #549).

Subprocess tests, because the things that matter here are what a CALLER sees --
the exit code it branches on, and whether two runs produce the same bytes. An
in-process test cannot check either honestly: `main()` returning 4 proves
nothing if `__main__` drops the value (place_optimize.py:146 does exactly that,
issue #551), and determinism across hash seeds needs separate interpreters.

The exit codes are a contract, not a detail:

    0  graded clean (or --exit-zero)     3  board state / no usable outline
    2  argparse, incl. a malformed intent  4  graded, violations found

4 rather than 1 because 3 is already taken and 1 is ambiguous with a crash. A
caller has to be able to tell "the floorplan is wrong" from "the grader is
broken" -- that distinction is the whole product.
"""

import json
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(ROOT, 'check_floorplan.py')
BOARD = os.path.join(ROOT, 'kicad_files', 'ulx3s.kicad_pcb')

VIOLATIONS_EXIT = 4
STATE_EXIT = 3

_TMP = tempfile.mkdtemp(prefix='fp549_')
_EMITTED = os.path.join(_TMP, 'emitted.json')

ROUND_BOARD = (
    '(kicad_pcb (version 20221018) (generator pcbnew)\n'
    '  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))\n'
    '  (gr_circle (center 50 50) (end 70 50) (stroke (width 0.1)'
    ' (type default)) (layer "Edge.Cuts"))\n  (net 0 "")\n)\n')


def _run(*argv, env=None):
    e = dict(os.environ)
    e.setdefault('PYTHONIOENCODING', 'utf-8')
    if env:
        e.update(env)
    r = subprocess.run([sys.executable, '-X', 'utf8', TOOL] + list(argv),
                       capture_output=True, text=True, cwd=ROOT, env=e)
    return r.returncode, r.stdout, r.stderr


def _summary(stdout):
    for line in stdout.splitlines():
        if line.startswith('JSON_SUMMARY: '):
            return json.loads(line[len('JSON_SUMMARY: '):])
    raise AssertionError('no JSON_SUMMARY line')


def _emit_once():
    if not os.path.exists(_EMITTED):
        code, out, err = _run(BOARD, '--emit-intent', _EMITTED)
        assert code == 0, (code, err)
    return _EMITTED


def test_emit_then_grade_is_clean_and_exits_zero():
    code, out, err = _run(BOARD, '--emit-intent', _EMITTED)
    assert code == 0, (code, err)
    doc = json.load(open(_EMITTED, encoding='utf-8'))
    assert doc['kind'] == 'floorplan-intent'
    assert 'not editable by this toolchain' in out, out

    code, out, err = _run(BOARD, '--intent', _EMITTED)
    assert code == 0, (code, err)
    s = _summary(out)
    assert s['pass'] is True and s['errors'] == 0
    print(f"  PASS: emit -> grade exits 0, {s['rules_run']} rules ran")


def test_violations_exit_4_and_exit_zero_overrides():
    raw = json.load(open(_emit_once(), encoding='utf-8'))
    raw['must_lock'] = ['R1']
    p = os.path.join(_TMP, 'unlocked.json')
    json.dump(raw, open(p, 'w', encoding='utf-8'))

    code, out, _ = _run(BOARD, '--intent', p, '-q')
    assert code == VIOLATIONS_EXIT, code
    s = _summary(out)
    assert s['pass'] is False and s['errors'] >= 1
    assert s['violations_by_rule'].get('must_lock') == 1, s['violations_by_rule']

    code, out, _ = _run(BOARD, '--intent', p, '-q', '--exit-zero')
    assert code == 0, code
    assert _summary(out)['pass'] is False, "the verdict changed with --exit-zero"
    print("  PASS: 4 on violations; --exit-zero returns 0 without lying "
          "about `pass`")


def test_argparse_failures_all_exit_2():
    broken = os.path.join(_TMP, 'broken.json')
    open(broken, 'w', encoding='utf-8').write('{"schema": 1,')
    wrongkind = os.path.join(_TMP, 'sidecar.json')
    json.dump({'schema': 1, 'round': 2, 'accepted': True},
              open(wrongkind, 'w', encoding='utf-8'))
    cases = {
        'no --intent and no --emit-intent': (BOARD,),
        'truncated intent': (BOARD, '--intent', broken),
        'missing intent file': (BOARD, '--intent',
                                os.path.join(_TMP, 'nope.json')),
        'a round sidecar as the intent': (BOARD, '--intent', wrongkind),
        'unknown --group-by source': (BOARD, '--intent', _emit_once(),
                                      '--group-by', 'bogus'),
        'missing board': ('nosuch.kicad_pcb', '--intent', _emit_once()),
    }
    for why, argv in cases.items():
        code, _out, _err = _run(*argv)
        assert code == 2, f"{why}: exit {code}, expected 2"
    print(f"  PASS: {len(cases)} malformed invocations all exit 2")


def test_a_board_with_no_usable_outline_exits_3_not_0():
    """#550: a round board parses its ring but reports board_bounds None. Every
    containment check would silently degrade to a bounding box that does not
    exist, so a clean report would mean 'stopped checking'."""
    p = os.path.join(_TMP, 'round.kicad_pcb')
    open(p, 'w', encoding='utf-8').write(ROUND_BOARD)
    for argv in ((p, '--emit-intent', os.path.join(_TMP, 'x.json')),
                 (p, '--intent', _emit_once())):
        code, _out, err = _run(*argv)
        assert code == STATE_EXIT, (argv[1], code)
        assert '550' in err, err
    print("  PASS: emit and grade both exit 3, citing #550")


def test_json_output_is_byte_identical_across_hash_seeds():
    """#457. Anything whose order can reach a report must be a property of the
    finding, not of dict iteration -- and only separate interpreters can show
    it."""
    outs = []
    for seed in ('0', '12345'):
        p = os.path.join(_TMP, f'out{seed}.json')
        code, stdout, err = _run(BOARD, '--intent', _emit_once(), '-q',
                                 '--json', p, env={'PYTHONHASHSEED': seed})
        assert code == 0, (seed, err)
        outs.append((open(p, 'rb').read(), stdout))
    assert outs[0][0] == outs[1][0], "--json differs across PYTHONHASHSEED"
    assert outs[0][1] == outs[1][1], "JSON_SUMMARY differs across PYTHONHASHSEED"
    print(f"  PASS: {len(outs[0][0])} bytes identical at seeds 0 and 12345")


def test_the_summary_reports_what_did_not_run():
    """Anti-vacuity for machines: `0 violations` and `0 rules ran` must not look
    the same. A caller that only reads `pass` would treat a fully-skipped grade
    as a clean board."""
    minimal = os.path.join(_TMP, 'minimal.json')
    json.dump({'schema': 1, 'kind': 'floorplan-intent', 'units': 'mm'},
              open(minimal, 'w', encoding='utf-8'))
    code, out, _ = _run(BOARD, '--intent', minimal, '-q')
    assert code == 0
    s = _summary(out)
    assert s['pass'] is True and s['violations'] == 0
    assert s['rules_run'] == 0, s['rules_run']
    assert s['rules_skipped'] == 9, s['rules_skipped']
    print(f"  PASS: an empty intent passes with rules_run=0, "
          f"rules_skipped={s['rules_skipped']} -- visibly vacuous")


def test_the_summary_carries_the_keys_a_caller_branches_on():
    code, out, _ = _run(BOARD, '--intent', _emit_once(), '-q')
    s = _summary(out)
    for k in ('pass', 'violations', 'errors', 'warnings', 'violations_by_rule',
              'rules_run', 'rules_skipped', 'blocks', 'blocks_resolved',
              'parts_covered', 'parts_total', 'cutouts', 'edge_contours',
              'overlap_area', 'oob_count', 'oob_amount', 'hpwl',
              'state_unplaced', 'state_partially_unplaced',
              'state_duplicate_fraction', 'state_spread_ratio',
              'state_outside_fraction'):
        assert k in s, f"JSON_SUMMARY lost {k}"
    json.dumps(s)
    print(f"  PASS: {len(s)} keys, all JSON-round-trippable")


def test_the_full_json_carries_the_measured_numbers():
    raw = json.load(open(_emit_once(), encoding='utf-8'))
    x0, y0, x1, y1 = raw['envelope']['rect']
    raw['envelope']['rect'] = [x0, y0, x1 + 7.5, y1]
    p = os.path.join(_TMP, 'wide.json')
    json.dump(raw, open(p, 'w', encoding='utf-8'))
    full = os.path.join(_TMP, 'full.json')
    code, _out, _ = _run(BOARD, '--intent', p, '-q', '--json', full)
    assert code == VIOLATIONS_EXIT, code
    doc = json.load(open(full, encoding='utf-8'))
    v = [x for x in doc['violations'] if x['rule'] == 'envelope']
    assert len(v) == 1, doc['violations']
    assert abs(v[0]['measured']['worst_corner_mm'] - 7.5) < 1e-6
    assert 'do not resize' in v[0]['message']
    # the outline block is reported whether or not anything was wrong with it
    assert doc['outline']['bounds'] and 'cutouts' in doc['outline']
    assert doc['state']['unplaced'] is False
    print(f"  PASS: measured 7.50mm, and the message refuses the resize")


def test_emit_writes_no_board_and_touches_nothing():
    """A report-only tool that mutates its input is the failure the whole
    design rests on not having."""
    before = (os.path.getmtime(BOARD), os.path.getsize(BOARD))
    listing = set(os.listdir(os.path.dirname(BOARD)))
    p = os.path.join(_TMP, 'emit2.json')
    code, _out, _ = _run(BOARD, '--emit-intent', p)
    assert code == 0
    assert (os.path.getmtime(BOARD), os.path.getsize(BOARD)) == before, \
        "the input board was modified"
    assert set(os.listdir(os.path.dirname(BOARD))) == listing, \
        "a stray file was written beside the board"
    print("  PASS: input board mtime/size unchanged, no stray siblings")


TESTS = [
    test_emit_then_grade_is_clean_and_exits_zero,
    test_violations_exit_4_and_exit_zero_overrides,
    test_argparse_failures_all_exit_2,
    test_a_board_with_no_usable_outline_exits_3_not_0,
    test_json_output_is_byte_identical_across_hash_seeds,
    test_the_summary_reports_what_did_not_run,
    test_the_summary_carries_the_keys_a_caller_branches_on,
    test_the_full_json_carries_the_measured_numbers,
    test_emit_writes_no_board_and_touches_nothing,
]


if __name__ == '__main__':
    for t in TESTS:
        print(f"--- {t.__name__}")
        t()
    print("ALL PASS")
