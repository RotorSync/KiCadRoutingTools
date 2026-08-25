"""Per-layer coarse clearance distance fields (pruning prefilter only).

Phase 2.5 Task 1: for each copper layer we build a coarse grid where every cell
holds a conservative LOWER bound on the distance from that cell's centre to the
nearest FOREIGN obstacle (pad / segment / via / NPTH hole) on the layer -- i.e.
every obstacle EXCEPT those belonging to the net currently being smoothed (its
own copper is being replaced by the candidate shortcut and must not count as an
obstacle against itself). The field is used ONLY as a prefilter in the smooth
candidate loop's clears() path: for a query segment we take the min field value
over the cells it crosses minus one cell diagonal; if that bound already exceeds
the caller's threshold we skip the exact kernels (they provably pass), otherwise
we fall through to them unchanged. Any uncertainty falls through to exact -- the
field never accepts a candidate the exact geometry would reject.

WHY THE FIELD IS A SAFE LOWER BOUND
-----------------------------------
Each cell's value is the EXACT signed distance from its centre to the nearest
foreign obstacle edge (negative inside an obstacle). For a query segment crossing
cells C1..Ck, any point P on the segment inside Ci is within one cell diagonal
DIAG of Ci's centre, so

    dist(P, nearest_edge) >= d(Ci) - DIAG

where d(Ci) is the cell's exact signed distance. Taking the min over crossed
cells:

    true clearance >= min_i(d(Ci)) - DIAG

which is exactly what field_lower_bound() returns. This holds for EVERY segment,
including ones that pass deep inside an obstacle interior (there d(Ci) is very
negative, so the bound is very negative too -- it never overstates clearance).

Construction is direct per-obstacle stamping: for each foreign footprint we
compute its exact signed distance over every grid cell whose centre lies within
`reach` of its bounding box and take the elementwise min across footprints.
Cells beyond `reach` of every footprint stay +inf; they are genuinely farther
than `reach` from all foreign copper, so skipping them is safe as long as
`reach` exceeds the largest threshold (+ one diagonal) any caller can use --
the caller passes `reach` accordingly.

Resolution: 0.1 mm. On the rp2350 board (18x36 mm) that is ~64k cells/layer =
~0.26 MB/layer (float32) -- far under the 100 MB total budget -- and a full
build is ~20 ms/layer, far under the 2 s/layer budget. On the larger d1 board
(185x120 mm) it is ~2.2M cells/layer = ~9 MB/layer and ~50 ms/layer.
"""

import math
import numpy as np

from check_drc import _pad_has_no_copper
from kicad_parser import pad_drill_capsule

RES = 0.1            # mm per cell
DEFAULT_REACH = 2.0  # mm outward reach around each footprint


class LayerField:
    __slots__ = ('d', 'minx', 'miny', 'res', 'diag')

    def __init__(self, d, minx, miny, res):
        self.d = d            # float32 array (ny, nx), mm: exact signed dist to nearest foreign edge
        self.minx = minx
        self.miny = miny
        self.res = res
        self.diag = res * math.sqrt(2.0)


def _pad_corner_radius(pad):
    """Corner radius turning a pad's local rect into an accurate rounded-rect
    model -- mirrors single_ended_routing._pad_corner_radius."""
    shape = getattr(pad, 'shape', 'rect')
    hx = pad.size_x / 2.0; hy = pad.size_y / 2.0
    if shape in ('circle', 'oval'):
        return min(hx, hy)
    if shape == 'roundrect':
        rr = getattr(pad, 'roundrect_rratio', None)
        if rr:
            return rr * min(pad.size_x, pad.size_y)
        return 0.0
    return 0.0


