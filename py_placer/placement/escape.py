"""Per-face escape-lane ledger: can the nets on this part's face get OUT?

The question this answers is the one that separates *"the router failed"* from
*"this was never routable"*. For each face of a fine-pitch part, count the lanes
SUPPLIED (how many tracks physically fit alongside that face at the board's own
clearance) against the lanes DEMANDED (how many nets have to leave through it).
A face in deficit is a **binding constraint**: net ordering only chooses WHICH
nets strand there, never how many. That distinction has cost whole runs --
multiple ordering experiments against a face whose ledger would have said it in
seconds.

Three things make this honest rather than a plausible number:

* **Supply is read from the board, never assumed.** Track width and clearance
  come from the netclass / dru floor the board actually carries, so the lane
  pitch is the one a route step would use. A hardcoded 0.2mm pitch would
  manufacture deficits on a fine board and hide them on a coarse one.
* **`blockers` names who ate the lanes.** A count says a face is short; the
  blocker list says which neighbouring part to move. That is the difference
  between a signal and an action, and it is the field to read first.
* **Interior pads count toward NO face.** A pad boxed in by its neighbours does
  not escape sideways at all -- it needs a via. Rolling it into a face's demand
  would blame the face for a fanout problem. Reported separately.

Deliberately NOT a min-cost-flow escape router (Yan & Wong, DAC 2009). That is
the right model eventually, but it needs a grid, a solver and a dependency, and
there is nothing in-repo to reuse -- `bga_fanout/escape.py` is ball-lattice
specific and `qfn_fanout/` has no escape model at all. It also has no baseline
to validate against. This ledger becomes that baseline.

numpy only; no networkx in the placement stack.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

FACES = ('north', 'east', 'south', 'west')

# A pad is "interior" when it is not on the part's own pad bounding box, i.e.
# it has neighbours on every side and cannot leave sideways at any pitch.
INTERIOR_EPS = 0.001

# Only parts this fine are worth a ledger. Above it, escape is not the binding
# constraint and the ledger reports noise. Same thresholds as the fanout
# decision in the routing skill, lifted here so there is ONE definition.
FINE_PITCH_MM = 0.6
FINE_PITCH_LARGE_MM = 0.8
LARGE_PAD_COUNT = 40
MIN_PADS = 6

_ARRAY_KEYS = ('BGA', 'PGA', 'LGA', 'CSP', 'WLCSP', 'WLP', 'CGA',
               'QFN', 'DFN', 'QFP')


@dataclass(frozen=True)
class FaceLedger:
    """One face of one part: lanes in, lanes out, and who is in the way."""
    ref: str
    face: str
    span_mm: float            # the face's own length
    lane_pitch_mm: float      # track + clearance, from the board's floor
    supply: int               # lanes that physically fit, after obstructions
    demand: int               # nets that must leave through this face
    blocked_mm: float         # span lost to neighbours / outline
    blockers: Tuple[str, ...]  # WHO ate it -- the actionable field
    nets: Tuple[int, ...]

    @property
    def deficit(self) -> int:
        """Lanes short. Positive means the face cannot pass its own nets."""
        return max(0, self.demand - self.supply)

    def to_dict(self):
        return {'ref': self.ref, 'face': self.face,
                'span_mm': round(self.span_mm, 3),
                'lane_pitch_mm': round(self.lane_pitch_mm, 4),
                'supply': self.supply, 'demand': self.demand,
                'deficit': self.deficit,
                'blocked_mm': round(self.blocked_mm, 3),
                'blockers': list(self.blockers), 'nets': len(self.nets)}


@dataclass(frozen=True)
class PartEscape:
    """Every face of one part, plus the pads no face can serve."""
    ref: str
    pitch_mm: float
    faces: Tuple[FaceLedger, ...]
    interior_pads: int        # need a via, not a lane -- a FANOUT signal
    interior_nets: Tuple[int, ...]

    @property
    def worst(self) -> Optional[FaceLedger]:
        live = [f for f in self.faces if f.deficit > 0]
        return max(live, key=lambda f: (f.deficit, f.face)) if live else None

    def to_dict(self):
        return {'ref': self.ref, 'pitch_mm': round(self.pitch_mm, 4),
                'faces': [f.to_dict() for f in self.faces],
                'interior_pads': self.interior_pads,
                'interior_nets': len(self.interior_nets),
                'worst_face': self.worst.face if self.worst else None,
                'worst_deficit': self.worst.deficit if self.worst else 0}


def _min_step(vals: Sequence[float]) -> float:
    return min((b - a for a, b in zip(vals, vals[1:])), default=float('inf'))


def pad_pitch(fp) -> float:
    """The part's own minimum pad-to-pad spacing, in mm.

    Read off the pad lattice rather than parsed from the footprint NAME: a
    house library's `MY_LIB:U_TINY` carries no pitch in its name, and a name
    that does carry one can disagree with the geometry.
    """
    if len(fp.pads) < 2:
        return float('inf')
    xs = sorted({round(p.local_x, 3) for p in fp.pads})
    ys = sorted({round(p.local_y, 3) for p in fp.pads})
    return min(_min_step(xs), _min_step(ys))


def has_interior_pads(fp) -> bool:
    xs = sorted({round(p.local_x, 3) for p in fp.pads})
    ys = sorted({round(p.local_y, 3) for p in fp.pads})
    if len(xs) < 3 or len(ys) < 3:
        return False
    minx, maxx, miny, maxy = xs[0], xs[-1], ys[0], ys[-1]
    return any(minx < round(p.local_x, 3) < maxx
               and miny < round(p.local_y, 3) < maxy for p in fp.pads)


def fine_pitch_parts(pcb_data, min_pads: int = MIN_PADS) -> List[str]:
    """Refs worth a lane ledger, by PITCH and geometry -- not by pin count.

    The same test the routing skill spells out inline for the fanout decision,
    lifted here so it has one definition rather than a copy in prose. Raw pad
    count is not a signal on its own: a 44-pin THT socket, a 2x20 header and a
    1.27mm connector all clear 40 pads and none of them has an escape problem.

    Through-hole parts are excluded (except PGAs): a THT pin is reachable on
    EVERY copper layer, so there is no escape to be short of.

    NOTE this is deliberately WIDER than the fanout test the routing skill
    applies, and the difference is the point. Fanout asks "are pads boxed in
    such that they need a via", which is why it requires interior pads or a
    large pin count. Escape asks "do the pads on this face fit through the
    channel beside it", and a fine-pitch PERIMETER part has that problem with
    no interior pad at all -- a 0.4mm QFN-64 is the canonical case. The skill's
    inline test catches that one through the footprint NAME, which a house
    library will not carry, so here it is caught by pitch.
    """
    out: List[str] = []
    for ref in sorted(pcb_data.footprints):
        fp = pcb_data.footprints[ref]
        if len(fp.pads) < min_pads:
            continue
        name = (fp.footprint_name or '').upper()
        smd = sum(1 for p in fp.pads if p.drill == 0)
        tht = sum(1 for p in fp.pads if p.drill > 0)
        if tht > smd and 'PGA' not in name:
            continue
        pitch = pad_pitch(fp)
        named = any(k in name for k in _ARRAY_KEYS)
        fine = (pitch <= FINE_PITCH_MM
                or (len(fp.pads) > LARGE_PAD_COUNT
                    and pitch <= FINE_PITCH_LARGE_MM))
        # A named array package still has to BE fine-pitch to have a lane
        # problem: a 1.27mm BGA escapes without help.
        if fine or (named and pitch <= FINE_PITCH_LARGE_MM):
            out.append(ref)
    return out


def lane_pitch(pcb_data, pcb_file: Optional[str] = None,
               track_width: Optional[float] = None,
               clearance: Optional[float] = None) -> float:
    """One lane = one track plus one clearance, at the board's OWN floor.

    Resolved from the board rather than defaulted, because the number this
    feeds is a hard verdict: at 0.2mm a face passes and at 0.35mm the same
    face is in deficit, and picking the wrong one manufactures or hides a
    structural finding. Explicit arguments win, then the board's Default
    netclass, then the repo defaults.
    """
    import routing_defaults as defaults
    path = pcb_file or getattr(pcb_data, 'source_path', None)
    if path and (track_width is None or clearance is None):
        try:
            import list_nets
            dr = list_nets.read_design_rules(path)
            if clearance is None:
                clearance = list_nets.board_default_netclass_param(
                    path, 'clearance', dr)
            if track_width is None:
                track_width = list_nets.board_default_netclass_param(
                    path, 'track_width', dr)
        except Exception:                       # noqa: BLE001 - best effort
            pass
    if not clearance:
        clearance = defaults.CLEARANCE
    if not track_width:
        track_width = getattr(defaults, 'TRACK_WIDTH', 0.25)
    return float(track_width) + float(clearance)


def _part_rect(fp) -> Tuple[float, float, float, float]:
    """The part's pad bounding box in board coordinates."""
    xs = [p.global_x for p in fp.pads]
    ys = [p.global_y for p in fp.pads]
    return min(xs), min(ys), max(xs), max(ys)


