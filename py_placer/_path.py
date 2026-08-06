"""Path bootstrap for the placement family (#522 layout, py_placer split).

The placement engine and its CLIs import BOTH the router engine modules
(kicad_parser, routing_defaults, route, obstacle_map, cli_banner, ...) which
live in ../py_router, and the read-only instruments (check_floorplan,
render_placement, ...) which live in ../py_tools. Import this FIRST in every
py_placer script:

    import _path  # noqa: F401

Running `python3 py_placer/<tool>.py` puts py_placer/ at sys.path[0], so this
module always resolves; it then makes both siblings importable flat, exactly
like py_tools/_path.py does for the tools.

Order matters: py_router goes on FIRST so the engine wins any basename clash,
matching what a py_router script itself sees.
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
for _sib in ('py_tools', 'py_router'):
    _p = os.path.abspath(os.path.join(_HERE, '..', _sib))
    if _p not in sys.path:
        sys.path.insert(0, _p)
# The placement package itself (py_placer/placement) is a subpackage of this
# directory, which is already sys.path[0] for a directly-run script -- but not
# when py_placer is imported from elsewhere, so pin it too.
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
