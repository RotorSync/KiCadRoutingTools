"""Floorplan intent: declare where things are supposed to go, then grade it (#549).

Every placement judgement in this toolchain collapses to "did `crossings` go
down". That number is indifferent between a sensible layout and a scattered one
with the same wirelength -- and a render only moves the judgement from a number
to a vibe. Nothing is ever *declared* about where parts belong, so nothing can
check whether they went there.

This module is the declaration and the check. An intent file says what the
floorplan is meant to be; `grade()` measures the board against it and returns
violations carrying the measured number next to the limit it broke. That makes
intent falsifiable: "the render looks fine" stops being available as a verdict.

THE BOARD OUTLINE IS NOT OURS TO CHANGE. `envelope` is READ from the board, not
authored -- `emit_intent` fills it from `board_bounds` and nothing here ever
writes Edge.Cuts. A part outside the envelope is a finding about the PART. Board
size, cutouts and slots are mechanical decisions (enclosure fit, panel rails,
connector apertures) that belong to the user; the honest response to a board
that is genuinely too small is to say so with the measured number and stop.

Design notes worth knowing before extending this:

* `refs` is the primitive for block membership, not `group`. Sheet group keys
  are opaque uuid paths (KiCad's Sheetname is absent from every corpus board),
  so nothing can author `"group": "sheet:1a2b3c4d"` without first listing the
  board. `group` is accepted, and matched against the raw key AND its
  `short_name` form, but globs over references are what a human or a model can
  actually write.

* A block that resolves to ZERO refs is an error, never a silent pass. A typo'd
  block grading clean is the exact failure this file exists to prevent.

* `rules_run` / `rules_skipped` are reported alongside the violation count. "0
  violations" and "0 rules ran" must not look the same to a machine.

* Every rule reuses the geometry the optimizer itself gates on -- `legality`'s
  `BoardOutlineGate` and `GradedPart`, `groups.decap_tethers`, `QuenchState`'s
  own `legality_metrics`. A grader with its own idea of "legal" grades the
  reimplementation, not the board.
"""
from __future__ import annotations

import fnmatch
import json
import math
import os
from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Optional, Sequence, Tuple

from . import legality
from . import groups as groups_mod

SCHEMA_VERSION = 1
KIND = 'floorplan-intent'

# Severity drives the exit code; 'warn' is reported and does not fail the run.
ERROR = 'error'
WARN = 'warn'

DEFAULT_ZONE_TOLERANCE_MM = 0.5
DEFAULT_ENVELOPE_TOLERANCE_MM = 0.5

_TOP_LEVEL_KEYS = {
    'schema', 'kind', 'board', 'units', 'envelope', 'defaults', 'blocks',
    'keepouts', 'edge_connectors', 'decaps', 'must_lock', 'legality_budget',
    'health', 'severity', 'context',
}
_BLOCK_KEYS = {'name', 'group', 'refs', 'zone', 'side', 'exclusive',
               'tolerance_mm', 'note'}
_EDGES = ('north', 'south', 'east', 'west')


class IntentError(ValueError):
    """A malformed intent file. Distinct from a violation: this is the intent
    being unreadable, not the board being wrong."""


# --------------------------------------------------------------------------
# records
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Violation:
    rule: str
    severity: str
    message: str
    ref: Optional[str] = None
    block: Optional[str] = None
    measured: Dict[str, object] = field(default_factory=dict)
    expected: Dict[str, object] = field(default_factory=dict)

    def sort_key(self):
        """Order is a property of the finding, never of dict iteration (#457)."""
        return (self.rule, self.ref or '', self.block or '', self.message)

    def to_dict(self):
        d = {'rule': self.rule, 'severity': self.severity,
             'message': self.message}
        if self.ref:
            d['ref'] = self.ref
        if self.block:
            d['block'] = self.block
        if self.measured:
            d['measured'] = self.measured
        if self.expected:
            d['expected'] = self.expected
        return d


@dataclass(frozen=True)
class Zone:
    name: str
    rect: Optional[Tuple[float, float, float, float]] = None
    side: Optional[str] = None
    group: Optional[str] = None
    refs: Tuple[str, ...] = ()
    exclusive: bool = False
    tolerance_mm: Optional[float] = None
    note: str = ''


@dataclass(frozen=True)
class Intent:
    schema: int
    kind: str
    board: str
    units: str
    envelope: Dict[str, object]
    defaults: Dict[str, object]
    blocks: Tuple[Zone, ...]
    keepouts: Tuple[Dict[str, object], ...]
    edge_connectors: Tuple[Dict[str, object], ...]
    decaps: Dict[str, object]
    must_lock: Tuple[str, ...]
    legality_budget: Dict[str, object]
    health: Dict[str, object]
    severity: Dict[str, str]
    source_path: str = ''

    def severity_of(self, rule: str, default: str = ERROR) -> str:
        return self.severity.get(rule, default)

    def zone_tolerance(self, zone: Zone) -> float:
        if zone.tolerance_mm is not None:
            return float(zone.tolerance_mm)
        return float(self.defaults.get('zone_tolerance_mm',
                                       DEFAULT_ZONE_TOLERANCE_MM))


# --------------------------------------------------------------------------
# loading and board-independent validation
# --------------------------------------------------------------------------

def _rect(value, where: str) -> Tuple[float, float, float, float]:
    if (not isinstance(value, (list, tuple)) or len(value) != 4
            or not all(isinstance(v, (int, float)) and not isinstance(v, bool)
                       for v in value)):
        raise IntentError(f"{where}: expected a rect [x0, y0, x1, y1] of four "
                          f"numbers, got {value!r}")
    x0, y0, x1, y1 = (float(v) for v in value)
    return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))


def _str_tuple(value, where: str) -> Tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        raise IntentError(f"{where}: expected a list of strings, got a bare "
                          f"string {value!r} (wrap it in a list)")
    if not isinstance(value, (list, tuple)) or not all(
            isinstance(v, str) for v in value):
        raise IntentError(f"{where}: expected a list of strings, got {value!r}")
    return tuple(value)


