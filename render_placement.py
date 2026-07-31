#!/usr/bin/env python3
"""Headless PNG stills of placement status (#431).

A routed board is self-evidently inspectable: open it in KiCad, the tracks are
there. A placement repair is a set of position deltas whose merit is invisible
without the context that motivated it -- which airwires were failing, which nets
walled them off, what moved and how far. `docs/placement-optimization.md` names
the acceptance criterion ("output that is visibly 'your placement, nudged' can
be reviewed in minutes") but nothing produced an artifact to review.

This composes; it does not re-render. Every pixel goes through
`route_render.BoardRenderer` (board outline, cutouts, zones, pads, drills, the
caption HUD) via its `overlays=` hook, and every world->pixel conversion through
`renderer.tf`. There is no coordinate arithmetic in this file -- a test asserts
the rect handed to the drawing layer is `state.parts[ref].rect()` identically,
which fails the day someone inlines a courtyard transform.

    python3 render_placement.py BOARD.kicad_pcb [-o OUT.png|OUTDIR] [options]
    python3 render_placement.py BOARD --before SEED --arrows -o delta.png
    python3 render_placement.py BOARD --zoom-group sheet:1a2b --per-side -o dir/
    python3 render_placement.py --per-round WORKDIR -o dir/

Known limits, stated because the artifact should not oversell (the issue's own
list): a dense board does not fit one readable frame, which is why delta-first
and focused crops are defaults rather than polish; a flat image projects away
the layer dimension; and geometry is not causality -- arrows show WHAT moved,
never why it helped. The caption strip carries the verdict, and a render without
it invites exactly the wrong review heuristic.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from typing import Dict, List, Optional, Sequence, Tuple

from PIL import ImageDraw

import routing_defaults as defaults
from kicad_parser import parse_kicad_pcb
from route_render import BoardRenderer, load_font

# --- palette (OmniLayout's categories: outline / THT / top SMD / back SMD) ----
C_COURT_F = (150, 152, 168)     # front courtyard
C_COURT_B = (108, 132, 160)     # back courtyard (cooler, like B.Cu)
C_COURT_DIM = (58, 60, 70)      # context part in delta-first mode
C_LOCKED = (92, 88, 74)         # locked: dimmed, and hatched
C_GHOST = (76, 76, 92)          # seed position
C_ARROW = (236, 214, 110)       # displacement
C_AIR = (86, 96, 112)           # ordinary airwire
C_AIR_FAIL = (232, 72, 72)      # failed net
C_AIR_BLOCK = (236, 158, 60)    # blocker net
C_AIR_PICK = (96, 214, 170)     # net named by --ratsnest-nets
C_LABEL = (226, 228, 238)
C_PAD_THT = (196, 150, 74)      # through-hole
C_PAD_F = (198, 172, 96)        # front SMD
C_PAD_B = (104, 150, 196)       # back SMD


# ---------------------------------------------------------------------------
# Model: what to draw, computed from the board (no PIL below this line)
# ---------------------------------------------------------------------------
class PlacementModel:
    """Everything drawable, derived once from a board.

    Uses a real `QuenchState` so the rects, airwires and metrics are the ones
    the OPTIMIZER acted on -- including the sorted-net_refs MST tie-break (#457)
    -- rather than a parallel implementation that drifts. Measured cost:
    interf_u 0.15s, ulx3s 0.70s, glasgow_revC 0.81s.
    """

    def __init__(self, pcb, pcb_file: str, *, exact: bool = True,
                 quench_kwargs: Optional[Dict] = None):
        self.pcb = pcb
        self.pcb_file = pcb_file
        self.state = None
        self.no_outline = False
        self.metrics: Dict[str, object] = {}
        if exact:
            self.state = self._build_state(quench_kwargs or {})
        if self.state is not None:
            self.metrics = self._metrics()

    def _build_state(self, kw):
        from placement.quench import QuenchState
        args = dict(clearance=defaults.CLEARANCE, board_edge_clearance=0.55,
                    crossing_penalty=10.0, halo_base=0.5, halo_coef=0.25,
                    halo_weight=2.0, edge_halo=2.0, edge_weight=2.0,
                    grid_step=defaults.GRID_STEP, length_weight=1.0)
        args.update({k: v for k, v in kw.items() if v is not None})
        try:
            return QuenchState(self.pcb, self.pcb_file, **args)
        except ValueError as e:
            # "No board boundary (Edge.Cuts) found" -- the optimizer genuinely
            # cannot gate containment without an outline, and relaxing that
            # guard to suit a VIEWER would be backwards. Fall back on an
            # in-memory bounds so everything that does NOT depend on the outline
            # (courtyards, sides, airwires, crossings, HPWL, overlap) stays
            # exact, and report oob as unavailable.
            if 'Edge.Cuts' not in str(e):
                raise
            from route_render import _geometry_bounds
            self.no_outline = True
            self.pcb.board_info.board_bounds = _geometry_bounds(self.pcb)
            return QuenchState(self.pcb, self.pcb_file, **args)

    def _metrics(self) -> Dict[str, object]:
        m = dict(self.state.total_cost())
        leg = self.state.legality_metrics()
        m.update(leg)
        if self.no_outline:
            # oob is meaningless without a real outline; say so rather than
            # printing a zero that reads as "clean".
            for k in ('oob_count', 'oob_amount', 'oob_area'):
                m[k] = None
        return m

    # -- geometry -----------------------------------------------------------
    def parts(self):
        return self.state.parts if self.state is not None else {}

    def rect(self, ref):
        """The part's courtyard rect, from the optimizer's own model."""
        p = self.state.parts.get(ref)
        return None if p is None else p.rect()

    def side(self, ref) -> str:
        p = self.state.parts.get(ref)
        return 'F' if p is None else p.side

    def sides(self, ref):
        p = self.state.parts.get(ref)
        return {'F'} if p is None else set(p.sides)

    def far_rect(self, ref):
        """Far-side footprint of a through-hole part: its DRILLED-PAD box, not
        its courtyard. `legality.rect_on` gates on exactly this, so drawing the
        courtyard on both sides would disagree with the grader."""
        p = self.state.parts.get(ref)
        if p is None or not getattr(p, 'has_tht', False):
            return None
        try:
            return p.tht_rect()
        except Exception:
            return None

    def airwires(self, net_ids=None):
        """(x1,y1,x2,y2,net_id) tuples, from the state's prebuilt cache."""
        out = []
        for nid, aws in sorted((self.state.net_airwires or {}).items()):
            if net_ids is not None and nid not in net_ids:
                continue
            out.extend(aws)
        return out

    def net_ids_for(self, names) -> set:
        want = set(names or ())
        return {nid for nid, n in self.pcb.nets.items()
                if nid > 0 and n.name in want}

    def net_ids_matching(self, patterns) -> set:
        """Net ids matching glob patterns, via the SHARED filter.

        `net_queries.matches_net_filter` is what `route.py --nets` and the plan
        executor use, so `'*USB*' '!*_N'` means here exactly what it means
        there -- including that a leading `!` is an exclusion, with the
        active-low caveat that helper already documents. A second matcher in a
        viewer would be a second set of surprises.
        """
        if not patterns:
            return set()
        from net_queries import matches_net_filter
        return {nid for nid, n in self.pcb.nets.items()
                if nid > 0 and n.name and matches_net_filter(n.name, list(patterns))}

    def refs_of_nets(self, net_ids) -> set:
        refs = set()
        for nid in net_ids:
            refs |= set(self.state.net_refs.get(nid, ()))
        return refs

    def net_points(self, net_ids) -> List[Tuple[float, float]]:
        pts = []
        for nid in sorted(net_ids):
            for ref in self.state.net_refs.get(nid, ()):
                p = self.state.parts.get(ref)
                if p is not None:
                    pts.extend((x, y) for x, y, _ in p.pad_globals())
        return pts


def moved_parts(before_pcb, after_pcb, tol: float = 1e-4) -> List[Dict]:
    """Refs whose position changed, with both poses. Sorted (#457)."""
    out = []
    for ref, a in sorted((before_pcb.footprints or {}).items()):
        b = (after_pcb.footprints or {}).get(ref)
        if b is None:
            continue
        if abs(a.x - b.x) > tol or abs(a.y - b.y) > tol \
                or abs((a.rotation or 0) - (b.rotation or 0)) > 1e-3:
            out.append({'reference': ref,
                        'from': (a.x, a.y, a.rotation or 0.0),
                        'to': (b.x, b.y, b.rotation or 0.0),
                        'dist': math.hypot(b.x - a.x, b.y - a.y)})
    return out


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------
def union_view(rects, pad_mm: float = 2.0, min_size: float = 8.0):
    """Bounding rect over `rects`, padded and floored.

    Generalizes animate_fanout_clearance._view_bounds' idea (frame to what will
    be DRAWN, not to the board). The min_size floor is why one nudged 0402 does
    not fill the screen.
    """
    xs, ys = [], []
    for r in rects:
        if not r:
            continue
        xs += [r[0], r[2]]
        ys += [r[1], r[3]]
    if not xs:
        return None
    x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
    x0, y0, x1, y1 = x0 - pad_mm, y0 - pad_mm, x1 + pad_mm, y1 + pad_mm
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    w, h = max(x1 - x0, min_size), max(y1 - y0, min_size)
    return (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)


def cluster_points(points, gap: float) -> List[List[Tuple[float, float]]]:
    """Single-link clusters of pad coordinates, deterministic in input order.

    Used for --focus. The issue assumed failure COORDINATES were in the #409
    JSON; they are not -- `blocking_info_to_dict` carries net names and cell
    counts only -- so the geometry comes from the board.
    """
    pts = sorted(set((round(x, 4), round(y, 4)) for x, y in points))
    clusters: List[List[Tuple[float, float]]] = []
    for p in pts:
        hit = [c for c in clusters
               if any(math.hypot(p[0] - q[0], p[1] - q[1]) <= gap for q in c)]
        if not hit:
            clusters.append([p])
        else:
            merged = [p]
            for c in hit:
                merged += c
                clusters.remove(c)
            clusters.append(merged)
    clusters.sort(key=lambda c: (-len(c), min(q[0] for q in c),
                                 min(q[1] for q in c)))
    return clusters


# ---------------------------------------------------------------------------
# Overlays -- all drawing goes through renderer.tf, never raw coordinates
# ---------------------------------------------------------------------------
def _w(r, mm: float, floor: int = 1) -> int:
    """A width in mm -> device px, honouring supersampling. A hardcoded pixel
    width silently halves at supersample=2."""
    return max(floor, int(round(r.tf.length(mm))))


def _rect_pts(r, rect):
    x0, y0 = r.tf.pt(rect[0], rect[1])
    x1, y1 = r.tf.pt(rect[2], rect[3])
    return [min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)]


