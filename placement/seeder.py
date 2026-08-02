"""Generate an initial placement from a declared floorplan intent.

The placement stack refines; it deliberately does not place from scratch
UNAIDED (placement_state.py:13, docs/placement-optimization.md) -- handed a
pile of parts it has nothing to inherit constraints from. This module is the
aided path: the intent file IS the constraint carrier (zones, edge bands,
locks, decap rules), so a board whose repo declares one can get a legal,
deterministic, seeded starting placement instead of a refusal.

What each intent construct becomes, in placement order:

  1. ``edge_connectors``   the declared edge, overhang centered in the band,
                           distributed evenly along the edge. Placed WITHOUT
                           the legality gate: overhanging the outline is the
                           point, and candidate_valid would veto it.
  2. single-ref zones      the zone center -- the "few hundred microns around
                           the spec coordinate" pattern for spec-pinned parts.
  3. multi-ref zones       members packed radially from the zone center,
                           highest pin count first, constrained to the zone
                           rect plus its declared tolerance.
  4. everything else       nearest legal pose to the centroid of its already-
                           placed partners (fanout-capped, so GND does not
                           drag everything to the board middle) -- which is
                           also what lands a decap next to its IC.

Rotations: the input rotation is tried IN FULL first and kept when it fits;
a part with no contained legal pose at it falls back to its 90-degree
lattice, and the note names the change. The intent schema cannot express a
rotation, so a part whose rotation is a DECISION (pin order, the U3 rot-180
case) must be locked -- an unlocked load-bearing rotation was never
protected from the quench either. Explore rotations deliberately with
place_portfolio's `poses` strategy.

Determinism: the only randomness is ``random.Random(f"{seed}")`` -- it breaks
ties in the packing order and jitters non-spec targets, so different seeds
give genuinely different (still legal) seeds while the same seed reproduces
byte for byte. Everything else iterates sorted (#457).
"""
from __future__ import annotations

import fnmatch
import math
import random
import re
from typing import Dict, List, Optional, Sequence, Set, Tuple

# Ring-search enumeration: nearest-first out to this radius, then a FINE ring
# near the target, then a coarse whole-board sweep. The fine pass exists
# because a packed board's remaining windows can be sub-millimeter -- measured:
# the 51x21 board's LDO had exactly one fully-legal window left, 0.09mm tall,
# which both a 1.0mm ring and a 2.0mm sweep step straight over. A part that
# finds nothing anywhere is reported UNSEATED, never silently dropped.
SEARCH_RADIUS_MM = 30.0
SEARCH_STEP_MM = 1.0
SEARCH_FINE_RADIUS_MM = 16.0
SEARCH_FINE_STEP_MM = 0.25
FALLBACK_STEP_MM = 2.0
TARGET_JITTER_MM = 1.5


def _rect_inside(rect, outer, tol: float) -> bool:
    return (rect[0] >= outer[0] - tol and rect[1] >= outer[1] - tol
            and rect[2] <= outer[2] + tol and rect[3] <= outer[3] + tol)


