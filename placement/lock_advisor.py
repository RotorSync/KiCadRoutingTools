"""Which parts should NOT be moved? Advice, never action (#431).

The user's ask was "lock components, but only the ones really needed on that
exact location (HAT ports etc)". This reports them with a reason each and prints
a paste-ready `--lock` list. It never locks anything, because a wrong auto-lock
silently freezes a part that needed to move and that failure is invisible --
the run just quietly does less.

## The strongest rule is a code fact, not folklore

`placement/quench.py:207` keeps only `net_id > 0` pads:

    self.pads_local = [(p.local_x, p.local_y, p.net_id)
                       for p in fp.pads if p.net_id > 0]

so a net-less NPTH mounting hole has `pin_count == 0` and `nets == []`. The only
skip in the part loop (`:340`) is `if not fp.pads`, and a mounting hole HAS a
pad. So it is in `state.parts`, it is movable, and **no airwire opposes it** --
only the halo and edge terms decide where it goes. The quench will slide an M3
hole across the board to relieve someone else's halo gradient. Measured:
`tigard.kicad_pcb` has 4 unlocked M3 holes in exactly this state.

## Confidence, and being honest about which signals are weak

Geometric rules measure the board. Lexical rules guess from names, and the
guess is asymmetric: on a stock-KiCad-library board `Connector_USB:USB_C_...`
is near-exact, while a house library (`interf_u:PGA120`) yields nothing. False
negatives are common, false positives rare -- the right shape for advice, and
the output says so.

A ref matched by rules from two DIFFERENT evidence channels (one geometric, one
lexical) promotes to `high`: two channels that fail independently.
"""
from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

CONFIDENCE_ORDER = {'high': 0, 'medium': 1, 'low': 2}

# The lexical tables live in part_class (run-4 A): the class question is
# consumed by three tools (this advisor, emit-intent, reconstruct) and a
# second copy would drift. The aliases keep this module's names stable.
from placement.part_class import (  # noqa: E402
    CONNECTOR_FP as _CONNECTOR_FP, MOUNT_FP as _MOUNT_FP,
    CONN_PREFIX_MED as _CONN_PREFIX_MED, CONN_PREFIX_LOW as _CONN_PREFIX_LOW,
    IFACE_PINS as _IFACE_PINS, SEAT_TOL_MM, classify_part, pose_plausible)

_PREFIX_RE = re.compile(r'^([A-Za-z]+)')

HIGH_PIN_ADVISORY = 40   # place_optimize has no --max-target-pins guard


@dataclass(frozen=True)
class LockFinding:
    ref: str
    confidence: str
    rules: Tuple[str, ...]
    reasons: Tuple[str, ...]
    evidence: Dict[str, object]
    already_locked: bool = False
    lock_source: Optional[str] = None      # 'kicad' | 'cli' | None
    # Run-4 A: the pose-independent class and its pose verdict. A part class
    # is what DETERMINES the position; pose_plausible says whether the
    # current pose is consistent with it (None = ungraded/no outline).
    part_class: Optional[str] = None
    class_confidence: Optional[str] = None
    pose_plausible: Optional[bool] = None


