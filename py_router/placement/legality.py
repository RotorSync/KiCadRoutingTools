"""Placement legality geometry: board side, true board outline, courtyard rects.

The single home for the geometric predicates that decide whether a placement is
LEGAL, as opposed to merely cheap. Both placement engines consume it:

- ``placement/quench.py`` -- the perturbative nudge/rotate/swap optimizer
- ``placement/fanout_clearance.py`` -- the decoupling-cap repair pass

and so can a placement grader: `placement_overlap_area` (total courtyard overlap
area) and `placement_out_of_board` are the two legality metrics the placement
scorecard needs (#456, feeding #411/#110), computed by the same code the
optimizers gate on rather than by a second implementation that can disagree.

Two things here are easy to get wrong and are therefore centralized:

**Board side.** Parts on opposite sides of the board overlap in XY without
overlapping in copper -- a back-side decoupling cap legitimately sits under a
front-side BGA. A side-blind clearance check calls that a collision, and since
only CANDIDATE poses are validated (never the incumbent), every candidate near
such a part is rejected and the part freezes in place. But a THROUGH-HOLE part
does occupy both sides: its leads pass through the board. So a part occupies its
own side with its full courtyard, and the opposite side only with the bounding
box of its drilled pads (`sides_occupied` / `rect_on` / `pair_min_gap`).

**The board is not its bounding box.** `board_bounds` is an axis-aligned bbox
(kicad_parser), so an inset of it is blind to an L-shaped outline, a notch, or an
interior cutout -- a part gets nudged into the hole. `BoardOutlineGate` measures
against the real Edge.Cuts rings, with the three-level short-circuit that makes
it affordable: board-level opt-out when the outline IS its bbox, a cached
per-part reachable-disk prune, then the exact ring test.

**An interior ring can be SWALLOWED WHOLE.** Both exact tests work from the
rect's four corners and four edges, so a rect large enough to contain an entire
interior ring passes them: no corner is off-board and no rect edge comes within
the margin of a ring edge. The swallow probe (`_swallow_pts`) is what closes
that hole, and it must see BOTH kinds of interior ring -- `board_cutouts` AND
the milled `board_edge_contours` that `drop_pad_containing_cutouts`
reclassified (#505). It saw only the former until #628.

**...but a milled ring's OWNER may swallow its own relief.** `board_edge_contours`
has exactly one producer -- `drop_pad_containing_cutouts` -- and it moves a
contour there only when the contour encloses **>= 2 pad centres**. So every
milled contour has a part living inside it BY CONSTRUCTION, and when that part's
courtyard is bigger than the ring (a connector over a milled relief) the #628
probe called the part board-violating at its own hand-placed seed pose. That is
not a hole in the board the part fell into; it is the part's own relief. Charging
it there is worse than useless: `violation()` going 0 -> margin at the seed hands
quench's unfreeze branch (`quench.py:686-700`, which accepts any overlap-free
pose whose board term merely DECREASES) a licence to walk the part toward a real
board edge, and makes the #456 scorecard report a correct hand placement as
`oob_count 1`. So the milled half of the probe is scoped by OWNERSHIP: the gate
precomputes ring -> owning refs at the SEED poses (stable board geometry, computed
once), and `rect_blocked`/`rect_outside_amount` take an optional `ref` that skips
the rings that ref owns. `ref=None` is exactly the pre-#636 behaviour, so a caller
that does not pass one is unchanged. Two things this deliberately does NOT do:
real `cutouts` are never owner-skipped (a part may never swallow a real hole), and
nothing here gives milled contours containment semantics (see `_swallow_pts`).
"""
from __future__ import annotations

import math
from typing import Dict, Iterable, List, NamedTuple, Optional, Sequence, Tuple

EPS = 1e-6

BOTH_SIDES = frozenset(('F', 'B'))


# --- rect primitives ---------------------------------------------------------
# rotate_local_bounds and rect_gap lived in duplicate in quench.py and
# fanout_clearance.py; both now re-export from here.

