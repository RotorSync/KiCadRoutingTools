"""A camera over a placement + routing chain (#431).

The user's ask: an overview establishing shot, a zoom to the block being worked
on, a pan when the work moves elsewhere on the board, and -- explicitly -- the
next moves only play *after* the pan completes.

That last clause is the whole design, turned into a structural rule rather than
a timing hope:

    A frame either MOVES THE CAMERA or CHANGES THE BOARD. Never both.

Every transition therefore happens over a frozen board, and every camera move is
followed by a settle BEAT before any action plays. Without the beat a hard cut
from motion into action reads as though the pan never finished.

Shot planning is a PURE FUNCTION over `Action` records -- no PIL, no pcbnew, no
board -- so the entire camera is testable as data. Keep it that way: this module
must not import PIL at module scope, because `animate_fanout_clearance` (a
pygame tool) imports the easing helpers from here.

Two things this deliberately does NOT do:

* Mirror the back side. A mirrored view destroys XY registration with every
  other frame, and `route_render` is already an un-mirrored X-ray projection of
  all layers. A side change gets a labelled cross-fade instead.
* Twitch. Consecutive loop rounds nudge the same parts, so without hysteresis
  the camera vibrates for a hundred frames saying nothing.
"""
from __future__ import annotations

import math
from typing import Dict, List, NamedTuple, Optional, Sequence, Tuple

Rect = Tuple[float, float, float, float]

# --- easing (shared; animate_fanout_clearance imports these) -----------------


