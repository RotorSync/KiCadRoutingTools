#!/usr/bin/env python3
"""#621: bga_fanout.py / qfn_fanout.py can stop themselves.

Before this, neither fanout main took `--deadline` and neither engine took a
`cancel_check`, so the only way to bound one was an external timeout -- which on
Windows is `TerminateProcess`: uncatchable, no atexit, no flush. Measured on
`ulx3s.kicad_pcb U1` with `timeout 5`: **rc 124, 0 JSON_SUMMARY lines, and no
output board at all**. From a log that is indistinguishable from a hang, and the
chain step after it has nothing to consume.

Three things are pinned here, and the middle one is the load-bearing one:

1.  A budget produces the tool's OWN exit code (7, not the shell's 124), exactly
    ONE JSON_SUMMARY, and a partial that says it is partial.
2.  The untried balls land in `deadline_skipped` and are ABSENT from
    `unescaped_nets`. Folding them into the failure ledger would report an
    unfinished search as a MEASURED escape failure -- the misreading
    krt_deadline's docstring exists to prevent, and the one
    tests/test_deadline.py already pins for place_reconstruct. It would also
    send the planner into a pointless tighter-clearance retry.
3.  INERTNESS. Without `--deadline` the summary carries the same values as
    before, and the engine emits byte-identical copper whether `cancel_check` is
    None or a predicate that never fires. That last check is what makes a corpus
    A/B unnecessary: the cancel cannot change routing, only truncate it.

    python3 -X utf8 tests/test_fanout_deadline.py
"""
import io
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'py_router'))
sys.path.insert(0, os.path.join(ROOT, 'py_tools'))

BOARD = os.path.join(ROOT, 'kicad_files', 'interf_u_unrouted_placed.kicad_pcb')
QFN_BOARD = os.path.join(ROOT, 'kicad_files', 'haasoscope_pro_max_test.kicad_pcb')

# The measured, uncancelled ledger for `bga_fanout.py <BOARD> --component U9`
# (1.5-3s, every ball escapes -- fast, and with real work to abandon).
BASE = {'requested': 75, 'escaped': 75, 'failed': 0, 'unescaped_nets': [],
        'skipped_nc': 24, 'component': 'U9'}
BASE_DRC_TOTAL = 0

BAD = []


def check(cond, label):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        BAD.append(label)


def _fanout(tmpdir, name, *extra, tool='bga_fanout.py', board=BOARD,
            component='U9'):
    out = os.path.join(tmpdir, name)
    p = subprocess.run(
        [sys.executable, '-X', 'utf8', os.path.join('py_router', tool), board,
         '--component', component, '--output', out, *extra],
        capture_output=True, text=True, encoding='utf-8', errors='replace',
        cwd=ROOT, timeout=900)
    return p, out


def _summaries(stdout):
    return [json.loads(l.split('JSON_SUMMARY: ', 1)[1])
            for l in stdout.splitlines() if l.startswith('JSON_SUMMARY: ')]


def test_deadline_zero_stops_on_our_own_terms(tmpdir):
    """--deadline 0 => rc 7, ONE summary, a coherent partial."""
    p, out = _fanout(tmpdir, 'dl0.kicad_pcb', '--deadline', '0')

    # Separate "the guard held" from "my control broke": an argparse error, an
    # ImportError or a missing board all exit non-zero too, and on BASE this
    # command exits 2 with `unrecognized arguments: --deadline`.
    if p.returncode == 2 and 'unrecognized arguments' in (p.stdout + p.stderr):
        check(False, '--deadline is a real flag (base: argparse rejects it)')
        return
    check(p.returncode != 124,
          'the TOOL chose the exit code (124 would be the shell)')
    check(p.returncode == 7,
          f'exit is krt_deadline.DEADLINE_EXIT (got {p.returncode})')

    docs = _summaries(p.stdout)
    check(len(docs) == 1,
          f'exactly ONE JSON_SUMMARY (got {len(docs)}) -- a bare print would '
          'leave krt_deadline._emitted False and the atexit hook would publish '
          'a SECOND, contradicting summary')
    if not docs:
        return
    d = docs[0]
    check(d.get('complete') is False, 'complete is False')
    check(d.get('status') == 'deadline',
          f"status == 'deadline' (got {d.get('status')!r})")
    check(bool(d.get('stopped_in')),
          f"stopped_in names the phase (got {d.get('stopped_in')!r})")

    skipped = d.get('deadline_skipped') or []
    check(bool(skipped),
          'the untried balls are NAMED in deadline_skipped')
    # THE anti-regression assertion.
    unescaped = set(d.get('unescaped_nets') or [])
    check(not (set(skipped) & unescaped),
          'NO untried ball is also in unescaped_nets -- an unfinished search '
          'has measured nothing, so it must not be reported as a failure')
    check(d.get('failed') == len(unescaped),
          'failed still counts ONLY unescaped_nets, not the skipped balls')
    check(d.get('requested') == d.get('escaped', 0) + d.get('failed', 0)
          + len(skipped),
          'the partial ledger adds up: requested == escaped + failed + skipped')
    check(d.get('requested') == BASE['requested'],
          f"the partial still requests the same {BASE['requested']} balls the "
          'finished run does')
    check(os.path.isfile(out),
          'the output board IS written (fanout is a chain step: its successor '
          'consumes the output path by name)')
    check('DEADLINE:' in p.stdout,
          'a DEADLINE banner names the board as partial')


