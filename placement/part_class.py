"""What determines this part's position? A pose-INDEPENDENT class (run-4 A).

Run 3 shipped its largest placement error because no machine-readable
component classification existed: the USB-C receptacle J1 sat 15.8 mm
interior of its true edge slot and every instrument called it fine -- the
lock advisor demanded a GEOMETRIC signal for high confidence, so a
*misplaced* edge part was demoted exactly when it mattered; `--emit-intent`
declared edge connectors from *current overhang only*, so it could only
re-declare parts that were already right; and the slide switch SW1's
by-design actuator overhang read as a defect all run until a hand-authored
intent said otherwise.

The class question is pose-independent -- "what determines where this part
belongs" is a property of what the part IS (footprint, pin functions, ref
convention), never of where it currently sits. Pose enters only through
`pose_plausible`, a separate verdict.

Two edge classes with deliberately different strength:

- ``edge_receptacle`` -- mates THROUGH the board edge (USB, HDMI, RJ45,
  D-sub, card slots, jacks). An interior pose is mechanically implausible:
  the plug could not reach it. This is the R1 test run 3 failed to apply.
- ``edge_actuator`` -- a switch/button whose actuator MAY overhang the
  edge by design (tigard SW1). Many switches are legitimately interior
  (tactile buttons), so this class makes NO implausibility claim; its value
  is that an OBSERVED overhang is by-design, not a defect.

Generic connector keywords (jst, molex, pinheader, header, socket,
terminal) deliberately do NOT map to an edge class: a JST wire-to-board
connector (tigard J7) is legitimately interior, and classifying it edge
would misplace it -- the false-positive guard the generalization gates
measure corpus-wide.

Thresholds derive from the board or the part, never from any one board:
``seat_tol`` defaults to 0.5 mm and callers should pass
``max(0.5, 2 * grid_step)``; overhang bands come from the part's own
extents. (Generalization requirement: no tigard constants.)
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

# ---------------------------------------------------------------------------
# The tables (single source of truth -- lock_advisor imports these).
# ---------------------------------------------------------------------------

# Lexical: substrings of footprint_name.lower(). Stock KiCad library naming;
# house libraries miss, which is why classification also reads pin functions.
CONNECTOR_FP = ('connector', 'usb', 'hdmi', 'rj45', 'jack', 'pinheader',
                'pinsocket', 'terminal', 'dsub', 'molex', 'jst', 'hirose',
                'microsd', 'sim', 'card', 'banana', 'header', 'socket')
MOUNT_FP = ('mountinghole', 'fiducial', 'testpoint')

# The edge-mating subset of CONNECTOR_FP, plus receptacle-only names. A part
# matching these mates through the board edge / an enclosure aperture.
EDGE_RECEPTACLE_FP = ('usb', 'hdmi', 'rj45', 'rj11', 'dsub', 'd-sub',
                      'microsd', 'sd_card', 'sdcard', 'sim', 'card',
                      'jack', 'displayport', 'cardedge', 'card_edge')
# Switches/buttons whose actuator may overhang the edge by design.
EDGE_ACTUATOR_FP = ('switch', 'slide', 'sw_', 'button', 'tact', 'pushbutton')

# Reference-designator prefixes. IEEE-315 convention, NOT a property of the
# file -- prefix evidence alone never makes a receptacle.
CONN_PREFIX_MED = ('J', 'P', 'X', 'CN')
CONN_PREFIX_LOW = ('SW', 'TP', 'MH', 'H', 'FID')
ACTUATOR_PREFIX = ('SW',)

# USB/edge pin functions KiCad writes only when the symbol carried one.
# CC1/CC2/SBU are USB-C receptacle pins specifically -- a very strong,
# house-library-proof receptacle signal.
IFACE_PINS = {'VBUS', 'D+', 'D-', 'CC1', 'CC2', 'SBU1', 'SBU2', 'SHIELD'}

_PREFIX_RE = re.compile(r'^([A-Za-z]+)')

SEAT_TOL_MM = 0.5      # default; callers pass max(0.5, 2*grid_step)


@dataclass(frozen=True)
class PartClass:
    name: Optional[str]            # edge_receptacle | edge_actuator |
    #                                mount_hole | fiducial | testpoint | None
    confidence: Optional[str]      # high | medium | low | None
    evidence: Tuple[str, ...] = ()


def _prefix(ref: str) -> str:
    m = _PREFIX_RE.match(ref or '')
    return (m.group(1) if m else '').upper()


def classify_part(fp, ref: str) -> PartClass:
    """Pose-independent classification from footprint name, ref prefix and
    pin functions. Never reads a coordinate."""
    name = (getattr(fp, 'footprint_name', '') or '').lower()
    pref = _prefix(ref)
    pads = getattr(fp, 'pads', None) or []

    # Structural: NPTH-only part = mounting hole (the strongest rule in the
    # advisor, restated here as a class).
    npth = [p for p in pads if getattr(p, 'pad_type', '') == 'np_thru_hole']
    plated_or_smd = [p for p in pads
                     if getattr(p, 'pad_type', '') != 'np_thru_hole']
    if npth and not plated_or_smd:
        return PartClass('mount_hole', 'high', ('npth-only pad set',))
    if 'mountinghole' in name:
        return PartClass('mount_hole', 'medium', ('footprint name',))
    if 'fiducial' in name or pref == 'FID':
        return PartClass('fiducial', 'medium', ('footprint name/prefix',))
    if 'testpoint' in name or pref == 'TP':
        return PartClass('testpoint', 'medium', ('footprint name/prefix',))

    pins = {str(getattr(p, 'pinfunction', '') or '').upper() for p in pads}
    iface = pins & IFACE_PINS
    fp_recep = any(k in name for k in EDGE_RECEPTACLE_FP)
    fp_act = any(k in name for k in EDGE_ACTUATOR_FP)

    if fp_recep or iface:
        ev = []
        if fp_recep:
            ev.append('footprint name matches the edge-receptacle table')
        if iface:
            ev.append(f"interface pin functions ({', '.join(sorted(iface))[:40]})")
        # Two independent channels (or the receptacle-specific CC pins) =
        # high; a name alone = medium. Prefix alone NEVER makes a receptacle.
        strong = (fp_recep and (iface or pref in CONN_PREFIX_MED)) or \
                 bool(iface & {'CC1', 'CC2', 'SBU1', 'SBU2'})
        return PartClass('edge_receptacle', 'high' if strong else 'medium',
                         tuple(ev))
    if fp_act or pref in ACTUATOR_PREFIX:
        ev = ['footprint name matches the edge-actuator table' if fp_act
              else f"reference prefix {pref!r} (convention only)"]
        return PartClass('edge_actuator', 'medium' if fp_act else 'low',
                         tuple(ev))
    return PartClass(None, None)


def pose_plausible(cls: Optional[str], outside_amount: Optional[float],
                   edge_clearance: Optional[float],
                   seat_tol: float = SEAT_TOL_MM) -> Optional[bool]:
    """Is the part's CURRENT pose consistent with its class?

    Only ``edge_receptacle`` makes a claim: it must overhang the outline or
    sit within ``seat_tol`` of an edge (the mating face has to reach the
    edge). Every other class returns True; unknown geometry returns None.

    Run-3 calibration for the test suite, not for the rule: displaced J1
    measured outside_amount 0.0 / edge_clearance 2.45 -- implausible; SW1
    measured outside_amount 1.6 -- plausible. A bare "far from every edge"
    threshold would MISS J1 (a swap can park an edge part near a different
    edge), which is why the conjunct is class-conditional.
    """
    if cls != 'edge_receptacle':
        return True
    if outside_amount is None or edge_clearance is None:
        return None
    return outside_amount > 1e-6 or edge_clearance <= seat_tol


def default_band(cls: str, fp, observed_overhang: float = 0.0
                 ) -> Dict[str, float]:
    """Class-default overhang band {min, max} in mm, from the part's own
    geometry -- never from board history."""
    if cls == 'edge_actuator':
        mx = (observed_overhang + 0.5) if observed_overhang > 1e-6 else 3.0
        return {'min': 0.0, 'max': round(mx, 3)}
    # receptacle: the shell face may protrude a little past the pad field;
    # bound by half the part's SMALLER extent (the depth axis) capped at 2.
    pads = getattr(fp, 'pads', None) or []
    if pads:
        xs = [getattr(p, 'global_x', 0.0) for p in pads]
        ys = [getattr(p, 'global_y', 0.0) for p in pads]
        half_min_extent = 0.5 * min(
            (max(xs) - min(xs)) if len(xs) > 1 else 2.0,
            (max(ys) - min(ys)) if len(ys) > 1 else 2.0)
        mx = min(2.0, max(0.5, half_min_extent))
    else:
        mx = 2.0
    return {'min': 0.0, 'max': round(max(mx, observed_overhang + 0.25), 3)}
