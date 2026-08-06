"""Structure-level placement reconstruction -- the "puzzle" solver.

For a board whose placement is WRONG (not merely rough): mechanically
determined parts first, then a rigid-displacement hypothesis, then an exact
simultaneous assignment. The pipeline (place_reconstruct.py drives it):

  classify          tiers: locked/statics, zero-net (frozen frame pieces),
                    anchors (large extents), smalls. The user's puzzle order.
  fit_pattern       propose-only: corner-inset fit on zero-net drilled parts
                    (mounting holes). Emits proposed positions, never applies.
                    (Bbox symmetry-transform slates: deferred to v2.)
  rigid_vector      offsets between current and proposed poses, agreeing up
                    to SIGN (a swap's signature), become the +/-v candidate
                    vectors -- reuse of run-2's R4, productized.
  assign            ONE simultaneous solve over each part's small candidate
                    set {stay, +v, -v, proposed slots}: an Assignment Problem
                    with Conflicts as a small ILP (scipy.optimize.milp /
                    HiGHS, in scipy >= 1.9). Colliding candidate pairs are
                    exclusion rows, so the squatter deadlock (a big part
                    evacuated because its home slot is occupied by parts that
                    would only move later) is structurally impossible: the
                    solver trades the squatters' small moves against the big
                    part's placement in one shot. Falls back to a
                    breakout-weighted coordinate descent (Morris, AAAI 1993)
                    without scipy.milp.
  legalize_residue  violation-driven minimal-move sweep (seeder.repair_
                    placement), escalating displacement caps.

Every stage is gated: it is APPLIED only if the lexicographic legality tuple
(pad conflict pairs, hole shortfall, banded pad-oob, stack pairs, hpwl,
courtyard overlap area -- see measure() for the run-4/run-6 ordering
rationale) does not worsen -- the run-2 lesson that a bare conflict-count
gate is gameable by pushing parts off the board, extended in run 6 with the
assembly (stacked-parts) conjunct hpwl-gaming cannot see.
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence, Set, Tuple

from placement import legality

GRID_TOL = 0.05          # pattern-fit residual tolerance (one routing grid)
MOVE_PENALTY_MM = 0.75   # ILP objective: flat cost per moved part (mm-equiv)
PATTERN_BONUS_MM = 2.0   # pattern-proposed slots are EVIDENCE: taking one is
                         # rewarded, not charged (a conflict-free displaced
                         # mounting hole has no other reason to go home)
DIST_TIEBREAK_PER_MM = 0.02
# ^ run-4 F1: nearest-slot tiebreak, scale-free (per mm of displacement).
# Zero-net pattern parts have NO net-anchor cost -- _net_anchor_cost's loop
# body never runs -- so two mounting holes and two free corners were exactly
# cost-degenerate and HiGHS picked arbitrarily: run 3 shipped H1/H3 at each
# other's corners, ~40 mm from home each, mechanically equivalent and
# recovery-visible (worth ~0.16 of recovery on that board). Nearest-slot is
# the minimal-perturbation choice; the weight stays far below
# PATTERN_BONUS_MM and MOVE_PENALTY_MM at real move scales (0.32 mm-equiv
# for a 15.8 mm move), so it can only break ties, never fight the evidence.
# This deliberately REVERSES placement/README.md's earlier position that
# hole-slot degeneracy is acceptable: equivalent to the board is not
# equivalent to recovery.


# --------------------------------------------------------------------------
# measurement -- the stage gate
# --------------------------------------------------------------------------

def _bbox_outside(ext, b) -> float:
    """Per-axis overshoot of the raw board bbox at ZERO margin. Exact on a
    rectangular outline; a consistent lower bound on a notched one -- the
    gate tuple only needs stage-to-stage comparability."""
    return (max(0.0, b[0] - ext[0]) + max(0.0, ext[2] - b[2])
            + max(0.0, b[1] - ext[1]) + max(0.0, ext[3] - b[3]))


def pad_oob_amount(state, edge_bands=None) -> float:
    """Summed pad/hole-extent off-board amount over all parts (zero margin).

    Run-4 F2: a ref declared in ``edge_bands`` ({ref: band_max_mm}, from the
    intent's edge_connectors) charges only the EXCESS past its band -- its
    declared overhang is by design, and without the allowance the gate tuple
    itself would revert a correct edge-homecoming (the move RAISES board-wide
    oob exactly by the legitimate overhang).

    Run-7 finding: this used to measure against the board's BOUNDING RECTANGLE
    while every other instrument used the real Edge.Cuts outline. On a
    non-rectangular board the two disagree completely -- a candidate that swept
    nine parts into a notch measured `oob 0.0` here and 14.29mm everywhere
    else, so the gate accepted an evacuation it was built to reject. The
    legality context already owns the outline-aware measurement (rings,
    cutouts, notches); use it, and keep the bbox form only as the fallback for
    a state that has no context.
    """
    oob = 0.0
    if state.legality_ctx is None:
        return oob
    bands = edge_bands or {}
    ctx = state.legality_ctx
    outline_aware = getattr(ctx, 'gate', None) is not None
    for ref, pp in ctx.parts.items():
        p = state.parts.get(ref)
        if p is None:
            continue
        if outline_aware:
            amt = ctx.pad_oob_amount(ref, p.x, p.y, p.rot, exact=True)
        else:
            ext = pp.extent(p.x, p.y, p.rot)
            if ext is None:
                continue
            amt = _bbox_outside(ext, state.board)
        oob += max(0.0, amt - bands.get(ref, 0.0))
    return oob


def measure(state, edge_bands=None) -> Tuple:
    """The lexicographic gate tuple. Smaller-or-equal is acceptable.

    Order (run-6): pad conflicts, hole shortfall, banded pad-oob, then
    STACK PAIRS (any-net cross-footprint pad intersections -- the assembly
    channel), then hpwl, then courtyard overlap as the LAST tiebreak.

    Two measured lessons live in this order. Run 4 demoted the AGGREGATE
    overlap_area below hpwl (it had vetoed a 44 mm hpwl homecoming over
    +0.73 mm^2 of courtyard kiss; run 2 measured it POSITIVELY correlated
    with distance-to-truth, r = +0.72 -- courtyards carry their own margin
    and human boards have a nonzero floor). Run 5 then shipped two 0402s
    STACKED because nothing above hpwl could see them: the stack-pair COUNT
    is the non-gameable per-pair channel (corpus-calibrated ZERO on all 33
    healthy boards, exact and AABB currencies), so it sits ABOVE hpwl where
    the aggregate never can."""
    m = state.pad_legality_metrics() if state.legality_ctx is not None else {}
    leg = state.legality_metrics()
    return (m.get('pad_conflict_pairs', 0),
            round(m.get('hole_shortfall', 0.0), 4),
            round(pad_oob_amount(state, edge_bands), 4),
            m.get('pad_intersection_pairs', 0),
            round(leg.get('hpwl', 0.0), 3),
            round(leg.get('overlap_area', 0.0), 4))


# --------------------------------------------------------------------------
# classify
# --------------------------------------------------------------------------

class Tiers:
    __slots__ = ('locked', 'zero_net', 'anchors', 'smalls', 'threshold',
                 'edge')

    def as_dict(self):
        return {'locked': sorted(self.locked),
                'zero_net': sorted(self.zero_net),
                'anchors': sorted(self.anchors),
                'smalls': sorted(self.smalls),
                'edge': sorted(self.edge),
                'anchor_extent_mm': self.threshold}


def part_extent_mm(state, ref: str) -> float:
    """Max pad-extent dimension of a part at its current rotation (mm)."""
    if state.legality_ctx is not None:
        pp = state.legality_ctx.parts.get(ref)
        if pp is not None:
            e = pp.extent_local(state.parts[ref].rot)
            if e is not None:
                return max(e[2] - e[0], e[3] - e[1])
    r = state.parts[ref].rect()
    return max(r[2] - r[0], r[3] - r[1])


def classify(state, intent=None, anchor_extent='auto') -> Tiers:
    """The puzzle tiers. Frame first: locked + zero-net (mechanical) parts;
    anchors = large pad extents (edge-connector intent refs always anchors);
    everything else is a small."""
    t = Tiers()
    t.locked = {r for r, p in state.parts.items() if p.locked}
    # A ref the intent declares must_lock belongs in the frame too. This used
    # to read the file's stamps only, so the three placement tools disagreed
    # about the same word: the grader demanded the stamp, the seeder treated it
    # as permission to move, and reconstruct did not look. A hand-written
    # must_lock is a statement that the ref's pose is a decision, which is
    # exactly what the frame tier means.
    if intent is not None and getattr(intent, 'must_lock', None):
        import fnmatch as _fn
        refs_all = sorted(state.parts)
        for pat in intent.must_lock:
            t.locked |= set(_fn.filter(refs_all, pat))
    t.zero_net = {r for r, p in state.parts.items()
                  if p.pin_count == 0 and r not in t.locked}
    free = [r for r in state.parts if r not in t.locked | t.zero_net]
    exts = sorted(part_extent_mm(state, r) for r in free)
    if anchor_extent == 'auto':
        p75 = exts[int(0.75 * (len(exts) - 1))] if exts else 3.5
        thr = max(3.5, p75)
    else:
        thr = float(anchor_extent)
    t.threshold = round(thr, 3)
    edge_refs = ({c['ref'] for c in intent.edge_connectors}
                 if intent is not None else set())
    t.edge = edge_refs & set(state.parts)   # run-4 F2: kept, not discarded
    t.anchors = {r for r in free
                 if part_extent_mm(state, r) >= thr or r in edge_refs}
    t.smalls = {r for r in free if r not in t.anchors}
    return t


# --------------------------------------------------------------------------
# fit_pattern (propose-only)
# --------------------------------------------------------------------------

def fit_corner_insets(state, tiers: Tiers) -> Dict[str, List[Tuple[float, float]]]:
    """Corner-inset fit over zero-net DRILLED parts (mounting holes).

    Survivors: holes whose (inset_x, inset_y) agree with a common inset within
    GRID_TOL, in DISTINCT corners. >= 2 survivors over-determine a
    translation; non-conforming holes get every FREE corner at the fitted
    inset as proposed positions. Propose-only: the assign stage (or its gate)
    decides."""
    b = state.board
    corners = {'SW': (b[0], b[1]), 'NW': (b[0], b[3]),
               'SE': (b[2], b[1]), 'NE': (b[2], b[3])}
    holes = []
    for ref in sorted(tiers.zero_net | (tiers.locked & set(state.parts))):
        p = state.parts[ref]
        if not p.has_tht:
            continue
        best = min(corners.items(),
                   key=lambda kv: abs(p.x - kv[1][0]) + abs(p.y - kv[1][1]))
        holes.append((ref, best[0], abs(p.x - best[1][0]),
                      abs(p.y - best[1][1])))
    from collections import defaultdict
    groups = defaultdict(list)
    for ref, corner, ix, iy in holes:
        if abs(ix - iy) <= 2 * GRID_TOL:
            groups[round((ix + iy) / 2 / GRID_TOL)].append(
                (ref, corner, (ix + iy) / 2))
    proposals: Dict[str, List[Tuple[float, float]]] = {}
    for _key, members in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        distinct = {m[1] for m in members}
        if len(members) < 2 or len(distinct) != len(members):
            continue
        inset = sum(m[2] for m in members) / len(members)
        survivors = {m[0] for m in members}
        free_corners = [c for c in corners if c not in distinct]
        for ref, _c, _ix, _iy in holes:
            if ref in survivors or ref in tiers.locked:
                continue
            cand = []
            for c in free_corners:
                cx, cy = corners[c]
                px = cx + inset if cx == b[0] else cx - inset
                py = cy + inset if cy == b[1] else cy - inset
                cand.append((round(px, 4), round(py, 4)))
            if cand:
                proposals[ref] = cand
        break
    return proposals


# --------------------------------------------------------------------------
# rigid_vector
# --------------------------------------------------------------------------

def rigid_vectors(state, proposals: Dict[str, List[Tuple[float, float]]],
                  grid_tol: float = GRID_TOL) -> List[Tuple[float, float]]:
    """Candidate group vectors from the pattern proposals: current - proposed,
    deduped up to SIGN (a swap displaces two groups by +v and -v).

    Run-4 F3(a): a vector is kept only with support from >= 2 DISTINCT refs
    -- R4's own letter ("two or more agree, up to sign"), which the code did
    not implement: every proposal x free-corner pairing minted a vector, and
    run 3's spurious cross-corner vector mis-moved J7 to 31.6 mm from home,
    WORSE than its 15.8 mm input.

    Run-5: the symmetric survivor of that rule is gone too. A symmetric
    cross-pairing (two holes x two corners) self-supports with 2 refs: the
    same refs mapped onto the same slots CROSSED mint a second, spurious
    vector. Competing pairings of the SAME ref-set onto the SAME slot-set
    are one assignment question, and only the minimal-mean-displacement
    pairing is evidence (the F1 nearest-slot principle applied at the
    vector layer); the crossed pairing ships each part ~2x farther. The
    post-assign prune sweep (`prune_assignment`) remains the backstop."""
    support: Dict[Tuple[float, float], Set[str]] = {}
    pairs: Dict[Tuple[float, float],
                List[Tuple[str, Tuple[float, float], float]]] = {}
    order: List[Tuple[float, float]] = []
    for ref, cands in sorted(proposals.items()):
        p = state.parts[ref]
        for (px, py) in cands:
            v = (p.x - px, p.y - py)
            d = math.hypot(*v)
            if d < grid_tol:
                continue
            slot = (round(px, 4), round(py, 4))
            canon = v if (v[1], v[0]) > (0, 0) else (-v[0], -v[1])
            for w in order:
                if math.hypot(canon[0] - w[0], canon[1] - w[1]) <= 2 * grid_tol:
                    support[w].add(ref)
                    pairs[w].append((ref, slot, d))
                    break
            else:
                key = (round(canon[0], 4), round(canon[1], 4))
                order.append(key)
                support[key] = {ref}
                pairs[key] = [(ref, slot, d)]
    kept = [v for v in order if len(support[v]) >= 2]
    groups: Dict[Tuple[frozenset, frozenset], List[Tuple[float, float]]] = {}
    for v in kept:
        gk = (frozenset(r for r, _s, _d in pairs[v]),
              frozenset(s for _r, s, _d in pairs[v]))
        groups.setdefault(gk, []).append(v)
    out = []
    for v in kept:
        gk = (frozenset(r for r, _s, _d in pairs[v]),
              frozenset(s for _r, s, _d in pairs[v]))
        grp = groups[gk]
        if len(grp) > 1:
            best = min(grp, key=lambda w: (
                sum(d for _r, _s, d in pairs[w]) / len(pairs[w]), w))
            if v != best:
                continue
        out.append(v)
    return out


EDGE_PREF_WEIGHT = 10.0
# ^ run-4 F2: mm-equiv charged per mm an edge-class part's pose sits from an
# edge, INSIDE the joint solve. Offering a displaced receptacle its band
# candidate is not enough (measured: J1's candidate was offered and never
# taken -- the net-anchor proxy pulls toward its partners, themselves
# misplaced), and seating it AFTER the solve fails too (measured: the seat
# collided with squatters the joint solve would have co-moved: 5 pad pairs,
# overlap +70). R1's letter -- an edge part's position is not a netlist
# question -- so for declared edge-less receptacles the edge metric REPLACES
# the net-anchor cost in the objective, and the exclusion rows resolve the
# squatters in the same solve. Generic: the weight scales a geometric metric,
# no board constants.


def edge_metric(state, ref: str, x: float, y: float,
                band: float) -> Optional[float]:
    """How far a pose is from being edge-seated, courtyard channel: 0 for an
    in-band overhang, else the courtyard edge clearance; None = past the
    band (illegal as a seat)."""
    p = state.parts[ref]
    rect = p.rect(x, y, p.rot)
    over = state.edge_gate.rect_outside_amount(rect)
    if over > 1e-6:
        return 0.0 if over <= band + 1e-6 else None
    return state.edge_gate.edge_clearance(rect)


def prune_assignment(state, old: Dict[str, Tuple[float, float, float]],
                     notes: Optional[List[str]] = None,
                     edge_bands: Optional[Dict[str, float]] = None,
                     exempt: Optional[Set[str]] = None) -> List[str]:
    """Per-part revert sweep after an ACCEPTED assignment (run-4 F3b).

    The stage gate is one board-wide lexicographic tuple, so an assignment
    that repairs the frame can smuggle individual mis-moves past it -- run
    3's spurious vector carried J7 to 31.6 mm from home (worse than its
    input) inside a hugely-improving move set, and the global gate cannot
    see per-part worsening. Walk the moved parts by DESCENDING displacement,
    tentatively restore each input pose, and keep the revert iff the gate
    tuple STRICTLY improves. Board-only, monotone by construction: the
    sweep can only improve the tuple, and a revert that would reintroduce a
    conflict is rejected by the same tuple.
    """
    pruned: List[str] = []

    def moved_dist(item):
        ref, (x, y, _r) = item
        p = state.parts[ref]
        return math.hypot(p.x - x, p.y - y)

    for ref, (x, y, rot) in sorted(old.items(), key=moved_dist, reverse=True):
        if exempt and ref in exempt:
            # F2: an edge-class seat is hpwl-worse BY DESIGN (the netlist
            # proxy is what its class overrules) -- pruning it back would
            # undo the seat one stage later.
            continue
        p = state.parts[ref]
        if math.hypot(p.x - x, p.y - y) < 1e-9 and abs(p.rot - rot) < 1e-9:
            continue
        base = measure(state, edge_bands)
        cur = (p.x, p.y, p.rot)
        state.apply_move(ref, x, y, rot)
        if measure(state, edge_bands) < base:
            pruned.append(ref)
        else:
            state.apply_move(ref, *cur)
    if notes is not None and pruned:
        notes.append(f"prune: reverted {len(pruned)} per-part mis-move(s) "
                     f"the global gate could not see: {', '.join(pruned)}")
    return pruned


# --------------------------------------------------------------------------
# exchange -- the joint +/-v lattice ILP (run-5 F4)
# --------------------------------------------------------------------------

EXCHANGE_LATTICE_TOL = 0.1
# ^ how close current-minus-input must sit to an integer multiple of the
# pattern vector for a part to be on the damage lattice (mm). Parts off the
# lattice (seeded elsewhere, nudged by another tool) are stay-only.


def _exchange_solve(state, v, exclude, edge_bands):
    """One joint solve of the exchange ILP for pattern vector ``v``.

    Every eligible part chooses an option k with pose = current + k*v,
    where the options are the +/-1 V-LATTICE AROUND ITS INPUT POSE
    (seed_x/seed_y): j_cur = round offset of current from input in v units;
    k is offered iff j_cur + k is in {-1, 0, +1} and the target pad extent
    stays on-board (banded). Rationale, all measured on the swap corpus:

    - The damage hypothesis is "parts displaced by +/-v"; poses more than
      one lattice step from the INPUT are not reachable by any such
      hypothesis. Offering current-relative +/-2 instead let the solver
      "improve" hpwl by relocating 13 never-displaced parts 31.6 mm out --
      board-only metrics all improved while correct structure broke, and
      no gate conjunct can see that. Input-anchoring makes every such move
      structurally unreachable while still letting the solver UNWIND or
      COMPLETE a part the ladder itself mis-moved (a part at input-v may
      take +2: undo the mis-move and come home).
    - Constraints are pairwise-absolute pad/hole legality on every option
      combo (skipping stay-stay: the status quo stays feasible), so a
      solution can only remove or keep existing conflicts, never add one.
    - The objective is EXACT per-net bbox hpwl at the chosen poses
      (linearized: per-net min/max vars over option-shifted pad bboxes)
      plus MOVE_PENALTY_MM per moved part. A pairwise spring proxy was
      measured WRONG here (valued the true exchange at -0.56 while real
      hpwl gained 60): only the gate's own currency can price a joint
      move.

    Returns {ref: k} for the chosen non-stay options ({} = all-stay or no
    solver).
    """
    try:
        import numpy as np
        from scipy.optimize import milp, LinearConstraint, Bounds
    except ImportError:
        return None
    vx, vy = v
    bands = edge_bands or {}
    excl = exclude or set()
    ctx = state.legality_ctx
    m = state.clearance + 0.1

    opts: Dict[str, List[int]] = {}
    for r, p in sorted(state.parts.items()):
        if r in excl or p.locked:
            opts[r] = [0]
            continue
        dx0, dy0 = p.x - p.seed_x, p.y - p.seed_y
        j_cur = None
        for j in (-1, 0, 1):
            if math.hypot(dx0 - j * vx, dy0 - j * vy) <= EXCHANGE_LATTICE_TOL:
                j_cur = j
                break
        if j_cur is None:
            opts[r] = [0]
            continue
        ss = [0]
        pp = ctx.parts.get(r)
        for k in (-2, -1, 1, 2):
            if j_cur + k not in (-1, 0, 1):
                continue
            ext = (pp.extent(p.x + k * vx, p.y + k * vy, p.rot)
                   if pp is not None else None)
            if ext is None or (_bbox_outside(ext, state.board)
                               <= bands.get(r, 0.0) + 1e-6):
                ss.append(k)
        opts[r] = ss

    refs = sorted(opts)
    ext_of = {}
    for r in refs:
        p = state.parts[r]
        pp = ctx.parts.get(r)
        for k in opts[r]:
            ext_of[(r, k)] = (pp.extent(p.x + k * vx, p.y + k * vy, p.rot)
                              if pp is not None else None)

    def near(ea, eb):
        return (ea is None or eb is None
                or (ea[2] + m >= eb[0] and eb[2] + m >= ea[0]
                    and ea[3] + m >= eb[1] and eb[3] + m >= ea[1]))

    conflict = []
    for i, a in enumerate(refs):
        for b in refs[i + 1:]:
            if len(opts[a]) == 1 and len(opts[b]) == 1:
                continue
            pa, pb = state.parts[a], state.parts[b]
            for ka in opts[a]:
                for kb in opts[b]:
                    if ka == 0 and kb == 0:
                        continue
                    if not near(ext_of[(a, ka)], ext_of[(b, kb)]):
                        continue
                    if _pair_conflicts(
                            state, a, (pa.x + ka * vx, pa.y + ka * vy),
                            b, (pb.x + kb * vx, pb.y + kb * vy)):
                        conflict.append((a, ka, b, kb))

    xidx = {}
    for r in refs:
        for k in opts[r]:
            xidx[(r, k)] = len(xidx)
    nx = len(xidx)

    nets: Dict[int, Dict[str, List[float]]] = {}
    for r, p in state.parts.items():
        for gx, gy, nid in p.pad_globals():
            d = nets.setdefault(nid, {})
            bb = d.get(r)
            if bb is None:
                d[r] = [gx, gy, gx, gy]
            else:
                bb[0] = min(bb[0], gx)
                bb[1] = min(bb[1], gy)
                bb[2] = max(bb[2], gx)
                bb[3] = max(bb[3], gy)
    net_ids = sorted(n for n, d in nets.items() if len(d) >= 2)

    nv = nx + 4 * len(net_ids)
    cost = np.zeros(nv)
    lb = np.zeros(nv)
    ub = np.ones(nv)
    for kk in range(len(net_ids)):
        for j, sgn in ((0, 1.0), (1, -1.0), (2, 1.0), (3, -1.0)):
            i = nx + 4 * kk + j
            cost[i] = sgn
            lb[i] = -np.inf
            ub[i] = np.inf
    for (r, k), i in xidx.items():
        if k != 0:
            cost[i] += MOVE_PENALTY_MM

    rows = []
    rl = []
    ru = []
    for r in refs:
        row = np.zeros(nv)
        for k in opts[r]:
            row[xidx[(r, k)]] = 1.0
        rows.append(row)
        rl.append(1.0)
        ru.append(1.0)
    for (a, ka, b, kb) in conflict:
        row = np.zeros(nv)
        row[xidx[(a, ka)]] = 1.0
        row[xidx[(b, kb)]] = 1.0
        rows.append(row)
        rl.append(-np.inf)
        ru.append(1.0)
    for kk, nid in enumerate(net_ids):
        iXmax, iXmin = nx + 4 * kk, nx + 4 * kk + 1
        iYmax, iYmin = nx + 4 * kk + 2, nx + 4 * kk + 3
        for r, bb in sorted(nets[nid].items()):
            kx = [(xidx[(r, k)], k * vx) for k in opts.get(r, [0]) if k != 0]
            ky = [(xidx[(r, k)], k * vy) for k in opts.get(r, [0]) if k != 0]
            for (idx, bound, coefs, upper) in (
                    (iXmax, bb[2], kx, True), (iXmin, bb[0], kx, False),
                    (iYmax, bb[3], ky, True), (iYmin, bb[1], ky, False)):
                row = np.zeros(nv)
                row[idx] = 1.0
                for i, c_ in coefs:
                    row[i] = -c_
                rows.append(row)
                if upper:
                    rl.append(bound)
                    ru.append(np.inf)
                else:
                    rl.append(-np.inf)
                    ru.append(bound)

    A = np.vstack(rows)
    integ = np.zeros(nv)
    integ[:nx] = 1
    res = milp(c=cost, constraints=LinearConstraint(A, rl, ru),
               integrality=integ, bounds=Bounds(lb, ub))
    if res.x is None:
        return {}
    return {r: k for (r, k), i in xidx.items() if res.x[i] > 0.5 and k != 0}


def exchange_stage(state, vectors: Sequence[Tuple[float, float]],
                   exclude: Optional[Set[str]] = None,
                   edge_bands: Optional[Dict[str, float]] = None,
                   notes: Optional[List[str]] = None,
                   max_iter: int = 10):
    """Gated joint +/-v group exchange along the pattern vectors.

    The measured limit this removes (run-4 F4): a self-consistent displaced
    ISLAND -- every member conflict-free where it sits, net-anchored to the
    other members -- presents no per-part gradient, so the assign ILP's
    static anchor proxy prices every member against stay and the
    simultaneous exchange is invisible. Every local mechanism probed on the
    swap corpus failed measurably (single-sign translate sets cascade to
    nothing against the OPPOSITE group's squatters; seeded best-response
    has a wrong attractor -- home parts chasing displaced partners beat
    the true exchange 315 to 60 in raw hpwl; interlock 2-coloring floods).
    Only the JOINT solve works: one ILP over the +/-1 input-lattice with
    absolute pairwise legality rows and the exact bbox-hpwl objective
    (see _exchange_solve).

    Each vector iterates solve -> apply -> measure-gate -> until all-stay
    or a gate rejection (exact snapshot restore). Healthy-board no-op by
    construction: candidate vectors exist only where the corner-pattern
    fit found displaced pattern parts (rigid_vectors is pattern-gated).

    Returns (report, old_poses): old_poses maps each moved ref to its
    pre-move pose, in prune_assignment's format.
    """
    report: Dict = {'attempts': 0, 'accepted': [], 'solver': True}
    old: Dict[str, Tuple[float, float, float]] = {}
    if not vectors:
        return report, old
    base = measure(state, edge_bands)
    for (vx, vy) in vectors:
        for _ in range(max_iter):
            report['attempts'] += 1
            sigma = _exchange_solve(state, (vx, vy), exclude, edge_bands)
            if sigma is None:
                report['solver'] = False
                if notes is not None:
                    notes.append('exchange: scipy.optimize.milp unavailable'
                                 ' -- stage skipped')
                return report, old
            if not sigma:
                break
            groups: Dict[int, List[str]] = {}
            for r, k in sigma.items():
                groups.setdefault(k, []).append(r)
            snap = {r: (state.parts[r].x, state.parts[r].y,
                        state.parts[r].rot) for r in sigma}
            for k, refs_ in sorted(groups.items()):
                state.apply_group_move(sorted(refs_), k * vx, k * vy)
            after = measure(state, edge_bands)
            # STRICT improvement: an exchange the tuple cannot see is churn.
            if after < base:
                base = after
                old.update(snap)
                report['accepted'].append(
                    {'v': [round(vx, 4), round(vy, 4)],
                     'moves': {str(k): sorted(refs_)
                               for k, refs_ in sorted(groups.items())},
                     'gate': list(after)})
                if notes is not None:
                    notes.append(
                        'exchange: '
                        + ' / '.join(f'{len(refs_)} part(s) by {k:+d}v'
                                     for k, refs_ in sorted(groups.items()))
                        + f' along ({vx:.3f}, {vy:.3f})')
            else:
                # exact snapshot restore -- apply_group_move's `+= dx`
                # would leave float residue on an inverse move
                for r, (x, y, rot) in snap.items():
                    state.apply_move(r, x, y, rot)
                break
    return report, old


# --------------------------------------------------------------------------
# assign -- the ILP
# --------------------------------------------------------------------------

def _net_anchor_cost(state, ref: str, x: float, y: float,
                     fixed: Set[str]) -> float:
    """Linear wirelength proxy: distance from the pose to each of the part's
    nets' centroids over OTHER parts' current poses (those parts move too, so
    this is a proxy -- the exclusions carry the hard constraints; this breaks
    ties). `fixed` weights: a centroid built only from frame parts would be
    empty on most nets, so all others count, frame parts double."""
    part = state.parts[ref]
    cost = 0.0
    for net in part.nets:
        sx = sy = w = 0.0
        for other in state.net_refs.get(net, ()):
            if other == ref:
                continue
            op = state.parts[other]
            ww = 2.0 if other in fixed else 1.0
            sx += ww * op.x
            sy += ww * op.y
            w += ww
        if w > 0:
            cost += math.hypot(x - sx / w, y - sy / w)
    return cost


def conflict_offset_vectors(state, *, cluster_tol: float = 1.5,
                            min_support: int = 3,
                            top_k: int = 3) -> List[Dict]:
    """Candidate rigid-translate vectors from CONFLICT-PAIR offsets (run-7
    A1: the vector source for boards with no mounting-hole pattern).

    The geometry: a translate-displaced part lands ON or NEAR stationary
    geometry (stacks, pad conflicts), and for every such pair the offset
    a.pos - b.pos approximates the damage vector to within ~part size --
    so the offsets of the damage's conflict pairs CLUSTER while a dense
    design's own incidental conflicts scatter. Clusters with
    ``min_support`` pairs (canonicalized up to sign, like rigid_vectors)
    become candidate vectors, largest support first.

    Design history, measured: the first A1 attempt fit block vectors from
    net-anchor targets and was abandoned -- healthy boards read up to 60mm
    of pure design slack (a part legitimately sits far from its net
    centroid) and healthy-vs-damaged rp2350 were indistinguishable
    (12.9 vs 12.0). Conflict offsets have the property that matters
    instead: NO conflicts, NO vectors -- the healthy-board no-op guarantee
    is structural, not a threshold.

    The vectors are COARSE (part-size error), which the consumers absorb:
    build_candidates offers +/-v poses whose residual mis-fit the
    legalize/repair rungs clean, and the exchange lattice stays consistent
    because the same vector list is used for the whole run.
    """
    ctx = state.legality_ctx
    if ctx is None:
        return []
    refs = sorted(state.parts)
    offsets = []
    for i, a in enumerate(refs):
        pa = state.parts[a]
        for b in refs[i + 1:]:
            pb = state.parts[b]
            sf = ctx.pair_shortfall(a, b)
            # STACK/HOLE pairs only -- these are clearance-independent
            # (physical intersection), so the no-op guarantee holds at ANY
            # grading clearance. Clearance-SHORTFALL pairs are excluded:
            # packed healthy designs read shortfalls at a too-strict
            # clearance (measured: the corpus sweep at the 0.25 default
            # minted a vector on a healthy board and moved it).
            if not (sf.stack or sf.hole > legality.EPS):
                continue
            offsets.append((pa.x - pb.x, pa.y - pb.y))
    clusters: List[List[Tuple[float, float]]] = []
    for (dx, dy) in offsets:
        if math.hypot(dx, dy) < 2.0:
            # small offsets are intra-pile NEIGHBOR pairs (two displaced
            # parts conflicting each other at design-scale spacing), not
            # displaced-onto-stationary evidence -- measured: they dominated
            # the clusters at ~1mm and drowned the damage vector
            continue
        canon = (dx, dy) if (dy, dx) > (0, 0) else (-dx, -dy)
        for cl in clusters:
            cx = sum(p[0] for p in cl) / len(cl)
            cy = sum(p[1] for p in cl) / len(cl)
            if math.hypot(canon[0] - cx, canon[1] - cy) <= cluster_tol:
                cl.append(canon)
                break
        else:
            clusters.append([canon])
    out = []
    for cl in clusters:
        if len(cl) < min_support:
            continue
        vx = sum(p[0] for p in cl) / len(cl)
        vy = sum(p[1] for p in cl) / len(cl)
        out.append({'v': (round(vx, 4), round(vy, 4)), 'support': len(cl)})
    out.sort(key=lambda d: -d['support'])
    return out[:top_k]




def build_candidates(state, tiers: Tiers,
                     vectors: Sequence[Tuple[float, float]],
                     proposals: Dict[str, List[Tuple[float, float]]],
                     edge_bands: Optional[Dict[str, float]] = None
                     ) -> Dict[str, List[Tuple[float, float]]]:
    """Per-part candidate positions: stay (always index 0), +/-each vector
    (kept only when the pad extent stays on-board), and any pattern-proposed
    slots. Locked parts get only stay.

    Run-4 F2: the anti-evacuation cull compares each candidate's off-board
    amount against the part's CURRENT pose -- and a DISPLACED edge part sits
    interior (cur_oob = 0), so its true home, which overhangs BY DESIGN, was
    culled unconditionally (run 3's J1 was never offered its edge slot).
    Refs declared in ``edge_bands`` ({ref: band_max_mm}) may overhang up to
    their band; 'stay' stays index 0 -- a declaration never forces a move.
    """
    out: Dict[str, List[Tuple[float, float]]] = {}
    pattern: Dict[str, Set[Tuple[float, float]]] = {}
    bands = edge_bands or {}
    for ref in sorted(state.parts):
        p = state.parts[ref]
        cands = [(p.x, p.y)]
        if not p.locked:
            for (vx, vy) in vectors:
                for sx, sy in ((vx, vy), (-vx, -vy)):
                    cands.append((round(p.x + sx, 4), round(p.y + sy, 4)))
            for slot in proposals.get(ref, ()):
                pattern.setdefault(ref, set()).add(slot)
                if slot not in cands:
                    cands.append(slot)
        kept = []
        pp = (state.legality_ctx.parts.get(ref)
              if state.legality_ctx is not None else None)
        cur_oob = None
        for i, (x, y) in enumerate(cands):
            if i > 0 and pp is not None:
                ext = pp.extent(x, y, p.rot)
                if ext is not None:
                    if cur_oob is None:
                        e0 = pp.extent(p.x, p.y, p.rot)
                        cur_oob = (_bbox_outside(e0, state.board)
                                   if e0 is not None else 0.0)
                    # Never offer a candidate whose pad copper leaves the
                    # board MORE than the part already does (S1: the
                    # conflict gate must not be satisfiable by evacuation)
                    # -- except up to a declared edge band (run-4 F2).
                    allow = max(cur_oob, bands.get(ref, 0.0))
                    if _bbox_outside(ext, state.board) > allow + 1e-6:
                        continue
            kept.append((x, y))
        out[ref] = kept
    return out, pattern


def solve_assignment(state, candidates: Dict[str, List[Tuple[float, float]]],
                     tiers: Tiers,
                     move_penalty: float = MOVE_PENALTY_MM,
                     notes: Optional[List[str]] = None,
                     pattern: Optional[Dict[str, Set]] = None,
                     edge_pref: Optional[Dict[str, float]] = None
                     ) -> Dict[str, int]:
    """Choose one candidate per part. Exact ILP when scipy.optimize.milp is
    available; breakout-weighted min-conflicts descent otherwise. Returns
    {ref: chosen index}.

    ``edge_pref`` ({ref: band_max_mm}): declared edge-class refs whose edge
    could not be named -- their objective term is the EDGE metric instead of
    the net-anchor proxy (see EDGE_PREF_WEIGHT)."""
    try:
        return _solve_ilp(state, candidates, tiers, move_penalty, notes,
                          pattern or {}, edge_pref or {})
    except ImportError:
        if notes is not None:
            notes.append('scipy.optimize.milp unavailable -- using the '
                         'breakout-descent fallback')
        return _solve_breakout(state, candidates, tiers, notes,
                               edge_pref=edge_pref or {})


def _pair_conflicts(state, a: str, pos_a, b: str, pos_b) -> bool:
    """Do these two candidate poses conflict, ABSOLUTELY? Repair semantics:
    unlike the quench's baseline-relative gate, the assign stage exists to
    REMOVE existing conflicts, and baseline-relative exclusions make the
    damaged status quo feasible at zero cost (measured: the ILP chose
    all-stay on the swap corpus). The outer stage gate still reverts any
    application that worsens the board."""
    ctx = state.legality_ctx
    pa, pb = state.parts[a], state.parts[b]
    cur = ctx.pair_shortfall(a, b, pose_a=(pos_a[0], pos_a[1], pa.rot),
                             pose_b=(pos_b[0], pos_b[1], pb.rot))
    # run-6: `stack` makes ANY-net pad intersection a conflict here too --
    # without it the assign/exchange solvers could co-place two same-net
    # parts in the same space (the shipped C14-on-R14 class: R14's
    # homecoming landed on squatting C14 and no exclusion row existed).
    return (cur.pad > legality.EPS or cur.hole > legality.EPS or cur.stack)


def _interacting_pairs(state, candidates):
    """Part pairs whose candidate extents can come near each other."""
    ctx = state.legality_ctx
    reach: Dict[str, Tuple[float, float, float, float]] = {}
    for ref, cands in candidates.items():
        pp = ctx.parts.get(ref)
        if pp is None:
            continue
        boxes = []
        for (x, y) in cands:
            e = pp.extent(x, y, state.parts[ref].rot)
            if e is not None:
                boxes.append(e)
        if boxes:
            reach[ref] = (min(b[0] for b in boxes), min(b[1] for b in boxes),
                          max(b[2] for b in boxes), max(b[3] for b in boxes))
    refs = sorted(reach)
    m = state.clearance + 0.1
    for i, a in enumerate(refs):
        ra = reach[a]
        for b in refs[i + 1:]:
            rb = reach[b]
            if (ra[2] + m >= rb[0] and rb[2] + m >= ra[0]
                    and ra[3] + m >= rb[1] and rb[3] + m >= ra[1]):
                yield a, b


def _solve_ilp(state, candidates, tiers, move_penalty, notes, pattern,
               edge_pref=None):
    import numpy as np
    from scipy.optimize import milp, LinearConstraint, Bounds
    from scipy.sparse import lil_matrix

    edge_pref = edge_pref or {}
    refs = sorted(candidates)
    var_of: Dict[Tuple[str, int], int] = {}
    costs: List[float] = []
    fixed = tiers.locked | tiers.zero_net
    for ref in refs:
        p = state.parts[ref]
        for k, (x, y) in enumerate(candidates[ref]):
            var_of[(ref, k)] = len(costs)
            if ref in edge_pref:
                # F2: class outranks the netlist proxy for edge parts (R1).
                m = edge_metric(state, ref, x, y, edge_pref[ref])
                c = EDGE_PREF_WEIGHT * (m if m is not None else 100.0)
            else:
                c = _net_anchor_cost(state, ref, x, y, fixed)
            # F1 nearest-slot tiebreak (see DIST_TIEBREAK_PER_MM). Applies to
            # every candidate; the stay pose charges 0 by construction.
            c += DIST_TIEBREAK_PER_MM * math.hypot(x - p.x, y - p.y)
            if (x, y) in pattern.get(ref, ()):
                c -= PATTERN_BONUS_MM       # evidence, not a cost
            elif k > 0:
                c += move_penalty
            costs.append(c)
    n = len(costs)

    rows: List[Tuple[List[int], float, float]] = []
    # one candidate per part
    for ref in refs:
        idx = [var_of[(ref, k)] for k in range(len(candidates[ref]))]
        rows.append((idx, 1.0, 1.0))
    # a pattern SLOT takes at most one part (two displaced holes must not both
    # claim the same corner -- holes carry no copper, so the pad exclusions
    # cannot see that collision)
    slot_users: Dict[Tuple[float, float], List[int]] = {}
    for ref in refs:
        for k, pos in enumerate(candidates[ref]):
            if pos in pattern.get(ref, ()):
                slot_users.setdefault(pos, []).append(var_of[(ref, k)])
    for pos, idx in sorted(slot_users.items()):
        if len(idx) > 1:
            rows.append((idx, 0.0, 1.0))
    # pairwise exclusions between conflicting candidate poses
    n_excl = 0
    for a, b in _interacting_pairs(state, candidates):
        for ka, pos_a in enumerate(candidates[a]):
            for kb, pos_b in enumerate(candidates[b]):
                if _pair_conflicts(state, a, pos_a, b, pos_b):
                    rows.append(([var_of[(a, ka)], var_of[(b, kb)]],
                                 0.0, 1.0))
                    n_excl += 1
    if notes is not None:
        notes.append(f'ILP: {n} binaries, {len(rows)} rows '
                     f'({n_excl} exclusions)')

    A = lil_matrix((len(rows), n))
    lb = np.zeros(len(rows))
    ub = np.zeros(len(rows))
    for i, (idx, lo, hi) in enumerate(rows):
        for j in idx:
            A[i, j] = 1.0
        lb[i] = lo
        ub[i] = hi
    res = milp(c=np.asarray(costs),
               constraints=LinearConstraint(A.tocsr(), lb, ub),
               integrality=np.ones(n),
               bounds=Bounds(0, 1))
    if res.status != 0 or res.x is None:
        # Infeasible (exclusions + one-per-part cannot all hold): fall back.
        if notes is not None:
            notes.append(f'ILP status {res.status} ({res.message}) -- '
                         f'breakout-descent fallback')
        return _solve_breakout(state, candidates, tiers, notes)
    choice: Dict[str, int] = {}
    for ref in refs:
        for k in range(len(candidates[ref])):
            if res.x[var_of[(ref, k)]] > 0.5:
                choice[ref] = k
                break
        else:
            choice[ref] = 0
    return choice


def _solve_breakout(state, candidates, tiers, notes, max_sweeps: int = 60,
                    edge_pref=None):
    """Min-conflicts coordinate descent with breakout constraint weighting
    (Morris 1993): at a local minimum every currently-conflicting pair's
    weight is incremented, so chronic squatters eventually move first.
    Deterministic."""
    refs = sorted(candidates)
    choice = {r: 0 for r in refs}
    weights: Dict[Tuple[str, str], float] = {}
    pairs = list(_interacting_pairs(state, candidates))
    by_ref: Dict[str, List[Tuple[str, str]]] = {r: [] for r in refs}
    for a, b in pairs:
        by_ref[a].append((a, b))
        by_ref[b].append((a, b))

    def pos(ref):
        return candidates[ref][choice[ref]]

    def pair_bad(a, b):
        return _pair_conflicts(state, a, pos(a), b, pos(b))

    def ref_cost(ref, k):
        x, y = candidates[ref][k]
        w = 0.0
        for (a, b) in by_ref[ref]:
            other = b if a == ref else a
            oxy = pos(other)
            if _pair_conflicts(state, ref,
                               (x, y), other, oxy):
                w += weights.get((a, b), 1.0)
        p = state.parts[ref]
        if edge_pref and ref in edge_pref:
            m = edge_metric(state, ref, x, y, edge_pref[ref])
            base_cost = EDGE_PREF_WEIGHT * (m if m is not None else 100.0)
        else:
            base_cost = _net_anchor_cost(state, ref, x, y,
                                         tiers.locked | tiers.zero_net)
        return (w, base_cost
                # F1 nearest-slot tiebreak, mirrored from _solve_ilp
                + DIST_TIEBREAK_PER_MM * math.hypot(x - p.x, y - p.y)
                + (MOVE_PENALTY_MM if k else 0.0))

    for sweep in range(max_sweeps):
        changed = 0
        for ref in refs:
            if state.parts[ref].locked or len(candidates[ref]) < 2:
                continue
            best = min(range(len(candidates[ref])),
                       key=lambda k: ref_cost(ref, k) + ((0.0, 0.0)
                                                         if k == choice[ref]
                                                         else (0.0, 1e-9)))
            if best != choice[ref]:
                choice[ref] = best
                changed += 1
        bad = [(a, b) for a, b in pairs if pair_bad(a, b)]
        if not bad:
            break
        if changed == 0:
            for key in bad:
                weights[key] = weights.get(key, 1.0) + 1.0
    if notes is not None:
        residual = sum(1 for a, b in pairs if pair_bad(a, b))
        notes.append(f'breakout descent: {residual} residual conflicting '
                     f'pair(s)')
    return choice