def test_no_deadline_is_field_for_field_unchanged(tmpdir):
    """The default path keeps its ledger, exactly."""
    p, out = _fanout(tmpdir, 'plain.kicad_pcb')
    check(p.returncode == 0, f'rc 0 (got {p.returncode})')
    docs = _summaries(p.stdout)
    check(len(docs) == 1, f'exactly one JSON_SUMMARY (got {len(docs)})')
    if not docs:
        return
    d = docs[0]
    for k, v in BASE.items():
        check(d.get(k) == v, f'{k} == {v!r} (got {d.get(k)!r})')
    check((d.get('drc_grazes') or {}).get('total') == BASE_DRC_TOTAL,
          f'drc_grazes.total == {BASE_DRC_TOTAL}')
    check('deadline_skipped' not in d,
          'no deadline_skipped key on an uncancelled run -- the field appears '
          'only when a budget actually cut the run short')
    check(d.get('complete') is True and d.get('status') == 'ok',
          "the krt_deadline envelope reports the run as complete/ok (the two "
          'keys emit() adds; every pre-existing field is unchanged above)')


def _copper(tracks, vias):
    """Coordinate-level identity of the emitted copper."""
    t = sorted((round(x['start'][0], 6), round(x['start'][1], 6),
                round(x['end'][0], 6), round(x['end'][1], 6),
                round(x['width'], 6), x['layer'], x['net_id'])
               for x in tracks)
    v = sorted((round(x['x'], 6), round(x['y'], 6), round(x['size'], 6),
                round(x['drill'], 6), tuple(x.get('layers') or ()), x['net_id'])
               for x in vias)
    return t, v


def test_engine_is_identical_with_a_cancel_that_never_fires():
    """INERTNESS, at the only level that counts: the copper.

    `cancel_check=None` vs `cancel_check=lambda: False` must emit the same
    tracks and vias, coordinate for coordinate. The checks added by #621 sit at
    loop HEADS and do nothing but `break`, so a predicate that never returns
    True cannot reach a single routing decision -- and this is the measurement
    that says so, which is why no corpus A/B is needed for this change.

    (A metric that scores the same with and without the change would be no
    evidence at all. This one is the opposite: it is defined to differ the
    instant a cancel check does anything other than break a loop.)
    """
    from kicad_parser import parse_kicad_pcb
    import bga_fanout

    def run(cc):
        pcb = parse_kicad_pcb(BOARD)          # fresh board each arm
        buf = io.StringIO()
        old = sys.stdout
        sys.stdout = buf
        try:
            tr, va, _vr, failed = bga_fanout.generate_bga_fanout(
                pcb.footprints['U9'], pcb, cancel_check=cc)
        finally:
            sys.stdout = old
        return _copper(tr, va), sorted(failed)

    try:
        (t0, v0), f0 = run(None)
        (t1, v1), f1 = run(lambda: False)
    except TypeError as exc:
        # Reported as a FAILED CHECK, not a traceback: on BASE the engine has
        # no cancel_check at all, and a crash here would abort the remaining
        # tests instead of accumulating -- which is how a red run stops telling
        # you what else is red.
        check(False, f'generate_bga_fanout takes cancel_check ({exc})')
        return
    check(len(t0) > 0, f'the control arm actually routed ({len(t0)} tracks)')
    check(t0 == t1,
          f'IDENTICAL track set ({len(t0)} vs {len(t1)} segments, coordinate '
          'for coordinate)')
    check(v0 == v1, f'IDENTICAL via set ({len(v0)} vs {len(v1)})')
    check(f0 == f1, 'IDENTICAL failed-net list')
    check(bga_fanout.LAST_DEADLINE_SKIPPED == [],
          'a cancel that never fires leaves LAST_DEADLINE_SKIPPED empty')


def test_qfn_main_has_the_same_contract(tmpdir):
    """The sibling main: same flag, same exit code, same ledger split."""
    if not os.path.isfile(QFN_BOARD):
        print('  SKIP  no QFN board'); return
    p, out = _fanout(tmpdir, 'qfn_dl0.kicad_pcb', '--deadline', '0',
                     tool='qfn_fanout.py', board=QFN_BOARD, component='U2')
    if p.returncode == 2 and 'unrecognized arguments' in (p.stdout + p.stderr):
        check(False, 'qfn_fanout --deadline is a real flag')
        return
    check(p.returncode == 7, f'qfn_fanout exits 7 (got {p.returncode})')
    docs = _summaries(p.stdout)
    check(len(docs) == 1, f'exactly one JSON_SUMMARY (got {len(docs)})')
    if not docs:
        return
    d = docs[0]
    check(d.get('complete') is False and d.get('status') == 'deadline',
          'complete False / status deadline')
    skipped = d.get('deadline_skipped') or []
    check(bool(skipped), 'untried pads are named in deadline_skipped')
    check(not (set(skipped) & set(d.get('unescaped_nets') or [])),
          'and none of them is also in unescaped_nets')
    check(os.path.isfile(out), 'the output board IS written')


def run():
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        for fn, args in (
                (test_deadline_zero_stops_on_our_own_terms, (tmp,)),
                (test_no_deadline_is_field_for_field_unchanged, (tmp,)),
                (test_engine_is_identical_with_a_cancel_that_never_fires, ()),
                (test_qfn_main_has_the_same_contract, (tmp,))):
            print(fn.__name__)
            fn(*args)
    print('\nFAIL: %d check(s)' % len(BAD) if BAD else '\nOK')
    for b in BAD:
        print('  - ' + b)
    return 1 if BAD else 0


if __name__ == '__main__':
    sys.exit(run())