def _face_of(pad, rect, pitch) -> Optional[str]:
    """Which face a pad escapes through, or None when it is interior.

    A pad on the bounding box escapes through the side it sits on; a corner pad
    is assigned to the side it is closest to, deterministically. `pitch/2` is
    the tolerance, so a pad row that is not perfectly collinear still counts.
    """
    minx, miny, maxx, maxy = rect
    tol = max(pitch / 2.0, INTERIOR_EPS)
    d = {'west': pad.global_x - minx, 'east': maxx - pad.global_x,
         'north': pad.global_y - miny, 'south': maxy - pad.global_y}
    near = min(d.values())
    if near > tol:
        return None
    # Ties resolve by FACES order, not by dict order, so the answer does not
    # depend on how the pads were enumerated.
    for f in FACES:
        if abs(d[f] - near) < 1e-9:
            return f
    return None


def _face_geometry(rect, face) -> Tuple[float, float, float, float]:
    """The face as a segment (x1, y1, x2, y2), in board coordinates."""
    minx, miny, maxx, maxy = rect
    return {'north': (minx, miny, maxx, miny),
            'south': (minx, maxy, maxx, maxy),
            'west': (minx, miny, minx, maxy),
            'east': (maxx, miny, maxx, maxy)}[face]


def _blocked_span(pcb_data, ref, rect, face, reach, courtyards=None):
    """How much of a face's span is unusable, and WHO took it.

    A neighbour parked off a face does not merely crowd it -- its own body
    occupies the channel the escaping tracks need. The overlap of the
    neighbour's extent with the face's span, projected onto the face, is what
    those tracks cannot use.

    `reach` is how far off the face to look: past that, a track has room to
    turn and the neighbour is no longer on the escape path.
    """
    minx, miny, maxx, maxy = rect
    horizontal = face in ('north', 'south')
    lo, hi = (minx, maxx) if horizontal else (miny, maxy)
    if face == 'north':
        band = (miny - reach, miny)
    elif face == 'south':
        band = (maxy, maxy + reach)
    elif face == 'west':
        band = (minx - reach, minx)
    else:
        band = (maxx, maxx + reach)

    intervals: List[Tuple[float, float, str]] = []
    for other in sorted(pcb_data.footprints):
        if other == ref:
            continue
        ofp = pcb_data.footprints[other]
        if not ofp.pads:
            continue
        oxmin, oymin, oxmax, oymax = _part_rect(ofp)
        across = (oymin, oymax) if horizontal else (oxmin, oxmax)
        if across[1] < band[0] or across[0] > band[1]:
            continue                              # not in the escape band
        along = (oxmin, oxmax) if horizontal else (oymin, oymax)
        a, b = max(lo, along[0]), min(hi, along[1])
        if b > a:
            intervals.append((a, b, other))

    # Union the intervals so two overlapping neighbours are not double-charged.
    intervals.sort()
    blocked = 0.0
    who: List[str] = []
    cur_a = cur_b = None
    for a, b, name in intervals:
        who.append(name)
        if cur_a is None:
            cur_a, cur_b = a, b
        elif a <= cur_b:
            cur_b = max(cur_b, b)
        else:
            blocked += cur_b - cur_a
            cur_a, cur_b = a, b
    if cur_a is not None:
        blocked += cur_b - cur_a
    # Deduped and ordered by how much each took, so the first name is the one
    # to move.
    by_ref: Dict[str, float] = {}
    for a, b, name in intervals:
        by_ref[name] = by_ref.get(name, 0.0) + (b - a)
    order = sorted(by_ref, key=lambda r: (-by_ref[r], r))
    return min(blocked, hi - lo), tuple(order)


