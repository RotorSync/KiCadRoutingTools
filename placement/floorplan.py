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


def _rects_touch(a, b) -> bool:
    return legality.rect_overlap_area(a, b) > legality.EPS


def _circle_hits_rect(cx, cy, radius, rect) -> bool:
    nx = min(max(cx, rect[0]), rect[2])
    ny = min(max(cy, rect[1]), rect[3])
    return math.hypot(nx - cx, ny - cy) < radius
