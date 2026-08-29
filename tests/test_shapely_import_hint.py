#!/usr/bin/env python3
"""Issue 3: a missing shapely must fail with a friendly, actionable message.

The plane engines need shapely (polygon union / Voronoi zone geometry). A raw
`ModuleNotFoundError: No module named 'shapely'` gives the user no hint about
what to install or where. This test pins that the failure names the dependency
and the fix.

Run:
    python3 tests/test_shapely_import_hint.py [-v]
"""
import argparse
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYROUTER = os.path.join(ROOT, 'py_router')

_BLOCKER = '''
import sys
sys.path.insert(0, {pyrouter!r})
import importlib.abc
class _Blocker(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name == 'shapely' or name.startswith('shapely.'):
            raise ImportError('No module named ' + repr(name))
        return None
sys.meta_path.insert(0, _Blocker())
try:
    {body}
except ImportError as e:
    print('CAUGHT:', str(e))
    raise SystemExit(0)
print('NO ERROR')
raise SystemExit(1)
'''


def _run_blocked(body, verbose):
    """Run `body` in a subprocess with shapely blocked, return output."""
    with tempfile.NamedTemporaryFile('w', suffix='.py', delete=False) as f:
        f.write(_BLOCKER.format(body=body, pyrouter=PYROUTER))
        path = f.name
    try:
        r = subprocess.run([sys.executable, path], cwd=ROOT,
                           capture_output=True, text=True, encoding='utf-8',
                           errors='replace')
    finally:
        os.unlink(path)
    txt = r.stdout + r.stderr
    if verbose:
        print(txt)
    return txt


def test_require_shapely_friendly(verbose):
    txt = _run_blocked(
        "from deps_hint import require_shapely; require_shapely()", verbose)
    assert 'NO ERROR' not in txt, 'shapely import unexpectedly succeeded'
    assert 'CAUGHT:' in txt, f'no friendly message: {txt}'
    assert 'shapely' in txt.lower(), f'message does not name shapely: {txt}'
    assert 'pip install shapely' in txt, f'message lacks install hint: {txt}'
    if verbose:
        print('  require_shapely friendly message: PASS')


def test_plane_zone_geometry_friendly(verbose):
    txt = _run_blocked("import plane_zone_geometry", verbose)
    assert 'NO ERROR' not in txt, 'plane_zone_geometry imported without shapely'
    assert 'CAUGHT:' in txt, f'no friendly message: {txt}'
    assert 'pip install shapely' in txt, f'message lacks install hint: {txt}'
    if verbose:
        print('  plane_zone_geometry friendly message: PASS')


def test_plane_resistance_friendly(verbose):
    txt = _run_blocked("import plane_resistance", verbose)
    assert 'NO ERROR' not in txt, 'plane_resistance imported without shapely'
    assert 'CAUGHT:' in txt, f'no friendly message: {txt}'
    assert 'pip install shapely' in txt, f'message lacks install hint: {txt}'
    if verbose:
        print('  plane_resistance friendly message: PASS')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('-v', '--verbose', action='store_true')
    args = ap.parse_args()
    fails = []
    for fn in (test_require_shapely_friendly,
               test_plane_zone_geometry_friendly,
               test_plane_resistance_friendly):
        try:
            fn(args.verbose)
        except AssertionError as e:
            fails.append(f'{fn.__name__}: {e}')
        except Exception as e:
            fails.append(f'{fn.__name__}: {type(e).__name__}: {e}')
    if fails:
        print('FAIL: ' + '; '.join(fails))
        return 1
    print('PASS: missing shapely raises a friendly, actionable ImportError')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