def _dash(d, p0, p1, on_px, off_px, fill, width):
    """PIL has no dashed line."""
    x0, y0 = p0
    x1, y1 = p1
    total = math.hypot(x1 - x0, y1 - y0)
    if total < 1e-6:
        return
    ux, uy = (x1 - x0) / total, (y1 - y0) / total
    t = 0.0
    while t < total:
        e = min(t + on_px, total)
        d.line([x0 + ux * t, y0 + uy * t, x0 + ux * e, y0 + uy * e],
               fill=fill, width=width)
        t = e + off_px


def draw_courtyards(d, r, model, refs, *, side=None, color=None, dim=False,
                    locked=(), width_mm=0.12):
    for ref in refs:
        rect = model.rect(ref)
        if rect is None:
            continue
        own = model.side(ref)
        if side is not None and side not in model.sides(ref):
            continue
        if side is not None and own != side:
            # Far side of a through-hole part: its DRILLED-PAD box, which is
            # what legality.rect_on gates on.
            rect = model.far_rect(ref)
            if rect is None:
                continue
        col = color or (C_COURT_DIM if dim else
                        (C_COURT_B if own == 'B' else C_COURT_F))
        if ref in locked:
            col = C_LOCKED
        box = _rect_pts(r, rect)
        d.rectangle(box, outline=col, width=_w(r, width_mm))
        if ref in locked:      # hatch so "locked" reads without a legend
            d.line([box[0], box[1], box[2], box[3]], fill=col, width=_w(r, 0.06))