def _try_place(state, ref: str, tx: float, ty: float, exclude: Set[str],
               constraint=None, tol: float = 0.5) -> bool:
    """Nearest FULLY-CONTAINED legal pose to (tx, ty); applies the move and
    returns True.

    `exclude` carries the not-yet-placed refs: the pile they still form at
    their meaningless input coordinates must not veto real poses.

    candidate_valid alone is not the right gate here: for a part whose
    INCUMBENT pose is off the board (a generator's default position can be),
    its #456 branch accepts poses that move strictly TOWARD the board while
    still outside it -- measured: a free LDO seeded 2.7mm outside the
    outline, "placed", unseated 0. Placement from scratch has no incumbent
    worth improving on, so full containment is demanded explicitly; the only
    deliberate off-board poses are the edge connectors, which stage 1 places
    without this helper.

    The part's CURRENT rotation is tried in full first, then the rest of its
    90-degree lattice: an unplaced pile's rotation is a generator default,
    not a decision, and a large part can have NO contained legal pose at it
    while fitting fine turned 90 (measured: the same LDO, 0 poses at rot 0
    against 3 at rot 90 on a packed 51x21 board). A part whose rotation IS a
    decision must be locked -- an unlocked "load-bearing rotation" was never
    protected from the quench either (the U3 lesson). The caller can see a
    fallback fired by comparing the part's rot before and after.

    Returns the courtyard clearance the pose was found at, or None. The full
    clearance is demanded first; when the whole board offers nothing, the
    search reruns at half, then at a 0.02mm floor -- dense boards carry
    sub-clearance courtyard pairs BY DESIGN (the reference hand seed for the
    51x21 board places its LDO 0.04mm from a locked decap; a 0.05 floor
    still refused that board), and refusing to seed what a human
    deliberately packs would fail real boards. Courtyards carry their own
    margin, so a small courtyard-to-courtyard gap is not a copper hazard. A
    relaxed placement is a NOTE for the caller, never silent."""
    from pose_score import _offsets
    part = state.parts[ref]

    def _ok(x, y, rot):
        if state.edge_gate.rect_outside_amount(part.rect(x, y, rot)) > 1e-9:
            return False
        return state.candidate_valid(ref, x, y, rot, exclude=exclude)

    full = state.clearance
    try:
        for clr in (full, full / 2.0, min(0.02, full)):
            # candidate_valid reads state.clearance; the incumbent-violation
            # cache is keyed on it implicitly, so clear it on every change.
            state.clearance = clr
            state._inc_violation.clear()
            for rot in [part.rot] + [(part.rot + d) % 360
                                     for d in (90.0, 180.0, 270.0)]:
                for radius, step in ((SEARCH_RADIUS_MM, SEARCH_STEP_MM),
                                     (SEARCH_FINE_RADIUS_MM,
                                      SEARCH_FINE_STEP_MM)):
                    for dx, dy in _offsets(radius, step):
                        x, y = round(tx + dx, 3), round(ty + dy, 3)
                        if constraint is not None and not _rect_inside(
                                part.rect(x, y, rot), constraint, tol):
                            continue
                        if _ok(x, y, rot):
                            state.apply_move(ref, x, y, rot)
                            return clr
                if constraint is not None:
                    continue    # a zone-constrained part stays in its zone
                u = state.usable
                grid = []
                nx = max(1, int((u[2] - u[0]) / FALLBACK_STEP_MM))
                ny = max(1, int((u[3] - u[1]) / FALLBACK_STEP_MM))
                for i in range(nx + 1):
                    for j in range(ny + 1):
                        x = round(u[0] + i * FALLBACK_STEP_MM, 3)
                        y = round(u[1] + j * FALLBACK_STEP_MM, 3)
                        grid.append(((x - tx) ** 2 + (y - ty) ** 2, x, y))
                grid.sort()
                for _, x, y in grid:
                    if _ok(x, y, rot):
                        state.apply_move(ref, x, y, rot)
                        return clr
    finally:
        state.clearance = full
        state._inc_violation.clear()
    return None


def _edge_pose(part, bounds, edge: str, frac: float, overhang: float
               ) -> Tuple[float, float]:
    """Center coordinates that put the part's courtyard `overhang` mm past
    the named edge of the BOUNDING BOX, at fraction `frac` along it. A first
    guess only -- see _edge_correct for why it cannot be the answer."""
    lx0, ly0, lx1, ly1 = part.rect(0.0, 0.0, part.rot)
    x0, y0, x1, y1 = bounds
    if edge == 'north':
        return x0 + (x1 - x0) * frac, y0 - overhang - ly0
    if edge == 'south':
        return x0 + (x1 - x0) * frac, y1 + overhang - ly1
    if edge == 'west':
        return x0 - overhang - lx0, y0 + (y1 - y0) * frac
    if edge == 'east':
        return x1 + overhang - lx1, y0 + (y1 - y0) * frac
    raise ValueError(f"unknown edge {edge!r}")


