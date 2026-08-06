#!/usr/bin/env python3
"""`open_single` -- routed-but-OPEN nets are COUNTED, not just named.

A net can keep a result while its copper leaves pads disconnected (a near-miss
stub). Before this key it landed in neither `routed_single` nor `failed_single`:
its pads showed up in `failed_multipoint` (names only), but every verdict that
counts failures -- converge.route_verdict, place_route_loop.metrics_from_summary
-- read `len(failed_single) + pad-deficit`, and a NON-multipoint open net
contributes to neither term. Probes read failures=0 on boards shipping open
copper (run-7: failed_single [] while the oracle listed six opens).

The contract under test:
  * route.py emits `open_single` (always present; multipoint nets excluded --
    their shortfall is already the pad deficit, so verdicts may add the terms).
  * route_verdict and metrics_from_summary count it.
  * Summaries WITHOUT the key (older logs) degrade to the old arithmetic.
"""
import inspect
import json
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'py_router'))  # #522
sys.path.insert(0, os.path.join(ROOT, 'py_tools'))  # #522


def test_route_verdict_counts_open_single():
    from converge import route_verdict
    base = {'failed_single': ['A'], 'open_single': ['B'],
            'failed_multipoint': [{'net_name': 'B', 'failed_pads': []}],
            'multipoint_pads_total': 0, 'multipoint_pads_connected': 0}
    n, note = route_verdict(base)
    assert n == 2, f"failed A + open B must count 2, got {n} ({note})"
    assert 'B' in note, "the open net's name must reach the note"

    legacy = dict(base)
    legacy.pop('open_single')
    n_legacy, _ = route_verdict(legacy)
    assert n_legacy == 1, (
        "a summary without the key must degrade to the old arithmetic")
    print("  PASS: route_verdict counts open_single (and degrades without it)")


def test_route_verdict_no_double_count_with_pad_deficit():
    """Multipoint opens are priced by the deficit; open_single excludes them by
    contract, so a summary carrying both terms adds cleanly."""
    from converge import route_verdict
    s = {'failed_single': [], 'open_single': ['B'],
         'failed_multipoint': [{'net_name': 'B', 'failed_pads': []},
                               {'net_name': 'MP', 'failed_pads': []}],
         'multipoint_pads_total': 10, 'multipoint_pads_connected': 7}
    n, _ = route_verdict(s)
    assert n == 4, f"open B (1) + pad deficit (3) must count 4, got {n}"
    print("  PASS: open_single adds to the pad deficit without double-count")


def test_metrics_from_summary_counts_open_single():
    from place_route_loop import metrics_from_summary
    s = {'failed_single': ['A'], 'open_single': ['B'],
         'failed_multipoint': [{'net_name': 'B', 'failed_pads': []}],
         'multipoint_pads_total': 0, 'multipoint_pads_connected': 0,
         'blockers': []}
    m = metrics_from_summary(s)
    assert m['failures'] == 2, f"expected 2 failures, got {m['failures']}"
    assert m['failed_nets'].count('B') == 1, (
        "the open net is a target exactly once (via failed_multipoint)")

    legacy = dict(s)
    legacy.pop('open_single')
    assert metrics_from_summary(legacy)['failures'] == 1
    print("  PASS: metrics_from_summary counts open_single")


def test_route_py_classification_has_the_third_bucket():
    """Source-level: the scope loop must classify kept-result broken nets."""
    import route
    src = inspect.getsource(route.batch_route)
    m = re.search(r"for net_name, net_id in single_ended_nets:(.*?)\n\n",
                  src, re.S)
    assert m, "the summary classification loop moved -- update this test"
    loop = m.group(0)
    assert 'open_single.append' in loop, (
        "a net with a result but broken pads must land in open_single, "
        "not fall through unclassified")
    assert 'is_multipoint' in loop, (
        "multipoint nets must be excluded from open_single (their shortfall "
        "is already the pad deficit -- counting both double-charges)")
    assert "'open_single': open_single" in src, (
        "the summary dict must carry the key")
    print("  PASS: route.py classifies the routed-but-OPEN bucket")


def test_end_to_end_key_present_and_clean():
    """A real run emits the key (empty on a clean route)."""
    board = os.path.join(ROOT, 'kicad_files', 'splitflap_driver.kicad_pcb')
    if not os.path.isfile(board):
        print("  SKIP: fixture missing")
        return
    with tempfile.TemporaryDirectory() as td:
        js = os.path.join(td, 's.json')
        out = os.path.join(td, 's.kicad_pcb')
        r = subprocess.run([sys.executable, '-X', 'utf8',
                            os.path.join(ROOT, 'py_router', 'route.py'), board, out,
                            '--nets', 'GND', '--json-out', js],
                           capture_output=True, text=True, encoding='utf-8',
                           errors='replace', cwd=ROOT)
        assert r.returncode == 0, r.stdout[-2000:]
        summary = json.load(open(js, encoding='utf-8'))
        assert 'open_single' in summary, (
            "open_single must always be present so verdict consumers can "
            "rely on it (absent = pre-key log, not 'clean')")
        assert summary['open_single'] == [], (
            f"clean GND route reported open nets: {summary['open_single']}")
    print("  PASS: real run emits open_single (empty when clean)")


if __name__ == '__main__':
    fns = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
    for fn in fns:
        print(f"--- {fn.__name__}")
        fn()
    print("ALL PASS")
