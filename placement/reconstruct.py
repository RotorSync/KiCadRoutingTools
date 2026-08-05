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
(pad conflict pairs, hole shortfall, pad-extent off-board amount, courtyard
overlap area, hpwl) does not worsen -- the run-2 lesson that a bare
conflict-count gate is gameable by pushing parts off the board.
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


# --------------------------------------------------------------------------
# measurement -- the stage gate
# --------------------------------------------------------------------------

def _bbox_outside(ext, b) -> float:
    """Per-axis overshoot of the raw board bbox at ZERO margin. Exact on a
    rectangular outline; a consistent lower bound on a notched one -- the
    gate tuple only needs stage-to-stage comparability."""
    return (max(0.0, b[0] - ext[0]) + max(0.0, ext[2] - b[2])
            + max(0.0, b[1] - ext[1]) + max(0.0, ext[3] - b[3]))


def pad_oob_amount(state) -> float:
    """Summed pad/hole-extent off-board amount over all parts (zero margin)."""
    oob = 0.0
    if state.legality_ctx is None:
        return oob
    for ref, pp in state.legality_ctx.parts.items():
        p = state.parts.get(ref)
        if p is None:
            continue
        ext = pp.extent(p.x, p.y, p.rot)
        if ext is not None:
            oob += _bbox_outside(ext, state.board)
    return oob


def measure(state) -> Tuple:
    """The lexicographic gate tuple. Smaller-or-equal is acceptable."""
    m = state.pad_legality_metrics() if state.legality_ctx is not None else {}
    leg = state.legality_metrics()
    return (m.get('pad_conflict_pairs', 0),
            round(m.get('hole_shortfall', 0.0), 4),
            round(pad_oob_amount(state), 4),
            round(leg.get('overlap_area', 0.0), 4),
            round(leg.get('hpwl', 0.0), 3))


# --------------------------------------------------------------------------
# classify
# --------------------------------------------------------------------------

class Tiers:
    __slots__ = ('locked', 'zero_net', 'anchors', 'smalls', 'threshold')

    def as_dict(self):
        return {'locked': sorted(self.locked),
                'zero_net': sorted(self.zero_net),
                'anchors': sorted(self.anchors),
                'smalls': sorted(self.smalls),
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
    deduped up to SIGN (a swap displaces two groups by +v and -v)."""
    vecs: List[Tuple[float, float]] = []
    for ref, cands in sorted(proposals.items()):
        p = state.parts[ref]
        for (px, py) in cands:
            v = (p.x - px, p.y - py)
            if math.hypot(*v) < grid_tol:
                continue
            canon = v if (v[1], v[0]) > (0, 0) else (-v[0], -v[1])
            if not any(math.hypot(canon[0] - w[0], canon[1] - w[1]) <= 2 * grid_tol
                       for w in vecs):
                vecs.append((round(canon[0], 4), round(canon[1], 4)))
    return vecs


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


def build_candidates(state, tiers: Tiers,
                     vectors: Sequence[Tuple[float, float]],
                     proposals: Dict[str, List[Tuple[float, float]]]
                     ) -> Dict[str, List[Tuple[float, float]]]:
    """Per-part candidate positions: stay (always index 0), +/-each vector
    (kept only when the pad extent stays on-board), and any pattern-proposed
    slots. Locked parts get only stay."""
    out: Dict[str, List[Tuple[float, float]]] = {}
    pattern: Dict[str, Set[Tuple[float, float]]] = {}
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
                    # conflict gate must not be satisfiable by evacuation).
                    if _bbox_outside(ext, state.board) > cur_oob + 1e-6:
                        continue
            kept.append((x, y))
        out[ref] = kept
    return out, pattern


def solve_assignment(state, candidates: Dict[str, List[Tuple[float, float]]],
                     tiers: Tiers,
                     move_penalty: float = MOVE_PENALTY_MM,
                     notes: Optional[List[str]] = None,
                     pattern: Optional[Dict[str, Set]] = None) -> Dict[str, int]:
    """Choose one candidate per part. Exact ILP when scipy.optimize.milp is
    available; breakout-weighted min-conflicts descent otherwise. Returns
    {ref: chosen index}."""
    try:
        return _solve_ilp(state, candidates, tiers, move_penalty, notes,
                          pattern or {})
    except ImportError:
        if notes is not None:
            notes.append('scipy.optimize.milp unavailable -- using the '
                         'breakout-descent fallback')
        return _solve_breakout(state, candidates, tiers, notes)


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
    return cur.pad > legality.EPS or cur.hole > legality.EPS


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


def _solve_ilp(state, candidates, tiers, move_penalty, notes, pattern):
    import numpy as np
    from scipy.optimize import milp, LinearConstraint, Bounds
    from scipy.sparse import lil_matrix

    refs = sorted(candidates)
    var_of: Dict[Tuple[str, int], int] = {}
    costs: List[float] = []
    fixed = tiers.locked | tiers.zero_net
    for ref in refs:
        for k, (x, y) in enumerate(candidates[ref]):
            var_of[(ref, k)] = len(costs)
            c = _net_anchor_cost(state, ref, x, y, fixed)
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


def _solve_breakout(state, candidates, tiers, notes, max_sweeps: int = 60):
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
        return (w, _net_anchor_cost(state, ref, x, y,
                                    tiers.locked | tiers.zero_net)
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