def _edge_correct(state, ref: str, edge: str, x: float, y: float,
                  target: float) -> Tuple[float, float]:
    """Walk the pose along the edge normal until the MEASURED overhang hits
    `target`. The analytic pose measures against the bounding box, but the
    grade's rule_edge_connector measures rect_outside_amount against the real
    Edge.Cuts rings -- on a non-rectangular outline the two differ by the
    local inset, and a seed placed by the bbox grades over its declared band
    (measured on splitflap: 4 connectors 0.1-0.2mm past their max)."""
    part = state.parts[ref]
    for _ in range(4):
        amt = state.edge_gate.rect_outside_amount(part.rect(x, y, part.rot))
        err = target - amt
        if abs(err) < 0.02:
            break
        if edge == 'north':
            y -= err
        elif edge == 'south':
            y += err
        elif edge == 'west':
            x -= err
        else:
            x += err
    return x, y


def _partner_centroid(state, ref: str, placed: Set[str],
                      max_fanout: int = 20) -> Optional[Tuple[float, float]]:
    """Centroid of already-placed partners' pads on shared nets. Nets owned by
    more than `max_fanout` parts are excluded for the routability.py reason:
    they reach everywhere by design and would collapse every centroid onto the
    board middle. Plane nets are NOT otherwise excluded here -- for a decap,
    the rail net is exactly what tethers it to its IC."""
    part = state.parts.get(ref)
    if part is None:
        return None
    xs: List[float] = []
    ys: List[float] = []
    for nid in part.nets:
        owners = state.net_refs.get(nid, ())
        if len(owners) > max_fanout:
            continue
        for other in owners:
            if other == ref or other not in placed:
                continue
            for gx, gy, pn in state.parts[other].pad_globals():
                if pn == nid:
                    xs.append(gx)
                    ys.append(gy)
    if not xs:
        return None
    return sum(xs) / len(xs), sum(ys) / len(ys)