def part_escape(pcb_data, ref, *, pitch_mm: Optional[float] = None,
                ignore_net_ids: Optional[Sequence[int]] = None,
                reach_mm: Optional[float] = None) -> PartEscape:
    """The full per-face ledger for one part.

    `ignore_net_ids` drops plane-routed rails, exactly as elsewhere in this
    module: a GND pad does not need a lane, it needs a via to the plane, and
    counting it as demand reports a deficit on every power-heavy face of every
    board.
    """
    fp = pcb_data.footprints[ref]
    ignored = set(ignore_net_ids or ())
    lane = pitch_mm if pitch_mm is not None else lane_pitch(pcb_data)
    rect = _part_rect(fp)
    pitch = pad_pitch(fp)
    reach = reach_mm if reach_mm is not None else max(lane * 4.0, 1.0)

    demand: Dict[str, List[int]] = {f: [] for f in FACES}
    interior: List[int] = []
    for pad in fp.pads:
        nid = getattr(pad, 'net_id', 0)
        if not nid or nid in ignored:
            continue
        face = _face_of(pad, rect, pitch if pitch != float('inf') else lane)
        if face is None:
            interior.append(nid)
        else:
            demand[face].append(nid)

    faces: List[FaceLedger] = []
    for f in FACES:
        x1, y1, x2, y2 = _face_geometry(rect, f)
        span = math.hypot(x2 - x1, y2 - y1)
        blocked, blockers = _blocked_span(pcb_data, ref, rect, f, reach)
        usable = max(0.0, span - blocked)
        nets = tuple(sorted(set(demand[f])))
        faces.append(FaceLedger(
            ref=ref, face=f, span_mm=span, lane_pitch_mm=lane,
            supply=int(usable // lane) if lane > 0 else 0,
            demand=len(nets), blocked_mm=blocked, blockers=blockers,
            nets=nets))
    return PartEscape(ref=ref, pitch_mm=pitch, faces=tuple(faces),
                      interior_pads=len(interior),
                      interior_nets=tuple(sorted(set(interior))))


def escape_ledger(pcb_data, *, refs: Optional[Sequence[str]] = None,
                  pcb_file: Optional[str] = None,
                  track_width: Optional[float] = None,
                  clearance: Optional[float] = None,
                  ignore_net_ids: Optional[Sequence[int]] = None
                  ) -> List[PartEscape]:
    """Ledgers for every fine-pitch part, worst deficit first.

    Returns [] on a board with no fine-pitch parts, which is the common case
    and is not a failure -- there is simply no escape question to ask.
    """
    lane = lane_pitch(pcb_data, pcb_file, track_width, clearance)
    targets = list(refs) if refs is not None else fine_pitch_parts(pcb_data)
    out = [part_escape(pcb_data, r, pitch_mm=lane,
                       ignore_net_ids=ignore_net_ids)
           for r in targets if r in pcb_data.footprints]
    out.sort(key=lambda p: (-(p.worst.deficit if p.worst else 0), p.ref))
    return out


def format_text(ledgers: Sequence[PartEscape], limit: int = 5) -> str:
    """The ledger as the table a human reads before blaming the router."""
    if not ledgers:
        return "escape lanes: no fine-pitch parts on this board"
    lines = []
    short = [p for p in ledgers if p.worst]
    lines.append(f"escape lanes ({len(ledgers)} fine-pitch part(s), "
                 f"{len(short)} with a face in deficit):")
    for p in ledgers[:limit]:
        flag = (f"  DEFICIT {p.worst.deficit} lane(s) on {p.worst.face}"
                if p.worst else "  ok")
        lines.append(f"  {p.ref} (pitch {p.pitch_mm:.2f}mm){flag}")
        for f in p.faces:
            if f.demand == 0 and f.supply == 0:
                continue
            mark = '!!' if f.deficit else '  '
            who = (f"; blocked {f.blocked_mm:.1f}mm by "
                   f"{', '.join(f.blockers[:3])}" if f.blockers else "")
            lines.append(f"   {mark} {f.face:<6} supply {f.supply:>3} / "
                         f"demand {f.demand:<3} "
                         f"(span {f.span_mm:.1f}mm @ {f.lane_pitch_mm:.3f}mm"
                         f"{who})")
        if p.interior_pads:
            lines.append(f"      {p.interior_pads} interior pad(s) escape "
                         f"through NO face -- that is a fanout question, not a "
                         f"lane one")
    if len(ledgers) > limit:
        lines.append(f"  ... {len(ledgers) - limit} more")
    return '\n'.join(lines)
