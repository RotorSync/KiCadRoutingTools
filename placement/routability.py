"""What tells you the FLOORPLAN is wrong, rather than the routing (#549, #407).

Discussion #407 names the unsolved problem exactly: *knowing when to stop routing
and go move something*. The two scars in it are both floorplan errors that a
router can only grind against --

    a magnetics block sitting ~80mm from BOTH its own endpoints, so 8 of its
    traces cut straight through the camera-bus corridor;

    ~22 residual nets that would not route whatever knob was turned, because
    the fix was re-floorplanning a quadrant.

Both were visible from move zero to anyone looking at the right number. The
signals below are those numbers. They are deliberately NOT part of the intent
rule set: an intent violation says "this is not the floorplan you declared", a
health signal says "this floorplan will fight the router whatever you declared".

Kept in its own module, not inside `floorplan.py`, because #459's mover
selection wants the displacement metric without the intent machinery.

HONEST COMPUTABILITY -- stated because the temptation is to ship all four:

  block displacement   computable today, from geometry alone. This IS #459's
                       "connectivity-centroid displacement".
  bus crossings        computable pre-route, but the corridor is a MODEL (a
                       straight rectangle between two pad clusters), not the
                       route. Screening signal, not a verdict.
  convergence          needs a declared list of critical net classes. Placement
                       has no net-class notion, and "critical" is design intent
                       rather than a fact in the file, so it comes from the
                       intent or it does not run. No silent heuristic.
  blocked-cell share   NOT computable pre-route at all. It needs #409's blocker
                       JSON, which only exists after a routing attempt.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np


# A net owned by more parts than this reaches everywhere by design -- GND, a
# supply rail -- so it says nothing about where any one block belongs. Measured
# on ulx3s: 329 nets, MEDIAN 2 owning parts, and exactly two above 20 (GND at
# 96, +3V3 at 45). Without this cut those two dominate every block's foreign-pad
# set, the "net centroid" collapses onto the board centroid, and the metric
# degenerates into "distance from the middle of the board" -- which is not a
# floorplan signal at all. Same reasoning as the plane-net exclusion the
# optimizer already wants from `--ignore-nets`.
DISPLACEMENT_MAX_FANOUT = 20


@dataclass(frozen=True)
class BlockDisplacement:
    """One block, and how far it sits from the parts it actually connects to."""
    block: str
    members: Tuple[str, ...]
    centroid: Tuple[float, float]
    net_centroid: Tuple[float, float]
    distance_mm: float
    foreign_pads: int
    nets: int
    nets_ignored: int = 0

    def to_dict(self):
        return {'block': self.block, 'members': len(self.members),
                'centroid': [round(v, 3) for v in self.centroid],
                'net_centroid': [round(v, 3) for v in self.net_centroid],
                'distance_mm': round(self.distance_mm, 4),
                'foreign_pads': self.foreign_pads, 'nets': self.nets,
                'nets_ignored': self.nets_ignored}


def block_displacements(state, blocks: Dict[str, Sequence[str]],
                        ignore_net_ids: Optional[Sequence[int]] = None,
                        max_fanout: int = DISPLACEMENT_MAX_FANOUT
                        ) -> List[BlockDisplacement]:
    """How far each block sits from the centroid of what it connects to.

    For a block B: the centroid of B's own connected pads, against the centroid
    of every pad on B's nets that belongs to a part OUTSIDE B. A large distance
    means the block was placed away from everything it talks to -- the 80mm
    magnetics case from #407, and the one failure mode a 3mm nudge provably
    cannot fix.

    Power and ground are excluded, by `ignore_net_ids` (pass the plane-net set,
    as `--ignore-nets` does elsewhere) and by `max_fanout` as a backstop. See
    DISPLACEMENT_MAX_FANOUT: without the cut this measures distance from the
    middle of the board.

    A block left with no foreign pads is OMITTED rather than reported as 0.0.
    "Connects to nothing outside itself" and "sits exactly on its partners" are
    different facts, and a zero reads as the second.
    """
    ignored = set(ignore_net_ids or ())
    if max_fanout:
        ignored.update(nid for nid, refs in state.net_refs.items()
                       if len(refs) > max_fanout)

    out: List[BlockDisplacement] = []
    for name in sorted(blocks):
        members = [r for r in sorted(blocks[name]) if r in state.parts]
        if not members:
            continue
        member_set = set(members)

        own: List[Tuple[float, float]] = []
        nets: set = set()
        dropped = 0
        for ref in members:
            for gx, gy, nid in state.parts[ref].pad_globals():
                if nid in ignored:
                    dropped += 1
                    continue
                own.append((gx, gy))
                nets.add(nid)
        if not own:
            continue

        foreign: List[Tuple[float, float]] = []
        for nid in sorted(nets):
            for ref in state.net_refs.get(nid, ()):
                if ref in member_set or ref not in state.parts:
                    continue
                for gx, gy, pn in state.parts[ref].pad_globals():
                    if pn == nid:
                        foreign.append((gx, gy))
        if not foreign:
            continue

        cx = sum(p[0] for p in own) / len(own)
        cy = sum(p[1] for p in own) / len(own)
        fx = sum(p[0] for p in foreign) / len(foreign)
        fy = sum(p[1] for p in foreign) / len(foreign)
        out.append(BlockDisplacement(
            block=name, members=tuple(members), centroid=(cx, cy),
            net_centroid=(fx, fy), distance_mm=math.hypot(fx - cx, fy - cy),
            foreign_pads=len(foreign), nets=len(nets), nets_ignored=dropped))
    return out


@dataclass(frozen=True)
class Corridor:
    """A straight rectangular channel between a bus's two pad clusters.

    A MODEL of where the bus wants to run, not where it will. Real routes bend
    around obstacles; this is a screening signal for "something large crosses
    the path this bus obviously wants", which is the `CSI-2 x MDI-Ethernet`
    smell from #407.
    """
    name: str
    net_ids: Tuple[int, ...]
    a: Tuple[float, float]
    b: Tuple[float, float]
    width_mm: float

    @property
    def length_mm(self) -> float:
        return math.hypot(self.b[0] - self.a[0], self.b[1] - self.a[1])

    def side_edges(self) -> List[Tuple[float, float, float, float]]:
        """The two long sides, as segments.

        This is what makes the whole thing cheap: "a foreign net pierces this
        corridor" becomes a crossing count against two segments, so it reuses
        `quench._count_crossings_np` instead of introducing a polygon clipper.
        """
        dx, dy = self.b[0] - self.a[0], self.b[1] - self.a[1]
        n = math.hypot(dx, dy)
        if n < 1e-9:
            return []
        ux, uy = -dy / n, dx / n            # unit normal
        h = self.width_mm / 2.0
        return [(self.a[0] + ux * h, self.a[1] + uy * h,
                 self.b[0] + ux * h, self.b[1] + uy * h),
                (self.a[0] - ux * h, self.a[1] - uy * h,
                 self.b[0] - ux * h, self.b[1] - uy * h)]

    def to_dict(self):
        return {'name': self.name, 'nets': len(self.net_ids),
                'a': [round(v, 3) for v in self.a],
                'b': [round(v, 3) for v in self.b],
                'width_mm': self.width_mm, 'length_mm': round(self.length_mm, 3)}


def _cluster_ends(state, net_ids: Sequence[int]):
    """Both endpoint clusters of a bus, as centroids.

    Each net contributes its two extreme pads (the ones furthest apart), and the
    clusters are the centroids of the two ends. Crude on purpose: a corridor is
    a screening model.
    """
    starts, ends = [], []
    for nid in net_ids:
        pts = []
        for ref in state.net_refs.get(nid, ()):
            if ref not in state.parts:
                continue
            pts.extend((gx, gy) for gx, gy, pn in state.parts[ref].pad_globals()
                       if pn == nid)
        if len(pts) < 2:
            continue
        best, pa, pb = -1.0, None, None
        for i, p in enumerate(pts):
            for q in pts[i + 1:]:
                d = (p[0] - q[0]) ** 2 + (p[1] - q[1]) ** 2
                if d > best:
                    best, pa, pb = d, p, q
        if pa[0] > pb[0] or (pa[0] == pb[0] and pa[1] > pb[1]):
            pa, pb = pb, pa            # orient deterministically (#457)
        starts.append(pa)
        ends.append(pb)
    if not starts:
        return None, None
    return ((sum(p[0] for p in starts) / len(starts),
             sum(p[1] for p in starts) / len(starts)),
            (sum(p[0] for p in ends) / len(ends),
             sum(p[1] for p in ends) / len(ends)))


def corridors_from_intent(state, pcb_data, specs: Sequence[Dict]
                          ) -> List[Corridor]:
    """Corridors for the bus specs an intent declares.

    Each spec is {'name', 'nets': [glob...], 'width_mm'}; net globs use the same
    dialect as `route.py --nets`, via the shared matcher, so a pattern means
    here what it means there.
    """
    from net_queries import matches_net_filter
    out: List[Corridor] = []
    for spec in specs:
        pats = list(spec.get('nets') or ())
        if not pats:
            continue
        ids = tuple(sorted(
            nid for nid, n in pcb_data.nets.items()
            if nid > 0 and n.name and matches_net_filter(n.name, pats)))
        if len(ids) < 2:
            continue
        a, b = _cluster_ends(state, ids)
        if a is None:
            continue
        out.append(Corridor(name=spec.get('name') or ','.join(pats),
                            net_ids=ids, a=a, b=b,
                            width_mm=float(spec.get('width_mm', 8.0))))
    return out


def foreign_crossings(state, corridor: Corridor,
                      ignore_net_ids: Optional[Sequence[int]] = None,
                      max_fanout: int = DISPLACEMENT_MAX_FANOUT
                      ) -> Tuple[int, List[int]]:
    """How many airwires of OTHER nets cross into this corridor, and whose.

    Counted against the corridor's two long sides with the quench's own crossing
    kernel, so the geometry is the one the placement cost already uses.

    Power and ground are excluded for the same reason as in
    `block_displacements`, and it matters more here: GND on ulx3s owns 96 parts,
    so its MST sprays airwires over the whole board and crosses EVERY corridor.
    Measured on the sdram corridor, the unfiltered top three offenders were GND,
    +5V and +3V3 -- a fiction, since those rails route on a plane rather than as
    traces through the channel. Left in, the signal reports "every bus is
    crossed by power" on every board and distinguishes nothing.
    """
    from .quench import _aw_array, _count_crossings_np
    sides = corridor.side_edges()
    if not sides:
        return 0, []
    own = set(corridor.net_ids)
    skip = set(ignore_net_ids or ())
    if max_fanout:
        skip.update(nid for nid, refs in state.net_refs.items()
                    if len(refs) > max_fanout)
    wall = _aw_array([(x1, y1, x2, y2, -1.0) for x1, y1, x2, y2 in sides])

    total = 0
    guilty: Dict[int, int] = {}
    for nid, aws in sorted(state.net_airwires.items()):
        if nid in own or nid in skip or not aws:
            continue
        n, _w = _count_crossings_np(_aw_array(aws), wall)
        if n:
            guilty[nid] = n
            total += n
    return total, [nid for nid, _n in sorted(guilty.items(),
                                             key=lambda kv: (-kv[1], kv[0]))]


def corridor_convergence(state, corridors: Sequence[Corridor],
                         class_nets: Dict[str, Sequence[int]],
                         ignore_net_ids: Optional[Sequence[int]] = None,
                         max_fanout: int = DISPLACEMENT_MAX_FANOUT
                         ) -> List[Tuple[str, List[str]]]:
    """Which declared critical classes crowd into each corridor.

    `class_nets` must come from the intent. Placement has no net-class notion,
    and "critical" is a design decision rather than a fact in the board file --
    guessing it would be exactly the silent heuristic this module refuses.
    """
    out = []
    for c in corridors:
        here = []
        for cls in sorted(class_nets):
            ids = set(class_nets[cls])
            if not ids or ids <= set(c.net_ids):
                continue
            n, guilty = foreign_crossings(state, c, ignore_net_ids,
                                          max_fanout)
            if n and ids & set(guilty):
                here.append(cls)
        if here:
            out.append((c.name, here))
    return out


def health(state, pcb_data, blocks: Dict[str, Sequence[str]],
           spec: Optional[Dict] = None) -> Dict[str, object]:
    """The composite. Each signal reports, or says why it did not run."""
    spec = spec or {}
    out: Dict[str, object] = {'skipped': {}}

    disp = block_displacements(
        state, blocks, ignore_net_ids=spec.get('ignore_net_ids'),
        max_fanout=int(spec.get('max_fanout', DISPLACEMENT_MAX_FANOUT)))
    if blocks:
        limit = spec.get('block_displacement_mm')
        out['block_displacement'] = [d.to_dict() for d in
                                     sorted(disp, key=lambda d: -d.distance_mm)]
        out['block_displacement_max_mm'] = (
            round(max((d.distance_mm for d in disp), default=0.0), 4))
        if limit is not None:
            out['blocks_displaced'] = sum(1 for d in disp
                                          if d.distance_mm > float(limit))
    else:
        out['skipped']['block_displacement'] = (
            'no blocks resolved; derive them with --group-by')

    specs = spec.get('bus_corridors') or []
    if specs:
        corridors = corridors_from_intent(state, pcb_data, specs)
        ign = spec.get('ignore_net_ids')
        fan = int(spec.get('max_fanout', DISPLACEMENT_MAX_FANOUT))
        rows = []
        for c in corridors:
            n, guilty = foreign_crossings(state, c, ign, fan)
            rows.append({**c.to_dict(), 'foreign_crossings': n,
                         'worst_nets': [pcb_data.nets[i].name
                                        for i in guilty[:5]
                                        if i in pcb_data.nets]})
        out['bus_corridors'] = rows
        out['bus_foreign_crossings'] = sum(r['foreign_crossings'] for r in rows)
        classes = spec.get('classes') or {}
        if classes:
            from net_queries import matches_net_filter
            cn = {cls: [nid for nid, n in pcb_data.nets.items()
                        if nid > 0 and n.name
                        and matches_net_filter(n.name, list(pats))]
                  for cls, pats in classes.items()}
            out['convergence'] = [
                {'corridor': name, 'classes': cls}
                for name, cls in corridor_convergence(state, corridors, cn,
                                                      ign, fan)]
        else:
            out['skipped']['convergence'] = (
                'health.classes not declared; placement has no net-class '
                'notion and "critical" is design intent, not a board fact')
    else:
        out['skipped']['bus_corridors'] = 'health.bus_corridors not declared'
        out['skipped']['convergence'] = 'no corridors to converge in'

    out['skipped']['blocked_cell_share'] = (
        'needs a routing attempt: the blocker JSON (#409) only exists after '
        'one, so this cannot be computed pre-route')
    return out
