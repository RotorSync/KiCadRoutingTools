#!/usr/bin/env python3
"""A consumer must be able to prove this clone is the engine it pinned.

A board repository points an environment variable at a KiCadRoutingTools clone
and routes with it. The strongest check available to that consumer has been
"does route.py exist as a file" -- which passes for a clone that is on the wrong
branch, years old, or missing the module the chain depends on. The chain then
runs, prints green, and describes an engine the repo does not pin.

That is the failure a no-fallbacks rule exists to prevent, and nothing detects
it, because every other signal in the run is produced BY the thing whose
identity is in doubt.

`--capabilities` publishes the inventory; `--require` asserts against it and
exits non-zero naming the gap.
"""
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from krt_capabilities import capabilities, missing, script_flags  # noqa: E402


def _run(args, cwd=ROOT):
    return subprocess.run([sys.executable, '-X', 'utf8'] + args,
                          capture_output=True, text=True, encoding='utf-8',
                          errors='replace', cwd=cwd)


def test_the_inventory_is_json_and_complete():
    caps = capabilities()
    assert caps['schema'] == 1
    assert caps['is_git_clone'] is True, "this repo is a git clone"
    absent = [m for m, ok in caps['modules'].items() if not ok]
    assert not absent, f"modules missing from this clone: {absent}"
    print(f"  PASS: {len(caps['modules'])} modules present, inventory is JSON")


def test_flags_are_read_without_importing():
    """Asking 'can you do X' must not be able to trigger a side effect."""
    flags = script_flags(os.path.join(ROOT, 'route.py'))
    for f in ('--track-width-floor', '--json-out', '--nets', '--rip-existing-nets'):
        assert f in flags, f"{f} not discovered in route.py"
    assert script_flags(os.path.join(ROOT, 'no_such_file.py')) == []
    print("  PASS: flags read from source, missing file is empty not fatal")


def test_require_passes_on_what_this_clone_has():
    r = _run([os.path.join(ROOT, 'krt_capabilities.py'), '--require',
              'route.py:--track-width-floor', 'route.py:--json-out',
              'route_disconnected_planes.py:--net-layers',
              'place_route_loop.py:--accept-cmd', 'check_floorplan.py'])
    assert r.returncode == 0, r.stderr
    print("  PASS: --require exits 0 when every token is satisfied")


def test_require_fails_loudly_and_names_every_gap():
    """The message must name what is missing -- a consumer chasing a routing
    symptom will never guess 'wrong branch' on its own."""
    r = _run([os.path.join(ROOT, 'krt_capabilities.py'), '--require',
              'route.py:--flag-that-does-not-exist', 'no_such_module.py'])
    assert r.returncode == 3, f"expected exit 3, got {r.returncode}"
    assert '--flag-that-does-not-exist' in r.stderr
    assert 'no_such_module.py' in r.stderr
    assert 'flag not supported' in r.stderr and 'module not present' in r.stderr, \
        "the two failure kinds must be distinguishable"
    print("  PASS: --require exits 3 and names both kinds of gap")


def test_missing_is_pure_and_testable():
    caps = {'modules': {'a.py': True, 'b.py': False}, 'flags': {'a.py': ['--x']}}
    assert missing(caps, ['a.py']) == []
    assert missing(caps, ['a.py:--x']) == []
    assert missing(caps, ['b.py']) == ['b.py (module not present)']
    assert missing(caps, ['a.py:--y']) == ['a.py --y (flag not supported)']
    print("  PASS: the gap computation is a pure function")


def test_route_py_answers_without_a_board():
    """`route.py --capabilities` must work with no positional argument: the
    question is asked before there is any trust in the clone, so requiring a
    board to ask it defeats the purpose."""
    r = _run([os.path.join(ROOT, 'route.py'), '--capabilities'])
    assert r.returncode == 0, r.stderr[-800:]
    caps = json.loads(r.stdout)
    assert caps['modules']['route.py'] is True
    assert '--json-out' in caps['flags']['route.py']
    print("  PASS: route.py --capabilities needs no input_file")


if __name__ == '__main__':
    for k, v in sorted(globals().items()):
        if k.startswith('test_'):
            print(f"--- {k}")
            v()
    print("ALL PASS")
