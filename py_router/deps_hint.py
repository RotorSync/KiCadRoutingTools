"""Friendly ImportError for optional-but-required third-party deps.

The plane engines need shapely (polygon union / Voronoi zone geometry). A raw
`ModuleNotFoundError: No module named 'shapely'` gives the user no hint about
what to install or where. This helper wraps the import so the failure names the
dependency and the fix, and points at the repo's own venv when it exists.
"""
from __future__ import annotations

import os


def _venv_hint() -> str:
    """Point at the repo's eda venv when it exists (the documented install path)."""
    here = os.path.dirname(os.path.abspath(__file__))
    for _rel in ("../.venv", "../eda/.venv", "../../.venv"):
        _p = os.path.normpath(os.path.join(here, _rel))
        if os.path.isdir(_p):
            return f" (the repo's venv is at {_p})"
    return ""


def require_shapely():
    """Import shapely or raise a friendly ImportError naming the dependency."""
    try:
        import shapely  # noqa: F401
        return shapely
    except ImportError as e:
        raise ImportError(
            "shapely is required for plane/zone geometry (polygon union, "
            "Voronoi zone boundaries, plane resistance). Install it with:\n"
            "    pip install shapely\n"
            "or, for the repo's documented environment:\n"
            "    /home/austin/eda/.venv/bin/pip install shapely"
            + _venv_hint()
        ) from e
