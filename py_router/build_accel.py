#!/usr/bin/env python3
"""Build optional mypyc-compiled accelerators for the hot pure-Python modules.

The pure-Python sources stay authoritative; this compiles a copy of each module
with mypyc and drops the resulting extension (.so/.pyd) next to the source,
where the import system prefers it. Environments without the extensions (e.g.
KiCad's bundled Python running the GUI plugin) transparently fall back to the
.py files — results are identical either way, compiled is just faster
(~5-10% on a route step, dominated by check_connected/UnionFind).

Usage:
    python3 py_router/build_accel.py            # build + deploy
    python3 py_router/build_accel.py --clean    # remove deployed accelerators
    python3 py_router/build_accel.py --status   # what is deployed, and is it stale?

Requires `mypy` (pip install mypy). Rebuild after editing any listed module —
a stale extension silently shadows the newer .py (--status flags this).
"""
import argparse
import glob
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

PY_ROUTER = os.path.dirname(os.path.abspath(__file__))
# Chosen from route-step profiles: check_connected (UnionFind replay + spatial
# queries) and geometry_utils (UnionFind, point/segment kernels) carry the bulk
# of the interpreted hot-path time; routing_utils adds the blocked-cells
# builders. pcb_modification is deliberately NOT compiled: debug workflows
# monkeypatch it, which compiled modules forbid.
MODULES = ['geometry_utils', 'check_connected', 'routing_utils']
MANIFEST = os.path.join(PY_ROUTER, 'accel_manifest.json')


def _sha(path: str) -> str:
    with open(path, 'rb') as f:
        return hashlib.sha256(f.read()).hexdigest()


def _deployed_exts():
    pats = [os.path.join(PY_ROUTER, f'{m}.cpython-*') for m in MODULES]
    pats.append(os.path.join(PY_ROUTER, '*__mypyc*'))
    out = []
    for p in pats:
        out.extend(g for g in glob.glob(p) if g.endswith(('.so', '.pyd')))
    return sorted(set(out))


def clean() -> None:
    removed = _deployed_exts()
    for p in removed:
        os.remove(p)
    if os.path.exists(MANIFEST):
        os.remove(MANIFEST)
    print(f"removed {len(removed)} accelerator file(s)" if removed
          else "no accelerators deployed")


def status() -> int:
    exts = _deployed_exts()
    if not exts:
        print("no accelerators deployed (pure-Python in use)")
        return 0
    for p in exts:
        print("deployed:", os.path.basename(p))
    if not os.path.exists(MANIFEST):
        print("WARNING: no manifest; cannot verify freshness — rebuild to be safe")
        return 1
    recorded = json.load(open(MANIFEST))
    stale = [m for m in MODULES
             if recorded.get(m) != _sha(os.path.join(PY_ROUTER, m + '.py'))]
    if stale:
        print("STALE (source edited since build; the old extension still "
              "shadows it — rebuild or --clean):", ', '.join(stale))
        return 1
    print("all accelerators match their sources")
    return 0


def build() -> int:
    try:
        import mypyc  # noqa: F401
    except ImportError:
        print("mypyc not available — install with: python3 -m pip install mypy")
        return 1
    tmp = tempfile.mkdtemp(prefix='accel_build_')
    try:
        for m in MODULES:
            shutil.copy(os.path.join(PY_ROUTER, m + '.py'), tmp)
        # Build out-of-tree: the repo root's __init__.py makes mypy treat the
        # checkout as a (possibly invalidly-named) package in-place.
        r = subprocess.run(
            [sys.executable, '-m', 'mypyc', '--ignore-missing-imports']
            + [m + '.py' for m in MODULES],
            cwd=tmp, capture_output=True, text=True)
        if r.returncode != 0:
            print(r.stdout + r.stderr)
            print("mypyc build FAILED; nothing deployed")
            return 1
        built = [p for p in glob.glob(os.path.join(tmp, '*'))
                 if p.endswith(('.so', '.pyd'))]
        if not built:
            print("mypyc reported success but produced no extensions?")
            return 1
        # Verify the compiled modules import and behave before deploying.
        check = ("import sys; sys.path[:0] = [%r, %r]; " % (tmp, PY_ROUTER)
                 + "import geometry_utils, check_connected, routing_utils; "
                 "assert geometry_utils.__file__.endswith(('.so', '.pyd')); "
                 "uf = geometry_utils.UnionFind(); uf.union((1, 2), 'a'); "
                 "assert uf.connected('a', (1, 2)) and not uf.connected('a', 'b'); "
                 "print('verify ok')")
        v = subprocess.run([sys.executable, '-c', check],
                           capture_output=True, text=True)
        if v.returncode != 0:
            print(v.stdout + v.stderr)
            print("compiled modules failed verification; nothing deployed")
            return 1
        for p in _deployed_exts():
            os.remove(p)
        for p in built:
            shutil.copy(p, PY_ROUTER)
        json.dump({m: _sha(os.path.join(PY_ROUTER, m + '.py')) for m in MODULES},
                  open(MANIFEST, 'w'), indent=1)
        print(f"deployed {len(built)} accelerator file(s) to py_router/:")
        for p in sorted(built):
            print("  ", os.path.basename(p))
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    g = ap.add_mutually_exclusive_group()
    g.add_argument('--clean', action='store_true', help='remove deployed accelerators')
    g.add_argument('--status', action='store_true', help='show deployed accelerators and staleness')
    args = ap.parse_args()
    if args.clean:
        clean()
        return 0
    if args.status:
        return status()
    return build()


if __name__ == '__main__':
    sys.exit(main())