@dataclass
class LockAdvice:
    findings: List[LockFinding] = field(default_factory=list)
    advisories: List[Dict] = field(default_factory=list)
    locked_refs: List[str] = field(default_factory=list)
    min_confidence: str = 'medium'
    use_globs: bool = False
    board: str = ''
    n_footprints: int = 0

    def suggested(self) -> List[LockFinding]:
        cap = CONFIDENCE_ORDER[self.min_confidence]
        return [f for f in self.findings
                if CONFIDENCE_ORDER[f.confidence] <= cap and not f.already_locked]

    def lock_patterns(self) -> List[str]:
        refs = [f.ref for f in self.suggested()]
        if not self.use_globs:
            return refs
        pats, seen = [], set()
        for r in refs:
            m = _PREFIX_RE.match(r)
            g = (m.group(1) + '*') if m else r
            if g not in seen:
                seen.add(g)
                pats.append(g)
        return pats

    def tally(self) -> Dict[str, int]:
        by = {c: 0 for c in CONFIDENCE_ORDER}
        for f in self.findings:
            by[f.confidence] += 1
        return {'findings_high': by['high'], 'findings_medium': by['medium'],
                'findings_low': by['low'],
                # The actionable number: high-confidence parts still free to
                # move. The round trip (feed the printed --lock back in) drives
                # it to 0, which is this design's own acceptance test.
                'unlocked_high': sum(1 for f in self.findings
                                     if f.confidence == 'high' and not f.already_locked),
                # findings already covered, by EITHER a (locked yes) in the file
                # or a --lock pattern the caller passed...
                'findings_covered': sum(1 for f in self.findings if f.already_locked),
                # ...vs parts the FILE pins, which may include parts no rule
                # fired on (a logo footprint, say) and so are not findings.
                'locked_in_file': len(self.locked_refs),
                'advisories': len(self.advisories)}


def _prefix(ref: str) -> str:
    m = _PREFIX_RE.match(ref or '')
    return (m.group(1) if m else '').upper()