def rotate_local_bounds(lmin_x, lmin_y, lmax_x, lmax_y, rotation):
    """Rotate a local bounding box by the footprint rotation (KiCad sign)."""
    rot = rotation % 360
    if abs(rot) < 0.01:
        return lmin_x, lmin_y, lmax_x, lmax_y
    angle = math.radians(-rot)
    cos_a, sin_a = math.cos(angle), math.sin(angle)
    corners = [(lmin_x, lmin_y), (lmax_x, lmin_y),
               (lmin_x, lmax_y), (lmax_x, lmax_y)]
    xs = [x * cos_a - y * sin_a for x, y in corners]
    ys = [x * sin_a + y * cos_a for x, y in corners]
    return min(xs), min(ys), max(xs), max(ys)


def rect_gap(a, b):
    """Smallest axis-aligned gap between two rects (negative if overlapping)."""
    dx = max(a[0] - b[2], b[0] - a[2])
    dy = max(a[1] - b[3], b[1] - a[3])
    if dx < 0 and dy < 0:
        return max(dx, dy)  # overlap amount (negative)
    return math.hypot(max(dx, 0), max(dy, 0)) if (dx > 0 and dy > 0) else max(dx, dy)


def rect_overlap_shortfall(a, b, clearance):
    """Clearance shortfall between two rects (0 when they are clear)."""
    return max(0.0, clearance - rect_gap(a, b))


def rect_overlap_area(a, b):
    """Area of the intersection of two rects (0 when disjoint).

    The OO ("total component overlap area") legality metric's primitive: unlike
    rect_gap this is a real area, so it stays comparable across boards.
    """
    w = min(a[2], b[2]) - max(a[0], b[0])
    if w <= 0.0:
        return 0.0
    h = min(a[3], b[3]) - max(a[1], b[1])
    return w * h if h > 0.0 else 0.0


def rect_area(rect):
    return max(0.0, rect[2] - rect[0]) * max(0.0, rect[3] - rect[1])


def point_to_seg_dist(px, py, x1, y1, x2, y2):
    """Distance from a point to a line segment."""
    dx, dy = x2 - x1, y2 - y1
    L2 = dx * dx + dy * dy
    if L2 < 1e-12:
        return math.hypot(px - x1, py - y1)
    t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / L2))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))


def ring_is_rect(ring):
    """True when an outline ring is an axis-aligned rectangle equal to its own
    bbox (shoelace area == bbox area) -- then the bbox inset test is exact and
    the per-candidate ring checks can be skipped entirely (#370 B2)."""
    xs = [p[0] for p in ring]
    ys = [p[1] for p in ring]
    bbox_area = (max(xs) - min(xs)) * (max(ys) - min(ys))
    if bbox_area <= 0:
        return False
    a = 0.0
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        a += x1 * y2 - x2 * y1
    return abs(abs(a) / 2.0 - bbox_area) <= max(1e-6, 1e-3 * bbox_area)


# --- board side model --------------------------------------------------------

def footprint_side(fp) -> str:
    """'F' or 'B' from a footprint's layer. Anything not B.* reads as front,
    matching how the rest of the package resolves side from `fp.layer`."""
    return 'B' if (getattr(fp, 'layer', '') or '').startswith('B') else 'F'


def footprint_has_through_pads(fp) -> bool:
    """True when any pad is drilled, so the part's leads occupy BOTH sides.

    Deliberately plain `drill > 0`, not `pad_is_plated_through`: the question
    here is physical body/lead obstruction, not electrical layer-tying, so an
    unplated hole blocks the far side exactly as a plated one does.
    """
    return any((getattr(p, 'drill', 0) or 0) > 0 for p in (fp.pads or []))


def sides_occupied(side: str, has_tht: bool) -> frozenset:
    """The board sides a part physically obstructs."""
    return BOTH_SIDES if has_tht else frozenset((side,))


def rect_on(side_wanted: str, own_side: str, courtyard_rect, tht_rect):
    """The rect a part presents on `side_wanted`.

    Its own side gets the full courtyard; the opposite side gets only the
    drilled-pad bounding box (None when the part has no drilled pads, i.e. it
    does not reach that side at all).
    """
    return courtyard_rect if side_wanted == own_side else tht_rect