def seed_from_intent(pcb_data, pcb_file: str, intent, rng: random.Random, *,
                     group_sources: Sequence[str] = (),
                     clearance: float = 0.25,
                     board_edge_clearance: float = 0.55,
                     grid_step: float = 0.1,
                     seed_refs: Optional[Set[str]] = None) -> Dict:
    """Compute a full placement for an unplaced board from its intent.

    Returns {'placements': [...], 'lock_refs': [...], 'unseated': [...],
    'notes': [...]}. `placements` covers every ref that was placed (writer
    format); `unseated` names parts NO legal pose was found for -- the caller
    reports them and the grade fails, deliberately.

    `seed_refs`, when given, scopes the seeding to exactly those refs: every
    other part is treated as authoritatively placed where it stands (the
    PARTIALLY-unplaced case -- a stacked pile beside a real placement, where
    re-deriving the placed parts would discard someone's work).
    """
    import pose_score
    from placement import floorplan

    state = pose_score.make_state(
        pcb_data, pcb_file, clearance=clearance,
        board_edge_clearance=board_edge_clearance, grid_step=grid_step)
    bounds = state.board
    refs_all = sorted(pcb_data.footprints)
    notes: List[str] = []

    blocks, block_problems = floorplan.resolve_blocks(
        intent, pcb_data, group_sources)
    for v in block_problems:
        notes.append(v.message)
    zones_by_name = {z.name: z for z in intent.blocks if z.rect is not None}

    lock_refs: List[str] = sorted({
        r for pat in intent.must_lock for r in fnmatch.filter(refs_all, pat)})

    placed: Set[str] = set()
    unplaced: Set[str] = {r for r, p in state.parts.items()}
    unseated: List[str] = []
    # A part locked IN THE FILE is already authoritatively placed -- a caller
    # that pre-placed its spec-fixed parts and stamped them (locked yes) must
    # not have the seeder re-derive them. Treated as placed from the start:
    # they anchor the connectivity centroids and obstruct packing, and every
    # later stage (edge connectors included) skips them. The same applies to
    # every ref outside an explicit `seed_refs` scope.
    for ref in sorted(state.parts):
        if state.parts[ref].locked or (seed_refs is not None
                                       and ref not in seed_refs):
            placed.add(ref)
            unplaced.discard(ref)
    # Deterministic tie-break values, drawn once in sorted order so the
    # stream never depends on set iteration.
    tiebreak = {r: rng.random() for r in sorted(state.parts)}

    def _order(refs):
        return sorted((r for r in refs if r in unplaced),
                      key=lambda r: (-state.parts[r].pin_count, tiebreak[r]))

    def _jitter():
        return (rng.uniform(-TARGET_JITTER_MM, TARGET_JITTER_MM),
                rng.uniform(-TARGET_JITTER_MM, TARGET_JITTER_MM))

    # ---- 1. edge connectors: spec geometry, no legality gate ---------------
    by_edge: Dict[str, List[Dict]] = {}
    for c in intent.edge_connectors:
        if c['ref'] not in state.parts:
            notes.append(f"edge connector {c['ref']} is not on this board")
        elif c['ref'] in unplaced:
            by_edge.setdefault(c.get('edge') or 'south', []).append(c)
    for edge in sorted(by_edge):
        specs = sorted(by_edge[edge], key=lambda c: c['ref'])
        for k, c in enumerate(specs):
            ref = c['ref']
            part = state.parts[ref]
            band = c.get('overhang_mm') or {}
            lo = float(band.get('min', 0.0))
            hi = band.get('max')
            overhang = (lo + float(hi)) / 2.0 if hi is not None else max(lo, 0.5)
            frac = (k + 1) / (len(specs) + 1)
            x, y = _edge_pose(part, bounds, edge, frac, overhang)
            x, y = _edge_correct(state, ref, edge, x, y, overhang)
            state.apply_move(ref, round(x, 3), round(y, 3), part.rot)
            placed.add(ref)
            unplaced.discard(ref)

    # Decap-governed caps are claimed by stage 2.5, never zone-packed: a
    # zone is a REGION and the decap rule is a distance to a specific pin --
    # a cap packed anywhere in a 15x9 zone routinely lands >3mm from the pin
    # it exists to serve (measured: the flash decap, zone-packed, graded
    # 3.5mm from the flash's VCC pin).
    decap_spec = getattr(intent, 'decaps', None) or {}
    decap_scope: Set[str] = set()
    if decap_spec.get('max_distance_mm') is not None:
        exempt = tuple(decap_spec.get('exempt') or ())
        decap_scope = {r for r in state.parts
                       if r[0] == 'C' and state.parts[r].pin_count == 2
                       and not any(fnmatch.fnmatch(r, pat) for pat in exempt)}

    # ---- 2. zoned blocks: radial pack from the zone center -----------------
    # A single-member zone is the spec-coordinate pattern (a rect a few
    # hundred microns wide around where the spec pins the part), so it gets
    # the exact center; multi-member zones jitter each target so different
    # seeds pack differently.
    for name in sorted(zones_by_name):
        z = zones_by_name[name]
        members = [r for r in _order(blocks.get(name, ()))
                   if r not in decap_scope]
        if not members:
            continue
        cx = (z.rect[0] + z.rect[2]) / 2.0
        cy = (z.rect[1] + z.rect[3]) / 2.0
        tol = intent.zone_tolerance(z)
        for ref in members:
            jx, jy = (0.0, 0.0) if len(members) == 1 else _jitter()
            rot_before = state.parts[ref].rot
            clr = _try_place(state, ref, cx + jx, cy + jy, unplaced - {ref},
                             constraint=z.rect, tol=tol)
            if clr is not None:
                placed.add(ref)
                unplaced.discard(ref)
                if state.parts[ref].rot != rot_before:
                    notes.append(f"{ref}: rotated {rot_before:g} -> "
                                 f"{state.parts[ref].rot:g} (no contained "
                                 f"pose at the input rotation)")
                if clr < state.clearance:
                    notes.append(f"{ref}: placed at reduced courtyard "
                                 f"clearance {clr:g} (none at "
                                 f"{state.clearance:g})")
            else:
                unseated.append(ref)
                notes.append(f"{ref}: no legal pose inside zone {name!r}")

    # ---- 2.5 decap-governed caps: one cap per supply PIN -------------------
    # A 100nF's two nets are a rail and GND -- both usually above the fanout
    # cap -- so the generic centroid stage would park every decap mid-board
    # and a pin-exact decap gate (3mm pad-edge per SUPPLY PIN) would fail.
    # The iteration is PIN-FIRST, not cap-first: a cap-first greedy spends
    # every cap on the biggest IC's pads and starves the flash (measured --
    # all ten caps claimed U1 pads, U3.8 graded 3.5mm). Pins are the rail
    # pads of PLACED ICs (U-prefix: a castellated row carries the rail too
    # and must not eat a claim), pair-collapsed (adjacent same-rail pins
    # under 1mm share one cap by design), biggest owner first; each pin
    # takes a matching-rail cap, preferring one whose declared zone CONTAINS
    # the pin so a zone-member cap serves its own block.
    if decap_scope:
        avail = [r for r in _order(sorted(unplaced)) if r in decap_scope]
        rail_of: Dict[str, int] = {}
        for ref in avail:
            rail = min((nid for nid in state.parts[ref].nets
                        if len(state.net_refs.get(nid, ())) >= 2),
                       key=lambda nid: (len(state.net_refs[nid]), nid),
                       default=None)
            if rail is not None:
                rail_of[ref] = rail
        rails = set(rail_of.values())
        pins: List[Tuple[int, str, float, float, int]] = []
        for owner in sorted(placed):
            if owner[0] != 'U':
                continue
            o = state.parts[owner]
            for gx, gy, pn in o.pad_globals():
                if pn in rails:
                    pins.append((-o.pin_count, owner, round(gx, 3),
                                 round(gy, 3), pn))
        pins.sort()
        zone_of_cap = {}
        for name in sorted(zones_by_name):
            for r in blocks.get(name, ()):
                if r in decap_scope and r not in zone_of_cap:
                    zone_of_cap[r] = zones_by_name[name]

        def _seat(ref, tx, ty, owner, pn, constraint=None, tol=0.5):
            clr = _try_place(state, ref, tx, ty, unplaced - {ref},
                             constraint=constraint, tol=tol)
            if clr is None and constraint is not None:
                clr = _try_place(state, ref, tx, ty, unplaced - {ref})
            if clr is None:
                return False
            avail.remove(ref)
            placed.add(ref)
            unplaced.discard(ref)
            p2 = state.parts[ref]
            net = getattr(pcb_data.nets.get(pn), 'name', pn)
            notes.append(f"{ref}: decap for {owner} pad(s) near ({tx}, {ty})"
                         f" [{net}], landed "
                         f"{math.hypot(p2.x - tx, p2.y - ty):.2f}mm"
                         + (f" at reduced clearance {clr:g}"
                            if clr < state.clearance else ""))
            return True

        # Pass 1: a cap declared in a zone serves a pin INSIDE that zone --
        # the flash's own decap covers the flash, whatever the owner-size
        # ordering says.
        remaining = []
        for key in pins:
            _, owner, x, y, pn = key
            hit = next((r for r in avail if rail_of.get(r) == pn
                        and zone_of_cap.get(r) is not None
                        and zone_of_cap[r].rect[0] <= x <= zone_of_cap[r].rect[2]
                        and zone_of_cap[r].rect[1] <= y <= zone_of_cap[r].rect[3]),
                       None)
            if hit is not None and _seat(
                    hit, x, y, owner, pn,
                    constraint=zone_of_cap[hit].rect,
                    tol=intent.zone_tolerance(zone_of_cap[hit])):
                continue
            remaining.append(key)

        # Pass 2, per rail: CLUSTER the remaining pins until they fit the
        # remaining caps, then seat one cap per cluster centroid. Twelve
        # graded pins over ten caps is the DESIGNED shape -- adjacent supply
        # pins share a cap -- so the collapse radius grows (1.0 -> 3.0mm)
        # until every pin belongs to a served cluster; a fixed radius either
        # starves the last pin or wastes two caps on one pair (both
        # measured).
        for rail in sorted(rails):
            pins_r = [k for k in remaining if k[4] == rail]
            caps_r = [r for r in avail if rail_of.get(r) == rail]
            if not pins_r or not caps_r:
                continue
            radius = 1.0
            while True:
                clusters: List[List[Tuple]] = []
                for key in pins_r:
                    _, owner, x, y, pn = key
                    home = next((c for c in clusters
                                 if c[0][1] == owner
                                 and math.hypot(x - c[0][2], y - c[0][3])
                                 <= radius), None)
                    (home.append(key) if home is not None
                     else clusters.append([key]))
                if len(clusters) <= len(caps_r) or radius >= 3.0:
                    break
                radius += 0.5
            for cluster, ref in zip(clusters, list(caps_r)):
                cx2 = round(sum(k[2] for k in cluster) / len(cluster), 3)
                cy2 = round(sum(k[3] for k in cluster) / len(cluster), 3)
                _seat(ref, cx2, cy2, cluster[0][1], rail)
            # pins beyond the cap supply, and caps no pin wanted, fall
            # through to the generic stage, which reports honestly

    # ---- 3. the rest: connectivity centroid --------------------------------
    center = ((bounds[0] + bounds[2]) / 2.0, (bounds[1] + bounds[3]) / 2.0)
    for ref in _order(sorted(unplaced)):
        target = _partner_centroid(state, ref, placed) or center
        jx, jy = _jitter()
        rot_before = state.parts[ref].rot
        clr = _try_place(state, ref, target[0] + jx, target[1] + jy,
                         unplaced - {ref})
        if clr is not None:
            placed.add(ref)
            unplaced.discard(ref)
            if state.parts[ref].rot != rot_before:
                notes.append(f"{ref}: rotated {rot_before:g} -> "
                             f"{state.parts[ref].rot:g} (no contained pose "
                             f"at the input rotation)")
            if clr < state.clearance:
                notes.append(f"{ref}: placed at reduced courtyard clearance "
                             f"{clr:g} (none at {state.clearance:g})")
        else:
            unseated.append(ref)
            notes.append(f"{ref}: no legal pose anywhere on the board")

    placements = [{'reference': ref,
                   'new_x': state.parts[ref].x, 'new_y': state.parts[ref].y,
                   'new_rotation': state.parts[ref].rot}
                  for ref in sorted(placed)]
    return {'placements': placements, 'lock_refs': lock_refs,
            'unseated': sorted(unseated), 'notes': notes}


