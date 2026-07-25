"""Exact zone-fill geometry from KiCad itself (pcbnew ZONE_FILLER).

Why this exists
---------------
ZoneFillModel and the oracle's island tracer are RASTER APPROXIMATIONS of
KiCad's fill. At marginal sites -- a pour pinched to ~the min-thickness
threshold, a corridor carved by clearance the model prices differently --
the approximation grades the fill CONNECTED where KiCad's exact polygon
math splits it. Every repair actor keyed off the model (region joiner,
island tracer) then sees one region where KiCad sees two, and the only
remaining actor is the link-router, which cannot handle zone|zone links
(zero-length: both endpoints are the pinch point) or walled endpoints.

This module asks KiCad for the truth: drive pcbnew's ZONE_FILLER headless
(refill + save, the fill-fidelity ground-truth recipe), then parse the
saved board's (filled_polygon ...) blocks. Each filled_polygon is one
connected island polygon -- island discovery comes free with the geometry,
so one pcbnew run yields both the diagnosis and the strap targets.

The pcbnew script is STRAIGHT-LINE module scope on purpose: pcbnew
scripting segfaults when board work happens inside functions/loops.

Availability: needs KiCad's bundled python (macOS/Windows) or a system
python with pcbnew (Linux distro installs). All entry points degrade to
None when unavailable; callers fall back to the raster model.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Dict, List, Optional, Tuple

EXACT_FILL_TIMEOUT = 300

_REFILL_SCRIPT = """\
import sys
import pcbnew
src = sys.argv[1]
dst = sys.argv[2]
board = pcbnew.LoadBoard(src)
filler = pcbnew.ZONE_FILLER(board)
zones = board.Zones()
filler.Fill(zones)
pcbnew.SaveBoard(dst, board)
print("REFILL_OK", len(list(zones)))
"""

_KICAD_PYTHONS = [
    "/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/"
    "Versions/Current/bin/python3",
    "/usr/bin/python3",
    os.path.expandvars(r"C:\Program Files\KiCad\bin\python.exe"),
]


def find_kicad_python() -> Optional[str]:
    """Path of a python that can import pcbnew, or None."""
    for cand in _KICAD_PYTHONS:
        if cand and os.path.isfile(cand):
            return cand
    return None


def _balanced_block(text: str, start: int) -> Tuple[str, int]:
    """The parenthesized block starting at text[start] == '(' and the index
    one past its closing paren. Quote-aware (net names may hold parens)."""
    depth = 0
    i = start
    n = len(text)
    in_str = False
    while i < n:
        c = text[i]
        if in_str:
            if c == '"' and text[i - 1] != '\\':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == '(':
            depth += 1
        elif c == ')':
            depth -= 1
            if depth == 0:
                return text[start:i + 1], i + 1
        i += 1
    return text[start:], n


_XY_RE = re.compile(r'\(xy\s+([-\d.]+)\s+([-\d.]+)\)')
_LAYER_RE = re.compile(r'\(layer\s+"([^"]+)"\)')
_NET_ID_RE = re.compile(r'\(net\s+(\d+)\)')
_NET_NAME_RE = re.compile(r'\(net_name\s+"((?:[^"\\]|\\.)*)"\)')
# KiCad 10 files reference zone nets BY NAME with no net table (#344 class):
# (net "Earth") -- there is no numeric token and no net_name attribute.
_NET_STR_RE = re.compile(r'\(net\s+"((?:[^"\\]|\\.)*)"\)')


def refill_islands(board_file: str, timeout: int = EXACT_FILL_TIMEOUT,
                   verbose: bool = False
                   ) -> Optional[Dict[Tuple[str, str],
                                      List[List[Tuple[float, float]]]]]:
    """{(net_name, layer): [island_polygon, ...]} from a KiCad refill of
    `board_file`, or None when pcbnew is unavailable / the refill fails.

    The board is staged into a temp dir WITH its sibling .kicad_pro so the
    refill runs at the project's real netclasses (a bare board refills at
    stock rules and shrinks tight pours -- the phantom-divergence trap).
    Each (filled_polygon ...) block of the saved board is one connected
    island polygon (KiCad's fracture output), so island discovery is free.
    """
    kpy = find_kicad_python()
    if kpy is None:
        return None
    tmpdir = tempfile.mkdtemp(prefix='exact_fill_')
    try:
        stem = os.path.splitext(os.path.basename(board_file))[0]
        staged = os.path.join(tmpdir, stem + '.kicad_pcb')
        shutil.copyfile(board_file, staged)
        sib_pro = os.path.splitext(board_file)[0] + '.kicad_pro'
        if os.path.isfile(sib_pro):
            shutil.copyfile(sib_pro, os.path.join(tmpdir,
                                                  stem + '.kicad_pro'))
        script = os.path.join(tmpdir, 'refill.py')
        with open(script, 'w') as f:
            f.write(_REFILL_SCRIPT)
        filled = os.path.join(tmpdir, stem + '_filled.kicad_pcb')
        r = subprocess.run([kpy, script, staged, filled],
                           capture_output=True, text=True, timeout=timeout)
        if 'REFILL_OK' not in (r.stdout or '') or not os.path.isfile(filled):
            if verbose:
                print(f"  (exact-fill refill failed: rc={r.returncode} "
                      f"{(r.stderr or '').strip()[-200:]})")
            return None
        with open(filled, 'r', encoding='utf-8') as f:
            text = f.read()
    except Exception as e:
        if verbose:
            print(f"  (exact-fill unavailable: {e})")
        return None
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return parse_filled_islands(text)


def parse_filled_islands(text: str
                         ) -> Dict[Tuple[str, str],
                                   List[List[Tuple[float, float]]]]:
    """Parse (zone ...)/(filled_polygon ...) blocks of saved board text into
    {(net_name, layer): [island_polygon, ...]}. Net resolution: the zone's
    net_name token when present (KiCad <=9 keeps it), else the numeric
    (net N) mapped through the file's net table / item net names."""
    id_to_name: Dict[int, str] = {}
    for m in re.finditer(r'\(net\s+(\d+)\s+"((?:[^"\\]|\\.)*)"\)', text):
        id_to_name[int(m.group(1))] = m.group(2)
    out: Dict[Tuple[str, str], List[List[Tuple[float, float]]]] = {}
    pos = 0
    while True:
        z = text.find('(zone', pos)
        if z < 0:
            break
        block, pos = _balanced_block(text, z)
        head = block[:block.find('(filled_polygon')] \
            if '(filled_polygon' in block else block
        nm = _NET_NAME_RE.search(head) or _NET_STR_RE.search(head)
        net_name = nm.group(1) if nm else None
        if net_name is None:
            ni = _NET_ID_RE.search(head)
            if ni:
                net_name = id_to_name.get(int(ni.group(1)))
        if not net_name:
            continue
        fp_pos = 0
        while True:
            fp = block.find('(filled_polygon', fp_pos)
            if fp < 0:
                break
            fp_block, fp_pos = _balanced_block(block, fp)
            lm = _LAYER_RE.search(fp_block)
            if not lm:
                continue
            poly = [(float(a), float(b))
                    for a, b in _XY_RE.findall(fp_block)]
            if len(poly) >= 3:
                out.setdefault((net_name, lm.group(1)), []).append(poly)
    return out


def point_in_poly(x: float, y: float,
                  poly: List[Tuple[float, float]]) -> bool:
    """Even-odd ray cast."""
    inside = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            xc = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < xc:
                inside = not inside
    return inside


def sample_poly_edges(poly: List[Tuple[float, float]], step: float = 0.25,
                      cap: int = 4000) -> List[Tuple[float, float]]:
    """Points along the polygon boundary every ~`step` mm (vertices always
    included), decimated to <= cap points."""
    import math
    pts: List[Tuple[float, float]] = []
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        pts.append((x1, y1))
        seg_len = math.hypot(x2 - x1, y2 - y1)
        for k in range(1, int(seg_len / step)):
            t = k * step / seg_len
            pts.append((x1 + (x2 - x1) * t, y1 + (y2 - y1) * t))
    if len(pts) > cap:
        stride = len(pts) // cap + 1
        pts = pts[::stride]
    return pts