def pair_min_gap(a_sides, a_side, a_rect, a_tht,
                 b_sides, b_side, b_rect, b_tht) -> Optional[float]:
    """Smallest rect gap between two parts over the sides they SHARE.

    None when they share no side -- then they cannot interact at all and every
    consumer (hard clearance, halo penalty, overlap grader) must skip the pair.
    On a single-sided board the shared set is always the one side and this
    reduces to plain courtyard-vs-courtyard, which is what keeps the existing
    behaviour (and the existing tests) unchanged.
    """
    best = None
    for s in (a_sides & b_sides):
        ra = rect_on(s, a_side, a_rect, a_tht)
        rb = rect_on(s, b_side, b_rect, b_tht)
        if ra is None or rb is None:
            continue
        g = rect_gap(ra, rb)
        if best is None or g < best:
            best = g
    return best


def pair_overlap_area(a_sides, a_side, a_rect, a_tht,
                      b_sides, b_side, b_rect, b_tht) -> float:
    """Largest per-shared-side overlap area between two parts (0 if none).

    Max rather than sum: on a THT/THT pair the same physical interference shows
    up on both sides, and counting it twice would double-charge one overlap.
    """
    worst = 0.0
    for s in (a_sides & b_sides):
        ra = rect_on(s, a_side, a_rect, a_tht)
        rb = rect_on(s, b_side, b_rect, b_tht)
        if ra is None or rb is None:
            continue
        worst = max(worst, rect_overlap_area(ra, rb))
    return worst


# --- milled-ring ownership ---------------------------------------------------

def _iter_footprints(footprints) -> Iterable:
    """Footprint objects from either a {ref: fp} mapping or a plain iterable."""
    if footprints is None:
        return ()
    values = getattr(footprints, 'values', None)
    return values() if callable(values) else footprints


def ring_owner_refs(ring, footprints) -> frozenset:
    """Component refs with at least one PAD CENTRE inside `ring`.

    The ownership predicate for the milled-ring swallow rule. It mirrors the
    producer: `drop_pad_containing_cutouts` reclassifies a contour into
    `board_edge_contours` exactly when >= 2 pad centres fall inside it, using
    this same ray-cast test, so "the ring encloses this part's own pads" is
    precisely "this part is (part of) the reason the ring is milled geometry".

    The threshold here is >= 1, not the producer's >= 2, and that is deliberate:
    the producer counts pads BOARD-WIDE, so a ring can be reclassified by one pad
    from each of two parts. Requiring 2 per part would then charge both of them
    for their own shared relief -- the same regression, one arity up. Each part
    is exempted only for the rings its own pads sit in; a ring it has no pad in
    is still a foreign relief it may not swallow.

    Poses are the SEED poses (`pad.global_x/global_y` as parsed). A ring's owner
    is stable board geometry, so this is computed once at gate construction
    rather than re-derived per candidate pose.
    """
    if len(ring) < 3:
        return frozenset()
    xs = [p[0] for p in ring]
    ys = [p[1] for p in ring]
    rx0, rx1, ry0, ry1 = min(xs), max(xs), min(ys), max(ys)
    # Local import: this module is imported at the top of both placement
    # engines and otherwise pulls nothing but math/typing.
    from kicad_parser import _pt_in_ring
    owners = set()
    for fp in _iter_footprints(footprints):
        ref = getattr(fp, 'reference', None)
        if not ref or ref in owners:
            continue
        for pad in (getattr(fp, 'pads', None) or ()):
            px = getattr(pad, 'global_x', None)
            py = getattr(pad, 'global_y', None)
            if px is None or py is None:
                continue
            if not (rx0 <= px <= rx1 and ry0 <= py <= ry1):
                continue        # bbox prune before the ray cast
            if _pt_in_ring(px, py, ring):
                owners.add(ref)
                break
    return frozenset(owners)


# --- board outline gate ------------------------------------------------------