def draw_ghosts(d, r, model, moves, *, width_mm=0.1):
    """Dashed rect at each part's seed pose -- the placement diff, drawn."""
    for m in moves:
        rect = model.rect(m['reference'])
        if rect is None:
            continue
        dx = m['from'][0] - m['to'][0]
        dy = m['from'][1] - m['to'][1]
        box = _rect_pts(r, (rect[0] + dx, rect[1] + dy, rect[2] + dx, rect[3] + dy))
        w = _w(r, width_mm)
        on, off = max(3, w * 3), max(3, w * 3)
        _dash(d, (box[0], box[1]), (box[2], box[1]), on, off, C_GHOST, w)
        _dash(d, (box[2], box[1]), (box[2], box[3]), on, off, C_GHOST, w)
        _dash(d, (box[2], box[3]), (box[0], box[3]), on, off, C_GHOST, w)
        _dash(d, (box[0], box[3]), (box[0], box[1]), on, off, C_GHOST, w)


def draw_arrows(d, r, moves, *, head_mm=0.7, width_mm=0.14, min_px=5):
    for m in moves:
        sx, sy = r.tf.pt(m['from'][0], m['from'][1])
        ex, ey = r.tf.pt(m['to'][0], m['to'][1])
        if math.hypot(ex - sx, ey - sy) < min_px:
            continue                      # too short to read; the ghost says it
        w = _w(r, width_mm)
        d.line([sx, sy, ex, ey], fill=C_ARROW, width=w)
        ang = math.atan2(ey - sy, ex - sx)
        h = max(4.0, r.tf.length(head_mm))
        for s in (+1, -1):
            a = ang + s * math.radians(150)
            d.line([ex, ey, ex + h * math.cos(a), ey + h * math.sin(a)],
                   fill=C_ARROW, width=w)