def stamp_locked(board_file: str, refs: Sequence[str]) -> int:
    """Insert `(locked yes)` into the named footprints, in place.

    Inserted immediately after the footprint's opening token, which is before
    the first pad -- the position placement/parser.extract_locked_refs (and
    KiCad itself) reads it from. The grade's must_lock rule demands the lock
    IN THE FILE, so writing the intent's locks here is what makes the emitted
    seed grade clean rather than merely hoped-correct."""
    from kicad_parser import find_matching_paren
    with open(board_file, 'r', encoding='utf-8') as f:
        content = f.read()
    want = set(refs)
    count = 0
    starts = [m.start() for m in re.finditer(r'\(footprint\s+"', content)]
    for start in reversed(starts):
        end = find_matching_paren(content, start)
        fp_text = content[start:end]
        m = re.search(r'\(property\s+"Reference"\s+"([^"]+)"', fp_text)
        if not m or m.group(1) not in want:
            continue
        if re.search(r'\(locked\s+yes\)', fp_text[:fp_text.find('(pad')
                                                  if '(pad' in fp_text else len(fp_text)]):
            continue
        open_m = re.match(r'\(footprint\s+"[^"]*"', fp_text)
        if not open_m:
            continue
        at = open_m.end()
        content = (content[:start + at] + '\n\t\t(locked yes)'
                   + content[start + at:])
        count += 1
    with open(board_file, 'w', encoding='utf-8') as f:
        f.write(content)
    return count
