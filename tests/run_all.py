#!/usr/bin/env python3
"""#382 E7: run the bare-__main__ test scripts and aggregate their exit codes.

The suite is 100+ standalone `if __name__ == "__main__": sys.exit(main())`
scripts (0 = pass, non-zero = fail). This runner discovers them, runs each as
`python3 tests/test_*.py` from the repo root (so the sys.path / kicad_files
conventions hold), and reports a pass/fail/skip summary. Exit code is 0 iff
every non-skipped test passed -- the same convention the individual scripts use.

Usage:
    python3 tests/run_all.py                 # run everything
    python3 tests/run_all.py --fast          # skip integration (CLI/board) tests
    python3 tests/run_all.py pad via         # only files whose name matches a term
    python3 tests/run_all.py --list          # print classification, run nothing
    python3 tests/run_all.py --timeout 300   # per-test timeout (seconds)
    python3 tests/run_all.py -j 1            # serial (default runs 4 in parallel)

A test is "integration" (slow; skipped by --fast) if its source shells out --
it imports run_utils or uses subprocess. That auto-classification needs no
maintained list; a new pytest-style test can instead use @pytest.mark.integration.
"""
import argparse
import glob
import os
import subprocess
import sys
import time

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS_DIR)

# Runner/helper modules that match test_*.py? None do, but be explicit.
_EXCLUDE = {'run_all.py', 'run_utils.py', 'run_doc_examples.py', 'conftest.py', 'synth.py'}

_INTEGRATION_MARKERS = ('import run_utils', 'from run_utils', 'subprocess')


def is_integration(path: str) -> bool:
    try:
        src = open(path, encoding='utf-8').read()
    except OSError:
        return False
    return any(m in src for m in _INTEGRATION_MARKERS)


def discover(filters):
    files = sorted(glob.glob(os.path.join(TESTS_DIR, 'test_*.py')))
    out = []
    for f in files:
        if os.path.basename(f) in _EXCLUDE:
            continue
        if filters and not any(term in os.path.basename(f) for term in filters):
            continue
        out.append(f)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('filters', nargs='*', help='only run files whose name contains a term')
    ap.add_argument('--fast', action='store_true', help='skip integration (CLI/board) tests')
    ap.add_argument('--timeout', type=float, default=600.0, help='per-test timeout in seconds')
    ap.add_argument('--jobs', '-j', type=int, default=4,
                    help='run this many tests in parallel (default 4; 1 = serial)')
    ap.add_argument('--list', action='store_true', help='list tests + classification, run nothing')
    args = ap.parse_args()

    tests = discover(args.filters)
    if not tests:
        print('No tests matched.')
        return 1

    if args.list:
        for f in tests:
            kind = 'integration' if is_integration(f) else 'unit'
            print(f'{kind:12s} {os.path.basename(f)}')
        print(f'\n{len(tests)} tests '
              f'({sum(is_integration(f) for f in tests)} integration).')
        return 0

    passed, failed, skipped = [], [], []
    to_run = []
    for f in tests:
        name = os.path.basename(f)
        if args.fast and is_integration(f):
            skipped.append(name)
            print(f'SKIP  {name}  (integration; --fast)')
            continue
        to_run.append(f)

    jobs = max(1, args.jobs)
    if jobs > 1 and to_run:
        # Pre-build the shared fixture boards ONCE, serially, before fanning
        # out: fixture_boards.ensure() builds into a shared kicad_files/ path,
        # and two workers racing the same build would collide (the module's
        # own __main__ exists exactly for this pre-build).
        try:
            subprocess.run([sys.executable,
                            os.path.join(TESTS_DIR, 'fixture_boards.py')],
                           cwd=ROOT, capture_output=True, text=True,
                           timeout=args.timeout)
        except subprocess.TimeoutExpired:
            print('WARN  fixture pre-build timed out; continuing')

    def run_one(f):
        name = os.path.basename(f)
        try:
            r = subprocess.run([sys.executable, f], cwd=ROOT,
                               capture_output=True, text=True, timeout=args.timeout)
        except subprocess.TimeoutExpired:
            return name, None, f'FAIL  {name}  (timeout after {args.timeout:.0f}s)'
        if r.returncode == 0:
            return name, True, f'PASS  {name}'
        tail = (r.stdout or '')[-800:] + (r.stderr or '')[-800:]
        return name, False, f'FAIL  {name}  (exit {r.returncode})\n{tail}'

    t0 = time.time()
    if jobs == 1:
        for f in to_run:
            name, ok, line = run_one(f)
            (passed if ok else failed).append(name)
            print(line)
    else:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=jobs) as ex:
            for name, ok, line in ex.map(run_one, to_run):
                (passed if ok else failed).append(name)
                print(line)

    dt = time.time() - t0
    print(f'\n{len(passed)} passed, {len(failed)} failed, {len(skipped)} skipped '
          f'in {dt:.1f}s')
    if failed:
        print('Failed: ' + ', '.join(failed))
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