def smoothstep(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return t * t * (3.0 - 2.0 * t)


def lerp_rect(a: Rect, b: Rect, t: float) -> Rect:
    return tuple(a[i] + (b[i] - a[i]) * t for i in range(4))  # type: ignore


def net_color(net_id: int) -> Tuple[int, int, int]:
    """Stable, well-separated colour per net id (grey for unconnected/0).

    Byte-for-byte the implementation from `animate_fanout_clearance`, which is
    the incumbent and has a committed golden GIF (docs/fanout-cap-placement.gif)
    -- so the shared version must reproduce ITS output, not an improved one.
    """
    import colorsys
    if not net_id or net_id <= 0:
        return (130, 130, 130)
    h = ((net_id * 0.61803398875) % 1.0)
    r, g, b = colorsys.hsv_to_rgb(h, 0.65, 1.0)
    return (int(r * 255), int(g * 255), int(b * 255))


# --- rect helpers ------------------------------------------------------------

def rect_center(r: Rect) -> Tuple[float, float]:
    return ((r[0] + r[2]) / 2, (r[1] + r[3]) / 2)


def rect_size(r: Rect) -> Tuple[float, float]:
    return (r[2] - r[0], r[3] - r[1])


def expand(r: Rect, frac: float) -> Rect:
    w, h = rect_size(r)
    dx, dy = w * frac, h * frac
    return (r[0] - dx, r[1] - dy, r[2] + dx, r[3] + dy)


def union(a: Optional[Rect], b: Optional[Rect]) -> Optional[Rect]:
    if a is None:
        return b
    if b is None:
        return a
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def aspect_fit(r: Rect, aspect: float) -> Rect:
    """Grow `r` (never shrink) to exactly `aspect` = width/height.

    Letterboxing done in WORLD space. `Transform` would letterbox anyway, but
    doing it here means `views_for()` returns the rect actually framed, so the
    tests can assert what the viewer sees.
    """
    w, h = rect_size(r)
    w, h = max(w, 1e-6), max(h, 1e-6)
    cx, cy = rect_center(r)
    if w / h < aspect:
        w = h * aspect
    else:
        h = w / aspect
    return (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)


def clamp_min_size(r: Rect, min_size: float) -> Rect:
    w, h = rect_size(r)
    cx, cy = rect_center(r)
    w, h = max(w, min_size), max(h, min_size)
    return (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)


def iou(a: Rect, b: Rect) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    if inter <= 0:
        return 0.0
    aw, ah = rect_size(a)
    bw, bh = rect_size(b)
    return inter / (aw * ah + bw * bh - inter)


# --- the shot list -----------------------------------------------------------

class Action(NamedTuple):
    """Something that changes the board: a placement round, or a routing step."""
    kind: str                 # 'place' | 'route'
    label: str
    focus: Optional[Rect]     # where it happens; None = the whole board
    side: Optional[str]       # 'F' | 'B' | None
    frames: int = 8           # content frames the producer will emit


class Shot(NamedTuple):
    kind: str                 # 'establish'|'transit'|'flip'|'beat'|'action'|'outro'
    frames: int
    view: Rect                # start view (== end view for non-transit shots)
    view_to: Optional[Rect] = None
    path: Optional[List[Rect]] = None
    label: str = ''
    side: Optional[str] = None
    action: Optional[Action] = None


class CameraOpts(NamedTuple):
    aspect: float = 16 / 9
    establish: int = 12
    outro: int = 10
    beat: int = 2
    pan: int = 8
    travel: int = 14
    zoom: int = 10
    flip: int = 6
    # Hysteresis. Below this overlap (and outside the scale band) the camera
    # moves; above it, it stays put. NOT optional -- consecutive rounds nudge
    # the same parts, and without it the camera vibrates.
    hold_iou: float = 0.70
    scale_band: Tuple[float, float] = (0.7, 1.4)
    # Beyond this fraction of the board diagonal a straight pan is an
    # unreadable smear, so the camera pulls back, crosses, and dives in.
    travel_frac: float = 0.35
    min_view_frac: float = 1 / 12
    min_view_mm: float = 8.0


def _min_view(overview: Rect, o: CameraOpts) -> float:
    w, h = rect_size(overview)
    return max(o.min_view_mm, math.hypot(w, h) * o.min_view_frac)


def plan_shots(actions: Sequence[Action], overview: Rect,
               o: Optional[CameraOpts] = None) -> List[Shot]:
    """The shot list for a chain. Pure: no PIL, no board, no IO."""
    o = o or CameraOpts()
    overview = aspect_fit(overview, o.aspect)
    mv = _min_view(overview, o)
    shots: List[Shot] = [Shot('establish', o.establish, overview,
                              label='overview')]
    cur, cur_side = overview, None

    for act in actions:
        tgt = overview if act.focus is None else \
            aspect_fit(clamp_min_size(expand(act.focus, 0.15), mv), o.aspect)

        # 1. A side change is not a pan: the two views occupy the same XY. It
        #    gets its own shot, alone, BEFORE any movement, so only one thing
        #    happens at a time.
        if act.side is not None and act.side != cur_side:
            # Adopting a side for the first time is not a flip -- there is
            # nothing to flip FROM, and a transition shot there just delays the
            # opening for no information.
            if cur_side is not None:
                shots.append(Shot('flip', o.flip, cur,
                                  label=f"-> {act.side} side", side=act.side))
            cur_side = act.side

        # 2. Move, unless we are already close enough (hysteresis).
        moved = False
        if not _close_enough(cur, tgt, o):
            cw, ch = rect_size(cur)
            tw, th = rect_size(tgt)
            d = math.dist(rect_center(cur), rect_center(tgt))
            bw, bh = rect_size(overview)
            if d > o.travel_frac * math.hypot(bw, bh):
                # dolly out -> cross -> dolly in. A pulled-back waypoint keeps
                # the viewer oriented and is what makes a long move readable.
                way = aspect_fit(union(cur, tgt), o.aspect)
                way = _shrink_to(way, overview)
                # When the camera is ALREADY pulled back (the opening move out
                # of the overview), the waypoint coincides with the start and
                # the first leg is a no-op that eats half the shot's frames.
                path = [cur, tgt] if _same_rect(way, cur) else [cur, way, tgt]
                shots.append(Shot('transit', o.travel, cur, tgt,
                                  path=path, label='travel', side=cur_side))
            else:
                kind_frames = o.zoom if abs((tw * th) - (cw * ch)) > 1e-9 else o.pan
                shots.append(Shot('transit', kind_frames, cur, tgt,
                                  path=[cur, tgt], label='pan', side=cur_side))
            cur = tgt
            moved = True

        # 3. Settle. THE clause: the moves play only once the camera has
        #    arrived, and a hard cut from motion into action does not read that
        #    way to a viewer.
        if moved:
            shots.append(Shot('beat', o.beat, cur, label='', side=cur_side))

        shots.append(Shot('action', act.frames, cur, label=act.label,
                          side=cur_side, action=act))

    if cur != overview:
        shots.append(Shot('transit', o.outro, cur, overview,
                          path=[cur, overview], label='overview',
                          side=cur_side))
    return shots


def _close_enough(cur: Rect, tgt: Rect, o: CameraOpts) -> bool:
    cw, ch = rect_size(cur)
    tw, th = rect_size(tgt)
    scale = (tw * th) / max(cw * ch, 1e-9)
    return (iou(cur, tgt) >= o.hold_iou
            and o.scale_band[0] <= math.sqrt(scale) <= o.scale_band[1])


def _same_rect(a: Rect, b: Rect, tol: float = 1e-6) -> bool:
    return all(abs(a[i] - b[i]) <= tol for i in range(4))


def _shrink_to(way: Rect, overview: Rect) -> Rect:
    """A travel waypoint never pulls back further than the whole board."""
    ww, wh = rect_size(way)
    ow, oh = rect_size(overview)
    return overview if (ww >= ow and wh >= oh) else way


def views_for(shot: Shot) -> List[Rect]:
    """One view per frame. Sampled by CUMULATIVE ARC LENGTH along the path --
    a per-leg lerp stalls at the waypoint of a 2-leg travel."""
    n = max(1, shot.frames)
    if shot.kind != 'transit' or not shot.path or len(shot.path) < 2:
        return [shot.view] * n
    path = shot.path
    segs = [math.dist(rect_center(path[i]), rect_center(path[i + 1]))
            + abs(rect_size(path[i])[0] - rect_size(path[i + 1])[0])
            for i in range(len(path) - 1)]
    total = sum(segs) or 1e-9
    cum = [0.0]
    for s in segs:
        cum.append(cum[-1] + s)
    out = []
    for k in range(n):
        u = smoothstep(k / (n - 1)) if n > 1 else 1.0
        dist = u * total
        i = 0
        while i < len(segs) - 1 and dist > cum[i + 1]:
            i += 1
        local = (dist - cum[i]) / (segs[i] or 1e-9)
        out.append(lerp_rect(path[i], path[i + 1], max(0.0, min(1.0, local))))
    out[-1] = path[-1]      # land exactly on target: no jump on arrival
    return out


def apply_budget(shots: Sequence[Shot], seconds: float, fps: float
                 ) -> List[Shot]:
    """Scale camera shots to fit a runtime budget.

    Content frames (`action`) are cut LAST and only if scaling everything else
    was not enough -- a movie that races past the moves to preserve a pan is
    backwards.
    """
    if not seconds or seconds <= 0:
        return list(shots)
    budget = int(seconds * fps)
    total = sum(s.frames for s in shots)
    if total <= budget:
        return list(shots)
    floors = {'transit': 3, 'beat': 1, 'establish': 4, 'outro': 3, 'flip': 2}
    out = list(shots)
    content = sum(s.frames for s in out if s.kind == 'action')
    camera = total - content
    room = max(0, budget - content)
    k = (room / camera) if camera else 1.0
    out = [s._replace(frames=max(floors.get(s.kind, 1), int(s.frames * k)))
           if s.kind != 'action' else s for s in out]
    if sum(s.frames for s in out) > budget:
        over = sum(s.frames for s in out) - budget
        # Only now touch the content, proportionally, never below 2.
        acts = [i for i, s in enumerate(out) if s.kind == 'action']
        for i in acts:
            share = max(2, out[i].frames - max(1, over // max(1, len(acts))))
            out[i] = out[i]._replace(frames=share)
    return out


def total_frames(shots: Sequence[Shot]) -> int:
    return sum(s.frames for s in shots)


def describe(shots: Sequence[Shot], fps: float = 6.0) -> str:
    n = total_frames(shots)
    bits = [f"{len(shots)} shot(s), {n} frames, {n / max(fps, 1e-9):.1f}s"]
    for s in shots:
        bits.append(f"  {s.kind:9s} {s.frames:3d}f  {s.label}")
    return "\n".join(bits)
