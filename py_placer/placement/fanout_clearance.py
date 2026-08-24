"""
Fanout-clearance placement repair: tidy decoupling caps around BGA fanout.

Issue #130: a BGA fanout drops vias near the ball field. Where a foreign-net
via lands under a decoupling cap, the via copper overlaps the cap pad -> a
real PAD-VIA DRC violation at the clearance floor. The root cause is
*placement*, so the fix is to nudge the caps.

This runs AFTER bga_fanout.py (so the real vias exist), because the fanout
usually only escapes *signal* balls; GND/power balls are connected later by
dropping vias straight down. That gives two complementary goals:

  * AVOID  - a cap pad must clear, by `clearance`, every real fanout via of a
             DIFFERENT net (these are the #130 violations), plus any foreign
             escape track on the cap's own copper side (escapes can land on the
             bottom; the under-pad fanout deliberately routes through movable
             caps' zones expecting THIS step to move them, #278), plus any
             foreign-net COMPONENT pad (#235/#275: a move may never slide a cap
             pad onto a neighbour's pad -> a PAD-PAD short). All three are
             violations to FIX when present at the seed, not just to avoid
             introducing. Same-net vias/pads are fine - a cap pad may sit right
             on one (via-in-pad / same-net copper sharing).
  * ATTRACT - pull each cap pad toward the nearest BGA ball of its OWN net, so
             that a later GND/power via dropped at the ball also lands on the
             cap pad (one shared via connects ball + cap + plane).

Move set: small nudges within a per-cap displacement budget plus 90-degree
rotations. Cost = foreign-via penetration (strong) + same-net attraction +
displacement (mild, so caps stay near their seed). The board edge,
locked-part courtyards, AND other caps' courtyards are HARD constraints: a
move may never introduce or worsen a courtyard overlap, so caps never end up
on top of each other. A cap that can't clear a foreign via within the base
budget gets its budget grown and rotations enabled until it fits or hits the
displacement cap; if it still can't, it's reported unresolved.
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Set, Tuple

from kicad_parser import PCBData, Segment, find_components_by_type
import routing_defaults as defaults
from bga_fanout.grid import analyze_bga_grid
from placement.parser import extract_courtyard_bboxes, extract_locked_refs
from placement.utility import compute_footprint_bbox_local, snap_to_grid
from placement.legality import (BoardOutlineGate, PadClearanceModel, PadFloor,
                                _pad_carries_copper, format_required_clause,
                                point_to_seg_dist, rect_gap, ring_is_rect,
                                rotate_local_bounds)

ROTATIONS = [0.0, 90.0, 180.0, 270.0]
EPS = 1e-6

# These four moved to placement/legality.py, the shared home with quench.py
# (which carried byte-identical copies of the first two). Aliased rather than
# renamed: the names are used throughout this module and imported by tests.
_rotate_local_bounds = rotate_local_bounds
_ring_is_rect = ring_is_rect
_rect_gap = rect_gap
_point_to_seg_dist = point_to_seg_dist

# Objective weights. Foreign-copper clearance (via + track + pad grazes, see
# graze_penalty) dominates everything, then pulling pads onto same-net balls,
# then a mild displacement regularizer (so caps that are already fine and far
# from a same-net ball stay put). Cap-cap / locked-part overlap is a HARD
# constraint (see hard_blocked), not weighted.
VIA_WEIGHT = 50.0
ATTRACT_WEIGHT = 1.0
DISPLACEMENT_WEIGHT = 0.3


def _point_to_rect_dist(px, py, rect):
    """Distance from a point to an axis-aligned rect (0 if inside)."""
    dx = max(rect[0] - px, px - rect[2], 0.0)
    dy = max(rect[1] - py, py - rect[3], 0.0)
    return math.hypot(dx, dy)


def _pad_pair_shortfall(pads_a, pads_b, clearance, effs=None):
    """Sum of different-net pad clearance shortfalls between two movable parts'
    pad rects (#275) -- the mover-vs-mover analogue of pad_penalty. Same-net
    pad pairs are fine (shared rail copper may touch).

    `effs` (#725) is the per-pair REQUIRED clearance, `effs[ia][ib]` aligned
    with pads_a x pads_b -- a pad override / netclass / .kicad_dru layer rule
    can put a pair's requirement above (or, for a relaxing rule, below) the
    flat scalar. None keeps the flat path, which is what a board declaring
    none of them takes."""
    pen = 0.0
    for ia, (ax0, ay0, ax1, ay1, anet) in enumerate(pads_a):
        row = None if effs is None else effs[ia]
        for ib, (bx0, by0, bx1, by1, bnet) in enumerate(pads_b):
            if anet == bnet:
                continue
            gap = _rect_gap((ax0, ay0, ax1, ay1), (bx0, by0, bx1, by1))
            clr = clearance if row is None else row[ib]
            if gap < clr - EPS:
                pen += (clr - gap)
    return pen


def _segs_cross(ax, ay, bx, by, cx, cy, dx, dy):
    """True if segments AB and CD properly intersect. Collinear overlaps are
    left to the endpoint-distance fallback (an endpoint then lies ON the other
    segment, giving distance 0 anyway)."""
    def orient(px, py, qx, qy, rx, ry):
        v = (qx - px) * (ry - py) - (qy - py) * (rx - px)
        return 0 if abs(v) < 1e-12 else (1 if v > 0 else -1)
    o1 = orient(ax, ay, bx, by, cx, cy)
    o2 = orient(ax, ay, bx, by, dx, dy)
    o3 = orient(cx, cy, dx, dy, ax, ay)
    o4 = orient(cx, cy, dx, dy, bx, by)
    return o1 != o2 and o3 != o4


def _seg_to_rect_dist(x1, y1, x2, y2, rect):
    """Exact distance from a segment to an axis-aligned rect (0 if touching
    or crossing). The centre+half-diagonal model this replaces overestimates
    the keep-out of elongated pads, which both missed real grazes and
    manufactured phantom ones (#278)."""
    rx0, ry0, rx1, ry1 = rect
    if (rx0 <= x1 <= rx1 and ry0 <= y1 <= ry1) or \
       (rx0 <= x2 <= rx1 and ry0 <= y2 <= ry1):
        return 0.0
    best = float('inf')
    for ex1, ey1, ex2, ey2 in ((rx0, ry0, rx1, ry0), (rx1, ry0, rx1, ry1),
                               (rx1, ry1, rx0, ry1), (rx0, ry1, rx0, ry0)):
        if _segs_cross(x1, y1, x2, y2, ex1, ey1, ex2, ey2):
            return 0.0
        best = min(best,
                   _point_to_seg_dist(x1, y1, ex1, ey1, ex2, ey2),
                   _point_to_seg_dist(x2, y2, ex1, ey1, ex2, ey2),
                   _point_to_seg_dist(ex1, ey1, x1, y1, x2, y2),
                   _point_to_seg_dist(ex2, ey2, x1, y1, x2, y2))
    return best


class _Cap:
    """A movable cap: pad offsets + courtyard bbox, in a seed-relative frame.

    Pad offsets and board-resolved half-sizes are captured at the seed
    placement; an additional 90-degree rotation rotates the offsets and swaps
    the half-extents, which is exact for axis-aligned pads (the normal case
    for decoupling caps).
    """

    def __init__(self, fp, courtyard_local, model=None):
        self.ref = fp.reference
        self.side = 'B' if (fp.layer or '').startswith('B') else 'F'
        self.seed_x, self.seed_y = fp.x, fp.y
        self.seed_rot = fp.rotation % 360
        self.x, self.y, self.rot = fp.x, fp.y, fp.rotation % 360
        # pads: (off_x, off_y, half_x, half_y, net_id) relative to fp center.
        # Copper pads only -- a footprint may define solder-paste apertures as
        # separate paste-only "pads" (e.g. gkl_misc C_0201_0603Metric), which are
        # not copper and must not enter the clearance/attraction geometry.
        self.pads = []
        # #725, mirroring PartPads (legality.py): the per-pad clearance FLOOR
        # rides in a parallel list index-aligned with self.pads, never widened
        # into the pad tuple -- pad_rects()' 5-tuple is unpacked positionally by
        # tests and by animate_fanout_clearance.py. `max_floor` is the
        # over-reach a broad phase must use (a .kicad_dru rule REPLACES, so it
        # can also LOWER a pair; a prune must never under-reach).
        # NOTE the copper filter below stays _Cap's own `endswith('.Cu')` test
        # and is deliberately NOT unified with legality._pad_carries_copper,
        # which additionally rejects np_thru_hole. Unifying would change which
        # pads enter pad_rects/pad_bbox/attraction and desynchronise the
        # n_copper cap-detection test in _Repair.__init__ -- a placement change
        # dressed as a clearance fix. Building the floors against THIS rule
        # makes the alignment true by construction.
        self.pad_floors = []
        self.max_floor = 0.0
        # Per-pad COPPER LAYER sets, index-aligned like pad_floors. Needed
        # because self.segments records only an 'F'/'B' side and files an
        # In1.Cu track under 'F' (a pre-existing model quirk), so an F-side cap
        # pad is compared against inner-layer tracks it can never touch. That
        # phantom is priced at the flat scalar today; without this set the
        # NETCLASS term -- which is layer-blind -- would raise it too, and the
        # pass would move caps to clear copper on a layer their pads do not
        # occupy. Measured on orangecrab at --clearance 0.1 with a Default
        # class of 0.3: R17/R18/R5's ENTIRE graze is that phantom, 0.082mm
        # each flat and 0.70/0.70/0.57mm raised. See _seg_effs.
        self.pad_layers = []
        for p in fp.pads:
            if not any(str(l).endswith('.Cu') for l in p.layers):
                continue  # paste/mask-only aperture, not copper
            off_x = p.global_x - fp.x
            off_y = p.global_y - fp.y
            tilt = math.radians(getattr(p, 'rect_rotation', 0.0) or 0.0)
            c, s = abs(math.cos(tilt)), abs(math.sin(tilt))
            hx, hy = p.size_x / 2, p.size_y / 2
            half_x = hx * c + hy * s
            half_y = hx * s + hy * c
            self.pads.append((off_x, off_y, half_x, half_y, p.net_id))
            if model is not None:
                # The GEOMETRY filter above stays loose on purpose (see the
                # comment), but a FLOOR must not: PadClearanceModel.pad_floor
                # reads local_clearance unconditionally, and an np_thru_hole
                # pad lists *.Cu while carrying no copper at all. Charging its
                # override would move a cap to clear copper that does not
                # exist -- and would contradict the model's own inertness
                # rule, which refuses to ACTIVATE for an NPTH-only override
                # (legality._pad_carries_copper, the watchy measurement).
                # Zero floor, appended unconditionally so the index alignment
                # the loose filter buys is preserved.
                # A pad that carries no copper is graded FLAT, whole stop --
                # not merely stripped of its own override. check_drc does not
                # grade such a pad at all, so letting the PARTNER's netclass
                # through pair() would charge a keep-out that does not exist,
                # which is the same defect as the off-layer phantom below.
                # The empty layer set is the marker the eff builders key on;
                # a real copper pad always resolves at least one layer.
                carries = _pad_carries_copper(p)
                fl = model.pad_floor(p) if carries else PadFloor(0.0, 0.0, None)
                self.pad_floors.append(fl)
                from check_drc import pad_copper_layers
                self.pad_layers.append(
                    frozenset(pad_copper_layers(p, model.board_copper))
                    if carries else frozenset())
                mf = model.max_floor(fl)
                if mf > self.max_floor:
                    self.max_floor = mf
        self.local_bounds = courtyard_local
        # Rotation-only geometry is reused across every candidate position
        # (millions of times on a dense board), so memoize it per angle and
        # just translate per call. Keyed by rounded rotation.
        self._rect_cache: Dict[float, Tuple[float, float, float, float]] = {}
        self._pad_cache: Dict[float, list] = {}
        self._pad_bbox_cache: Dict[float, Tuple[float, float, float, float]] = {}
        # One-slot POSE memos: cost() and the shortfall helpers each rebuild
        # the same translated geometry for the same candidate 4-6 times in a
        # row (5.7M pad_rects calls on mez_rx); remember the last pose.
        self._pr_key = None
        self._pr_out: list = []
        self._rc_key = None
        self._rc_out: Tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)

    def rect(self, x=None, y=None, rot=None):
        x = self.x if x is None else x
        y = self.y if y is None else y
        rot = self.rot if rot is None else rot
        pose = (x, y, rot)
        if pose == self._rc_key:
            return self._rc_out
        # local_bounds is the footprint-LOCAL courtyard; rotate by the
        # absolute placement angle (matching how static obstacle rects are
        # built), not the delta from seed. The rotation is position-independent
        # -> cache it and translate.
        key = round(rot, 3)
        b = self._rect_cache.get(key)
        if b is None:
            b = _rotate_local_bounds(*self.local_bounds, rot)
            self._rect_cache[key] = b
        out = (x + b[0], y + b[1], x + b[2], y + b[3])
        self._rc_key, self._rc_out = pose, out
        return out

    def _pad_cache_for(self, rot):
        key = round((rot - self.seed_rot) % 360, 3)
        cache = self._pad_cache.get(key)
        if cache is None:
            delta = (rot - self.seed_rot) % 360
            rad = math.radians(-delta)
            c, s = math.cos(rad), math.sin(rad)
            swap = round(delta) % 180 == 90
            cache = []
            for off_x, off_y, hx, hy, net in self.pads:
                ox = off_x * c - off_y * s
                oy = off_x * s + off_y * c
                HX, HY = (hy, hx) if swap else (hx, hy)
                cache.append((ox, oy, HX, HY, net))
            self._pad_cache[key] = cache
            if cache:
                self._pad_bbox_cache[key] = (
                    min(ox - HX for ox, oy, HX, HY, net in cache),
                    min(oy - HY for ox, oy, HX, HY, net in cache),
                    max(ox + HX for ox, oy, HX, HY, net in cache),
                    max(oy + HY for ox, oy, HX, HY, net in cache))
            else:
                self._pad_bbox_cache[key] = (0.0, 0.0, 0.0, 0.0)
        return key, cache

    def pad_rects(self, x=None, y=None, rot=None):
        x = self.x if x is None else x
        y = self.y if y is None else y
        rot = self.rot if rot is None else rot
        pose = (x, y, rot)
        if pose == self._pr_key:
            return self._pr_out
        _key, cache = self._pad_cache_for(rot)
        out = []
        for ox, oy, HX, HY, net in cache:
            cx, cy = x + ox, y + oy
            out.append((cx - HX, cy - HY, cx + HX, cy + HY, net))
        self._pr_key, self._pr_out = pose, out
        return out

    def pad_bbox(self, x=None, y=None, rot=None):
        """Union bbox of pad_rects at a pose -- a containment-conservative
        prescreen: any pad-pair gap is >= the bbox-pair gap, so two caps whose
        pad bboxes are >= clearance apart have EXACTLY zero pad shortfall."""
        x = self.x if x is None else x
        y = self.y if y is None else y
        rot = self.rot if rot is None else rot
        key, _cache = self._pad_cache_for(rot)
        bb = self._pad_bbox_cache[key]
        return (x + bb[0], y + bb[1], x + bb[2], y + bb[3])


def _candidate_positions(cap, max_disp, step, grid_step):
    seen = set()
    out = []
    n = int(max_disp / step)
    for ix in range(-n, n + 1):
        for iy in range(-n, n + 1):
            cx = cap.seed_x + ix * step
            cy = cap.seed_y + iy * step
            if math.hypot(cx - cap.seed_x, cy - cap.seed_y) > max_disp + 1e-9:
                continue
            cx = snap_to_grid(cx, grid_step)
            cy = snap_to_grid(cy, grid_step)
            key = (round(cx, 4), round(cy, 4))
            if key not in seen:
                seen.add(key)
                out.append((cx, cy))
    return out


class _Repair:
    def __init__(self, pcb_data: PCBData, pcb_file: str,
                 clearance: float, grid_step: float,
                 board_edge_clearance: float, near_margin: float,
                 capture_radius: float, default_via_size: float,
                 cap_prefix: str, extra_locked: Set[str],
                 max_displacement_cap: float = 3.0):
        bounds = pcb_data.board_info.board_bounds
        if bounds is None:
            raise ValueError("No board boundary (Edge.Cuts) found")
        self.board = bounds
        # Copper-to-EDGE, not a different-net pair requirement, so #725 leaves
        # it alone: it is KiCad's `min_copper_edge_clearance`, a separate rule
        # a netclass cannot express. Two caveats worth stating rather than
        # implying: `board_edge_clearance` is a plain CLI flag (default 0.55)
        # and is NOT auto-read from the board the way the routing steps read
        # their edge constraint, and the GUI never passes it, so the plugin
        # always gets the 0.55 signature default. Separately,
        # nudge_vias_for_unresolved's own edge tests use bare `clearance`
        # instead of this margin -- both pre-existing, both filed.
        margin = max(clearance, board_edge_clearance)
        self.usable = (bounds[0] + margin, bounds[1] + margin,
                       bounds[2] - margin, bounds[3] - margin)
        # Real board outline / cutouts (#370 B2): `usable` is a bbox inset,
        # blind to interior cutouts and non-rectangular outlines, so a cap
        # could be nudged over a switch window or past a curved edge. When the
        # outline is not exactly the bbox (or cutouts exist), candidate rects
        # are additionally gated against the true Edge.Cuts rings in
        # _blocked_geom. Per-cap laziness: only caps whose reachable disk can
        # touch a ring pay for the exact test. The gate itself now lives in
        # placement/legality.py, shared with quench (#456 item 2).
        self.edge_gate = BoardOutlineGate(pcb_data.board_info, margin)
        self._edge_margin = margin
        # ref -> the milled rings this mover's own pads sit inside (#628), the
        # same exemption quench takes. Needed HERE and not only at the margin-0
        # gates because this one runs at max(clearance, board_edge_clearance)
        # and reaches the swallow probe through rect_blocked, whose ring term is
        # BOOLEAN -- it returns True regardless of margin, so the "a margin-0
        # gate is numerically inert" argument that covers lock_advisor /
        # placement_state / seeder does not reach it. Seed-pose, cached, never
        # invalidated, exactly like quench's.
        self._owned_rings_cache: Dict[str, frozenset] = {}
        self._edge_active = self.edge_gate.active
        self._max_disp_cap = max_displacement_cap
        self.clearance = clearance
        # #725: `clearance` is ONE flat scalar, but KiCad's different-net
        # requirement for a pair is max(clearance, netclass a, netclass b),
        # then REPLACED by any .kicad_dru rule on the layers the two share,
        # then raised by either item's own pad `(clearance ...)` override.
        # This pass is the second channel of #697's defect: priced flat, a pair
        # separated by more than `clearance` but less than its real requirement
        # is never charged, so the repair reports nothing to fix on geometry
        # check_drc flags. PadClearanceModel resolves it exactly as check_drc
        # does and is STRICTLY INERT on a board declaring none of the three.
        # `notes` must be read BEFORE the active-drop: a FAILED read (an
        # unreadable sibling) is exactly what makes the model look inert.
        _model = PadClearanceModel.for_board(pcb_data, clearance, pcb_file)
        self.clearance_notes = list(_model.notes)
        self._floors = _model if _model.active else None
        # Pad-layer scope for the non-pad items below. A via spans copper, so
        # it is scoped to ALL copper: check_drc's pad-via path passes
        # layers_b=None meaning "scope to the pad's own layers", and
        # intersecting with all-copper reproduces that exactly (blind vias
        # included). Never None here -- PadClearanceModel.pair does
        # `fb.layers or ()`, and an EMPTY shared set discards every dru rule.
        self._all_cu = frozenset(pcb_data.board_info.copper_layers or ())
        self._via_floor: Dict[int, PadFloor] = {}
        self._seg_floor: Dict[Tuple[int, str], PadFloor] = {}
        # #617: KiCad's copper-to-hole rule is the BOARD's `min_hole_clearance`
        # whenever it declares one above the 0.20 NPTH fab floor -- the same
        # value check_drc grades at. The NPTH keep-out rects below were grown
        # to the flat floor only, so on such a board a cap pad could be parked
        # in the declared band and read clean here while the checker flagged
        # it. Raise-only and cached per board path, so a board that declares
        # nothing keeps byte-identical keep-outs. `config` is None because the
        # placement engine has no GridRouteConfig anywhere in this call chain
        # (repair_fanout_clearance takes scalars); the board read is driven by
        # pcb_data.source_path, so the declared floor arrives regardless.
        from obstacle_map import resolve_hole_clearance
        self.npth_floor = max(defaults.NPTH_TO_TRACK_CLEARANCE,
                              resolve_hole_clearance(pcb_data, None))
        self.grid_step = grid_step
        self.capture_radius = capture_radius
        # cap_prefix may list several reference prefixes (e.g. "C,R" = caps and
        # resistors); str.startswith() accepts the tuple directly.
        self._cap_prefixes = tuple(p.strip() for p in str(cap_prefix).split(',')
                                   if p.strip()) or ('C',)

        courtyards = extract_courtyard_bboxes(pcb_file)
        locked = set(extract_locked_refs(pcb_file)) | extra_locked
        self.locked_refs = locked

        # --- avoidance: the REAL fanout vias (after fanout) ---
        # Each (x, y, net, keepout) where keepout = via_radius + clearance; a
        # cap pad must clear vias of a DIFFERENT net by this. Through-vias, so
        # they apply to caps on either board side.
        # #725: `keepout` is the OVER-REACH -- radius + max(clearance, this
        # via's own upper-bound floor). It feeds the prunes, the locked-part
        # warning and the animator's drawn disk, all of which may over-reach.
        # The EXACT per-(cap pad, via) value is resolved in the eff lists
        # below, never from this slot. Byte-identical (radius + clearance) when
        # the model is inert. The 4-tuple shape is pinned: tests assign
        # st.vias wholesale and animate_fanout_clearance.py unpacks it.
        self.vias: List[Tuple[float, float, int, float]] = []
        # The RADIUS, keyed on the tuple's identity. _via_effs has to strip the
        # over-reach back off element 3, and deriving the radius arithmetically
        # would silently mis-price a tuple a test assigned wholesale with a
        # different keep-out convention. Absent from this map -> graded at the
        # tuple's own keep-out slot verbatim, i.e. exactly as injected.
        #
        # The VALUE holds the tuple as well as the radius, so the map itself
        # keeps every registered tuple alive. Unlike the pad/segment floor maps
        # -- which are filled only in __init__, where self.foreign_pads and
        # self.segments hold their tuples -- this one gains entries later, when
        # nudge_vias_for_unresolved relocates a via. A tuple that died while its
        # id entry lived would hand a recycled id ANOTHER via's radius,
        # silently.
        self._via_radius_by_id: Dict[int, Tuple[tuple, float]] = {}
        for v in pcb_data.vias:
            size = v.size if v.size and v.size > 0 else default_via_size
            t = (v.x, v.y, v.net_id,
                 size / 2.0 + self._item_reach(self._via_floor_for(v.net_id)))
            self.vias.append(t)
            self._via_radius_by_id[id(t)] = (t, size / 2.0)

        # --- avoidance: foreign-net tracks on the cap's own side ---
        # Fanout escapes can land on the bottom (cap) side; attraction could
        # then pull a cap onto an escape track -> a PAD-SEGMENT violation.
        # Keyed by side so a B.Cu cap only avoids B.Cu tracks.
        # #725: element 5 is the OVER-REACH, like self.vias[3] above. The
        # segment's floor is keyed by its REAL layer, not by the F/B `side`
        # collapse on the next line (which files an In1.Cu track under 'F') --
        # a .kicad_dru rule is layer-scoped, and check_drc's pad-segment path
        # resolves it as a single-layer REPLACE on seg.layer. The side collapse
        # is pre-existing and deliberately left alone here.
        self.segments: List[Tuple[float, float, float, float, int, float, str]] = []
        # The 7-tuple carries `side`, not the real layer, so the floor cannot be
        # re-derived from it later -- keep it on the tuple's identity. The
        # tuples live for this object's lifetime, so the id is stable; a tuple a
        # test injects is simply absent and grades flat.
        self._seg_floor_by_id: Dict[int, PadFloor] = {}
        # ...and its REAL layer, for the same reason: the pruned list is built
        # on the F/B side collapse, so _seg_effs needs the true layer to tell a
        # pair that shares copper from one that cannot.
        self._seg_layer_by_id: Dict[int, str] = {}
        for s in pcb_data.segments:
            side = 'B' if (s.layer or '').startswith('B') else 'F'
            fl = self._seg_floor_for(s.net_id, s.layer)
            t = (s.start_x, s.start_y, s.end_x, s.end_y, s.net_id,
                 s.width / 2.0 + self._item_reach(fl), side)
            self.segments.append(t)
            if self._floors is not None:
                self._seg_layer_by_id[id(t)] = s.layer
            if fl is not None:
                self._seg_floor_by_id[id(t)] = fl

        # --- attraction: BGA balls grouped by net ---
        # A cap pad is pulled toward the nearest ball of its own net, so a via
        # dropped later at that ball also lands on the cap pad. Restricted to
        # real BGA footprints (detect_package_type == 'BGA').
        self.attract: Dict[int, List[Tuple[float, float]]] = {}
        bga_bboxes: List[Tuple[float, float, float, float]] = []
        self.bga_refs: List[str] = []
        for fp in find_components_by_type(pcb_data, 'BGA'):
            self.bga_refs.append(fp.reference)
            grid = analyze_bga_grid(fp)
            if grid is not None:
                bga_bboxes.append((grid.min_x, grid.min_y, grid.max_x, grid.max_y))
            else:
                lb = compute_footprint_bbox_local(fp)
                b = _rotate_local_bounds(*lb, fp.rotation)
                bga_bboxes.append((fp.x + b[0], fp.y + b[1],
                                   fp.x + b[2], fp.y + b[3]))
            for p in fp.pads:
                if p.net_id > 0:
                    self.attract.setdefault(p.net_id, []).append(
                        (p.global_x, p.global_y))
        # Kept for visualization (animate_fanout_clearance.py): the BGA ball-field
        # bounding boxes that define the region of interest.
        self.bga_bboxes = bga_bboxes

        # --- static obstacle rects (locked parts incl. BGAs) ---
        # and movable caps near a BGA. Courtyard collisions are checked
        # per board side only: a back-side decoupling cap legitimately sits
        # under a top-side BGA (they overlap in XY but not in copper). Via
        # disks are through-vias, so they apply to caps on either side.
        # --- avoidance: foreign-net COMPONENT pads (#235) ---
        # The cap optimizer must not slide a cap pad onto a neighbouring
        # component's pad of a different net -> a PAD-PAD short. Each entry is
        # an axis-aligned pad rect plus its net and side ('F'/'B', or None for
        # a through-hole pad that blocks both sides). Same-net pads are fine.
        self.foreign_pads: List[
            Tuple[float, float, float, float, int, Optional[str]]] = []
        # #725: index-aligned with self.foreign_pads (the 6-tuple shape is
        # pinned -- tests inject into cap_foreign_pads directly). An NPTH
        # entry gets floor None / mf 0.0: its rect is already inflated to the
        # hole floor, and the copper-to-HOLE rule is net-independent, so the
        # new machinery must never raise it via a neighbouring pad's netclass.
        # KNOWN GAP, deliberately not closed here: check_drc DOES honour the
        # hole pad's OWN `local_clearance` on that rule (check_drc.py, the
        # `max(npth_clr, local_clearance)` in the copper-to-hole pass, #505),
        # and `lc` is net-independent too -- so this under-blocks by
        # `lc - max(npth_floor, clearance)` on such a pad. Measured on
        # kicad_files/ulx3s.kicad_pcb, AUDIO1's two 1.7mm NPTH holes at
        # lc=0.400: 1.050mm modelled vs 1.250mm required, a 0.200mm
        # under-block. It is the HOLE rule, not the different-net copper rule
        # #725 is about, and it interacts with the #617 balance below; filed
        # separately. (watchy's 8 NPTH overrides are all 0.100, below the 0.20
        # fab floor, so that board is numerically inert here.)
        # NB: self.foreign_pads (and self.segments) are read nowhere after
        # __init__ -- the pruned per-cap lists are what the sweep uses. They
        # must NOT be deleted as dead: the id-keyed floor maps below depend on
        # those tuples staying alive, and a recycled id would return ANOTHER
        # item's floor, silently, with no error.
        self.foreign_pad_floors: List[Optional[PadFloor]] = []
        self.foreign_pad_mf: List[float] = []
        self.caps: Dict[str, _Cap] = {}
        self.static_rects: List[Tuple[Tuple[float, float, float, float], str]] = []
        for ref, fp in pcb_data.footprints.items():
            if not fp.pads:
                continue
            lb = courtyards.get(ref) or compute_footprint_bbox_local(fp)
            # Count COPPER pads only: paste-only apertures (split-paste 0201
            # footprints) would otherwise push a 2-terminal cap past the 2-pad
            # test and wrongly exclude it from placement (#130).
            n_copper = sum(1 for p in fp.pads
                           if any(str(l).endswith('.Cu') for l in p.layers))
            is_cap = (ref.startswith(self._cap_prefixes) and n_copper <= 2
                      and ref not in locked)
            if is_cap:
                cap = _Cap(fp, lb, self._floors)
                if self._near_any(cap.rect(), bga_bboxes, near_margin):
                    self.caps[ref] = cap
                    continue
            # everything else is a static obstacle
            side = 'B' if (fp.layer or '').startswith('B') else 'F'
            b = _rotate_local_bounds(*lb, fp.rotation)
            self.static_rects.append(((fp.x + b[0], fp.y + b[1],
                                       fp.x + b[2], fp.y + b[3]), side))
            # record this part's copper pads as foreign-pad keep-outs
            from check_drc import _pad_has_no_copper
            from kicad_parser import pad_drill_circles
            for p in fp.pads:
                copper = [l for l in p.layers if str(l).endswith('.Cu')]
                if _pad_has_no_copper(p):
                    # Copper-less drilled pad (NPTH mounting hole -- an
                    # np_thru_hole lists *.Cu but carries NO ring, #370 B2):
                    # the DRILL still removes any copper closer than the
                    # NPTH-to-track floor, so a cap pad slid over it is a real
                    # fab violation regardless of net. Blocks BOTH sides
                    # (through hole); graded at the NPTH floor by inflating
                    # the rect; net -1 never matches a cap pad's net (even a
                    # net-tied mounting hole is not connectable copper, #328).
                    if (p.drill or 0) > 0:
                        grow = max(0.0, self.npth_floor - clearance)
                        for hx, hy, hd in pad_drill_circles(p):
                            hr = hd / 2.0 + grow
                            self.foreign_pads.append(
                                (hx - hr, hy - hr, hx + hr, hy + hr, -1, None))
                            self.foreign_pad_floors.append(None)
                            self.foreign_pad_mf.append(0.0)
                    continue
                if not copper:
                    continue  # paste/mask-only aperture
                through = (p.drill or 0) > 0
                pside = None if through else (
                    'B' if any(str(l).startswith('B') for l in copper) else 'F')
                tilt = math.radians(getattr(p, 'rect_rotation', 0.0) or 0.0)
                c, s = abs(math.cos(tilt)), abs(math.sin(tilt))
                hx, hy = p.size_x / 2, p.size_y / 2
                half_x = hx * c + hy * s
                half_y = hx * s + hy * c
                self.foreign_pads.append(
                    (p.global_x - half_x, p.global_y - half_y,
                     p.global_x + half_x, p.global_y + half_y,
                     p.net_id, pside))
                _fl = None if self._floors is None else self._floors.pad_floor(p)
                self.foreign_pad_floors.append(_fl)
                self.foreign_pad_mf.append(
                    0.0 if _fl is None else self._floors.max_floor(_fl))

        # Per-cap spatially-pruned neighbour lists (perf). A cap moves at most
        # max_displacement_cap from its seed, so anything whose seed gap already
        # exceeds that (plus the relevant spans / clearance) can NEVER constrain
        # it -- excluding it is exact, not an approximation. This turns the
        # per-candidate hard_blocked / penalty loops from O(all parts) into
        # O(handful), the dominant cost on dense boards (#213 profiling).
        # #725: same identity-keyed map for foreign pads -- a pad's floor
        # depends on its own `(clearance ...)` and its copper layers, neither of
        # which the 6-tuple carries.
        self._fp_floor_by_id: Dict[int, PadFloor] = {}
        if self._floors is not None:
            for _t, _f in zip(self.foreign_pads, self.foreign_pad_floors):
                if _f is not None:
                    self._fp_floor_by_id[id(_t)] = _f
        # Per-(cap, neighbour) REQUIRED-clearance memos. model.pair() is
        # pose-independent, so it is resolved ONCE per (cap, neighbour) pair
        # and reused for every candidate pose. The memos are filled lazily, so
        # the first fill for a pair does happen inside cost() -- measured on a
        # 0.5-class board: 572 of 62,565 pair_with_source calls, against
        # 251,477 cost() calls. Each memo is (source_list, rows) and is
        # rebuilt when the source list identity changes -- which makes
        # repair_fanout_clearance's wholesale `st.cap_vias = {...}` reassignment
        # self-heal, and makes a test that injects into cap_foreign_pads grade
        # flat rather than mis-index.
        self._cap_pad_eff: Dict[str, Tuple[list, list]] = {}
        self._cap_seg_eff: Dict[str, Tuple[list, list]] = {}
        self._cap_via_eff: Dict[str, Tuple[list, list]] = {}
        self._cap_pair_eff: Dict[Tuple[str, str], list] = {}

        cap_geom: Dict[str, Tuple[float, float, float, Tuple]] = {}
        for ref, cap in self.caps.items():
            r = cap.rect()
            cx, cy = (r[0] + r[2]) / 2.0, (r[1] + r[3]) / 2.0
            span = math.hypot(r[2] - cx, r[3] - cy)
            cap_geom[ref] = (cx, cy, span, r)

        self.cap_foreign_pads: Dict[str, List[
            Tuple[float, float, float, float, int, Optional[str]]]] = {}
        # static obstacle rects (with their global index, same side) in reach
        self.cap_static: Dict[str, List[Tuple[int, Tuple]]] = {}
        # other movable caps (refs, same side) that could ever touch this one
        self.cap_caps: Dict[str, List[str]] = {}
        # foreign-net tracks on the cap's side in reach
        self.cap_segs: Dict[str, List[Tuple]] = {}
        # through-vias in reach (each (vx, vy, net, keepout))
        self.cap_vias: Dict[str, List[Tuple[float, float, int, float]]] = {}
        # #725: every reach below is raised by THE TWO ITEMS' OWN maxima, never
        # by a board-wide maximum -- these lists gate per-candidate loops, so a
        # board-wide bound would slow every cap for the sake of one keep-clear
        # pad. `cap.max_floor` and `foreign_pad_mf` are 0.0 on an inert board,
        # so `max(clearance, 0.0, 0.0) == clearance` and the reaches are
        # byte-identical. Left un-raised on purpose: cap_static below, which
        # gates _overlap (courtyards, not copper).
        cap_refs = list(self.caps)
        for ref, cap in self.caps.items():
            ccx, ccy, span, crect = cap_geom[ref]
            reach = max_displacement_cap + span
            cap_mf = cap.max_floor
            near_pads = []
            for j, fp_pad in enumerate(self.foreign_pads):
                px = (fp_pad[0] + fp_pad[2]) / 2.0
                py = (fp_pad[1] + fp_pad[3]) / 2.0
                phalf = math.hypot(fp_pad[2] - px, fp_pad[3] - py)
                req = max(clearance, cap_mf, self.foreign_pad_mf[j])
                if math.hypot(px - ccx, py - ccy) <= reach + req + phalf:
                    near_pads.append(fp_pad)
            self.cap_foreign_pads[ref] = near_pads

            near_static = []
            for idx, (sr, side) in enumerate(self.static_rects):
                if side != cap.side:
                    continue
                if _rect_gap(crect, sr) <= max_displacement_cap + clearance + EPS:
                    near_static.append((idx, sr))
            self.cap_static[ref] = near_static

            near_caps = []
            for oref in cap_refs:
                if oref == ref or self.caps[oref].side != cap.side:
                    continue
                # both caps can move, so the combined reach is 2x the budget
                if (_rect_gap(crect, cap_geom[oref][3])
                        <= 2 * max_displacement_cap
                        + max(clearance, cap_mf, self.caps[oref].max_floor)
                        + EPS):
                    near_caps.append(oref)
            self.cap_caps[ref] = near_caps

            near_segs = []
            # seg[5] already carries the segment's own over-reach, so adding
            # this cap's excess over the flat scalar bounds the pair.
            seg_reach = (max_displacement_cap + 2 * span + clearance
                         + max(0.0, cap_mf - clearance))
            for seg in self.segments:
                if seg[6] != cap.side:
                    continue
                d = _point_to_seg_dist(ccx, ccy, seg[0], seg[1], seg[2], seg[3])
                if d <= seg_reach + seg[5]:
                    near_segs.append(seg)
            self.cap_segs[ref] = near_segs

            near_vias = []
            # v[3] = via_radius + its own keep-out; a via that can reach a pad
            # must be within (move + span + keep-out) of the seed. #725: plus
            # this cap's excess over the flat scalar, which bounds the pair.
            via_slack = max(0.0, cap_mf - clearance)
            for v in self.vias:
                if math.hypot(v[0] - ccx, v[1] - ccy) <= (
                        max_displacement_cap + span + v[3] + via_slack):
                    near_vias.append(v)
            self.cap_vias[ref] = near_vias

        # Baseline same-side overlaps at the seed placement. Collisions are
        # scored RELATIVE to these: the repair must not introduce or worsen a
        # courtyard overlap, but it leaves pre-existing tight placements alone
        # (those aren't this issue's concern, and chasing them causes churn).
        self.base_static: Dict[Tuple[str, int], float] = {}
        for ref, cap in self.caps.items():
            seed_rect = cap.rect()
            for idx, (r, side) in enumerate(self.static_rects):
                if side == cap.side:
                    self.base_static[(ref, idx)] = self._overlap(seed_rect, r)
        self.base_cap: Dict[frozenset, float] = {}
        cap_items = list(self.caps.items())
        for i, (ra, ca) in enumerate(cap_items):
            ra_rect = ca.rect()
            for rb, cb in cap_items[i + 1:]:
                if cb.side == ca.side:
                    self.base_cap[frozenset((ra, rb))] = self._overlap(
                        ra_rect, cb.rect())
        # #441: PER-NET seed shortfall {snet: pen}, so the accept gate forbids
        # penetrating a net that was clear at the seed (not just worsening the
        # summed total).
        self.base_seg: Dict[str, Dict[int, float]] = {
            ref: self._seg_shortfalls(ref, cap, cap.x, cap.y, cap.rot)
            for ref, cap in self.caps.items()}
        # Baseline foreign-pad encroachment at the seed (#235): the repair may
        # not introduce or worsen a foreign-net pad-pad overlap, but tolerates
        # one already present at the seed (not this step's concern). Per-net (#441).
        self.base_pad: Dict[str, Dict[int, float]] = {
            ref: self._pad_shortfalls(ref, cap, cap.x, cap.y, cap.rot)
            for ref, cap in self.caps.items()}
        # Baseline foreign-VIA penetration at the seed (#445): via_penalty was
        # only a WEIGHTED cost term, so a move relieving a big track graze
        # could pay for dropping a pad onto an existing via (zynq_ad9364 R2
        # rotated onto the DDR3_VREF via -- a KiCad-confirmed pad-via short).
        # Same per-net hard gate as tracks/pads: never penetrate a via net
        # beyond its seed shortfall.
        self.base_via: Dict[str, Dict[int, float]] = {
            ref: self._via_shortfalls(ref, cap, cap.x, cap.y, cap.rot)
            for ref, cap in self.caps.items()}
        # Baseline mover-vs-mover pad encroachment at the seed (#275). The
        # cap-cap COURTYARD baseline above tolerates pre-existing overlaps,
        # but overlap depth is a poor proxy for pad geometry: two 45-degree
        # parts (fpga_sdram C11/FB1) kept courtyard overlap <= baseline while
        # their different-net pads slid into contact -- a PAD-PAD short that
        # #235's foreign_pads never sees because both parts are movers. Guard
        # at pad level, relative to the seed, over the same pruned pairs.
        self.base_cap_pad: Dict[frozenset, float] = {}
        for ref, cap in self.caps.items():
            seed_pads = cap.pad_rects()
            for oref in self.cap_caps[ref]:
                key = frozenset((ref, oref))
                if key not in self.base_cap_pad:
                    # #725: the baseline must be in the SAME currency as the
                    # candidate it gates. Priced flat while candidates are
                    # priced at the requirement, _worsens_any_net compares two
                    # different units, so the accept gate reads a pose as
                    # worse-than-seed on a pair that did not change.
                    # Measured by reverting exactly this line at HEAD: the
                    # placement count moves in a CLASS-DEPENDENT direction and
                    # not by much (0.4 identical, 0.5 gives 50 vs 48 and one
                    # more unresolved, 1.0 gives 31 vs 34) -- so the symptom is
                    # a quietly wrong answer, not a visible seizure, which is
                    # why the test asserts the CURRENCY and not a count.
                    self.base_cap_pad[key] = _pad_pair_shortfall(
                        seed_pads, self.caps[oref].pad_rects(), self.clearance,
                        self._pair_effs(ref, cap, oref, self.caps[oref]))

    @staticmethod
    def _near_any(rect, bboxes, margin):
        for b in bboxes:
            if _rect_gap(rect, b) <= margin:
                return True
        return False

    def _cap_may_reach_edge(self, ref, cap):
        """Cached prune for the real-outline gate (#370 B2): a cap moves at
        most max_displacement_cap from its seed, so only caps whose reachable
        disk can touch an Edge.Cuts ring ever need the exact ring test."""
        return self.edge_gate.may_reach(
            ref, cap.rect(cap.seed_x, cap.seed_y, cap.seed_rot),
            self._max_disp_cap)

    def _owned_rings(self, ref) -> frozenset:
        """Cached: the milled rings this mover's OWN pads sit inside (#628).

        The cap analogue of QuenchState._owned_rings, and it matters for the
        same reason: a milled contour is reclassified out of board_cutouts
        precisely BECAUSE it encloses >= 2 pad centres, so a two-pad part whose
        pads are what triggered that reclassification lives on its own relief.
        Without the exemption `_rect_edge_blocked` rejects EVERY candidate for
        it -- there is no unfreeze branch here, so the cap simply never moves.

        Pad CENTRES at the SEED pose: `pad_rects` builds each rect as
        centre +/- half-extent, so the bbox midpoint is the centre exactly.
        Seed rather than candidate pose is the anti-gaming choice quench
        documents -- ownership at the candidate pose would let any cap claim a
        ring by moving onto it.
        """
        owned = self._owned_rings_cache.get(ref)
        if owned is None:
            cap = self.caps[ref]
            pts = [((bx0 + bx1) * 0.5, (by0 + by1) * 0.5)
                   for (bx0, by0, bx1, by1, _net) in
                   cap.pad_rects(cap.seed_x, cap.seed_y, cap.seed_rot)]
            owned = self.edge_gate.rings_enclosing(pts) if pts else frozenset()
            self._owned_rings_cache[ref] = owned
        return owned

    def _rect_edge_blocked(self, rect, ref=None):
        """True when a candidate courtyard rect leaves the REAL board outline,
        enters a cutout, or comes within the edge margin of either (#370 B2 --
        the bbox `usable` inset is blind to cutouts / curved outlines).

        `ref` exempts the mover from the swallow rule on the milled rings it
        OWNS (#628); real cutouts are never exempt, by construction in
        `rings_enclosing`. `ref=None` charges every ring, the old behaviour."""
        return self.edge_gate.rect_blocked(
            rect, skip_rings=self._owned_rings(ref) if ref is not None else None)

    # ---- #725: per-pair required clearance -------------------------------
    # A via or a track carries no pad `(clearance ...)` override, so its floor
    # is a pure function of (net, layer scope) and is memoised on that key --
    # never on a list position, because st.vias is reassigned wholesale by
    # tests and rebuilt by nudge_vias_for_unresolved, which would desync any
    # index-aligned parallel list.

    def _via_floor_for(self, net_id):
        """The PadFloor standing in for a via: its net's class floor, no pad
        override, scoped to all copper (see self._all_cu). None when inert."""
        if self._floors is None:
            return None
        f = self._via_floor.get(net_id)
        if f is None:
            f = PadFloor(self._floors.net_floor.get(net_id or 0, 0.0), 0.0,
                         self._all_cu if self._floors.layer_rules else None)
            self._via_floor[net_id] = f
        return f

    def _seg_floor_for(self, net_id, layer):
        """The PadFloor standing in for a track: its net's class floor, no pad
        override, scoped to its OWN layer -- which reproduces check_drc's
        single-layer REPLACE for pad-segment, since with one shared layer
        pads_shared_layer_clearance is all-or-nothing. None when inert."""
        if self._floors is None:
            return None
        key = (net_id, layer)
        f = self._seg_floor.get(key)
        if f is None:
            f = PadFloor(self._floors.net_floor.get(net_id or 0, 0.0), 0.0,
                         frozenset({layer}) if self._floors.layer_rules else None)
            self._seg_floor[key] = f
        return f

    def _item_reach(self, floor):
        """The over-reach for a via/track keep-out: never below the flat
        scalar, raised by that item's own upper bound. Exactly `clearance`
        when the model is inert, so the keep-outs stay byte-identical."""
        if floor is None:
            return self.clearance
        return max(self.clearance, self._floors.max_floor(floor))

    # Public resolvers. nudge_vias_for_unresolved composes its requirements
    # from these rather than re-deriving them, so there is ONE answer per pair
    # kind in this module.

    def required(self, fa, fb):
        """Required clearance between two resolved floors (either may be None
        -- an NPTH keep-out, a test-injected tuple -- which grades flat)."""
        return self._pair_or_flat(fa, fb)

    def pad_floor(self, pad):
        """The floor for a real parser Pad; None when inert."""
        return None if self._floors is None else self._floors.pad_floor(pad)

    def via_floor(self, net_id):
        """The floor standing in for a via of `net_id`; None when inert."""
        return self._via_floor_for(net_id)

    def seg_floor(self, net_id, layer):
        """The floor standing in for a track on `layer`; None when inert."""
        return self._seg_floor_for(net_id, layer)

    def via_required(self, pad_floor, via_net):
        """The required clearance for one (cap pad, via) pair.

        The offender test in nudge_vias_for_unresolved and via_penalty's
        grazing test MUST both resolve through here: if they diverge, the
        nudger chases a via the grader no longer flags (or reports a cap
        unresolved that its own nudger then refuses to see). The via eff rows
        are built from this same call, so they cannot drift from it either."""
        return self._pair_or_flat(pad_floor, self._via_floor_for(via_net))

    def _pair_or_flat(self, fa, fb):
        """model.pair for two floors, falling back to the flat scalar whenever
        either side has none (an NPTH keep-out rect, a test-injected tuple)."""
        if fa is None or fb is None:
            return self.clearance
        return self._floors.pair(fa, fb)

    def _cap_pad_layers(self, cap):
        """This cap's per-pad copper layer sets, or None when unavailable.

        None for a duck-typed cap, and for a board that declares no copper
        layers at all: `pad_copper_layers(p, [])` resolves nothing for a `*.Cu`
        pad, so every such pad would read as copper-less and take the
        off-layer fallback -- an UNDER-block. It hits only `*.Cu` pads (an
        `F.Cu` / `B.Cu` / `F&B.Cu` pad still resolves), which is 0 of 308 cap
        pads on orangecrab but 468 of 2186 across the tracked corpus, all
        through-hole. Scoping switches off entirely rather than per-pad,
        because a board with no copper layers has no layer question to answer.
        """
        pl = getattr(cap, 'pad_layers', None)
        if pl is None or len(pl) != len(getattr(cap, 'pad_floors', ())):
            return None
        return pl if self._all_cu else None

    @staticmethod
    def _flat_pad(cap_layers, i):
        """True when cap pad `i` carries no copper and must be graded flat."""
        return cap_layers is not None and not cap_layers[i]

    def _cap_floors_ok(self, cap):
        """True when this cap carries a usable, index-aligned floor list. A
        _Cap built without the model (or by a test) grades flat."""
        pf = getattr(cap, 'pad_floors', None)
        return (self._floors is not None and pf is not None
                and len(pf) == len(getattr(cap, 'pads', ())))

    def _pad_effs(self, ref, cap):
        """[cap pad i][pruned foreign pad j] required clearance, or None for
        the flat path."""
        if not self._cap_floors_ok(cap):
            return None
        src = self.cap_foreign_pads[ref]
        rec = self._cap_pad_eff.get(ref)
        if rec is not None and rec[0] is src:
            return rec[1]
        by_id = self._fp_floor_by_id
        cl = self._cap_pad_layers(cap)
        rows = [[self.clearance] * len(src) if self._flat_pad(cl, i)
                else [self._pair_or_flat(fa, by_id.get(id(t))) for t in src]
                for i, fa in enumerate(cap.pad_floors)]
        self._cap_pad_eff[ref] = (src, rows)
        return rows

    def _seg_effs(self, ref, cap):
        """[cap pad i][pruned segment j] final half-width KEEP-OUT -- the
        track's half width plus that pair's required clearance -- or None."""
        if not self._cap_floors_ok(cap):
            return None
        src = self.cap_segs[ref]
        rec = self._cap_seg_eff.get(ref)
        if rec is not None and rec[0] is src:
            return rec[1]
        by_id = self._seg_floor_by_id
        floors = [by_id.get(id(t)) for t in src]
        # t[5] is half width + the segment's own over-reach; strip it back.
        halves = [t[5] - self._item_reach(f) for t, f in zip(src, floors)]
        # A pair that shares NO copper layer keeps the FLAT scalar. cap_segs is
        # pruned on the 'F'/'B' side collapse, which files an In1.Cu track
        # under 'F', so it contains F-pad/inner-track pairs that cannot touch.
        # The dru term already scopes itself away from them (empty shared set),
        # but the netclass term does not -- and raising a phantom is how this
        # change would move caps for a non-violation. Charging it flat leaves
        # the pre-existing phantom exactly as it was; removing it altogether is
        # a separate fix to the side collapse, filed rather than folded in.
        cap_layers = self._cap_pad_layers(cap)
        seg_layers = [self._seg_layer_by_id.get(id(t)) for t in src]

        def eff(fa, mine, j):
            sl = seg_layers[j]
            if mine is not None and sl is not None and sl not in mine:
                return self.clearance
            return self._pair_or_flat(fa, floors[j])

        rows = [[halves[j] + eff(fa, None if cap_layers is None
                                 else cap_layers[i], j)
                 for j in range(len(src))]
                for i, fa in enumerate(cap.pad_floors)]
        self._cap_seg_eff[ref] = (src, rows)
        return rows

    def _via_effs(self, ref, cap, vias):
        """[cap pad i][via j] final KEEP-OUT for `vias` -- the via's radius plus
        that pair's required clearance -- or None for the flat path. The
        requirement is resolved through via_required, so this can never
        disagree with the offender test in nudge_vias_for_unresolved."""
        if not self._cap_floors_ok(cap):
            return None
        rec = self._cap_via_eff.get(ref)
        if rec is not None and rec[0] is vias:
            return rec[1]
        # The radius comes from the identity map, never from arithmetic on
        # v[3]: a tuple a test assigned wholesale carries its own keep-out
        # convention, and re-deriving would mis-price it silently. Absent ->
        # graded flat, i.e. v[3] is used as-is.
        by_id = self._via_radius_by_id
        cl = self._cap_pad_layers(cap)
        rows = []
        for i, fa in enumerate(cap.pad_floors):
            flat = self._flat_pad(cl, i)
            row = []
            for v in vias:
                rec = by_id.get(id(v))
                if rec is None:
                    row.append(v[3])
                elif flat:
                    row.append(rec[1] + self.clearance)
                else:
                    row.append(rec[1] + self.via_required(fa, v[2]))
            rows.append(row)
        self._cap_via_eff[ref] = (vias, rows)
        return rows

    def _pair_effs(self, ref, cap, oref, other):
        """[this cap's pad i][other mover's pad j] required clearance, or None.
        Keyed on the ORDERED pair so the row index always addresses `cap`; the
        summed shortfall itself is symmetric, so the seed baseline is not."""
        if not (self._cap_floors_ok(cap) and self._cap_floors_ok(other)):
            return None
        key = (ref, oref)
        rows = self._cap_pair_eff.get(key)
        # The other three memos guard on their source list's identity; this one
        # has no list to key on, so it validates its own SHAPE instead. Both
        # exist for the same reason: st.caps / st.cap_* are assignable from a
        # test, and a stale memo here is an IndexError or a wrong requirement.
        if rows is not None and (len(rows) != len(cap.pad_floors)
                                 or (rows and len(rows[0]) != len(other.pad_floors))):
            rows = None
        if rows is None:
            mine, theirs = self._cap_pad_layers(cap), self._cap_pad_layers(other)
            rows = [[self.clearance
                     if (self._flat_pad(mine, i) or self._flat_pad(theirs, j))
                     else self._pair_or_flat(fa, fb)
                     for j, fb in enumerate(other.pad_floors)]
                    for i, fa in enumerate(cap.pad_floors)]
            self._cap_pair_eff[key] = rows
        return rows

    def required_rows(self, net_names=None, limit=200):
        """Rows `[cap_ref, '<kind> <partner>', mm, source]` for every pair this
        pass actually CHARGED at a requirement above the flat scalar, at the
        SEED pose or the final one (see `both` below for why both).

        Same 4-column shape as legality.grade_pad_legality's 'required', so
        legality.format_required_clause renders it unchanged. `[]` when inert.
        Only charged pairs are listed -- an in-reach pair that happens to be
        clear is not a finding, it is the normal case on a declaring board.
        The partner is named by NET, not by reference: foreign_pads / vias /
        segments carry no owning ref and their tuple shapes are pinned."""
        if self._floors is None:
            return []
        names = net_names or {}
        pair_with_source = self._floors.pair_with_source
        rows = []

        def net_label(net):
            if net == 0:
                return '<no net>'
            if net == -1:
                return '<hole>'
            return names.get(net, str(net))

        def best(floors_a, items, floor_of, net_of, only,
                 cap_layers=None, layer_of=None, side_of=None):
            """Worst requirement, with its source, over the items on `only`.

            Takes the SAME two per-pad guards the eff builders take, because a
            report that re-derives the price instead of mirroring it reports
            pairs the pass never charged. Both were measured: without the
            layer guard, up to 43% of the rows on rp2350 named a cap pad
            against a track on a layer it does not occupy -- pairs check_drc
            does not grade and `_seg_effs` prices at the flat scalar."""
            out = {}
            for t in items:
                net = net_of(t)
                if net not in only:
                    continue
                fb = floor_of(t)
                if fb is None:
                    continue
                # an SMD foreign pad on the other board side is skipped by
                # _pad_shortfalls, so it charges nothing and belongs in no row
                pside = None if side_of is None else side_of(t)
                if pside is not None and pside != cap.side:
                    continue
                layer = None if layer_of is None else layer_of(t)
                for i, fa in enumerate(floors_a):
                    # a copper-less cap pad is graded flat, so it charges
                    # nothing and belongs in no row
                    if _Repair._flat_pad(cap_layers, i):
                        continue
                    # ...and neither does a pair that shares no copper layer
                    if (layer is not None and cap_layers is not None
                            and layer not in cap_layers[i]):
                        continue
                    mm, src = pair_with_source(fa, fb)
                    if src and mm > out.get(net, (0.0, ''))[0]:
                        out[net] = (mm, src)
            return out

        for ref, cap in self.caps.items():
            if not self._cap_floors_ok(cap):
                continue
            fls = cap.pad_floors
            x, y, rot = cap.x, cap.y, cap.rot
            sx, sy, srot = cap.seed_x, cap.seed_y, cap.seed_rot

            def both(fn):
                """Nets charged at the SEED pose or the final one.

                The seed half is the point: the pass SUCCEEDS by leaving
                nothing charged, so a final-pose-only report is empty exactly
                when the raised requirement did its work -- and the operator
                could not tell a run graded at --clearance from one graded at
                five times it."""
                out = set(fn(ref, cap, sx, sy, srot))
                out |= set(fn(ref, cap, x, y, rot))
                return out

            cl = self._cap_pad_layers(cap)
            for kind, items, floor_of, net_of, charged, layer_of, side_of in (
                    ('pad', self.cap_foreign_pads[ref],
                     lambda t: self._fp_floor_by_id.get(id(t)), lambda t: t[4],
                     both(self._pad_shortfalls), None, lambda t: t[5]),
                    ('via', self.cap_vias[ref],
                     lambda t: self._via_floor_for(t[2]), lambda t: t[2],
                     both(self._via_shortfalls), None, None),
                    ('track', self.cap_segs[ref],
                     lambda t: self._seg_floor_by_id.get(id(t)), lambda t: t[4],
                     both(self._seg_shortfalls),
                     lambda t: self._seg_layer_by_id.get(id(t)), None)):
                for net, (mm, src) in best(fls, items, floor_of, net_of,
                                           set(charged), cl, layer_of,
                                           side_of).items():
                    rows.append([ref, '{} {}'.format(kind, net_label(net)),
                                 round(mm, 6), src])
            # mover-vs-mover: the shortfall is a scalar per pair, so charge the
            # pair as a whole and name it by the partner's reference.
            cand = cap.pad_rects(x, y, rot)
            seed = cap.pad_rects(sx, sy, srot)
            for oref in self.cap_caps[ref]:
                other = self.caps[oref]
                if not self._cap_floors_ok(other):
                    continue
                effs = self._pair_effs(ref, cap, oref, other)
                # seed pose OR final pose, for the reason `both` gives above
                now = _pad_pair_shortfall(cand, other.pad_rects(),
                                          self.clearance, effs)
                was = _pad_pair_shortfall(
                    seed, other.pad_rects(other.seed_x, other.seed_y,
                                          other.seed_rot),
                    self.clearance, effs)
                if now <= EPS and was <= EPS:
                    continue
                mm, src = 0.0, ''
                theirs = self._cap_pad_layers(other)
                for i, fa in enumerate(fls):
                    if self._flat_pad(cl, i):
                        continue
                    for j, fb in enumerate(other.pad_floors):
                        if self._flat_pad(theirs, j):
                            continue
                        m, sc = pair_with_source(fa, fb)
                        if sc and m > mm:
                            mm, src = m, sc
                if src:
                    rows.append([ref, 'cap ' + oref, round(mm, 6), src])
        rows.sort(key=lambda r: (-r[2], r[0], r[1]))
        return rows[:limit]

    def _overlap(self, a, b):
        """Courtyard-clearance shortfall between two rects (0 if clear).

        Deliberately NOT #725-converted: a courtyard is a mechanical/assembly
        extent, not copper, so charging a netclass or a pad override here would
        move caps for a reason KiCad's DRC never raises. The cap_static prune
        that gates it is therefore still exact at the flat scalar."""
        return max(0.0, self.clearance - _rect_gap(a, b))

    def via_penalty(self, cap, x, y, rot, vias=None, ref=None):
        """Sum of foreign-net via penetration depths for a cap placement
        (how far each pad intrudes inside a different-net via's keep-out).

        vias defaults to the full board list; callers with a ref pass the
        per-cap pruned list (self.cap_vias[ref]), which is exact -- a via that
        can penetrate a pad is necessarily within the cap's reach.

        #725: `ref` keys the per-pair required-clearance memo. Without it (the
        4-positional test/default path) the flat scalar is used, which is what
        the whole-board `vias` default is for anyway."""
        vias = self.vias if vias is None else vias
        effs = None if ref is None else self._via_effs(ref, cap, vias)
        pen = 0.0
        if effs is None:
            # The tuple's slot is the prune OVER-reach, not a requirement, so
            # grading it directly would charge more than any pair needs (and
            # more than upstream's slot, which was radius + clearance). Recover
            # the radius where we know it and grade at the flat scalar, which
            # is what this path documents. A tuple absent from the map -- one a
            # test assigned wholesale -- is graded exactly as injected.
            by_id = self._via_radius_by_id
            for (bx0, by0, bx1, by1, net) in cap.pad_rects(x, y, rot):
                for v in vias:
                    vx, vy, vnet, keepout = v
                    if vnet == net:
                        continue
                    rec = by_id.get(id(v))
                    ko = keepout if rec is None else rec[1] + self.clearance
                    d = _point_to_rect_dist(vx, vy, (bx0, by0, bx1, by1))
                    if d < ko - EPS:
                        pen += (ko - d)
            return pen
        # #725 active path: effs[i][j] IS the pair's keep-out (via radius +
        # required clearance), precomputed -- the tuple's own keepout slot is
        # the prune over-reach and is not the requirement.
        for i, (bx0, by0, bx1, by1, net) in enumerate(cap.pad_rects(x, y, rot)):
            row = effs[i]
            for j, (vx, vy, vnet, _ko) in enumerate(vias):
                if vnet == net:
                    continue
                ko = row[j]
                d = _point_to_rect_dist(vx, vy, (bx0, by0, bx1, by1))
                if d < ko - EPS:
                    pen += (ko - d)
        return pen

    def _via_shortfalls(self, ref, cap, x, y, rot):
        """PER-FOREIGN-NET via penetration for a placement, keyed by the via's
        net_id (#445) -- the via analogue of _seg_shortfalls/_pad_shortfalls.
        A net absent from the dict is fully clear; a positive value is a real
        PAD-VIA DRC violation. Backs the hard accept gate so a move can never
        drop a pad onto a via net that was clear at the seed, no matter how
        much track graze it relieves elsewhere (zynq_ad9364 R2 onto the
        DDR3_VREF via)."""
        by_net: Dict[int, float] = {}
        vias = self.cap_vias[ref]
        effs = self._via_effs(ref, cap, vias)
        if effs is None:
            for (bx0, by0, bx1, by1, net) in cap.pad_rects(x, y, rot):
                for vx, vy, vnet, keepout in vias:
                    if vnet == net:
                        continue
                    d = _point_to_rect_dist(vx, vy, (bx0, by0, bx1, by1))
                    if d < keepout - EPS:
                        by_net[vnet] = by_net.get(vnet, 0.0) + (keepout - d)
            return by_net
        for i, (bx0, by0, bx1, by1, net) in enumerate(cap.pad_rects(x, y, rot)):
            row = effs[i]
            for j, (vx, vy, vnet, _ko) in enumerate(vias):
                if vnet == net:
                    continue
                keepout = row[j]
                d = _point_to_rect_dist(vx, vy, (bx0, by0, bx1, by1))
                if d < keepout - EPS:
                    by_net[vnet] = by_net.get(vnet, 0.0) + (keepout - d)
        return by_net

    def _seg_shortfalls(self, ref, cap, x, y, rot):
        """PER-FOREIGN-NET, same-side track clearance shortfall for a placement,
        keyed by the track's net_id (#441): {snet: sum of (halfw - dist) over that
        net's segments this cap penetrates}. A net absent from the dict is fully
        clear. halfw already includes the clearance, so a positive shortfall is a
        real PAD-SEGMENT DRC violation; kept PER-NET so the accept gate can forbid
        penetrating a net that was CLEAR at the seed even when the aggregate
        shortfall drops -- the zynq_ad9364 GND<->NetR2_2 repair-pass short (a move
        that relieved a 260um DDR3_BA2 graze by dropping a GND pad 85um onto a
        previously-clear NetR2_2 track looked like a net improvement to the old
        scalar baseline). Uses the per-cap pruned, same-side track list."""
        by_net: Dict[int, float] = {}
        segs = self.cap_segs[ref]
        effs = self._seg_effs(ref, cap)
        if effs is None:
            for (bx0, by0, bx1, by1, net) in cap.pad_rects(x, y, rot):
                for x1, y1, x2, y2, snet, halfw, side in segs:
                    if snet == net:
                        continue
                    # cheap reject: the segment's bbox can't reach the pad rect
                    if (min(x1, x2) > bx1 + halfw or max(x1, x2) < bx0 - halfw
                            or min(y1, y2) > by1 + halfw
                            or max(y1, y2) < by0 - halfw):
                        continue
                    d = _seg_to_rect_dist(x1, y1, x2, y2, (bx0, by0, bx1, by1))
                    if d < halfw - EPS:
                        by_net[snet] = by_net.get(snet, 0.0) + (halfw - d)
            return by_net
        # #725 active path: effs[i][j] IS the pair's keep-out, and the cheap
        # bbox reject widens with it -- a reject left at the flat half width
        # would silently drop the pairs this fix exists to charge.
        for i, (bx0, by0, bx1, by1, net) in enumerate(cap.pad_rects(x, y, rot)):
            row = effs[i]
            for j, (x1, y1, x2, y2, snet, _hw, side) in enumerate(segs):
                if snet == net:
                    continue
                halfw = row[j]
                if (min(x1, x2) > bx1 + halfw or max(x1, x2) < bx0 - halfw or
                        min(y1, y2) > by1 + halfw or max(y1, y2) < by0 - halfw):
                    continue
                d = _seg_to_rect_dist(x1, y1, x2, y2, (bx0, by0, bx1, by1))
                if d < halfw - EPS:
                    by_net[snet] = by_net.get(snet, 0.0) + (halfw - d)
        return by_net

    def seg_penalty(self, ref, cap, x, y, rot):
        """Scalar sum of the per-net track clearance shortfalls (the objective
        term / graze report). See _seg_shortfalls for the per-net breakdown the
        accept gate uses."""
        return sum(self._seg_shortfalls(ref, cap, x, y, rot).values())

    def _pad_shortfalls(self, ref, cap, x, y, rot):
        """PER-FOREIGN-NET component-pad clearance shortfall (#235), keyed by the
        foreign pad's net_id (#441 per-net, mirroring _seg_shortfalls): how far
        each cap pad intrudes inside a different-net pad's clearance keep-out. A
        through-hole foreign pad blocks both sides; an SMD one only its own side.
        Same-net pads are ignored (a shared via / touching same-net copper is
        fine)."""
        by_net: Dict[int, float] = {}
        fpads = self.cap_foreign_pads[ref]
        effs = self._pad_effs(ref, cap)
        if effs is None:
            for (bx0, by0, bx1, by1, net) in cap.pad_rects(x, y, rot):
                for (px0, py0, px1, py1, pnet, pside) in fpads:
                    if pnet == net:
                        continue
                    if pside is not None and pside != cap.side:
                        continue  # SMD pad on the other side
                    gap = _rect_gap((bx0, by0, bx1, by1), (px0, py0, px1, py1))
                    if gap < self.clearance - EPS:
                        by_net[pnet] = by_net.get(pnet, 0.0) + (self.clearance - gap)
            return by_net
        for i, (bx0, by0, bx1, by1, net) in enumerate(cap.pad_rects(x, y, rot)):
            row = effs[i]
            for j, (px0, py0, px1, py1, pnet, pside) in enumerate(fpads):
                if pnet == net:
                    continue
                if pside is not None and pside != cap.side:
                    continue  # SMD pad on the other side
                eff = row[j]
                gap = _rect_gap((bx0, by0, bx1, by1), (px0, py0, px1, py1))
                if gap < eff - EPS:
                    by_net[pnet] = by_net.get(pnet, 0.0) + (eff - gap)
        return by_net

    def pad_penalty(self, ref, cap, x, y, rot):
        """Scalar sum of the per-net foreign-pad shortfalls."""
        return sum(self._pad_shortfalls(ref, cap, x, y, rot).values())

    @staticmethod
    def _worsens_any_net(cand_by_net, seed_by_net):
        """True if the candidate penetrates ANY foreign net beyond its seed
        shortfall (+EPS) -- in particular, penetrates a net that was CLEAR
        (absent from seed_by_net) at the seed. Per-net so relieving net A can
        never pay for a NEW short on net B (#441)."""
        for net, pen in cand_by_net.items():
            if pen > seed_by_net.get(net, 0.0) + EPS:
                return True
        return False

    def attraction(self, cap, x, y, rot):
        """Sum over pads of the distance to the nearest same-net BGA ball,
        clamped to capture_radius (so a pad with no same-net ball within reach
        contributes a flat constant and creates no long-range pull)."""
        total = 0.0
        for (bx0, by0, bx1, by1, net) in cap.pad_rects(x, y, rot):
            balls = self.attract.get(net)
            if not balls:
                continue
            cx, cy = (bx0 + bx1) / 2.0, (by0 + by1) / 2.0
            nearest = min(math.hypot(ax - cx, ay - cy) for ax, ay in balls)
            total += min(nearest, self.capture_radius)
        return total

    def _blocked_geom(self, ref, cap, x, y, rot):
        """The cheap, purely geometric hard constraints: board edge and
        introduced/worsened same-side courtyard or mover-pad overlaps."""
        rect = cap.rect(x, y, rot)
        if (rect[0] < self.usable[0] or rect[1] < self.usable[1]
                or rect[2] > self.usable[2] or rect[3] > self.usable[3]):
            return True
        # Real outline / cutout gate (#370 B2): only when the bbox inset is
        # not exact (non-rect outline or cutouts) and this cap can reach one.
        if self._edge_active and self._cap_may_reach_edge(ref, cap) \
                and self._rect_edge_blocked(rect, ref=ref):
            return True
        # only same-side parts/caps within reach can collide (pre-pruned)
        for idx, r in self.cap_static[ref]:
            base = self.base_static.get((ref, idx), 0.0)
            if self._overlap(rect, r) > base + EPS:
                return True
        cand_pads = None
        cand_bbox = None
        for other_ref in self.cap_caps[ref]:
            pair = frozenset((ref, other_ref))
            other = self.caps[other_ref]
            if self._overlap(rect, other.rect()) > \
                    self.base_cap.get(pair, 0.0) + EPS:
                return True
            # no new/worse different-net pad encroachment against another
            # MOVER at its current pose (#275); each accepted move preserves
            # the pairwise seed baseline, so the invariant holds inductively
            # as both parts move.
            # Pad-bbox prescreen: pads are contained in their union bbox, so
            # every pad-pair gap >= the bbox gap; bboxes >= clearance apart
            # means the shortfall is exactly 0 <= base + EPS -- skip the
            # pairwise scan (the dominant cost of the candidate sweep).
            # #725: the prescreen is a SKIP, so it must widen with the pair's
            # requirement -- left at the flat scalar it silently drops exactly
            # the pairs a pad override / netclass / dru rule raises. Bounded by
            # the two movers' OWN maxima, never a board-wide one: this runs per
            # candidate pose.
            if cand_bbox is None:
                cand_bbox = cap.pad_bbox(x, y, rot)
            screen = self.clearance if self._floors is None else max(
                self.clearance, cap.max_floor, other.max_floor)
            if _rect_gap(cand_bbox, other.pad_bbox()) >= screen:
                continue
            if cand_pads is None:
                cand_pads = cap.pad_rects(x, y, rot)
            if _pad_pair_shortfall(cand_pads, other.pad_rects(),
                                   self.clearance,
                                   self._pair_effs(ref, cap, other_ref, other)) > \
                    self.base_cap_pad.get(pair, 0.0) + EPS:
                return True
        return False

    def hard_blocked(self, ref, cap, x, y, rot):
        """True if a placement leaves the board or introduces/worsens a
        same-side courtyard overlap (with a locked part OR another movable
        cap) beyond its baseline, or worsens a foreign track/pad graze past
        its seed baseline. Caps may never overlap each other's footprints,
        so any new cap-cap overlap is rejected outright."""
        if self._blocked_geom(ref, cap, x, y, rot):
            return True
        # #441 per-net: no NEW/worse overlap with ANY foreign-net track -- forbid
        # penetrating a net that was clear at the seed even if the summed shortfall
        # drops (the zynq GND<->NetR2_2 butterfly). Same for foreign component pads.
        if self._worsens_any_net(self._seg_shortfalls(ref, cap, x, y, rot),
                                 self.base_seg.get(ref, {})):
            return True
        if self._worsens_any_net(self._pad_shortfalls(ref, cap, x, y, rot),
                                 self.base_pad.get(ref, {})):
            return True
        # #445: same per-net gate for existing VIAS -- via_penalty alone is a
        # weighted objective, and a big track-graze relief could pay for
        # dropping a pad onto a via (zynq_ad9364 R2 onto DDR3_VREF).
        if self._worsens_any_net(self._via_shortfalls(ref, cap, x, y, rot),
                                 self.base_via.get(ref, {})):
            return True
        return False

    def graze_penalty(self, ref, cap, x, y, rot):
        """Total foreign-copper clearance shortfall for a placement: via
        (#130) + same-side track (#278 PAD-SEGMENT) + component pad (#275
        PAD-PAD). Anything positive is a shipped DRC violation, so all three
        are violations to FIX, not just baselines to preserve."""
        return (self.via_penalty(cap, x, y, rot, self.cap_vias[ref], ref=ref)
                + self.seg_penalty(ref, cap, x, y, rot)
                + self.pad_penalty(ref, cap, x, y, rot))

    def cost(self, ref, cap, x, y, rot):
        if self._blocked_geom(ref, cap, x, y, rot):
            return float('inf')
        seg_by_net = self._seg_shortfalls(ref, cap, x, y, rot)
        pad_by_net = self._pad_shortfalls(ref, cap, x, y, rot)
        via_by_net = self._via_shortfalls(ref, cap, x, y, rot)
        # #441/#445 per-net accept gate (mirror hard_blocked): reject a move
        # that penetrates any foreign net -- track, pad, or VIA -- beyond its
        # seed, even if the total improves.
        if (self._worsens_any_net(seg_by_net, self.base_seg.get(ref, {}))
                or self._worsens_any_net(pad_by_net, self.base_pad.get(ref, {}))
                or self._worsens_any_net(via_by_net, self.base_via.get(ref, {}))):
            return float('inf')
        seg_pen = sum(seg_by_net.values())
        pad_pen = sum(pad_by_net.values())
        disp = math.hypot(x - cap.seed_x, y - cap.seed_y)
        graze = (sum(via_by_net.values()) + seg_pen + pad_pen)
        return (VIA_WEIGHT * graze
                + ATTRACT_WEIGHT * self.attraction(cap, x, y, rot)
                + DISPLACEMENT_WEIGHT * disp)


def repair_fanout_clearance(pcb_data: PCBData, pcb_file: str,
                            clearance: float = 0.2,
                            grid_step: float = 0.1,
                            board_edge_clearance: float = 0.55,
                            near_margin: float = 1.0,
                            capture_radius: float = 2.0,
                            default_via_size: float = 0.3,
                            step: float = 0.2,
                            max_displacement: float = 2.0,
                            max_displacement_cap: float = 3.0,
                            displacement_growth: float = 1.5,
                            allow_rotations: bool = True,
                            cap_prefix: str = "C,R,FB",
                            lock_refs: Optional[List[str]] = None,
                            max_passes: int = 30,
                            via_clear_fallback: bool = True,
                            verbose: bool = False,
                            on_move=None,
                            # progress_callback(current, total, label): the
                            # candidate-position sweep per cap x up to 30
                            # passes is the slow part, so each cap visit
                            # reports (GUI status line). None = silent.
                            progress_callback=None) -> Dict:
    """Nudge near-BGA decoupling caps off foreign-net fanout copper (vias
    #130, escape tracks #278, component pads #275) and toward same-net balls.
    Run AFTER bga_fanout.py.

    Returns a dict with 'placements' (list of {reference,new_x,new_y,
    new_rotation} for moved caps), 'resolved', 'unresolved' (refs still
    grazing foreign copper), and 'bga_refs'.

    via_clear_fallback (#213): when True (default), any cap the normal cost
    descent leaves grazing foreign copper is jumped to the nearest position
    that fully clears it (still respecting every hard clearance). It is
    deliberately NOT exposed on the CLI / GUI -- flip this argument in code to
    disable.

    on_move, if given, is a callback invoked with the internal _Repair state
    once at the seed placement and again after every accepted cap move. It is
    used purely for visualization (animate_fanout_clearance.py) and has no
    effect on the result.
    """
    extra_locked: Set[str] = set()
    if lock_refs:
        import fnmatch
        for ref in pcb_data.footprints:
            if any(fnmatch.fnmatch(ref, pat) for pat in lock_refs):
                extra_locked.add(ref)

    st = _Repair(pcb_data, pcb_file, clearance, grid_step,
                 board_edge_clearance, near_margin, capture_radius,
                 default_via_size, cap_prefix, extra_locked,
                 max_displacement_cap=max_displacement_cap)

    print(f"BGAs: {', '.join(st.bga_refs) or '(none)'}  "
          f"fanout vias: {len(st.vias)}  "
          f"movable near-BGA caps: {len(st.caps)}")
    # #725: the early returns carry 'required' / 'clearance_notes' too, so a
    # caller does not have to special-case a no-op board for them. (They still
    # omit 'via_moves' / 'new_segments', as they always have -- every caller
    # reads the dict with .get.)
    if not st.vias:
        print("No vias on the board - run this AFTER bga_fanout.py.")
        return {'placements': [], 'resolved': [], 'unresolved': [],
                'bga_refs': st.bga_refs, 'required': [],
                'clearance_notes': list(st.clearance_notes)}
    if not st.caps:
        print("No movable caps near a BGA - nothing to do.")
        return {'placements': [], 'resolved': [], 'unresolved': [],
                'bga_refs': st.bga_refs, 'required': [],
                'clearance_notes': list(st.clearance_notes)}

    # Initial violators: any foreign-copper clearance shortfall (via #130,
    # track #278, pad #275) is a shipped DRC violation to fix.
    violators0 = [r for r, c in st.caps.items()
                  if st.graze_penalty(r, c, c.x, c.y, c.rot) > EPS]
    print(f"Caps initially grazing foreign copper (via/track/pad): "
          f"{len(violators0)}"
          + (f" ({', '.join(sorted(violators0))})" if violators0 else ""))

    # LOCKED parts are excluded from moving by design, but a foreign via inside
    # a locked pad's keep-out is then unfixable here -- surface it instead of
    # silently reporting a clean pass (#254: locked back-side cap under a BGA
    # via-in-pad). The fanout avoids locked copper for NEW vias; this warning
    # catches boards fanned before that fix or vias from other sources.
    locked_hits = []
    for ref in sorted(st.locked_refs):
        fp = pcb_data.footprints.get(ref)
        if fp is None:
            continue
        for p in fp.pads:
            # #725: an np_thru_hole pad lists *.Cu and carries no copper. It
            # is still WARNED ABOUT -- upstream did, foreign_pads does, and a
            # via inside a 1.7mm mounting hole is exactly what this warning is
            # for -- but it is graded FLAT: its keep-clear override is not
            # copper and must not raise the pair. Dropping it outright (the
            # first version of this fix) removed real rows on boards that
            # declare nothing at all.
            if not any(str(l).endswith('.Cu') for l in p.layers):
                continue
            _carries = _pad_carries_copper(p)
            # ...and the RECT gets the same rect_rotation inflation _Cap and
            # foreign_pads use. This was the one pad-geometry site in the file
            # built from raw size_x/size_y, so a tilted locked pad under-blocked
            # by (sqrt(2)-1)*half along the board axes -- 0.14mm on glasgow's
            # 45-degree R9. Converting the clearance term and leaving the shape
            # would be exactly the half-conversion this change is about.
            tilt = math.radians(getattr(p, 'rect_rotation', 0.0) or 0.0)
            _c, _s = abs(math.cos(tilt)), abs(math.sin(tilt))
            _hx, _hy = p.size_x / 2.0, p.size_y / 2.0
            ex, ey = _hx * _c + _hy * _s, _hx * _s + _hy * _c
            rect = (p.global_x - ex, p.global_y - ey,
                    p.global_x + ex, p.global_y + ey)
            # Graded at the pair's REAL requirement, like everything else.
            # `keepout` is the prune over-reach, so re-form the pair's keep-out
            # from the via's radius. A locked fiducial's keep-clear override is
            # exactly the case this warning exists to surface, and priced flat
            # it under-reported it.
            pfl = st.pad_floor(p) if _carries else None
            for vx, vy, vnet, keepout in st.vias:
                if vnet == p.net_id:
                    continue
                ko = (keepout - st._item_reach(st.via_floor(vnet))
                      + st.via_required(pfl, vnet))
                if _point_to_rect_dist(vx, vy, rect) < ko - EPS:
                    locked_hits.append((ref, p.pad_number, vx, vy))
    if locked_hits:
        print(f"WARNING: {len(locked_hits)} foreign via(s) inside LOCKED parts' pad "
              f"clearance (cannot move a locked part):")
        for ref, pnum, vx, vy in locked_hits[:10]:
            print(f"    {ref}.{pnum} <-> via at ({vx:.2f},{vy:.2f})")

    if on_move is not None:
        on_move(st)  # seed frame

    budget = {r: max_displacement for r in st.caps}
    rotate = {r: False for r in st.caps}
    # process worst violators first
    order = sorted(st.caps, key=lambda r: st.graze_penalty(
        r, st.caps[r], st.caps[r].x, st.caps[r].y, st.caps[r].rot),
        reverse=True)

    # Pause the CYCLIC collector for the sweep. The candidate loops allocate
    # short-lived tuples/lists at a rate that fires gen-2 collections, and
    # each of those scans the ENTIRE process heap -- in a live GUI chain
    # session (4 prior steps of boards/fills/engines resident) the identical
    # sweep measured 5.9x slower than in a fresh process (mez_rx: 184s vs
    # 31s, same call counts) purely from GC pressure. The sweep's garbage is
    # acyclic, so reference counting reclaims it either way.
    import gc
    _gc_was_enabled = gc.isenabled()
    gc.disable()

    try:
        for pass_num in range(1, max_passes + 1):
            moves = 0
            for _oi, ref in enumerate(order):
                if progress_callback:
                    progress_callback(
                        _oi + 1, len(order),
                        f"Cap optimize pass {pass_num}: {ref}")
                cap = st.caps[ref]
                current = st.cost(ref, cap, cap.x, cap.y, cap.rot)
                rots = ROTATIONS if (allow_rotations and rotate[ref]) \
                    else [cap.rot]
                best = (current, cap.x, cap.y, cap.rot)
                for cx, cy in _candidate_positions(cap, budget[ref], step,
                                                   grid_step):
                    for rot in rots:
                        if (cx, cy, rot) == (cap.x, cap.y, cap.rot):
                            continue
                        c = st.cost(ref, cap, cx, cy, rot)
                        if c < best[0] - EPS:
                            best = (c, cx, cy, rot)
                if best[0] < current - EPS:
                    cap.x, cap.y, cap.rot = best[1], best[2], best[3]
                    moves += 1
                    if on_move is not None:
                        on_move(st)
                    if verbose:
                        print(f"  pass {pass_num}: {ref} -> "
                              f"({cap.x:.3f},{cap.y:.3f}) rot {cap.rot:g} "
                              f"cost {best[0]:.3f}")

            residual = {r: st.graze_penalty(r, st.caps[r], st.caps[r].x,
                                            st.caps[r].y, st.caps[r].rot)
                        for r in st.caps}
            still = [r for r, p in residual.items() if p > EPS]
            if not still:
                print(f"All foreign-copper grazes cleared after {pass_num} "
                      f"pass(es).")
                break
            if moves == 0:
                # stuck: escalate budget / rotations for the remaining
                # violators
                grown = False
                for r in still:
                    if not rotate[r] and allow_rotations:
                        rotate[r] = True
                        grown = True
                    elif budget[r] < max_displacement_cap - EPS:
                        budget[r] = min(budget[r] * displacement_growth,
                                        max_displacement_cap)
                        grown = True
                if not grown:
                    print(f"Stuck with {len(still)} cap(s) still grazing "
                          f"foreign copper (at the displacement cap): "
                          f"{', '.join(sorted(still))}")
                    break
                if verbose:
                    print(f"  escalating budget/rotation for {len(still)} "
                          f"cap(s)")

        # Fallback (#213): a cap may still graze a foreign via because the
        # soft cost (same-net attraction + displacement) judged the tiny
        # penetration cheaper than a clear-but-distant spot, so the greedy
        # descent never relocates it. A shipped PAD-VIA short is worse than a
        # displaced decap, so for any cap still in via-conflict, jump it to
        # the best position (within the full displacement budget) that FULLY
        # clears every foreign via -- cost() still rejects any hard-constraint
        # violation (board edge, courtyard, foreign track/pad #235), so this
        # can never introduce a new short/overlap; it only overrides the soft
        # trade-off. If no clean via-clearing spot exists the cap is left
        # as-is and reported unresolved (needs a via re-drop, not a move).
        if via_clear_fallback:
            stuck = [r for r in st.caps
                     if st.graze_penalty(r, st.caps[r], st.caps[r].x,
                                         st.caps[r].y,
                                         st.caps[r].rot) > EPS]
            rots_all = ROTATIONS if allow_rotations else None
            for _fi, ref in enumerate(stuck):
                if progress_callback:
                    progress_callback(
                        _fi + 1, len(stuck),
                        f"Cap optimize: via-clear fallback for {ref}")
                cap = st.caps[ref]
                rots = rots_all if rots_all is not None else [cap.rot]
                best = None  # (cost, x, y, rot)
                for cx, cy in _candidate_positions(cap, max_displacement_cap,
                                                   step, grid_step):
                    for rot in rots:
                        if st.graze_penalty(ref, cap, cx, cy, rot) > EPS:
                            continue
                        c = st.cost(ref, cap, cx, cy, rot)  # inf if blocked
                        if c == float('inf'):
                            continue
                        if best is None or c < best[0] - EPS:
                            best = (c, cx, cy, rot)
                if best is not None:
                    cap.x, cap.y, cap.rot = best[1], best[2], best[3]
                    disp = math.hypot(cap.x - cap.seed_x, cap.y - cap.seed_y)
                    if verbose:
                        print(f"  fallback: {ref} relocated to clear foreign "
                              f"copper at disp {disp:.2f}mm -> "
                              f"({cap.x:.3f},{cap.y:.3f}) rot {cap.rot:g}")
    finally:
        if _gc_was_enabled:
            gc.enable()

    placements = []
    resolved = []
    for ref, cap in st.caps.items():
        moved = (abs(cap.x - cap.seed_x) > EPS or abs(cap.y - cap.seed_y) > EPS
                 or abs(cap.rot - cap.seed_rot) > EPS)
        if moved:
            placements.append({'reference': ref, 'new_x': cap.x,
                               'new_y': cap.y, 'new_rotation': cap.rot})
        if (ref in violators0
                and st.graze_penalty(ref, cap, cap.x, cap.y, cap.rot) <= EPS):
            resolved.append(ref)
    unresolved = [r for r in st.caps
                  if st.graze_penalty(r, st.caps[r], st.caps[r].x,
                                      st.caps[r].y, st.caps[r].rot) > EPS]

    # Last resort (#313): a cap still grazing at the displacement cap is
    # usually BOXED (no clear cap position exists) -- move the offending
    # fanout via instead, dragging its attached escape-segment ends.
    via_moves, new_segs = ([], [])
    if unresolved:
        via_moves, new_segs = nudge_vias_for_unresolved(st, pcb_data, clearance)
        if via_moves:
            # refresh the per-cap pruned via lists before re-grading
            st.cap_vias = {r: st.vias for r in st.caps}
            unresolved = [r for r in st.caps
                          if st.graze_penalty(r, st.caps[r], st.caps[r].x,
                                              st.caps[r].y, st.caps[r].rot) > EPS]

    print(f"Moved {len(placements)} cap(s); resolved {len(resolved)}/"
          f"{len(violators0)} initial violations; "
          f"{len(unresolved)} unresolved.")
    if unresolved:
        print(f"  Unresolved (need manual attention): {', '.join(sorted(unresolved))}")
    # #725: disclose what was graded ABOVE the flat --clearance, and why.
    # Printed from the ENGINE so the CLI and the GUI plugin both inherit it.
    required = st.required_rows({n.net_id: n.name for n in pcb_data.nets.values()})
    _clause = format_required_clause({'required': required})
    if _clause:
        print(f"  above the {clearance}mm floor: {_clause}")
    for _note in st.clearance_notes:
        print(f"  pad clearance: {_note}")

    return {'placements': placements, 'resolved': resolved,
            'unresolved': unresolved, 'bga_refs': st.bga_refs,
            'via_moves': via_moves, 'new_segments': new_segs,
            'required': required, 'clearance_notes': list(st.clearance_notes)}


def _point_in_poly(px, py, poly) -> bool:
    """Ray-cast point-in-polygon for zone containment (#313 pour-tie reconnect)."""
    n = len(poly)
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]; xj, yj = poly[j]
        if ((yi > py) != (yj > py)) and \
           (px < (xj - xi) * (py - yi) / ((yj - yi) or 1e-12) + xi):
            inside = not inside
        j = i
    return inside


def nudge_vias_for_unresolved(st, pcb_data, clearance: float,
                              max_shift: float = 0.6):
    """Last resort for caps still grazing at the displacement cap (#313): the
    cap is boxed (glasgow C77: no clear cap position exists within 2mm), so
    move the OFFENDING fanout via a fraction of a millimetre instead. The
    original attached segments are NOT touched; electrical continuity is
    restored by NEW short connector segments from the via's new position back
    to the fanout stub start (the via's old position) on every layer that had
    same-net copper terminating there (plus the pad's copper layer for a
    via-in-pad). mm-exact validation throughout. Returns
    (via_moves, new_segments) for the writer:
      via_moves    = [(old_x, old_y, via_dict_at_new_pos)]
      new_segments = [segment dicts {'start','end','width','layer','net_id'}]
    """
    H2H_VIA = 0.2    # JLC via-hole to via-hole floor
    H2H_PAD = 0.45   # JLC via-hole to pad-hole floor
    via_moves, new_segments = [], []

    unresolved = [r for r in st.caps
                  if st.graze_penalty(r, st.caps[r], st.caps[r].x,
                                      st.caps[r].y, st.caps[r].rot) > EPS]
    if not unresolved:
        return via_moves, new_segments

    # Board edge / outline + NPTH floors (#370 B3): the mover has a 0.6mm
    # budget but validated candidates against copper only -- a via or its new
    # connector could be pushed off the board / into a cutout, and connectors
    # had no NPTH-hole test at all.
    from check_drc import (board_edge_geometry, _point_on_board,
                           _point_to_rings_distance, _segment_to_rings_distance,
                           point_to_pad_distance, _pad_has_no_copper,
                           segment_to_rect_distance)
    from kicad_parser import pad_drill_capsule
    from routing_utils import into_pad_frame_point
    from single_ended_routing import _seg_foreign_hole_dist
    edge_rings, edge_outer, edge_cutouts = board_edge_geometry(
        getattr(pcb_data, 'board_info', None))
    bounds = getattr(getattr(pcb_data, 'board_info', None), 'board_bounds', None)
    # #617 deliberately leaves this at the flat fab floor. The mover searches a
    # 0.6 mm budget for a spot where BOTH the relocated via and its connector
    # back to the stub validate; raising the connector's hole floor does not
    # relocate the via somewhere better, it makes the whole search return "no
    # clear spot" and the #130 pad-via graze the pass exists to fix persists.
    # Measured on the #370-B3 harness with a declared 0.25: raised -> 0 moves,
    # 0 connectors; flat -> 1 move, 1 connector.
    npth_clr = max(clearance, defaults.NPTH_TO_TRACK_CLEARANCE)

    def edge_ok_point(x, y, r):
        need = r + clearance
        if edge_rings:
            if not _point_on_board(x, y, edge_outer, edge_cutouts):
                return False
            return _point_to_rings_distance(x, y, edge_rings) >= need - 1e-6
        if bounds:
            return (x - bounds[0] >= need and bounds[2] - x >= need and
                    y - bounds[1] >= need and bounds[3] - y >= need)
        return True

    def edge_ok_seg(sx, sy, ex, ey, hw):
        need = hw + clearance
        if edge_rings:
            if not (_point_on_board(sx, sy, edge_outer, edge_cutouts) and
                    _point_on_board(ex, ey, edge_outer, edge_cutouts)):
                return False
            return _segment_to_rings_distance(sx, sy, ex, ey,
                                              edge_rings) >= need - 1e-6
        if bounds:
            return all(x - bounds[0] >= need and bounds[2] - x >= need and
                       y - bounds[1] >= need and bounds[3] - y >= need
                       for x, y in ((sx, sy), (ex, ey)))
        return True

    # (rect, net, cap_layer): cap pads exist only on the cap's own copper
    # side -- connector segments on OTHER layers cannot graze them (the
    # missing layer gate rejected every candidate: the connector necessarily
    # starts at the old via position, inside the grazed cap's keep-out, but
    # only the VIA barrel -- not an inner-layer connector -- conflicts there).
    # #725: every requirement below resolves through st's own resolvers, so the
    # nudger and the grader cannot disagree. `getattr` throughout -- tests pass
    # a duck-typed _FakeSt that carries none of this, and must grade flat.
    _req = getattr(st, 'required', None)
    _via_req = getattr(st, 'via_required', None)
    _pad_fl = getattr(st, 'pad_floor', None)
    _via_fl = getattr(st, 'via_floor', None)
    _seg_fl = getattr(st, 'seg_floor', None)

    def req(fa, fb):
        return clearance if _req is None else _req(fa, fb)

    def via_req(pad_floor, via_net):
        return clearance if _via_req is None else _via_req(pad_floor, via_net)

    def pad_fl(p):
        return None if _pad_fl is None else _pad_fl(p)

    def via_fl(net):
        return None if _via_fl is None else _via_fl(net)

    def seg_fl(net, layer):
        return None if _seg_fl is None else _seg_fl(net, layer)

    def cap_floors_of(cap, rects):
        """This cap's per-pad floors, index-aligned with `rects` -- or None,
        which grades flat. Tests drive this function with a duck-typed cap that
        carries neither the attribute nor a matching length.

        A pad whose `pad_layers` entry is EMPTY carries no copper, and its
        entry comes back None so it grades flat here too. The eff builders key
        on that same marker; without it the nudger would charge a phantom the
        grader does not, hand itself a via nothing flagged, and print an
        operator-facing line naming a non-violation.
        """
        pf = getattr(cap, 'pad_floors', None)
        if pf is None or len(pf) != len(rects):
            return None
        pl = getattr(cap, 'pad_layers', None)
        if pl is None or len(pl) != len(pf):
            return list(pf)
        return [f if pl[i] else None for i, f in enumerate(pf)]

    # `all_cap_rects` is function-local, so it CAN carry the pad's floor.
    all_cap_rects = []
    for ref, cap in st.caps.items():
        fp = pcb_data.footprints.get(ref)
        cl = getattr(fp, 'layer', 'F.Cu') if fp is not None else 'F.Cu'
        _rects = cap.pad_rects(cap.x, cap.y, cap.rot)
        pf = cap_floors_of(cap, _rects)
        for i, (bx0, by0, bx1, by1, net) in enumerate(_rects):
            all_cap_rects.append((bx0, by0, bx1, by1, net, cl,
                                  None if pf is None else pf[i]))

    # Flatten the board's pads ONCE, in pads_by_net iteration order, resolving
    # each pad's floor and drill capsule here instead of inside the 16-angle x
    # 12-radius sweep below. Strictly cheaper than the previous per-call walk.
    board_pads = []
    for _pads in pcb_data.pads_by_net.values():
        for p in _pads:
            if getattr(p, 'component_ref', None) in st.caps:
                continue  # movable caps handled by the final rects above
            cap_ = pad_drill_capsule(p) if (p.drill and p.drill > 0) else None
            board_pads.append((p, pad_fl(p), not _pad_has_no_copper(p), cap_))

    def valid_via_pos(v, nx, ny):
        vr = (v.size or 0.5) / 2.0
        vfl = via_fl(v.net_id)
        # never off the board / into a cutout / inside the edge margin (#370 B3)
        if not edge_ok_point(nx, ny, vr):
            return False
        for (bx0, by0, bx1, by1, net, _cl, pfl) in all_cap_rects:
            # via barrel spans all layers: no layer gate here
            if net != v.net_id and _point_to_rect_dist(
                    nx, ny, (bx0, by0, bx1, by1)) < vr + via_req(pfl, v.net_id):
                return False
        for p, pfl, has_cu, cap_ in board_pads:
            # rotation/shape-aware pad copper distance (#370 B3, #356
            # class: the axis-aligned size_x/size_y rect under-blocked
            # rotated pads). NPTH pads carry no copper -- drill-only.
            if (p.net_id != v.net_id and has_cu
                    and point_to_pad_distance(nx, ny, p) < vr + req(pfl, vfl)):
                return False
            if cap_ is not None:
                # slot/offset-aware drill capsule (net-INDEPENDENT floor, so
                # deliberately NOT #725-resolved -- see npth_clr above)
                (c1x, c1y), (c2x, c2y), prad = cap_
                if _point_to_seg_dist(nx, ny, c1x, c1y, c2x, c2y) < \
                        (v.drill or 0.3) / 2.0 + prad + H2H_PAD:
                    return False
        for ov in pcb_data.vias:
            if ov is v:
                continue
            d = math.hypot(nx - ov.x, ny - ov.y)
            if ov.net_id != v.net_id and d < vr + (ov.size or 0.5) / 2.0 + \
                    req(vfl, via_fl(ov.net_id)):
                return False
            if d < (v.drill or 0.3) / 2.0 + (ov.drill or 0.3) / 2.0 + H2H_VIA:
                return False
        for s in pcb_data.segments:
            if s.net_id == v.net_id:
                continue
            if _point_to_seg_dist(nx, ny, s.start_x, s.start_y, s.end_x, s.end_y) \
                    < vr + s.width / 2.0 + req(vfl, seg_fl(s.net_id, s.layer)):
                return False
        return True

    def connector_clear(net_id, layer, width, sx, sy, ex, ey):
        hw = width / 2.0
        # The connector is a TRACK on `layer`, so it resolves like one. Note
        # this honours netclasses and .kicad_dru LAYER rules, but not
        # TRACK-SCOPED .kicad_dru rules, which live in a channel
        # PadClearanceModel does not carry (kicad_dru.read_board_track_clearances,
        # applied by check_drc at the seg-seg site only). Under-blocking a
        # geometric connector that is separately DRC'd is the safe direction.
        # Filed as #735. (check_drc.py tags that channel `#549`; GitHub #549 is
        # a closed, unrelated issue, so this cites the tracker instead.)
        cfl = seg_fl(net_id, layer)
        # board edge / cutouts + NPTH drill holes at their floor (#370 B3):
        # a connector is drawn geometrically, not routed, so it must gate
        # against Edge.Cuts and copper-less holes itself.
        if not edge_ok_seg(sx, sy, ex, ey, hw):
            return False
        if _seg_foreign_hole_dist(pcb_data, net_id, sx, sy, ex, ey) < \
                npth_clr + hw - 1e-4:
            return False
        for (bx0, by0, bx1, by1, net, cl, pfl) in all_cap_rects:
            if cl != layer:
                continue  # cap pads only exist on the cap's own side
            if net != net_id and _seg_to_rect_dist(
                    sx, sy, ex, ey, (bx0, by0, bx1, by1)) < hw + req(pfl, cfl):
                return False
        for p, pfl, has_cu, _cap in board_pads:
            if p.net_id == net_id:
                continue
            if not _pad_on_layer(p, layer):
                continue
            if not has_cu:
                continue  # NPTH: no copper; the hole check above covers it
            # rotation-aware pad rect (#370 B3): rotate the segment into
            # the pad's frame so a tilted pad is tested against its true
            # rectangle (distance is rotation-invariant).
            rx1, ry1 = into_pad_frame_point(sx, sy, p)
            rx2, ry2 = into_pad_frame_point(ex, ey, p)
            d, _ = segment_to_rect_distance(
                rx1, ry1, rx2, ry2, p.global_x, p.global_y,
                p.size_x / 2.0, p.size_y / 2.0)
            if d < hw + req(pfl, cfl):
                return False
        for ov in pcb_data.vias:
            if ov.net_id != net_id and _point_to_seg_dist(
                    ov.x, ov.y, sx, sy, ex, ey) < (ov.size or 0.5) / 2.0 + hw \
                    + req(cfl, via_fl(ov.net_id)):
                return False
        for s2 in pcb_data.segments:
            if s2.net_id == net_id or s2.layer != layer:
                continue
            if _segs_cross(sx, sy, ex, ey, s2.start_x, s2.start_y,
                           s2.end_x, s2.end_y):
                return False
            d = min(_point_to_seg_dist(sx, sy, s2.start_x, s2.start_y, s2.end_x, s2.end_y),
                    _point_to_seg_dist(ex, ey, s2.start_x, s2.start_y, s2.end_x, s2.end_y),
                    _point_to_seg_dist(s2.start_x, s2.start_y, sx, sy, ex, ey),
                    _point_to_seg_dist(s2.end_x, s2.end_y, sx, sy, ex, ey))
            if d < hw + s2.width / 2.0 + req(cfl, seg_fl(s2.net_id, s2.layer)):
                return False
        return True

    for ref in sorted(unresolved):
        cap = st.caps[ref]
        rects = cap.pad_rects(cap.x, cap.y, cap.rot)
        # #725: this predicate MUST match via_penalty's, which is why both go
        # through st.via_required. Left at the flat scalar while the grader
        # resolves the requirement, a cap reported unresolved because of a
        # raised via would yield an EMPTY offender list -- the pass would print
        # nothing and report the cap unresolved forever.
        pfls = cap_floors_of(cap, rects)
        offenders = []
        for v in pcb_data.vias:
            vr = (v.size or 0.5) / 2.0
            for i, (bx0, by0, bx1, by1, net) in enumerate(rects):
                if v.net_id != net and _point_to_rect_dist(
                        v.x, v.y, (bx0, by0, bx1, by1)) < vr + via_req(
                            None if pfls is None else pfls[i], v.net_id) - EPS:
                    offenders.append(v)
                    break
        for v in offenders:
            # Layers needing a connector back to the stub start: every layer
            # with same-net copper terminating at the old via position, plus
            # the copper layer of a same-net pad the via sits inside
            # (via-in-pad -- the pad connection must follow the via).
            conn_layers = {}
            # #313: copper terminating within the VIA BODY was electrically
            # joined through the via and needs a connector on its layer. The old
            # 1um match missed copper offset by grid quantization or a prior
            # #280 via-nudge; the via body radius is the correct connectivity
            # tolerance (a track end buried in the via is connected).
            tol = max(1e-3, v.size / 2.0)
            for s in pcb_data.segments:
                if s.net_id != v.net_id:
                    continue
                if (math.hypot(s.start_x - v.x, s.start_y - v.y) < tol
                        or math.hypot(s.end_x - v.x, s.end_y - v.y) < tol):
                    w = conn_layers.get(s.layer)
                    conn_layers[s.layer] = min(w, s.width) if w else s.width
            # via-in-pad: rotation/shape-aware containment (the axis-aligned
            # size_x/size_y bbox mis-classified rotated/oval/custom pads).
            from check_drc import point_to_pad_distance
            for p in pcb_data.pads_by_net.get(v.net_id, []):
                if point_to_pad_distance(v.x, v.y, p) > 1e-6:
                    continue  # via not inside this pad's copper
                fallback = (min(conn_layers.values()) if conn_layers
                            else defaults.TRACK_WIDTH)
                pad_cu = [l for l in (p.layers or [])
                          if l.endswith('.Cu') and not l.startswith('*')]
                if not pad_cu and any(l.endswith('.Cu') for l in (p.layers or [])):
                    # THT *.Cu pad: no concrete side layer -- the pad barrel ties
                    # every layer, so reconnect on the via's own span layers
                    # (previously this pad got NO connector at all).
                    pad_cu = [l for l in (v.layers or []) if l.endswith('.Cu')] or ['F.Cu']
                for pl in pad_cu:
                    if pl not in conn_layers:
                        conn_layers[pl] = fallback
            # zone/pour ties: a via stitching a same-net pour needs a connector
            # on that layer or the pour tie is silently dropped by the move.
            for z in (getattr(pcb_data, 'zones', []) or []):
                if z.net_id != v.net_id or not getattr(z, 'polygon', None):
                    continue
                if _point_in_poly(v.x, v.y, z.polygon) and z.layer not in conn_layers:
                    conn_layers[z.layer] = (min(conn_layers.values()) if conn_layers
                                            else defaults.TRACK_WIDTH)
            found = None
            r = 0.05
            while found is None and r <= max_shift + 1e-9:
                for k in range(16):
                    ang = k * math.pi / 8
                    nx = round(v.x + r * math.cos(ang), 4)
                    ny = round(v.y + r * math.sin(ang), 4)
                    if not valid_via_pos(v, nx, ny):
                        continue
                    if all(connector_clear(v.net_id, layer, w, v.x, v.y, nx, ny)
                           for layer, w in conn_layers.items()):
                        found = (nx, ny)
                        break
                r += 0.05
            if found is None:
                print(f"  via-nudge: no clear spot for {ref}'s offending via "
                      f"at ({v.x:.2f}, {v.y:.2f}) within {max_shift}mm")
                continue
            nx, ny = found
            old = (v.x, v.y)
            for layer, w in conn_layers.items():
                sd = {'start': (old[0], old[1]), 'end': (nx, ny),
                      'width': w, 'layer': layer, 'net_id': v.net_id}
                new_segments.append(sd)
                pcb_data.segments.append(Segment(
                    start_x=old[0], start_y=old[1], end_x=nx, end_y=ny,
                    width=w, layer=layer, net_id=v.net_id))
            v.x, v.y = nx, ny
            # w2[2]/w2[3] (net and keep-out) are carried through unchanged --
            # a moved via keeps its requirement. #725: this rebuild gives the
            # list a NEW identity, which is what makes _Repair's per-cap
            # required-clearance memos rebuild instead of going stale.
            _radii = getattr(st, '_via_radius_by_id', None)
            _rebuilt = []
            for w2 in st.vias:
                if (abs(w2[0] - old[0]) < 1e-6 and abs(w2[1] - old[1]) < 1e-6):
                    moved = (nx, ny, w2[2], w2[3])
                    # A relocated via is a NEW tuple, so it would drop out of
                    # the radius map and be graded at its keep-out slot -- the
                    # prune OVER-reach -- instead of the pair's requirement.
                    # Carry the radius across; the via did not change size.
                    if _radii is not None and id(w2) in _radii:
                        _radii[id(moved)] = (moved, _radii[id(w2)][1])
                    _rebuilt.append(moved)
                else:
                    _rebuilt.append(w2)
            st.vias = _rebuilt
            via_moves.append((old[0], old[1],
                              {'x': nx, 'y': ny, 'size': v.size, 'drill': v.drill,
                               'layers': v.layers, 'net_id': v.net_id}))
            nm = pcb_data.nets[v.net_id].name if v.net_id in pcb_data.nets else v.net_id
            print(f"  via-nudge: moved {nm} via ({old[0]:.3f},{old[1]:.3f}) -> "
                  f"({nx:.3f},{ny:.3f}) to free {ref}; {len(conn_layers)} "
                  f"connector segment(s) back to the stub start")
    return via_moves, new_segments


def _pad_on_layer(pad, layer):
    layers = getattr(pad, 'layers', None) or []
    return layer in layers or '*.Cu' in layers
