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
    moved = {os.path.splitext(f)[0] for f in os.listdir(os.path.join(ROOT, "py_router"))
             if f.endswith(".py")}
    offenders = []
    for fn in os.listdir(ROOT):
        if not fn.endswith(".py"):
            continue
        tree = ast.parse(open(os.path.join(ROOT, fn), encoding="utf-8").read())
        for node in ast.walk(tree):
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
