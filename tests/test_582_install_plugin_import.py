"""install_plugin.py must be importable/runnable from any cwd (issue #582).

The #522 refactor moved the engine into py_router/, but install_plugin.py kept a
flat `from startup_checks import get_cargo_version`. It therefore died on import
-- before parsing a single argument -- so the documented manual-install path was
unusable on main, and no test noticed because nothing exercised the script.

`--help` is enough: argparse exits 0 only if every module-level import resolved
first, so this catches the whole class without installing anything.
"""
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "install_plugin.py")


def _run(cwd):
    return subprocess.run([sys.executable, SCRIPT, "--help"],
                          cwd=cwd, capture_output=True, text=True)


def test_help_from_repo_root():
    r = _run(ROOT)
    assert r.returncode == 0, f"install_plugin.py --help failed:\n{r.stderr}"
    assert "usage: install_plugin.py" in r.stdout


def test_help_from_foreign_cwd():
    """python puts the SCRIPT's dir on sys.path, not the cwd -- so an absolute
    invocation from anywhere must still resolve py_router.*."""
    with tempfile.TemporaryDirectory() as d:
        r = _run(d)
    assert r.returncode == 0, f"install_plugin.py --help failed from {d}:\n{r.stderr}"


def test_no_flat_imports_of_moved_modules():
    """No root-level script may flat-import a module that now lives in py_router/."""
    import ast
    moved = set()
    for pkg in ("py_router", "py_tools", "py_placer"):
        d = os.path.join(ROOT, pkg)
        if os.path.isdir(d):
            moved |= {os.path.splitext(f)[0] for f in os.listdir(d) if f.endswith(".py")}
    offenders = []

    def _unconditional(tree):
        """Imports that run on execution: module body + module-level if/try blocks,
        but NOT inside a def/class. A function-local `import x` in a try/except is a
        deliberate best-effort lookup (krt_capabilities), not a load-time failure;
        an import under `if __name__ == '__main__'` DOES run and must be caught
        (that is exactly how check_rigid_consistency.py was broken)."""
        stack = list(tree.body)
        while stack:
            n = stack.pop()
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            yield n
            for f in ("body", "orelse", "finalbody", "handlers"):
                stack.extend(getattr(n, f, []) or [])

    for fn in os.listdir(ROOT):
        if not fn.endswith(".py"):
            continue
        src = open(os.path.join(ROOT, fn), encoding="utf-8").read()
        # Two legitimate conventions in this repo: import as py_router.<mod>, OR
        # put the package dir on sys.path first (grade_final, compare_to_original,
        # check_rigid_consistency). Only flag a flat import with NEITHER -- which
        # is what made install_plugin.py die on import (#582).
        if any(f"'{pkg}'" in src or f'"{pkg}"' in src
               for pkg in ("py_router", "py_tools", "py_placer")) and "sys.path" in src:
            continue
        tree = ast.parse(src)
        for node in _unconditional(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module.split(".")[0] in moved:
                    offenders.append(f"{fn}:{node.lineno}: from {node.module} import ...")
            elif isinstance(node, ast.Import):
                for a in node.names:
                    if a.name.split(".")[0] in moved:
                        offenders.append(f"{fn}:{node.lineno}: import {a.name}")
    assert not offenders, ("root scripts must import moved modules as py_router.<mod>:\n  "
                           + "\n  ".join(offenders))


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as e:
                fails += 1
                print(f"FAIL {name}: {e}")
    sys.exit(1 if fails else 0)