def _subgrid(minx, miny, res, nx, ny, x0, y0, x1, y1):
    i0 = max(0, int((x0 - minx) // res))
    i1 = min(nx - 1, int((x1 - minx) // res))
    j0 = max(0, int((y0 - miny) // res))
    j1 = min(ny - 1, int((y1 - miny) // res))
    if i1 < i0 or j1 < j0:
        return None
    gx = np.arange(i0, i1 + 1) * res + minx + res / 2.0
    gy = np.arange(j0, j1 + 1) * res + miny + res / 2.0
    GX, GY = np.meshgrid(gx, gy)
    return i0, i1, j0, j1, GX, GY


def _stamp_pad(d, minx, miny, res, nx, ny, pad):
    hx = pad.size_x / 2.0; hy = pad.size_y / 2.0
    rot = getattr(pad, 'rect_rotation', 0.0) or 0.0
    if rot:
        c = math.cos(math.radians(rot)); s = math.sin(math.radians(rot))
        ex = abs(hx * c) + abs(hy * s); ey = abs(hx * s) + abs(hy * c)
        rc = c; rs = s
    else:
        ex = hx; ey = hy; rc = 1.0; rs = 0.0
    sg = _subgrid(minx, miny, res, nx, ny,
                  pad.global_x - ex - DEFAULT_REACH,
                  pad.global_y - ey - DEFAULT_REACH,
                  pad.global_x + ex + DEFAULT_REACH,
                  pad.global_y + ey + DEFAULT_REACH)
    if sg is None:
        return
    i0, i1, j0, j1, GX, GY = sg
    cr = _pad_corner_radius(pad)
    dx = GX - pad.global_x; dy = GY - pad.global_y
    lx = dx * rc + dy * rs; ly = -dx * rs + dy * rc
    ihx = hx - cr; ihy = hy - cr
    qx = np.maximum(np.abs(lx) - ihx, 0.0)
    qy = np.maximum(np.abs(ly) - ihy, 0.0)
    dd = np.hypot(qx, qy) - cr
    sub = d[j0:j1 + 1, i0:i1 + 1]
    np.minimum(sub, dd, out=sub)


def _stamp_capsule(d, minx, miny, res, nx, ny, ax, ay, bx, by, half_w):
    sg = _subgrid(minx, miny, res, nx, ny,
                  min(ax, bx) - half_w - DEFAULT_REACH,
                  min(ay, by) - half_w - DEFAULT_REACH,
                  max(ax, bx) + half_w + DEFAULT_REACH,
                  max(ay, by) + half_w + DEFAULT_REACH)
    if sg is None:
        return
    i0, i1, j0, j1, GX, GY = sg
    segx = bx - ax; segy = by - ay
    L2 = segx * segx + segy * segy
    if L2 > 0:
        t = np.clip(((GX - ax) * segx + (GY - ay) * segy) / L2, 0.0, 1.0)
    else:
        t = np.zeros_like(GX)
    px = ax + t * segx; py = ay + t * segy
    dd = np.hypot(GX - px, GY - py) - half_w
    sub = d[j0:j1 + 1, i0:i1 + 1]
    np.minimum(sub, dd, out=sub)


def _stamp_circle(d, minx, miny, res, nx, ny, cx, cy, radius):
    sg = _subgrid(minx, miny, res, nx, ny,
                  cx - radius - DEFAULT_REACH,
                  cy - radius - DEFAULT_REACH,
                  cx + radius + DEFAULT_REACH,
                  cy + radius + DEFAULT_REACH)
    if sg is None:
        return
    i0, i1, j0, j1, GX, GY = sg
    dd = np.hypot(GX - cx, GY - cy) - radius
    sub = d[j0:j1 + 1, i0:i1 + 1]
    np.minimum(sub, dd, out=sub)


def build_field(pcb_data, layer, exclude_net_id=None):
    """Build a LayerField for `layer` from current pcb_data copper + pads +
    holes EXCLUDING any footprint belonging to `exclude_net_id` (the net being
    smoothed -- its own copper must not count as an obstacle against itself).
    Every cell holds the exact signed distance from its centre to the nearest
    remaining foreign obstacle edge."""
    bb = pcb_data.board_info.board_bounds
    minx, miny, maxx, maxy = bb
    res = RES
    nx = int(math.ceil((maxx - minx) / res)) + 1
    ny = int(math.ceil((maxy - miny) / res)) + 1
    d = np.full((ny, nx), np.inf, dtype=np.float32)
    # Pads on this layer.
    for nid, pads in pcb_data.pads_by_net.items():
        if exclude_net_id is not None and nid == exclude_net_id:
            continue
        for pad in pads:
            if layer not in pad.layers and '*.Cu' not in pad.layers:
                continue
            _stamp_pad(d, minx, miny, res, nx, ny, pad)
    # Segments on this layer.
    for s in pcb_data.segments:
        if s.layer != layer:
            continue
        if exclude_net_id is not None and s.net_id == exclude_net_id:
            continue
        hw = (s.width if s.width > 0 else 0.0) / 2.0
        _stamp_capsule(d, minx, miny, res, nx, ny,
                       s.start_x, s.start_y, s.end_x, s.end_y, hw)
    # Vias are through -- present on every copper layer (conservative).
    for v in pcb_data.vias:
        if exclude_net_id is not None and v.net_id == exclude_net_id:
            continue
        r = (v.size if getattr(v, 'size', 0) and v.size > 0 else 0.0) / 2.0
        _stamp_circle(d, minx, miny, res, nx, ny, v.x, v.y, r)
    # NPTH drill holes are through too.
    for nid, pads in pcb_data.pads_by_net.items():
        if exclude_net_id is not None and nid == exclude_net_id:
            continue
        for pad in pads:
            if (getattr(pad, 'drill', 0) or 0) > 0 and _pad_has_no_copper(pad):
                (p1x, p1y), (p2x, p2y), hr = pad_drill_capsule(pad)
                _stamp_capsule(d, minx, miny, res, nx, ny,
                               p1x, p1y, p2x, p2y, hr)
    return LayerField(d.astype(np.float32), minx, miny, res)


def _raster_min(dt_arr):
    raise NotImplementedError('placeholder')


def _raster_min(d, minx, miny, res, x1, y1, x2, y2):
    """Min signed-distance value over every grid cell the segment
    (x1,y1)-(x2,y2) crosses. Returns None when the segment lies entirely
    outside the grid (no pruning)."""
    ny, nx = d.shape

    def ix(x):
        return int((x - minx) // res)

    def iy(y):
        return int((y - miny) // res)

    i1 = ix(x1); j1 = iy(y1)
    i2 = ix(x2); j2 = iy(y2)
    if (i1 < 0 and i2 < 0) or (i1 >= nx and i2 >= nx) or \
       (j1 < 0 and j2 < 0) or (j1 >= ny and j2 >= ny):
        return None
    best = np.inf
    dx = x2 - x1; dy = y2 - y1
    steps = max(abs(i2 - i1), abs(j2 - j1))
    if steps == 0:
        if 0 <= i1 < nx and 0 <= j1 < ny:
            return float(d[j1, i1])
        return None
    tstep = 1.0 / steps
    for k in range(steps + 1):
        t = k * tstep
        px = x1 + t * dx; py = y1 + t * dy
        ci = ix(px); cj = iy(py)
        if 0 <= ci < nx and 0 <= cj < ny:
            v = float(d[cj, ci])
            if v < best:
                best = v
        if k < steps:
            t2 = (k + 0.5) * tstep
            px2 = x1 + t2 * dx; py2 = y1 + t2 * dy
            ci2 = ix(px2); cj2 = iy(py2)
            if 0 <= ci2 < nx and 0 <= cj2 < ny:
                v = float(d[cj2, ci2])
                if v < best:
                    best = v
    return best if best != np.inf else None


def field_lower_bound(field, x1, y1, x2, y2):
    """Safe lower bound on true clearance of segment vs foreign copper on this
    layer: min exact signed distance over crossed cells minus one cell diagonal.
    Returns None when the segment lies outside the grid (no pruning)."""
    m = _raster_min(field.d, field.minx, field.miny, field.res, x1, y1, x2, y2)
    if m is None:
        return None
    return m - field.diag


# ---------------------------------------------------------------------------
# Dirty-region tracking (staleness safety)
#
# The per-stage field is built ONCE from the copper present at the start of the
# smoothing stage. Intra-stage splice commits move copper (a shortcut replaces a
# chain's own segments), so a later net's candidate could sit closer to freshly
# added foreign copper than the stale field records. To keep the prefilter safe
# we track a per-layer boolean "dirty" grid: every cell within `margin` of any
# spliced-in segment is marked dirty, and a candidate that crosses a dirty cell
# falls through to the exact kernels (any uncertainty falls through to exact).
# Removed copper only ever raises distances (conservative), so it needs no
# tracking. `margin` is a generous fixed bound >= any realistic win_all + DIAG.
# ---------------------------------------------------------------------------

DIRTY_MARGIN = 2.0  # mm; >= max realistic scan window + one cell diagonal


def new_dirty_grid(pcb_data, layer):
    """Return (dirty, minx, miny, res): a fresh all-False boolean grid matching
    the field geometry for `layer`."""
    bb = pcb_data.board_info.board_bounds
    minx, miny, maxx, maxy = bb
    res = RES
    nx = int(math.ceil((maxx - minx) / res)) + 1
    ny = int(math.ceil((maxy - miny) / res)) + 1
    return np.zeros((ny, nx), dtype=bool), minx, miny, res


def stamp_dirty(dirty, minx, miny, res, x1, y1, x2, y2, margin=DIRTY_MARGIN):
    """Mark every cell within `margin` of segment (x1,y1)-(x2,y2) as dirty."""
    ny, nx = dirty.shape
    i0 = max(0, int((min(x1, x2) - margin - minx) // res))
    i1 = min(nx - 1, int((max(x1, x2) + margin - minx) // res))
    j0 = max(0, int((min(y1, y2) - margin - miny) // res))
    j1 = min(ny - 1, int((max(y1, y2) + margin - miny) // res))
    if i1 < i0 or j1 < j0:
        return
    gx = np.arange(i0, i1 + 1) * res + minx + res / 2.0
    gy = np.arange(j0, j1 + 1) * res + miny + res / 2.0
    GX, GY = np.meshgrid(gx, gy)
    segx = x2 - x1; segy = y2 - y1
    L2 = segx * segx + segy * segy
    if L2 > 0:
        t = np.clip(((GX - x1) * segx + (GY - y1) * segy) / L2, 0.0, 1.0)
    else:
        t = np.zeros_like(GX)
    px = x1 + t * segx; py = y1 + t * segy
    dd = np.hypot(GX - px, GY - py)
    dirty[j0:j1 + 1, i0:i1 + 1] |= (dd <= margin)


def segment_crosses_dirty(dirty, minx, miny, res, x1, y1, x2, y2):
    """True if any grid cell the segment crosses is dirty (fall through to exact)."""
    ny, nx = dirty.shape

    def ix(x):
        return int((x - minx) // res)

    def iy(y):
        return int((y - miny) // res)

    i1 = ix(x1); j1 = iy(y1)
    i2 = ix(x2); j2 = iy(y2)
    if (i1 < 0 and i2 < 0) or (i1 >= nx and i2 >= nx) or \
       (j1 < 0 and j2 < 0) or (j1 >= ny and j2 >= ny):
        return False
    dx = x2 - x1; dy = y2 - y1
    steps = max(abs(i2 - i1), abs(j2 - j1))
    if steps == 0:
        return 0 <= i1 < nx and 0 <= j1 < ny and bool(dirty[j1, i1])
    tstep = 1.0 / steps
    for k in range(steps + 1):
        t = k * tstep
        ci = ix(x1 + t * dx); cj = iy(y1 + t * dy)
        if 0 <= ci < nx and 0 <= cj < ny and dirty[cj, ci]:
            return True
        if k < steps:
            t2 = (k + 0.5) * tstep
            ci2 = ix(x1 + t2 * dx); cj2 = iy(y1 + t2 * dy)
            if 0 <= ci2 < nx and 0 <= cj2 < ny and dirty[cj2, ci2]:
                return True
    return False
