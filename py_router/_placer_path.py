"""Make the placement package importable from the router (py_placer split).

The router has ONE dependency on the placement family: `placement.groups`,
which derives component blocks for `route.py --group-by` / `--group` and for
`group_routing.py`. That package lives in ../py_placer since the placement
split, and py_router scripts run with py_router/ as sys.path[0], so a bare
`from placement.groups import ...` no longer resolves on its own.

Import this before that import:

    import _placer_path  # noqa: F401
    from placement.groups import derive_groups

APPENDED, not inserted: the router's own modules must always win a basename
clash, exactly as they did when placement/ sat inside py_router/. The isdir()
guard keeps a FLAT installed layout (PCM zip) working, where everything is
already in one directory and this is a no-op.

This is deliberately one-directional. py_placer/_path.py points the other way
(placer -> router); nothing here should grow into importing placement engines.
"""
import os
import sys

_PLACER = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'py_placer'))
if os.path.isdir(_PLACER) and _PLACER not in sys.path:
    sys.path.append(_PLACER)