def load_intent(path: str) -> Intent:
    """Read and structurally validate an intent file.

    Raises IntentError on anything unreadable. Board-relative checks (does this
    block resolve, is this zone inside the envelope) belong to `grade`.
    """
    try:
        with open(path, encoding='utf-8') as fh:
            raw = json.load(fh)
    except (OSError, ValueError) as exc:
        raise IntentError(f"{path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise IntentError(f"{path}: expected a JSON object at the top level")
    return intent_from_dict(raw, source_path=path)


def intent_from_dict(raw: Dict, source_path: str = '') -> Intent:
    unknown = sorted(set(raw) - _TOP_LEVEL_KEYS)
    if unknown:
        raise IntentError(
            f"unknown top-level key(s): {', '.join(unknown)}. Known keys: "
            f"{', '.join(sorted(_TOP_LEVEL_KEYS))}")

    schema = raw.get('schema')
    if schema != SCHEMA_VERSION:
        raise IntentError(
            f"schema {schema!r}: this build reads schema {SCHEMA_VERSION}")
    kind = raw.get('kind')
    if kind != KIND:
        # A round sidecar or a lock-advisor dump handed in by mistake reads as
        # JSON and would otherwise grade as an empty, clean intent.
        raise IntentError(
            f"kind {kind!r}: expected {KIND!r}. This does not look like a "
            f"floorplan intent file")
    units = raw.get('units', 'mm')
    if units != 'mm':
        raise IntentError(f"units {units!r}: only 'mm' is supported")

    envelope = raw.get('envelope') or {}
    if not isinstance(envelope, dict):
        raise IntentError("envelope: expected an object")
    if 'rect' in envelope and envelope['rect'] is not None:
        envelope = dict(envelope)
        envelope['rect'] = _rect(envelope['rect'], 'envelope.rect')

    blocks: List[Zone] = []
    seen_names = set()
    for i, b in enumerate(raw.get('blocks') or []):
        if not isinstance(b, dict):
            raise IntentError(f"blocks[{i}]: expected an object")
        bad = sorted(set(b) - _BLOCK_KEYS)
        if bad:
            raise IntentError(f"blocks[{i}]: unknown key(s) {', '.join(bad)}")
        name = b.get('name') or f"block{i}"
        if name in seen_names:
            raise IntentError(f"blocks[{i}]: duplicate block name {name!r}")
        seen_names.add(name)
        side = b.get('side')
        if side is not None and side not in ('F', 'B'):
            raise IntentError(
                f"blocks[{i}] ({name}): side {side!r}, expected 'F' or 'B'")
        zone_rect = b.get('zone')
        blocks.append(Zone(
            name=name,
            rect=_rect(zone_rect, f"blocks[{i}].zone") if zone_rect else None,
            side=side,
            group=b.get('group'),
            refs=_str_tuple(b.get('refs'), f"blocks[{i}].refs"),
            exclusive=bool(b.get('exclusive', False)),
            tolerance_mm=b.get('tolerance_mm'),
            note=b.get('note', '') or '',
        ))
        if not blocks[-1].refs and not blocks[-1].group:
            raise IntentError(
                f"blocks[{i}] ({name}): needs `refs` (reference globs) or "
                f"`group` (a --group-by block name). Prefer `refs`: sheet group "
                f"keys are uuid paths you cannot author without listing them")

    keepouts = []
    for i, k in enumerate(raw.get('keepouts') or []):
        if not isinstance(k, dict):
            raise IntentError(f"keepouts[{i}]: expected an object")
        k = dict(k)
        k.setdefault('name', f"keepout{i}")
        if 'rect' in k and k['rect'] is not None:
            k['rect'] = _rect(k['rect'], f"keepouts[{i}].rect")
        elif 'circle' in k and k['circle'] is not None:
            c = k['circle']
            if (not isinstance(c, (list, tuple)) or len(c) != 3
                    or not all(isinstance(v, (int, float)) for v in c)):
                raise IntentError(
                    f"keepouts[{i}].circle: expected [x, y, radius]")
            k['circle'] = tuple(float(v) for v in c)
        else:
            raise IntentError(f"keepouts[{i}] ({k['name']}): needs `rect` or "
                              f"`circle`")
        k['sides'] = tuple(k.get('sides') or ('F', 'B'))
        k['allow'] = _str_tuple(k.get('allow'), f"keepouts[{i}].allow")
        keepouts.append(k)

    conns = []
    for i, c in enumerate(raw.get('edge_connectors') or []):
        if not isinstance(c, dict) or not c.get('ref'):
            raise IntentError(f"edge_connectors[{i}]: expected an object with "
                              f"a `ref`")
        c = dict(c)
        edge = c.get('edge')
        if edge is not None and edge not in _EDGES:
            raise IntentError(
                f"edge_connectors[{i}] ({c['ref']}): edge {edge!r}, expected "
                f"one of {', '.join(_EDGES)}")
        oh = c.get('overhang_mm')
        if oh is not None and not isinstance(oh, dict):
            raise IntentError(f"edge_connectors[{i}] ({c['ref']}): "
                              f"overhang_mm expects {{'min': .., 'max': ..}}")
        conns.append(c)

    severity = raw.get('severity') or {}
    if not isinstance(severity, dict) or any(
            v not in (ERROR, WARN) for v in severity.values()):
        raise IntentError(
            f"severity: expected {{rule: 'error'|'warn'}}, got {severity!r}")

    budget = raw.get('legality_budget') or {}
    if not isinstance(budget, dict):
        raise IntentError("legality_budget: expected an object")
    if 'oob_area' in budget:
        # Refused loudly rather than ignored, because it is the ONE legality
        # number that lies about cutouts. `out_of_board_area` measures against
        # the rectangular usable inset only -- its own docstring calls itself "a
        # lower bound on a notched one" -- so a part sitting ENTIRELY inside a
        # milled slot scores oob_count=1, oob_amount>0 and oob_area=0.0. A
        # budget on it would grade a part in a hole as clean.
        raise IntentError(
            "legality_budget.oob_area: not gateable. out_of_board_area is "
            "measured against the bounding-box inset, so a part sitting inside "
            "a CUTOUT scores 0.0 area and would grade clean. Use oob_count or "
            "oob_amount, which both see the real Edge.Cuts rings")
    unknown_budget = sorted(set(budget) - {'overlap_area', 'oob_count',
                                           'oob_amount'})
    if unknown_budget:
        raise IntentError(
            f"legality_budget: unknown key(s) {', '.join(unknown_budget)}. "
            f"Known: overlap_area, oob_count, oob_amount")

    return Intent(
        schema=schema, kind=kind, board=raw.get('board', '') or '',
        units=units, envelope=envelope,
        defaults=raw.get('defaults') or {},
        blocks=tuple(blocks), keepouts=tuple(keepouts),
        edge_connectors=tuple(conns),
        decaps=raw.get('decaps') or {},
        must_lock=_str_tuple(raw.get('must_lock'), 'must_lock'),
        legality_budget=raw.get('legality_budget') or {},
        health=raw.get('health') or {},
        severity={str(k): str(v) for k, v in severity.items()},
        source_path=source_path,
    )


def validate_intent(intent: Intent) -> List[Violation]:
    """Checks that need no board: does the intent contradict itself.

    Kept separate from `grade` so an intent can be reviewed before a board
    exists, and so a self-contradictory intent is reported as such rather than
    as a pile of board violations.
    """
    out: List[Violation] = []
    env = intent.envelope.get('rect')

    for z in intent.blocks:
        if z.rect is None:
            continue
        if env is not None and not _rect_contains(env, z.rect):
            out.append(Violation(
                rule='intent_zone_outside_envelope',
                severity=intent.severity_of('intent_zone_outside_envelope'),
                block=z.name,
                message=(f"zone {z.name!r} {_fmt_rect(z.rect)} is not inside "
                         f"the envelope {_fmt_rect(env)}"),
                measured={'zone': list(z.rect)},
                expected={'envelope': list(env)}))

    # Two zones overlapping on a shared side is an intent that cannot be
    # satisfied, whatever the board does.
    for i, a in enumerate(intent.blocks):
        for b in intent.blocks[i + 1:]:
            if a.rect is None or b.rect is None:
                continue
            if a.side and b.side and a.side != b.side:
                continue
            area = legality.rect_overlap_area(a.rect, b.rect)
            if area > legality.EPS:
                out.append(Violation(
                    rule='intent_zone_overlap',
                    severity=intent.severity_of('intent_zone_overlap'),
                    block=a.name,
                    message=(f"zones {a.name!r} and {b.name!r} overlap by "
                             f"{area:.2f}mm2 on the same side; no placement can "
                             f"satisfy both"),
                    measured={'overlap_area_mm2': round(area, 4),
                              'other_block': b.name},
                    expected={'overlap_area_mm2': 0.0}))
    return out


# --------------------------------------------------------------------------
# geometry helpers
# --------------------------------------------------------------------------

def _rect_contains(outer, inner, tol: float = 0.0) -> bool:
    return (inner[0] >= outer[0] - tol and inner[1] >= outer[1] - tol
            and inner[2] <= outer[2] + tol and inner[3] <= outer[3] + tol)


def _rect_escape(outer, inner) -> Tuple[float, str]:
    """How far `inner` sticks out of `outer`, and on which side. 0.0 when in."""
    worst, axis = 0.0, ''
    for amount, name in ((outer[0] - inner[0], 'west'),
                         (outer[1] - inner[1], 'north'),
                         (inner[2] - outer[2], 'east'),
                         (inner[3] - outer[3], 'south')):
        if amount > worst:
            worst, axis = amount, name
    return worst, axis


def _fmt_rect(r) -> str:
    return f"[{r[0]:.2f}, {r[1]:.2f}, {r[2]:.2f}, {r[3]:.2f}]"


def _ceil4(v: float) -> float:
    """Round UP to 4 decimals. See the legality_budget note in emit_intent."""
    return math.ceil(v * 1e4 - 1e-9) / 1e4


def _rects_touch(a, b) -> bool:
    return legality.rect_overlap_area(a, b) > legality.EPS


def _circle_hits_rect(cx, cy, radius, rect) -> bool:
    nx = min(max(cx, rect[0]), rect[2])
    ny = min(max(cy, rect[1]), rect[3])
    return math.hypot(nx - cx, ny - cy) < radius


# --------------------------------------------------------------------------
# the board's own outline, checked before anything is graded against it
# --------------------------------------------------------------------------

def _is_simple_rectangle(segments) -> bool:
    """The parser's own rectangle short-circuit, reproduced.

    `extract_board_contours` returns ([], []) for THREE different reasons:
    fewer than 3 segments, a simple axis-aligned 4-segment rectangle (where the
    bounding box IS the outline, exactly), and a chaining failure. Only the
    third is a defect, and `board_outlines` alone cannot tell them apart -- so
    the same test is applied here rather than guessed at.
    """
    if len(segments) != 4:
        return False
    vertices = set()
    for seg in segments:
        vertices.add((round(seg[0][0], 3), round(seg[0][1], 3)))
        vertices.add((round(seg[1][0], 3), round(seg[1][1], 3)))
    if len(vertices) != 4:
        return False
    return all(abs(s[0][0] - s[1][0]) < 0.001 or abs(s[0][1] - s[1][1]) < 0.001
               for s in segments)


def outline_state(pcb_data, pcb_file: str = '') -> Dict[str, object]:
    """What the parser made of Edge.Cuts, and whether it can be trusted.

    A broken outline degrades SILENTLY today: unclosable segment groups are
    dropped, `extract_board_contours` returns ([], []), `BoardOutlineGate.active`
    goes False, and every containment test quietly falls back to the bounding
    box. No exception, no warning. A grader that inherits that fallback reports
    a clean board because it stopped checking -- the single worst thing this
    file could do.

    So the envelope is checked before anything is graded against it:

      * Edge.Cuts geometry exists but `board_bounds` is None. That is #550:
        `extract_board_bounds` reads neither board-level `gr_circle` nor
        `gr_curve`, so a round board reads as having no outline at all on the
        text path while the pcbnew path sees it fine.
      * A parsed ring reaches OUTSIDE `board_bounds` -- the same bug, curve
        flavour, where the bbox scan misses the bulge.
      * Segments exist, they are not a simple rectangle, and no ring chained.

    None of these are graded around; the run refuses and says which.
    """
    bi = pcb_data.board_info
    outlines = list(getattr(bi, 'board_outlines', None) or [])
    if not outlines and getattr(bi, 'board_outline', None):
        outlines = [bi.board_outline]
    cutouts = list(getattr(bi, 'board_cutouts', None) or [])
    contours = list(getattr(bi, 'board_edge_contours', None) or [])
    bounds = bi.board_bounds

    segments = []
    if pcb_file and os.path.exists(pcb_file):
        try:
            from kicad_parser import _collect_edge_cuts_segments
            with open(pcb_file, encoding='utf-8') as fh:
                segments = _collect_edge_cuts_segments(fh.read())
        except (OSError, ImportError, ValueError):
            segments = []
    rectangle = _is_simple_rectangle(segments)

    problems: List[str] = []
    ring_pts = [p for ring in outlines for p in ring]
    if bounds is None:
        if ring_pts or segments:
            problems.append(
                f"the board has {len(segments)} Edge.Cuts segment(s) but "
                f"board_bounds is None (#550: extract_board_bounds reads "
                f"neither gr_circle nor gr_curve, so a round or curve-cornered "
                f"board reads as having no outline)")
        else:
            problems.append("the board has no Edge.Cuts outline")
    elif ring_pts:
        out = max(max(bounds[0] - x, x - bounds[2], bounds[1] - y,
                      y - bounds[3]) for x, y in ring_pts)
        if out > 1e-3:
            problems.append(
                f"a parsed outline ring reaches {out:.3f}mm outside "
                f"board_bounds (#550: the bbox scan missed a curve or circle)")
    if not outlines and len(segments) >= 3 and not rectangle:
        problems.append(
            f"{len(segments)} Edge.Cuts segments chained into no closed ring, "
            f"and they are not a simple rectangle; containment would silently "
            f"fall back to the bounding box")

    return {'bounds': tuple(round(v, 6) for v in bounds) if bounds else None,
            'outlines': len(outlines), 'cutouts': len(cutouts),
            'edge_contours': len(contours),
            'edge_segments': len(segments),
            'simple_rectangle': rectangle,
            'problems': problems,
            'trustworthy': not problems}


# --------------------------------------------------------------------------
# block resolution
# --------------------------------------------------------------------------

def resolve_blocks(intent: Intent, pcb_data, group_sources: Sequence[str] = ()
                   ) -> Tuple[Dict[str, List[str]], List[Violation]]:
    """{block name: sorted refs} plus a violation per block that resolved empty.

    A block is `refs` globs, `group` membership, or both unioned. `group` is
    matched against the raw derive_groups key AND its `short_name` form, because
    `short_name` is what `--list-groups` prints and therefore what anyone would
    copy into an intent file.
    """
    refs_all = sorted(pcb_data.footprints)
    derived: Dict[str, List[str]] = {}
    if group_sources and any(z.group for z in intent.blocks):
        derived = groups_mod.derive_groups(pcb_data, tuple(group_sources))
    by_short = {}
    for key, members in derived.items():
        by_short.setdefault(groups_mod.short_name(key), []).extend(members)

    out: Dict[str, List[str]] = {}
    problems: List[Violation] = []
    for z in intent.blocks:
        members = set()
        for pattern in z.refs:
            members.update(fnmatch.filter(refs_all, pattern))
        if z.group:
            found = derived.get(z.group)
            if found is None:
                found = by_short.get(z.group)
            if found is None:
                problems.append(Violation(
                    rule='block_unresolved', block=z.name,
                    severity=intent.severity_of('block_unresolved'),
                    message=(f"block {z.name!r}: group {z.group!r} does not "
                             f"exist on this board. Available: "
                             f"{', '.join(sorted(by_short)[:6]) or 'none'}"
                             f"{' ...' if len(by_short) > 6 else ''}. Derive "
                             f"them with --group-by and --list-groups first"),
                    measured={'group': z.group,
                              'available': sorted(by_short)[:12]}))
            else:
                members.update(found)
        out[z.name] = sorted(members)
        if not members:
            problems.append(Violation(
                rule='block_unresolved', block=z.name,
                severity=intent.severity_of('block_unresolved'),
                message=(f"block {z.name!r} matched no footprint on this board "
                         f"(refs {list(z.refs)!r}). A block that resolves to "
                         f"nothing grades clean, so this is an error rather "
                         f"than an empty block"),
                measured={'refs': list(z.refs), 'matched': 0}))
    return out, problems


# --------------------------------------------------------------------------
# rules
# --------------------------------------------------------------------------

class _Ctx:
    """What every rule is handed. Built once; rules never re-derive geometry."""

    def __init__(self, intent, pcb_data, pcb_file, state, blocks, locked,
                 outline):
        self.intent = intent
        self.pcb = pcb_data
        self.pcb_file = pcb_file
        self.state = state
        self.blocks = blocks
        self.locked = locked
        self.outline = outline
        self.outline_bounds = outline.get('bounds')
        self.parts = {p.ref: p for p in state.graded_parts()}
        self.gate = state.edge_gate
        self.legality = state.legality_metrics()
        self.envelope = intent.envelope.get('rect')
        self.owner: Dict[str, str] = {}
        for name, refs in sorted(blocks.items()):
            for r in refs:
                self.owner.setdefault(r, name)

    def sev(self, rule: str) -> str:
        return self.intent.severity_of(rule)


def _nearest_edge(rect, bounds) -> str:
    d = {'west': rect[0] - bounds[0], 'north': rect[1] - bounds[1],
         'east': bounds[2] - rect[2], 'south': bounds[3] - rect[3]}
    return min(d, key=lambda k: d[k])


def rule_envelope(ctx) -> Iterator[Violation]:
    """The declared envelope must BE the board outline, not a wish about it.

    A mismatch is never a licence to resize. It means the intent was written
    against a different board or revision, and grading parts against the wrong
    rectangle reports violations that are really the author bookkeeping.
    """
    env = ctx.envelope
    bounds = ctx.outline_bounds
    if env is None or bounds is None:
        return
    tol = float(ctx.intent.envelope.get('tolerance_mm',
                                        DEFAULT_ENVELOPE_TOLERANCE_MM))
    delta = max(abs(a - b) for a, b in zip(env, bounds))
    if delta > tol:
        yield Violation(
            rule='envelope', severity=ctx.sev('envelope'),
            message=(f"the intent envelope {_fmt_rect(env)} is not this board "
                     f"outline {_fmt_rect(bounds)} (worst corner {delta:.2f}mm "
                     f"> {tol}mm). The outline is fixed: correct the intent, "
                     f"do not resize the board"),
            measured={'board_bounds': [round(v, 4) for v in bounds],
                      'worst_corner_mm': round(delta, 4)},
            expected={'envelope': list(env), 'tolerance_mm': tol})


def rule_zone_containment(ctx) -> Iterator[Violation]:
    for z in ctx.intent.blocks:
        if z.rect is None:
            continue
        tol = ctx.intent.zone_tolerance(z)
        for ref in ctx.blocks.get(z.name, ()):
            part = ctx.parts.get(ref)
            if part is None:
                continue
            out, axis = _rect_escape(z.rect, part.rect)
            if out > tol:
                yield Violation(
                    rule='zone_containment',
                    severity=ctx.sev('zone_containment'), ref=ref, block=z.name,
                    message=(f"{ref} courtyard extends {out:.2f}mm past the "
                             f"{axis} edge of block {z.name!r}"),
                    measured={'rect': [round(v, 4) for v in part.rect],
                              'outside_mm': round(out, 4), 'axis': axis},
                    expected={'zone': list(z.rect), 'tolerance_mm': tol})


def rule_zone_side(ctx) -> Iterator[Violation]:
    for z in ctx.intent.blocks:
        if not z.side:
            continue
        for ref in ctx.blocks.get(z.name, ()):
            part = ctx.parts.get(ref)
            if part is not None and part.side != z.side:
                yield Violation(
                    rule='zone_side', severity=ctx.sev('zone_side'),
                    ref=ref, block=z.name,
                    message=(f"{ref} is on side {part.side} but block "
                             f"{z.name!r} declares side {z.side}"),
                    measured={'side': part.side}, expected={'side': z.side})


def rule_zone_exclusive(ctx) -> Iterator[Violation]:
    """An exclusive zone is real estate reserved for its block, so a stranger
    inside it is the finding. This is what makes "keep this area clear for the
    RF section" checkable rather than aspirational."""
    for z in ctx.intent.blocks:
        if z.rect is None or not z.exclusive:
            continue
        members = set(ctx.blocks.get(z.name, ()))
        for ref, part in sorted(ctx.parts.items()):
            if ref in members:
                continue
            if z.side and part.side != z.side:
                continue
            area = legality.rect_overlap_area(part.rect, z.rect)
            if area > legality.EPS:
                yield Violation(
                    rule='zone_exclusive', severity=ctx.sev('zone_exclusive'),
                    ref=ref, block=z.name,
                    message=(f"{ref} (not a member of {z.name!r}) intrudes "
                             f"{area:.2f}mm2 into its exclusive zone"),
                    measured={'overlap_area_mm2': round(area, 4),
                              'owner': ctx.owner.get(ref)},
                    expected={'zone': list(z.rect), 'overlap_area_mm2': 0.0})


def rule_keepout(ctx) -> Iterator[Violation]:
    for k in ctx.intent.keepouts:
        name = k['name']
        allow = k.get('allow') or ()
        sides = set(k.get('sides') or ('F', 'B'))
        for ref, part in sorted(ctx.parts.items()):
            if any(fnmatch.fnmatch(ref, pat) for pat in allow):
                continue
            # A through-hole part occupies BOTH faces: its leads pass through
            # the keep-out even when its body sits on the other side.
            if not (set(part.sides) & sides):
                continue
            rects = [part.rect]
            if part.tht_rect is not None:
                rects.append(part.tht_rect)
            hit = 0.0
            for r in rects:
                if k.get('rect') is not None:
                    hit = max(hit, legality.rect_overlap_area(r, k['rect']))
                else:
                    cx, cy, radius = k['circle']
                    if _circle_hits_rect(cx, cy, radius, r):
                        hit = max(hit, 1.0)
            if hit > legality.EPS:
                shape = (_fmt_rect(k['rect']) if k.get('rect') is not None
                         else f"circle {k['circle']}")
                yield Violation(
                    rule='keepout', severity=ctx.sev('keepout'), ref=ref,
                    message=(f"{ref} ({part.side}) is inside keep-out "
                             f"{name!r} {shape}"),
                    measured={'keepout': name, 'side': part.side,
                              'sides_occupied': sorted(part.sides)},
                    expected={'allow': list(allow)})


def rule_edge_connector(ctx) -> Iterator[Violation]:
    """A connector that must reach the board edge: a card edge, a USB shell, a
    HAT header. This is the one class of part whose courtyard leaving the
    outline is CORRECT, so declaring it is also what stops `oob_count` from
    reporting it as a defect forever."""
    for c in ctx.intent.edge_connectors:
        ref = c['ref']
        part = ctx.parts.get(ref)
        if part is None:
            yield Violation(
                rule='edge_connector', severity=ctx.sev('edge_connector'),
                ref=ref, message=f"edge connector {ref} is not on this board",
                measured={'found': False})
            continue
        amount = ctx.gate.rect_outside_amount(part.rect)
        lim = c.get('overhang_mm') or {}
        lo = float(lim.get('min', 0.0))
        hi = lim.get('max')
        if amount < lo - legality.EPS:
            yield Violation(
                rule='edge_connector', severity=ctx.sev('edge_connector'),
                ref=ref, message=(f"{ref} overhangs the outline by "
                                  f"{amount:.2f}mm, under the declared minimum "
                                  f"{lo:.2f}mm"),
                measured={'overhang_mm': round(amount, 4)},
                expected={'min': lo, 'max': hi})
        elif hi is not None and amount > float(hi) + legality.EPS:
            yield Violation(
                rule='edge_connector', severity=ctx.sev('edge_connector'),
                ref=ref, message=(f"{ref} overhangs the outline by "
                                  f"{amount:.2f}mm, past the declared maximum "
                                  f"{float(hi):.2f}mm"),
                measured={'overhang_mm': round(amount, 4)},
                expected={'min': lo, 'max': float(hi)})
        edge = c.get('edge')
        if edge and ctx.outline_bounds:
            actual = _nearest_edge(part.rect, ctx.outline_bounds)
            if actual != edge:
                yield Violation(
                    rule='edge_connector', severity=ctx.sev('edge_connector'),
                    ref=ref, message=(f"{ref} sits nearest the {actual} edge "
                                      f"but is declared on the {edge} edge"),
                    measured={'edge': actual}, expected={'edge': edge})


def rule_decap_distance(ctx) -> Iterator[Violation]:
    """Decoupling caps within reach of the IC they decouple.

    The tether is `groups.decap_tethers` -- the same nearest-IC-sharing-a-net
    rule the placement grouper uses, with the distance it already measures. A
    second implementation here would grade the reimplementation.
    """
    spec = ctx.intent.decaps or {}
    limit = spec.get('max_distance_mm')
    if limit is None:
        return
    limit = float(limit)
    exempt = tuple(spec.get('exempt') or ())
    radius = float(spec.get('search_radius_mm', groups_mod.DECAP_RADIUS_MM))
    tethers = groups_mod.decap_tethers(ctx.pcb, radius=radius)
    for ic in sorted(tethers):
        for cap, dist in tethers[ic]:
            if any(fnmatch.fnmatch(cap, pat) for pat in exempt):
                continue
            if dist > limit + legality.EPS:
                yield Violation(
                    rule='decap_distance', severity=ctx.sev('decap_distance'),
                    ref=cap, block=ctx.owner.get(cap),
                    message=(f"{cap} is {dist:.2f}mm from {ic}, the IC it "
                             f"decouples (limit {limit:.2f}mm)"),
                    measured={'distance_mm': round(dist, 4), 'ic': ic},
                    expected={'max_distance_mm': limit})


def rule_must_lock(ctx) -> Iterator[Violation]:
    """Parts the intent says must not move, that the FILE does not pin.

    Unlocked is not a violation of physics, it is a violation of the plan: a
    mounting hole with no net has no airwire at all, so nothing but the halo
    term decides where the optimizer slides it.
    """
    refs = sorted(ctx.pcb.footprints)
    for pattern in ctx.intent.must_lock:
        matched = fnmatch.filter(refs, pattern)
        if not matched:
            yield Violation(
                rule='must_lock', severity=ctx.sev('must_lock'),
                message=f"must_lock pattern {pattern!r} matched no footprint",
                measured={'pattern': pattern, 'matched': 0})
            continue
        for ref in matched:
            if ref not in ctx.locked:
                yield Violation(
                    rule='must_lock', severity=ctx.sev('must_lock'), ref=ref,
                    message=(f"{ref} is declared must_lock but is not locked "
                             f"in the board file"),
                    measured={'locked': False, 'pattern': pattern},
                    expected={'locked': True})


def rule_legality(ctx) -> Iterator[Violation]:
    """Courtyard overlap and off-board parts, against a declared budget.

    `oob_area` is deliberately NOT gateable. `out_of_board_area` measures
    against the rectangular usable inset only -- its own docstring calls it "a
    lower bound on a notched one" -- so a part sitting ENTIRELY inside a cutout
    scores count=1, amount>0, area=0.0. Gating on area would grade a part in a
    slot as clean. Count and amount both see the real rings.
    """
    budget = ctx.intent.legality_budget or {}
    for key, label in (('overlap_area', 'courtyard overlap area (mm2)'),
                       ('oob_count', 'parts leaving the board outline'),
                       ('oob_amount', 'total off-board overhang (mm)')):
        if key not in budget:
            continue
        got = ctx.legality.get(key)
        if got is None:
            continue
        lim = float(budget[key])
        if got > lim + legality.EPS:
            yield Violation(
                rule='legality', severity=ctx.sev('legality'),
                message=(f"{label}: {got:.3f} exceeds the declared budget "
                         f"{lim:.3f}"),
                measured={key: round(float(got), 4)}, expected={key: lim})


RULES = (
    ('envelope', rule_envelope),
    ('zone_containment', rule_zone_containment),
    ('zone_side', rule_zone_side),
    ('zone_exclusive', rule_zone_exclusive),
    ('keepout', rule_keepout),
    ('edge_connector', rule_edge_connector),
    ('decap_distance', rule_decap_distance),
    ('must_lock', rule_must_lock),
    ('legality', rule_legality),
)

# Why a rule did not run. Reported so that "0 violations" and "0 rules ran"
# cannot look the same to a reader or to a machine.
_SKIP_REASON = {
    'envelope': 'the intent declares no envelope.rect',
    'zone_containment': 'no block declares a zone',
    'zone_side': 'no block declares a side',
    'zone_exclusive': 'no block is marked exclusive',
    'keepout': 'the intent declares no keepouts',
    'edge_connector': 'the intent declares no edge_connectors',
    'decap_distance': 'the intent declares no decaps.max_distance_mm',
    'must_lock': 'the intent declares no must_lock patterns',
    'legality': 'the intent declares no legality_budget',
}


def _wants(intent: Intent, rule: str) -> bool:
    if rule == 'envelope':
        return intent.envelope.get('rect') is not None
    if rule == 'zone_containment':
        return any(z.rect is not None for z in intent.blocks)
    if rule == 'zone_side':
        return any(z.side for z in intent.blocks)
    if rule == 'zone_exclusive':
        return any(z.exclusive and z.rect is not None for z in intent.blocks)
    if rule == 'keepout':
        return bool(intent.keepouts)
    if rule == 'edge_connector':
        return bool(intent.edge_connectors)
    if rule == 'decap_distance':
        return (intent.decaps or {}).get('max_distance_mm') is not None
    if rule == 'must_lock':
        return bool(intent.must_lock)
    if rule == 'legality':
        return bool(intent.legality_budget)
    return True


# --------------------------------------------------------------------------
# grade
# --------------------------------------------------------------------------

@dataclass
class GradeResult:
    intent: Intent
    board: str
    violations: List[Violation]
    blocks: Dict[str, List[str]]
    legality: Dict[str, object]
    outline: Dict[str, object]
    state: Dict[str, object]
    rules_run: Tuple[str, ...]
    rules_skipped: Dict[str, str]
    n_footprints: int

    @property
    def errors(self) -> List[Violation]:
        return [v for v in self.violations if v.severity == ERROR]

    @property
    def warnings(self) -> List[Violation]:
        return [v for v in self.violations if v.severity != ERROR]

    @property
    def passed(self) -> bool:
        return not self.errors


class UntrustworthyOutline(ValueError):
    """The board outline did not parse into something a grader may rely on.

    Raised rather than graded around, because the fallback is INVISIBLE: with no
    rings, `BoardOutlineGate.active` is False and every containment test quietly
    degrades to the bounding box. A grader that inherits that reports a clean
    board because it stopped checking.
    """

    def __init__(self, problems):
        self.problems = list(problems)
        super().__init__('; '.join(self.problems))


def grade(intent: Intent, pcb_data, pcb_file: str, *,
          group_sources: Sequence[str] = (), clearance: Optional[float] = None,
          board_edge_clearance: Optional[float] = None) -> GradeResult:
    """Measure a board against its declared floorplan intent."""
    from .quench import QuenchState
    from . import placement_state
    import routing_defaults as defaults

    outline = outline_state(pcb_data, pcb_file)
    if not outline['trustworthy']:
        raise UntrustworthyOutline(outline['problems'])

    qargs = dict(clearance=clearance if clearance is not None
                 else defaults.CLEARANCE,
                 board_edge_clearance=(board_edge_clearance
                                       if board_edge_clearance is not None
                                       else 0.55),
                 crossing_penalty=10.0, halo_base=0.5, halo_coef=0.25,
                 halo_weight=2.0, edge_halo=2.0, edge_weight=2.0,
                 grid_step=defaults.GRID_STEP, length_weight=1.0)
    state = QuenchState(pcb_data, pcb_file, **qargs)

    try:
        from .parser import extract_locked_refs
        locked = extract_locked_refs(pcb_file) if pcb_file else set()
    except (OSError, ValueError):
        locked = set()

    blocks, block_problems = resolve_blocks(intent, pcb_data, group_sources)
    ctx = _Ctx(intent, pcb_data, pcb_file, state, blocks, locked, outline)

    violations = list(validate_intent(intent)) + list(block_problems)
    ran: List[str] = []
    skipped: Dict[str, str] = {}
    for name, fn in RULES:
        if not _wants(intent, name):
            skipped[name] = _SKIP_REASON.get(name, 'not requested')
            continue
        ran.append(name)
        violations.extend(fn(ctx))
    violations.sort(key=lambda v: v.sort_key())

    st = placement_state.assess_placement(pcb_data, pcb_file)
    return GradeResult(
        intent=intent, board=pcb_file, violations=violations, blocks=blocks,
        legality={k: (round(float(v), 4) if isinstance(v, float) else v)
                  for k, v in ctx.legality.items()},
        outline=outline,
        state={'unplaced': st.unplaced,
               'partially_unplaced': st.partially_unplaced,
               'has_copper': st.has_copper,
               'n_footprints': st.n_footprints,
               'distinct_positions': st.distinct_positions,
               'duplicate_fraction': round(st.duplicate_fraction, 4),
               'spread_ratio': (None if st.spread_ratio is None
                                else round(st.spread_ratio, 4)),
               'outside_fraction': (None if st.outside_fraction is None
                                    else round(st.outside_fraction, 4)),
               'stacked_refs': list(st.stacked_refs),
               'segments': st.segments, 'vias': st.vias},
        rules_run=tuple(ran), rules_skipped=skipped,
        n_footprints=len(pcb_data.footprints))


# --------------------------------------------------------------------------
# emit: a starter intent, derived from the board
# --------------------------------------------------------------------------

def emit_intent(pcb_data, pcb_file: str, *,
                group_sources: Sequence[str] = ('kicad', 'sheet'),
                zone_pad_mm: float = 1.0) -> Dict:
    """A starter intent READ OFF the board, for a human or a model to edit.

    Everything here describes what the board already is. The envelope is
    `board_bounds` verbatim -- never a rounded or convenient rectangle -- and
    the cutouts are emitted as read-only `context` so an editor can see what the
    parts have to avoid without being able to mistake it for something to
    change. Nothing in this module writes Edge.Cuts.

    The emitted intent grades CLEAN by construction. That is the point: it is a
    baseline to tighten, and the round trip (emit then grade) is what proves the
    rules are wired to real geometry rather than silently skipping.
    """
    from .quench import QuenchState
    import routing_defaults as defaults

    outline = outline_state(pcb_data, pcb_file)
    if not outline['trustworthy']:
        raise UntrustworthyOutline(outline['problems'])

    state = QuenchState(pcb_data, pcb_file, clearance=defaults.CLEARANCE,
                        board_edge_clearance=0.55, crossing_penalty=10.0,
                        halo_base=0.5, halo_coef=0.25, halo_weight=2.0,
                        edge_halo=2.0, edge_weight=2.0,
                        grid_step=defaults.GRID_STEP, length_weight=1.0)
    parts = {p.ref: p for p in state.graded_parts()}
    bounds = outline['bounds']

    derived = (groups_mod.derive_groups(pcb_data, tuple(group_sources))
               if group_sources else {})

    # Candidate zones: each block's member bbox, padded, then CLAMPED to the
    # envelope. Without the clamp a block touching the board edge gets a zone
    # that leaves the outline, which the emitted intent would then flag against
    # itself.
    cand = {}
    for key in sorted(derived):
        members = [r for r in derived[key] if r in parts]
        if len(members) < 2:
            continue
        rects = [parts[r].rect for r in members]
        cand[key] = (members, (
            max(bounds[0], min(r[0] for r in rects) - zone_pad_mm),
            max(bounds[1], min(r[1] for r in rects) - zone_pad_mm),
            min(bounds[2], max(r[2] for r in rects) + zone_pad_mm),
            min(bounds[3], max(r[3] for r in rects) + zone_pad_mm)))

    # A ZONE is a spatial claim, and most derived blocks cannot make one. A
    # schematic sheet is a FUNCTIONAL grouping: its members are scattered across
    # the board, so its bounding box swallows most of the others -- on ulx3s all
    # 10 sheet bboxes mutually overlap, up to 4508mm2. Emitting those as zones
    # produces an intent no placement could satisfy, and it would be the
    # emitter, not the board, that was wrong.
    #
    # So a zone is emitted only where it is DISJOINT from every other kept zone,
    # tightest first. Membership is still emitted for the rest -- it is what
    # zone_side, must_lock and the reader all want -- with the omission stated
    # rather than left as a silent absence.
    kept: List[Tuple[float, float, float, float]] = []
    zoned = set()
    for key in sorted(cand, key=lambda k: (legality.rect_area(cand[k][1]), k)):
        rect = cand[key][1]
        if any(legality.rect_overlap_area(rect, other) > legality.EPS
               for other in kept):
            continue
        kept.append(rect)
        zoned.add(key)

    blocks = []
    for key in sorted(cand):
        members, rect = cand[key]
        sides = {parts[r].side for r in members}
        entry = {
            'name': groups_mod.short_name(key),
            'group': groups_mod.short_name(key),
            'refs': sorted(members),
        }
        if key in zoned:
            entry['zone'] = [round(v, 3) for v in rect]
            entry['note'] = 'derived from the board; tighten or delete'
        else:
            entry['note'] = ('members are spread across the board and their '
                             'bounding box overlaps another block, so no zone '
                             'is claimed. Add one only if this really is a '
                             'contiguous area')
        if len(sides) == 1:
            entry['side'] = sides.pop()
        blocks.append(entry)

    # Parts already overhanging the outline are edge connectors by observation.
    # Recording them is what stops oob_count reporting them forever.
    conns = []
    for ref in sorted(parts):
        amt = state.edge_gate.rect_outside_amount(parts[ref].rect)
        if amt > legality.EPS:
            conns.append({'ref': ref, 'edge': _nearest_edge(parts[ref].rect,
                                                            bounds),
                          'overhang_mm': {'min': 0.0,
                                          'max': round(amt + 0.5, 3)}})

    locked = sorted(extract_locked_refs_safe(pcb_file))
    leg = state.legality_metrics()
    return {
        'schema': SCHEMA_VERSION,
        'kind': KIND,
        'board': os.path.basename(pcb_file),
        'units': 'mm',
        'envelope': {'rect': [round(v, 4) for v in bounds],
                     'tolerance_mm': DEFAULT_ENVELOPE_TOLERANCE_MM},
        'defaults': {'zone_tolerance_mm': DEFAULT_ZONE_TOLERANCE_MM},
        'blocks': blocks,
        'keepouts': [],
        'edge_connectors': conns,
        'decaps': {},
        'must_lock': locked,
        'legality_budget': {
            # Rounded UP, not to nearest. round() can land up to 5e-5 BELOW the
            # measured value, which is 50x legality.EPS -- so a budget written
            # from a board would fail against that same board. An emitted
            # intent that does not grade clean makes the round trip useless as
            # a check that the rules are wired to real geometry (watchy:
            # overlap 9.09724 was written as 9.0972 and instantly violated).
            'overlap_area': _ceil4(float(leg['overlap_area'])),
            'oob_count': int(leg['oob_count']),
        },
        'context': {
            'note': ('read-only, describing the board as it is. The outline is '
                     'not editable by this toolchain: size, cutouts and slots '
                     'are mechanical decisions the user owns'),
            'cutouts': [[[round(x, 3), round(y, 3)] for x, y in ring]
                        for ring in (pcb_data.board_info.board_cutouts or [])],
            'edge_contours': len(
                getattr(pcb_data.board_info, 'board_edge_contours', None) or []),
        },
    }


def extract_locked_refs_safe(pcb_file: str):
    try:
        from .parser import extract_locked_refs
        return extract_locked_refs(pcb_file) if pcb_file else set()
    except (OSError, ValueError):
        return set()


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------

def format_text(r: GradeResult) -> str:
    lines = []
    board = os.path.basename(r.board) or '<board>'
    lines.append(f"Floorplan: {board} vs {os.path.basename(r.intent.source_path) or 'intent'}")
    lines.append(f"  {r.n_footprints} footprints, {len(r.blocks)} block(s), "
                 f"{sum(len(v) for v in r.blocks.values())} part(s) covered")
    ol = r.outline
    lines.append(f"  outline: {ol['outlines']} ring(s), {ol['cutouts']} cutout(s), "
                 f"{ol['edge_contours']} milled contour(s)")
    if not r.violations:
        lines.append(f"  PASS: {len(r.rules_run)} rule(s) ran, no violations")
    else:
        lines.append(f"  {len(r.errors)} error(s), {len(r.warnings)} warning(s) "
                     f"from {len(r.rules_run)} rule(s)")
        for v in r.violations:
            tag = 'ERROR' if v.severity == ERROR else 'warn '
            lines.append(f"    [{tag}] {v.rule}: {v.message}")
    if r.rules_skipped:
        lines.append(f"  {len(r.rules_skipped)} rule(s) did not run:")
        for name in sorted(r.rules_skipped):
            lines.append(f"    - {name}: {r.rules_skipped[name]}")
    return '\n'.join(lines)


def to_json(r: GradeResult) -> Dict:
    return {
        'schema': SCHEMA_VERSION,
        'board': r.board,
        'intent': r.intent.source_path,
        'pass': r.passed,
        'violations': [v.to_dict() for v in r.violations],
        'blocks': {k: list(v) for k, v in sorted(r.blocks.items())},
        'legality': r.legality,
        'outline': r.outline,
        'state': r.state,
        'rules_run': list(r.rules_run),
        'rules_skipped': r.rules_skipped,
        'n_footprints': r.n_footprints,
    }


def summary(r: GradeResult) -> Dict:
    """The flat JSON_SUMMARY dict, shaped like place_optimize's."""
    by_rule: Dict[str, int] = {}
    for v in r.violations:
        by_rule[v.rule] = by_rule.get(v.rule, 0) + 1
    out = {
        'board': os.path.basename(r.board),
        'intent': os.path.basename(r.intent.source_path),
        'pass': r.passed,
        'violations': len(r.violations),
        'errors': len(r.errors),
        'warnings': len(r.warnings),
        'violations_by_rule': by_rule,
        'rules_run': len(r.rules_run),
        'rules_skipped': len(r.rules_skipped),
        'blocks': len(r.blocks),
        'blocks_resolved': sum(1 for v in r.blocks.values() if v),
        'parts_covered': len({ref for v in r.blocks.values() for ref in v}),
        'parts_total': r.n_footprints,
        'cutouts': r.outline['cutouts'],
        'edge_contours': r.outline['edge_contours'],
    }
    out.update(r.legality)
    for k in ('unplaced', 'partially_unplaced', 'has_copper',
              'duplicate_fraction', 'spread_ratio', 'outside_fraction'):
        out[f"state_{k}"] = r.state[k]
    return out