def advise_locks(pcb_data, pcb_file: Optional[str] = None, *,
                 lock_patterns: Optional[Sequence[str]] = None,
                 min_confidence: str = 'medium',
                 edge_margin: float = 1.0,
                 use_globs: bool = False) -> LockAdvice:
    """Findings for every footprint, sorted (confidence, ref). Never mutates."""
    from placement.legality import BoardOutlineGate, footprint_side
    from placement.placement_state import _global_rect
    from placement.legality import rotate_local_bounds

    adv = LockAdvice(min_confidence=min_confidence, use_globs=use_globs,
                     board=pcb_file or '', n_footprints=len(pcb_data.footprints or {}))

    kicad_locked = set()
    if pcb_file:
        try:
            from placement.parser import extract_locked_refs
            kicad_locked = extract_locked_refs(pcb_file)
        except Exception:
            kicad_locked = set()
    kicad_locked |= {r for r, fp in (pcb_data.footprints or {}).items()
                     if getattr(fp, 'locked', False)}

    sides = {}
    if pcb_file:
        try:
            from placement.parser import extract_courtyard_sides
            sides = extract_courtyard_sides(pcb_file)
        except Exception:
            sides = {}

    gate = None
    try:
        if getattr(pcb_data.board_info, 'board_bounds', None) is not None:
            gate = BoardOutlineGate(pcb_data.board_info, 0.0)
    except Exception:
        gate = None

    for ref, fp in sorted((pcb_data.footprints or {}).items()):
        if not fp.pads:
            continue
        rules: List[str] = []
        reasons: List[str] = []
        geometric = lexical = False

        npth = [p for p in fp.pads if getattr(p, 'pad_type', '') == 'np_thru_hole']
        plated = [p for p in fp.pads if _is_plated(p)]
        netted = [p for p in fp.pads if getattr(p, 'net_id', 0) > 0]
        fp_name = (fp.footprint_name or '').lower()
        pref = _prefix(ref)

        ev: Dict[str, object] = {
            'footprint_name': fp.footprint_name, 'ref_prefix': pref,
            'connected_pins': len(netted), 'npth_pads': len(npth),
            'plated_pads': len(plated), 'total_pads': len(fp.pads),
            'side': footprint_side(fp),
        }

        rect = _global_rect(fp, sides.get(ref), rotate_local_bounds)
        if gate is not None and rect is not None:
            try:
                out = gate.rect_outside_amount(rect)
                clr = gate.edge_clearance(rect)
                ev['outside_amount_mm'] = round(out, 4)
                ev['edge_clearance_mm'] = round(clr, 4)
            except Exception:
                out = clr = None
        else:
            out = clr = None

        # --- rule 1: NPTH mounting hole. Structural fact.
        if npth and not plated:
            rules.append('mounting_hole_npth')
            reasons.append(
                f"mounting hole: {len(npth)} non-plated through hole(s), no copper "
                f"pads, {len(netted)} connected pin(s) -> invisible to the airwire "
                f"cost, so only the halo term decides where it goes")
            geometric = True

        # --- rule 2: the body leaves the outline. Geometric fact.
        if out is not None and out > 1e-6:
            rules.append('off_board_overhang')
            reasons.append(f"courtyard leaves the board outline by {out:.2f} mm")
            geometric = True
        # --- rule 3: close to the edge. Measurement exact, inference weak.
        elif clr is not None and clr < edge_margin:
            rules.append('near_board_edge')
            reasons.append(f"{clr:.2f} mm from the board edge "
                           f"(under --lock-edge-margin {edge_margin:g})")
            geometric = True

        # --- rule 4/5: lexical.
        if any(k in fp_name for k in _MOUNT_FP) and 'mounting_hole_npth' not in rules:
            rules.append('connector_footprint_name')
            reasons.append(f"footprint name {fp.footprint_name!r} is a "
                           f"mounting/fiducial/testpoint part")
            lexical = True
        elif any(k in fp_name for k in _CONNECTOR_FP):
            rules.append('connector_footprint_name')
            reasons.append(f"footprint name {fp.footprint_name!r} matches the "
                           f"connector table (naming heuristic: misses house "
                           f"libraries entirely)")
            lexical = True
        if pref in _CONN_PREFIX_MED:
            rules.append('connector_ref_prefix')
            reasons.append(f"reference prefix {pref!r} (IEEE-315 connector)")
            lexical = True
        elif pref in _CONN_PREFIX_LOW:
            rules.append('connector_ref_prefix')
            reasons.append(f"reference prefix {pref!r} (naming convention only)")
            lexical = True

        # --- rule 6: interface pin functions. Bonus signal only.
        pins = {str(getattr(p, 'pinfunction', '') or '').upper() for p in fp.pads}
        if pins & _IFACE_PINS:
            rules.append('connector_pinfunction')
            reasons.append("carries USB/edge interface pin functions "
                           f"({', '.join(sorted(pins & _IFACE_PINS))[:40]})")
            lexical = True

        # --- advisory (reported, deliberately NOT in the lock list)
        if len(netted) >= HIGH_PIN_ADVISORY:
            adv.advisories.append({
                'ref': ref, 'kind': 'high_pin_count',
                'connected_pins': len(netted),
                'reason': (f"{len(netted)} connected pins. place_route_loop guards "
                           f"these with --max-target-pins but place_optimize does "
                           f"NOT, so this part CAN be dragged; "
                           f"docs/placement-optimization.md records that as how "
                           f"placements get destroyed. Consider --lock for a "
                           f"conservative first run.")})

        # Run-4 A: classify (pose-independent) and grade the pose against
        # the class. Only edge_receptacle makes a plausibility claim.
        pc = classify_part(fp, ref)
        plaus = pose_plausible(pc.name, out, clr, SEAT_TOL_MM)
        if pc.name:
            ev['part_class'] = pc.name
            ev['class_confidence'] = pc.confidence
            ev['pose_plausible'] = plaus

        if not rules and not (pc.name == 'edge_receptacle' and plaus is False):
            continue

        conf = 'high' if ('mounting_hole_npth' in rules
                          or 'off_board_overhang' in rules) else None
        if conf is None:
            # Two INDEPENDENT evidence channels beat either alone.
            if geometric and lexical:
                conf = 'high'
                reasons.append("[a geometric and a lexical rule agree "
                               "-> promoted to HIGH]")
            elif ('connector_footprint_name' in rules
                  or 'connector_ref_prefix' in rules and pref in _CONN_PREFIX_MED
                  or 'near_board_edge' in rules):
                conf = 'medium'
            else:
                conf = 'low'

        # Run-4 A: a lock is not a placement. An edge-RECEPTACLE whose pose
        # is implausible (interior: the plug cannot reach it) is demoted out
        # of the paste-ready list -- run 3's J1 carried three lexical
        # connector signals at MEDIUM, and a medium-cutoff round trip would
        # have frozen it 15.8 mm from its true edge slot.
        if pc.name == 'edge_receptacle' and plaus is False:
            conf = 'low'
            reasons.append(
                "[DEMOTED: edge-receptacle class in an IMPLAUSIBLE pose -- "
                f"no overhang and {ev.get('edge_clearance_mm', '?')} mm off "
                f"every edge (seat tol {SEAT_TOL_MM:g}). A lock is not a "
                "placement: reconstruct/repair this part to an edge before "
                "freezing it.]")

        locked = ref in kicad_locked
        src = 'kicad' if locked else None
        if not locked and lock_patterns:
            for pat in lock_patterns:
                if fnmatch.fnmatch(ref, pat):
                    locked, src = True, 'cli'
                    break
        adv.findings.append(LockFinding(
            ref=ref, confidence=conf, rules=tuple(rules), reasons=tuple(reasons),
            evidence=ev, already_locked=locked, lock_source=src,
            part_class=pc.name, class_confidence=pc.confidence,
            pose_plausible=plaus))

    adv.locked_refs = sorted(kicad_locked)
    adv.findings.sort(key=lambda f: (CONFIDENCE_ORDER[f.confidence], f.ref))
    adv.advisories.sort(key=lambda a: a['ref'])
    return adv