class BoardOutlineGate:
    """Board-containment tests against the REAL Edge.Cuts outline (#370 B2).

    Callers keep using the cheap `usable` bbox inset as a first filter and reach
    for this only when it is not exact. Three levels of short-circuit:

    1. `active` is False when the board's outline IS its bounding box (one ring,
       rectangular, no cutouts) -- then the inset test is already exact, and it
       is also False when the parser found no usable ring at all, in which case
       the bbox is all we have and behaviour is unchanged.
    2. `edges_near(key, seed_rect, travel)` -- cached per key; a part that cannot
       reach any ring from its seed never pays for the exact test, and one that
       can gets the short list of edges to measure against rather than all of
       them. Pass that list back into the level-3 calls; skipping this prune is
       correct but roughly an order of magnitude slower in a candidate loop.
    3. `rect_blocked(rect)` / `rect_outside_amount(rect)` -- the exact tests.

    Caching note: a key's answer is computed from the pose and budget of the
    FIRST call, so a caller whose budget grows later must use a distinct key.
    Over-estimating `travel` is always safe (more edges, same verdict).

    Three ring collections, and they mean different things:

    * `cutouts` -- real holes. Also drive the on-board (inside-outline,
      outside-cutouts) point test, so their INTERIOR is off-board.
    * `milled` -- `board_edge_contours` (#505): Edge.Cuts geometry KiCad mills
      along that is neither a hole nor an outer ring. They carry an EDGE
      CLEARANCE role only; they are deliberately NOT given containment
      semantics -- see `_swallow_pts`. Each has an OWNER set (`milled_owners`,
      parallel list): the refs whose own pads sit inside it, which the swallow
      rule does not fire on -- see `ring_owner_refs`.
    * `rings` -- the flat union, what the distance tests measure against.

    Pass `footprints` (a `{ref: Footprint}` mapping or any iterable of
    footprints, i.e. `pcb_data.footprints`) to enable the ownership scoping;
    without it every milled ring is ownerless and the gate behaves exactly as it
    did before, which is what keeps a caller that does not supply them unchanged.
    """

    def __init__(self, board_info, margin: float, footprints=None):
        # Function-local import: check_drc pulls the DRC stack, and this module
        # is imported at the top of both placement engines. Same reason
        # fanout_clearance imported it locally.
        from check_drc import board_edge_geometry
        rings, outer, cutouts = board_edge_geometry(board_info)
        self.rings = rings
        self.outer = outer
        self.cutouts = cutouts
        # Milled interior contours (#505). Already inside `rings` (that is what
        # board_edge_geometry does with them), so the distance tests see them;
        # kept separately because the SWALLOW probe needs them too (#628).
        self.milled = [c for c in
                       (getattr(board_info, 'board_edge_contours', None) or [])
                       if len(c) >= 3]
        # ring -> the refs that own it, computed ONCE from the seed poses. Every
        # milled contour encloses >= 2 pad centres by construction (that is the
        # reclassification predicate), so this is never uniformly empty on a
        # board that has one -- unless no footprints were supplied.
        self.milled_owners = [ring_owner_refs(c, footprints) for c in self.milled]
        # One representative vertex per interior ring, for the swallow probe in
        # rect_blocked / rect_outside_amount, paired with the ring's owner set.
        # Any vertex serves: a ring wholly inside the rect has every vertex
        # inside it, and a ring only partly inside is already caught by the
        # corner and edge tests.
        #
        # CUTOUTS ARE ALWAYS OWNERLESS (empty set), and that is a rule, not an
        # accident of the data: a part may never swallow a real hole, owner or
        # not. Ownership is vacuous there anyway -- a ring enclosing >= 2 pad
        # centres is not a cutout by the time it reaches here (it was
        # reclassified) -- but a cutout enclosing ONE pad centre survives as a
        # cutout, so the exemption has to be denied explicitly rather than left
        # to the arithmetic.
        #
        # DELIBERATELY NOT a containment rule. A milled contour's interior is
        # NOT declared off-board here, and must not be: board_edge_contours
        # mixes a real inner OUTLINE (interior = board -- crkbd's nested half,
        # bus_pirate5's 60.8x80.2mm inner ring) with a milled SLOT (interior =
        # not board), and the parser cannot tell them apart. Calling the
        # interior off-board re-opens #291, where bus_pirate5 lost all 870 pads
        # and the run broke the chain. "A part may not swallow a milled ring it
        # does not own" is true for both readings, which is exactly why it is
        # safe.
        self._swallow_pts = (
            [(r[0][0], r[0][1], frozenset()) for r in self.cutouts]
            + [(c[0][0], c[0][1], own)
               for c, own in zip(self.milled, self.milled_owners)])
        self.margin = margin
        self.bounds = getattr(board_info, 'board_bounds', None)
        self.usable = None
        if self.bounds is not None:
            self.usable = (self.bounds[0] + margin, self.bounds[1] + margin,
                           self.bounds[2] - margin, self.bounds[3] - margin)
        self.active = bool(rings) and (
            bool(cutouts) or len(rings) > 1 or not ring_is_rect(rings[0]))
        self._near: Dict[object, list] = {}
        self._edges = None

    def edges(self):
        """Every ring edge as (x1, y1, x2, y2), built once."""
        if self._edges is None:
            out = []
            for ring in self.rings:
                n = len(ring)
                for i in range(n):
                    out.append((ring[i][0], ring[i][1],
                                ring[(i + 1) % n][0], ring[(i + 1) % n][1]))
            self._edges = out
        return self._edges

    # -- level 2: cached reachability prune
    def edges_near(self, key, seed_rect, travel: float, center=None) -> list:
        """Cached: the ring edges a part seeded at `seed_rect` could ever bring
        a pose within the edge margin of, given `travel` of displacement budget.

        Keyed on the SEED pose, so the reachable disk covers every pose the part
        can ever take and the answer holds for the whole run. Empty means the
        exact ring test can be skipped entirely; otherwise this is the short list
        the exact test should measure against instead of every ring, which is
        what keeps a dense candidate loop affordable (the segment-to-ring
        distances dominated a naive port).

        `center` is the part's pose ORIGIN, and callers whose parts can ROTATE
        must pass it. Measuring the span from the seed rect's own centre assumes
        the rect stays centred there, which is false for a courtyard that is
        off-centre from the footprint origin: a 90/180/270 pose swings it about
        the origin, and the reachable disk drawn around the seed centre can then
        miss an edge the part really can reach. Rotation preserves distance FROM
        THE ORIGIN, so the max origin-to-corner distance bounds every pose
        exactly. (Omitting it is safe only for parts that never rotate, or whose
        courtyard is origin-symmetric -- the fanout caps this prune came from.)
        """
        near = self._near.get(key)
        if near is None:
            if center is not None:
                cx, cy = center
                span = max(math.hypot(px - cx, py - cy)
                           for px in (seed_rect[0], seed_rect[2])
                           for py in (seed_rect[1], seed_rect[3]))
            else:
                cx = (seed_rect[0] + seed_rect[2]) / 2.0
                cy = (seed_rect[1] + seed_rect[3]) / 2.0
                span = math.hypot(seed_rect[2] - cx, seed_rect[3] - cy)
            reach = travel + span + self.margin + EPS
            # An edge farther than reach from that centre cannot come within
            # margin of any reachable pose: every point of a reachable rect lies
            # within travel + span of it.
            near = [e for e in self.edges()
                    if point_to_seg_dist(cx, cy, *e) <= reach]
            self._near[key] = near
        return near

    def may_reach(self, key, seed_rect, travel: float, center=None) -> bool:
        return bool(self.edges_near(key, seed_rect, travel, center))

    # -- level 3: the exact tests
    def rect_blocked(self, rect, edges=None, ref=None) -> bool:
        """True when a rect leaves the real outline, enters a cutout, comes
        within the edge margin of any Edge.Cuts ring, or SWALLOWS an interior
        ring (a cutout or a milled contour it does not own) whole.

        `edges` restricts the distance test to a pre-filtered edge list from
        `edges_near`; omitted, every ring edge is measured.

        `ref` is the component being tested. Naming it exempts the rect from the
        swallow rule on the MILLED rings that ref owns -- its own relief, not a
        hole it fell into. `ref=None` (the default) charges every ring, which is
        the pre-#636 behaviour and keeps any caller that does not pass one
        unchanged. Real cutouts are never exempt. `rect_outside_amount` applies
        the identical skip; the two must agree or the documented
        `rect_outside_amount == 0 iff not rect_blocked` invariant breaks.
        """
        from check_drc import _point_on_board, _seg_seg_dist_coords
        x0, y0, x1, y1 = rect
        for (px, py) in ((x0, y0), (x1, y0), (x1, y1), (x0, y1)):
            if not _point_on_board(px, py, self.outer, self.cutouts):
                return True
        for (ax, ay, bx, by) in ((x0, y0, x1, y0), (x1, y0, x1, y1),
                                 (x1, y1, x0, y1), (x0, y1, x0, y0)):
            for (ex1, ey1, ex2, ey2) in (self.edges() if edges is None else edges):
                if _seg_seg_dist_coords(ax, ay, bx, by,
                                        ex1, ey1, ex2, ey2) < self.margin:
                    return True
        # An interior ring FULLY INSIDE the rect evades both tests above -- no
        # corner is off-board and no rect edge comes near a ring edge (#628),
        # unless it is a milled ring this very part owns.
        for (cx, cy, owners) in self._swallow_pts:
            if ref is not None and ref in owners:
                continue
            if x0 <= cx <= x1 and y0 <= cy <= y1:
                return True
        return False

    def bbox_outside_amount(self, rect) -> float:
        """Total per-axis overshoot of the `usable` bbox inset (0 when inside).

        This is the whole board test on a rectangular board, and the fallback
        when the parser produced no outline rings.
        """
        if self.usable is None:
            return 0.0
        u = self.usable
        return (max(0.0, u[0] - rect[0]) + max(0.0, u[1] - rect[1])
                + max(0.0, rect[2] - u[2]) + max(0.0, rect[3] - u[3]))

    def rect_outside_amount(self, rect, exact: bool = True, edges=None,
                            ref=None) -> float:
        """Magnitude of a rect's board-boundary violation; 0 iff fully legal.

        Zero exactly when the bbox inset holds AND `rect_blocked` is False, so
        it can drive a "may not worsen" acceptance rule and a grader from one
        definition. It is a distance-like magnitude, not an area -- see
        `out_of_board_area` for an area.

        `exact=False` skips the ring terms, for a caller that has already
        established via `may_reach` that this rect cannot come near a ring. The
        ring terms are ~100x the cost of the bbox term, and in a hot candidate
        loop most parts are nowhere near an edge.

        `ref` scopes the milled-ring swallow rule exactly as in `rect_blocked`,
        and MUST be passed by any caller that passes it there -- the invariant
        above is what lets `violation()==0` imply the hard gate passes.
        """
        amt = self.bbox_outside_amount(rect)
        if not (self.active and exact):
            return amt
        from check_drc import _point_on_board, _point_to_rings_distance, \
            _seg_seg_dist_coords
        use = self.edges() if edges is None else edges
        x0, y0, x1, y1 = rect
        for (px, py) in ((x0, y0), (x1, y0), (x1, y1), (x0, y1)):
            if not _point_on_board(px, py, self.outer, self.cutouts):
                # How far the stray corner must come back, floored at the margin
                # so an off-board corner always outweighs a mere margin graze.
                amt += max(self.margin,
                           _point_to_rings_distance(px, py, self.rings))
        for (ax, ay, bx, by) in ((x0, y0, x1, y0), (x1, y0, x1, y1),
                                 (x1, y1, x0, y1), (x0, y1, x0, y0)):
            d = min((_seg_seg_dist_coords(ax, ay, bx, by, *e) for e in use),
                    default=float('inf'))
            if d < self.margin:
                amt += self.margin - d
        # Swallowed interior ring (cutout or unowned milled contour), same probe,
        # same owner skip and same charge as rect_blocked's -- the two must agree
        # on legality or `violation()==0` stops implying `not rect_blocked()`
        # (#628).
        for (cx, cy, owners) in self._swallow_pts:
            if ref is not None and ref in owners:
                continue
            if x0 <= cx <= x1 and y0 <= cy <= y1:
                amt += self.margin
        return amt

    def edge_clearance(self, rect, edges=None) -> float:
        """Distance from a rect to the nearest board edge (rings when we have
        them, else the raw bbox). Negative is not reported -- a rect straddling
        an edge reads as 0. Drives the SOFT edge-margin penalty, which otherwise
        measures to the bounding box and ignores notches entirely.

        `edges` restricts the measurement to a pre-filtered list; a caller that
        knows the rect is farther than it cares about from every edge should not
        call this at all.
        """
        if self.active:
            from check_drc import _seg_seg_dist_coords
            use = self.edges() if edges is None else edges
            x0, y0, x1, y1 = rect
            best = float('inf')
            for (ax, ay, bx, by) in ((x0, y0, x1, y0), (x1, y0, x1, y1),
                                     (x1, y1, x0, y1), (x0, y1, x0, y0)):
                for e in use:
                    d = _seg_seg_dist_coords(ax, ay, bx, by, *e)
                    if d < best:
                        best = d
            return max(0.0, best)
        if self.bounds is None:
            return float('inf')
        b = self.bounds
        return max(0.0, min(rect[0] - b[0], rect[1] - b[1],
                            b[2] - rect[2], b[3] - rect[3]))

    def out_of_board_area(self, rect) -> float:
        """Area of `rect` outside the usable bbox inset (exact on a rectangular
        board; a lower bound on a notched one, where the rings also bite)."""
        if self.usable is None:
            return 0.0
        inside = rect_overlap_area(rect, self.usable)
        return max(0.0, rect_area(rect) - inside)