def draw_airwires(d, r, airwires, *, color=C_AIR, width_mm=0.05):
    w = _w(r, width_mm)
    for (x1, y1, x2, y2, _nid) in airwires:
        d.line([*r.tf.pt(x1, y1), *r.tf.pt(x2, y2)], fill=color, width=w)


def draw_ref_labels(d, r, model, refs, *, min_px=14, lo=11, hi=34):
    """Reference designators, sized to the PART rather than to the image.

    Scaling the font by image height alone gives an 8px label on a 400px render
    whatever the zoom -- drawn, technically present, and unreadable. A label is
    only useful if you can read it, so it is sized from the part's own footprint
    on screen and skipped entirely when the part is too small to carry one.
    """
    for ref in refs:
        rect = model.rect(ref)
        if rect is None:
            continue
        box = _rect_pts(r, rect)
        w, h = box[2] - box[0], box[3] - box[1]
        if max(w, h) < min_px:
            continue                      # too small to letter; the arrow says it
        # fit to the shorter side, but let a long ref use the longer one
        size = int(max(lo, min(hi, min(max(w, h) * 0.45,
                                       (w * 1.6) / max(1, len(ref))))))
        d.text(((box[0] + box[2]) / 2, (box[1] + box[3]) / 2), ref,
               fill=C_LABEL, font=load_font(size), anchor='mm',
               stroke_width=max(1, size // 9), stroke_fill=(12, 14, 16))


def _dim(c, k=0.32):
    return (int(c[0] * k), int(c[1] * k), int(c[2] * k))


def pad_fill_for(model, side=None):
    """OmniLayout's encoding: through-hole / front SMD / back SMD read
    differently. A colour and a filter over pads BoardRenderer already knows how
    to rasterize -- custom polygons, capsules, roundrect, rotated rects.

    On a PER-SIDE panel the far side's SMD pads are dimmed rather than dropped:
    they are still context you need (a back-side part is why a front trace has
    to go around), but they must not compete with the side you asked to see.
    Through-hole pads stay full brightness on both panels, because they are
    physically on both -- a hole cannot move on one side only.
    """
    def fill(p):
        if getattr(p, 'drill', 0):
            return C_PAD_THT               # on both sides, always full
        ref = getattr(p, 'component_ref', None)
        own = model.side(ref) if ref else 'F'
        base = C_PAD_B if own == 'B' else C_PAD_F
        return base if (side is None or own == side) else _dim(base)
    return fill


# ---------------------------------------------------------------------------
# Panels
# ---------------------------------------------------------------------------
class PanelSpec:
    """One image: a view, a side filter, what is prominent, and a caption."""

    def __init__(self, model, *, view=None, side=None, prominent=(), moves=(),
                 hot_nets=(), blocker_nets=(), pick_nets=(), label='',
                 opts=None):
        self.model = model
        self.view = view
        self.side = side
        self.prominent = set(prominent)
        self.moves = list(moves)
        self.hot_nets = set(hot_nets)
        self.blocker_nets = set(blocker_nets)
        self.pick_nets = set(pick_nets)     # ids, from --ratsnest-nets
        self.label = label
        self.o = opts or {}


def overlay_for(spec: PanelSpec):
    """Compose the primitives into one `fn(draw, renderer)` for frame()."""
    m, o = spec.model, spec.o
    all_refs = sorted(m.parts())
    prom = sorted(spec.prominent) or all_refs
    context = [r for r in all_refs if r not in spec.prominent] \
        if (spec.prominent and o.get('delta_first', True)) else []
    locked = {r for r in all_refs if getattr(m.parts()[r], 'locked', False)}

    hot = m.net_ids_for(spec.hot_nets)
    blocked = m.net_ids_for(spec.blocker_nets) - hot
    if spec.pick_nets:
        # Named nets win over both defaults: between "the nets that moved" and
        # "every net on the board" is the case that actually comes up -- the
        # handful you are chasing. Draw only those, in their own colour.
        quiet_ids = set(spec.pick_nets)
    elif o.get('ratsnest_all') or not spec.prominent:
        # Every net. Either the hairball switch, or there is no delta to be
        # "first" about -- delta-first with nothing prominent would draw NO
        # airwires at all, which is not a summary, it is a blank.
        quiet_ids = None
    else:
        # Delta-first: only the moved parts' nets plus the attributed ones. The
        # issue's limit 1 -- a full ratsnest on a dense board is exactly the
        # useless hairball KiCad already shows.
        quiet_ids = set()
        for ref in spec.prominent:
            p = m.parts().get(ref)
            if p is not None:
                quiet_ids |= set(p.nets)
    def _draw(d, r):
        if o.get('borders', True):
            if context:
                draw_courtyards(d, r, m, context, side=spec.side, dim=True)
            draw_courtyards(d, r, m, prom, side=spec.side, locked=locked)
        if o.get('pads', True):
            r.draw_pads(d, fill_for=pad_fill_for(m, spec.side))
        if o.get('ratsnest', True):
            ids = quiet_ids if quiet_ids is not None else None
            quiet = [a for a in m.airwires(ids)
                     if a[4] not in hot and a[4] not in blocked
                     and a[4] not in spec.pick_nets]
            draw_airwires(d, r, quiet, color=C_AIR)
            if spec.pick_nets:
                draw_airwires(d, r, m.airwires(spec.pick_nets),
                              color=C_AIR_PICK, width_mm=0.08)
            draw_airwires(d, r, m.airwires(blocked), color=C_AIR_BLOCK,
                          width_mm=0.07)
            draw_airwires(d, r, m.airwires(hot), color=C_AIR_FAIL, width_mm=0.09)
        if spec.moves and o.get('ghosts', True):
            draw_ghosts(d, r, m, spec.moves)
        if spec.moves and o.get('arrows', True):
            draw_arrows(d, r, spec.moves)
        if o.get('labels', True):
            draw_ref_labels(d, r, m, prom)
    return _draw


def caption(spec: PanelSpec, extra: Optional[Dict] = None) -> str:
    """The metrics strip. Without it a reader adopts the wrong heuristic --
    "lots moved, looks broken" and "barely moved, looks safe" are both wrong,
    and only these numbers carry the verdict (#431 limit 3)."""
    m = spec.model.metrics
    bits = [spec.label] if spec.label else []
    if spec.side:
        bits.append(f"side {spec.side}")
    for k, fmt in (('crossings', '{:.0f}'), ('hpwl', '{:.1f}mm')):
        if m.get(k) is not None:
            bits.append(f"{k} " + fmt.format(m[k]))
    if m.get('overlap_area') is not None:
        bits.append(f"overlap {m['overlap_area']:.2f}mm2")
    bits.append("oob n/a (no Edge.Cuts)" if spec.model.no_outline
                else (f"oob {m['oob_count']:.0f}" if m.get('oob_count') is not None
                      else ''))
    for k in ('failures', 'iterations', 'vias'):
        if extra and extra.get(k) is not None:
            v = extra[k]
            bits.append(f"{k} {v:,}" if isinstance(v, int) else f"{k} {v}")
    if extra and extra.get('verdict'):
        bits.append(extra['verdict'])
    if spec.moves:
        bits.append(f"{len(spec.moves)} moved")
    return "  |  ".join(b for b in bits if b)


def render_panel(spec: PanelSpec, *, size=1600, supersample=2, extra=None):
    r = BoardRenderer(spec.model.pcb, size=size, supersample=supersample,
                      show_pads=False, view=spec.view,
                      layers=([spec.side + '.Cu']
                              if spec.side in ('F', 'B')
                              and (spec.side + '.Cu') in
                              spec.model.pcb.board_info.copper_layers else None))
    return r.frame(overlays=[overlay_for(spec)], label=caption(spec, extra))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _bool_pair(parser, name, default, help_on):
    dest = name.replace('-', '_')
    g = parser.add_mutually_exclusive_group()
    g.add_argument(f'--{name}', dest=dest, action='store_true',
                   default=default, help=help_on)
    g.add_argument(f'--no-{name}', dest=dest, action='store_false')


def build_parser():
    p = argparse.ArgumentParser(
        description="Headless PNG stills of placement status (#431).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  render_placement.py board.kicad_pcb -o state.png
  render_placement.py placed.kicad_pcb --before seed.kicad_pcb -o delta.png
  render_placement.py board.kicad_pcb --list-groups --group-by sheet
  render_placement.py board.kicad_pcb --zoom-group sheet:58d913ec --per-side -o out/
""")
    p.add_argument('board', nargs='?', help='the board to render ("after")')
    p.add_argument('--before', metavar='BOARD',
                   help='seed board; enables ghost rects and displacement arrows')
    p.add_argument('-o', '--output', help='PNG path (one panel) or directory')
    p.add_argument('--summary-json', metavar='FILE',
                   help='route.py JSON_SUMMARY or a loop round log, naming the '
                        'failed and blocker nets to highlight')
    p.add_argument('--zoom-group', metavar='BLOCK',
                   help='frame one placement block (same names as route.py --group)')
    p.add_argument('--group-by', default='auto', metavar='SOURCES',
                   help='how blocks are derived (default: auto = kicad,sheet)')
    p.add_argument('--list-groups', action='store_true',
                   help='list the blocks --group-by would derive, and exit')
    p.add_argument('--focus', action='store_true',
                   help='also emit one cropped panel per failed-net cluster')
    p.add_argument('--focus-gap', type=float, default=10.0, metavar='MM')
    p.add_argument('--max-focus', type=int, default=6)
    p.add_argument('--zoom-pad', type=float, default=2.0, metavar='MM')
    p.add_argument('--per-side', action='store_true',
                   help='emit an F and a B panel instead of one flattened view')
    _bool_pair(p, 'borders', True, 'draw component courtyards (default: on)')
    _bool_pair(p, 'labels', True, 'draw component references (default: on)')
    _bool_pair(p, 'ratsnest', True, 'draw airwires (default: on)')
    _bool_pair(p, 'pads', True, 'draw pads (default: on)')
    _bool_pair(p, 'ghosts', True, 'draw seed rects when --before is given')
    _bool_pair(p, 'arrows', True, 'draw displacement arrows when --before is given')
    _bool_pair(p, 'delta-first', True,
               'moved parts prominent, everything else faint (default: on)')
    p.add_argument('--ratsnest-nets', nargs='+', metavar='PATTERN',
                   help='draw airwires for JUST these nets, in their own '
                        'colour, and label the parts that own them. Same glob '
                        'syntax as route.py --nets, exclusions included '
                        "(e.g. '*USB*' '/CLK*' '!*_N'). Between the nets that "
                        'moved and every net on the board, this is the case '
                        'that actually comes up: the handful you are chasing')
    p.add_argument('--ratsnest-all', action='store_true',
                   help='draw EVERY net, not just the moved/attributed ones. The '
                        'hairball switch: on a dense board this reproduces exactly '
                        'the unreadable ratsnest KiCad already shows')
    p.add_argument('--metrics', choices=('exact', 'none'), default='exact')
    p.add_argument('--clearance', type=float, default=None)
    p.add_argument('--size', type=int, default=1600)
    p.add_argument('--supersample', type=int, default=2)
    p.add_argument('--json', action='store_true',
                   help='print a JSON_SUMMARY line with per-panel views + metrics')
    p.add_argument('--quiet', action='store_true')
    return p


def _load_summary(path):
    """Failed + blocker net names from a JSON_SUMMARY file or a route log.

    Uses place_route_loop's OWN arithmetic (metrics_from_summary, split out for
    exactly this) rather than re-deriving failure counts here.
    """
    if not path or not os.path.isfile(path):
        return [], [], {}
    txt = open(path, encoding='utf-8', errors='replace').read()
    summary = None
    try:
        summary = json.loads(txt)
    except ValueError:
        from place_route_loop import merge_route_summaries
        summary = merge_route_summaries(txt)
    if not summary:
        return [], [], {}
    from place_route_loop import metrics_from_summary
    m = metrics_from_summary(summary, txt)
    return m['failed_nets'], m['blockers'], m


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.board:
        parser.error('a board is required')

    pcb = parse_kicad_pcb(args.board)

    if args.list_groups:
        from placement.groups import derive_groups, parse_sources, short_name
        blocks = derive_groups(pcb, parse_sources(args.group_by))
        if not blocks:
            print(f"No placement blocks from sources {args.group_by!r}.")
            return 0
        print(f"{len(blocks)} placement block(s) from {args.group_by!r}:")
        for n, refs in sorted(blocks.items(), key=lambda kv: (-len(kv[1]), kv[0])):
            back = sum(1 for r in refs
                       if (pcb.footprints[r].layer or '').startswith('B'))
            print(f"  {short_name(n):34s} parts={len(refs):3d}  "
                  f"front={len(refs) - back:3d} back={back:3d}")
        return 0

    # The renderer WARNS where the placement CLIs refuse: being able to SEE an
    # unplaced board is the whole point of having a renderer for one.
    from placement.placement_state import gate_or_exit
    state = gate_or_exit(pcb, args.board, 'render_placement.py', warn_only=True)

    model = PlacementModel(pcb, args.board, exact=True,
                           quench_kwargs={'clearance': args.clearance})

    moves = moved_parts(parse_kicad_pcb(args.before), pcb) if args.before else []
    failed, blockers, route_metrics = _load_summary(args.summary_json)
    prominent = {m['reference'] for m in moves} | \
        model.refs_of_nets(model.net_ids_for(failed))

    pick = model.net_ids_matching(args.ratsnest_nets)
    if args.ratsnest_nets and not pick:
        print(f"  no nets match {' '.join(args.ratsnest_nets)}", file=sys.stderr)
    if pick:
        # The parts owning a named net become prominent, so "show me /CLK" also
        # labels and un-dims the parts it runs between -- a bare set of wires
        # with nothing identified is not much of an answer.
        prominent |= model.refs_of_nets(pick)

    opts = {'borders': args.borders, 'labels': args.labels,
            'ratsnest': args.ratsnest, 'pads': args.pads,
            'ghosts': args.ghosts, 'arrows': args.arrows,
            'delta_first': args.delta_first, 'ratsnest_all': args.ratsnest_all}

    view = None
    tag = os.path.splitext(os.path.basename(args.board))[0]
    if args.zoom_group:
        from group_routing import GroupRoutingError, block_refs, resolved_name
        from placement.groups import parse_sources, short_name
        srcs = parse_sources(args.group_by)
        try:
            refs = block_refs(pcb, args.zoom_group, srcs)
        except GroupRoutingError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 2
        full = resolved_name(pcb, args.zoom_group, srcs)
        if full and full != args.zoom_group and not args.quiet:
            print(f"  --zoom-group {args.zoom_group!r} resolved to block {full!r}")
        view = union_view([model.rect(r) for r in refs], args.zoom_pad)
        prominent |= set(refs)
        tag = short_name(full or args.zoom_group).replace(':', '_')
    elif state.unplaced:
        # A pile renders as a dot in the corner of an empty board unless the
        # frame follows the PARTS rather than the outline.
        view = union_view([model.rect(r) for r in model.parts()], 5.0)

    sides = ('F', 'B') if args.per_side else (None,)
    if args.per_side:
        back = sum(1 for fp in pcb.footprints.values()
                   if (fp.layer or '').startswith('B'))
        if back == 0 and 'B.Cu' not in pcb.board_info.copper_layers:
            sides = (None,)
            if not args.quiet:
                print("  (no back-side footprints and no B.Cu: skipping the B panel)")

    panels = []
    for side in sides:
        panels.append(PanelSpec(model, view=view, side=side, prominent=prominent,
                                moves=moves, hot_nets=failed,
                                blocker_nets=blockers, pick_nets=pick,
                                label=tag, opts=opts))
    if args.focus and failed:
        pts = model.net_points(model.net_ids_for(failed))
        for i, cl in enumerate(cluster_points(pts, args.focus_gap)[:args.max_focus]):
            fview = union_view([(x, y, x, y) for x, y in cl], args.focus_gap / 2)
            panels.append(PanelSpec(model, view=fview, side=None,
                                    prominent=prominent, moves=moves,
                                    hot_nets=failed, blocker_nets=blockers,
                                    pick_nets=pick,
                                    label=f"{tag} focus {i + 1}", opts=opts))

    extra = None
    if route_metrics:
        extra = {'failures': route_metrics.get('failures'),
                 'iterations': route_metrics.get('iterations'),
                 'vias': route_metrics.get('vias')}

    out = args.output or (os.path.splitext(args.board)[0] + '_placement.png')
    many = len(panels) > 1
    if many:
        os.makedirs(out, exist_ok=True)
    written = []
    for i, spec in enumerate(panels):
        img = render_panel(spec, size=args.size, supersample=args.supersample,
                           extra=extra)
        if many:
            bits = [tag]
            if spec.side:
                bits.append(spec.side)
            if 'focus' in spec.label:
                bits.append(f"focus{i}")
            path = os.path.join(out, "_".join(bits) + ".png")
        else:
            path = out
        img.save(path)
        written.append(path)
        if not args.quiet:
            print(f"  wrote {path}  ({img.size[0]}x{img.size[1]})")

    if args.json:
        print("JSON_SUMMARY: " + json.dumps({
            'panels': [{'label': s.label, 'side': s.side, 'view': s.view,
                        'path': w} for s, w in zip(panels, written)],
            'moved': len(moves),
            'failed_nets': sorted(failed), 'blocker_nets': sorted(blockers),
            'metrics': dict(model.metrics),
            'unplaced': state.unplaced, 'no_outline': model.no_outline,
        }, sort_keys=True, default=str))
    return 0


if __name__ == '__main__':
    sys.exit(main())