def _is_plated(pad) -> bool:
    """Plated barrel, not merely 'has a drill' -- CLAUDE.md #328."""
    try:
        from kicad_parser import pad_is_plated_through
        return bool(pad_is_plated_through(pad))
    except Exception:
        return bool(getattr(pad, 'drill', 0)) and \
            getattr(pad, 'pad_type', '') != 'np_thru_hole'


def format_text(adv: LockAdvice) -> str:
    free = [f for f in adv.findings if not f.already_locked]
    out = [f"Lock advisor: {len(adv.findings)} position-critical part(s); "
           f"{len(free)} currently FREE TO MOVE.",
           "Nothing was locked. This is advice.", ""]
    for f in adv.findings:
        if f.already_locked:
            continue
        head = f"  {f.confidence.upper():<7s}{f.ref:<6s}"
        out.append(head + f.reasons[0])
        for r in f.reasons[1:]:
            out.append(" " * len(head) + "+ " + r)
    already = [f.ref for f in adv.findings if f.already_locked]
    if already:
        out += ["", f"  Already locked, no action needed: {', '.join(already)}"]
    if adv.advisories:
        out += ["", "Advisory - NOT in the pattern list below:"]
        for a in adv.advisories:
            out.append(f"  {a['ref']}  {a['reason']}")
    pats = adv.lock_patterns()
    out += ["", "Paste this (review it first):"]
    out.append("  --lock " + " ".join(pats) if pats
               else "  (nothing at or above --lock-confidence "
                    f"{adv.min_confidence})")
    if adv.use_globs:
        out.append("  NOTE: --suggest-locks-globs collapsed exact refs to globs; "
                   "each one freezes every part it matches.")
    return "\n".join(out)


def to_json(adv: LockAdvice) -> Dict:
    return {
        'schema': 1,
        'board': adv.board,
        'n_footprints': adv.n_footprints,
        'min_confidence': adv.min_confidence,
        'locked_refs': adv.locked_refs,
        'findings': [{'ref': f.ref, 'confidence': f.confidence,
                      'rules': list(f.rules), 'reasons': list(f.reasons),
                      'evidence': f.evidence, 'already_locked': f.already_locked,
                      'lock_source': f.lock_source,
                      'part_class': f.part_class,
                      'class_confidence': f.class_confidence,
                      'pose_plausible': f.pose_plausible} for f in adv.findings],
        'advisories': adv.advisories,
        'lock_patterns': adv.lock_patterns(),
        'lock_argv': (['--lock'] + adv.lock_patterns()) if adv.lock_patterns() else [],
    }