# --- graders (#456 comment: shared with the #411/#110 placement scorecard) ----

class GradedPart(NamedTuple):
    """One placed part, as the graders want it: absolute rects, resolved side."""
    ref: str
    side: str                       # 'F' or 'B'
    rect: Tuple[float, float, float, float]      # courtyard, own side
    tht_rect: Optional[Tuple[float, float, float, float]] = None
    has_tht: bool = False

    @property
    def sides(self) -> frozenset:
        return sides_occupied(self.side, self.has_tht)


class OutOfBoard(NamedTuple):
    count: int          # parts with any board-boundary violation
    amount: float       # summed rect_outside_amount (distance-like)
    area: float         # summed out_of_board_area (mm^2, bbox-exact)


def placement_overlap_area(parts: Sequence[GradedPart]) -> float:
    """OO: total pairwise courtyard overlap area, side-aware (mm^2).

    Zero on a legal placement. Cross-side pairs contribute nothing unless a
    through-hole part's lead field reaches the other side.
    """
    total = 0.0
    items = list(parts)
    for i, a in enumerate(items):
        for b in items[i + 1:]:
            total += pair_overlap_area(a.sides, a.side, a.rect, a.tht_rect,
                                       b.sides, b.side, b.rect, b.tht_rect)
    return total


def placement_out_of_board(parts: Sequence[GradedPart], board_info,
                           margin: float, footprints=None) -> OutOfBoard:
    """OoB: how many parts leave the real board outline, and by how much.

    `footprints` (`pcb_data.footprints`) enables the milled-ring ownership
    scoping, so a connector sitting over its own milled relief is not graded
    out-of-board (#628). Omitted, every milled ring is ownerless and the grade is
    unchanged -- but a scorecard that has the footprints should pass them, or it
    will report a correct hand placement as `oob_count 1`.
    """
    gate = BoardOutlineGate(board_info, margin, footprints=footprints)
    count = 0
    amount = 0.0
    area = 0.0
    for p in parts:
        amt = gate.rect_outside_amount(p.rect, ref=p.ref)
        if amt > EPS:
            count += 1
            amount += amt
            area += gate.out_of_board_area(p.rect)
    return OutOfBoard(count=count, amount=amount, area=area)
