#!/usr/bin/env python3
"""Rotated-footprint local/global frame invariants (the PR 633 / 301ee1b3 fix).

`kicad_parser._global_to_local` must be the EXACT inverse of `local_to_global`.
It shipped for a long time computing R(-theta).(g - f), which is
local_to_global's FORWARD map (its body negates the angle, the KiCad
convention) rather than its inverse R(+theta).(g - f) -- so both sin signs were
wrong and every rotated footprint round-tripped to a reflected point (measured
wrong for 437/825 pads on kit-dev-coldfire, worst 147.4 mm).

Why this needs a gate rather than a code comment: the bug was INVISIBLE to the
whole suite. `_global_to_local` has one caller -- `build_pcb_data_from_board`'s
pad-local fallback -- so the CLI text parser never runs it, and the fallback
only became live when KiCad 7+ removed `pad.GetPos0()`. Meanwhile NO gate runs
a fanout step, and fanout is where `pad.local_x/local_y` is consumed. It sat
wrong under a modern pcbnew with nothing to notice.

Covered here (all wx-free and pcbnew-free -- the GUI path is simulated by
recomputing locals through `_global_to_local`, which is exactly what
`build_pcb_data_from_board` does):

  1. round-trip identity on every rotated footprint of several real boards
  2. a CHANGE DETECTOR: the old transposed body must FAIL that round-trip, so
     this file cannot quietly go vacuous if someone makes both sides symmetric
  3. reduction: at 0/180 deg the two bodies agree (an unrotated board must not
     be able to "pass" by accident)
  4. bga_fanout.rotate_frame round-trip at NON-orthogonal angles -- the only
     consumer that turns a wrong local into MOVED COPPER (it snaps target pads
     to `cx + local_x`), and the one no in-repo board exercises.
     NOT covered by tests/test_bga_fanout_rotated.py (#137): that one builds a
     SYNTHETIC grid whose locals are supplied directly, so `_global_to_local`
     never runs and its `back(fwd(p)) == p` check is a property of the two
     frame functions alone. The invariant here is the other one -- that the
     pad LOCALS agree with that rotation, which is what decides whether the
     snapped ball grid sits on the real balls.
  5. qfn_fanout side classification agrees between file-true locals and
     GUI-recomputed locals -- the exact symptom the bug produced (a 90 deg
     QFN-76 reported bottom 17/top 3 where the truth is bottom 3/top 17)

End-to-end coverage through the real FanoutTab lives in
tests/gui_parity/test_fanout_rotated_gui.py (needs KiCad python).
"""
import math
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, 'py_router'))  # #522
sys.path.insert(0, os.path.join(_ROOT, 'py_tools'))  # #522

from kicad_parser import parse_kicad_pcb, local_to_global, _global_to_local
from bga_fanout.rotate_frame import is_orthogonal, to_axis_aligned_frame
from qfn_fanout.layout import analyze_qfn_layout, analyze_pad

# local_to_global snaps to KiCad's integer-nm grid, so a perfect round trip
# still lands ~1e-7 mm out; two chained rotations, ~2x that. 1e-5 mm = 10 nm.
TOL = 1e-5

# Boards carrying rotated footprints. cap_chain is deliberately included with
# zero rotated parts: it pins that the unrotated case is genuinely inert.
BOARDS = [
    ('glasgow_revC.kicad_pcb', 174),
    ('kit-dev-coldfire-xilinx_5213.kicad_pcb', 111),
    ('interf_u_unrouted.kicad_pcb', 15),
    ('haasoscope_pro_max_test.kicad_pcb', 7),
    ('cap_chain.kicad_pcb', 0),
]

QFN_BOARD, QFN_REF = 'haasoscope_pro_max_test.kicad_pcb', 'U2'   # QFN-76 @ 90 deg
BGA_BOARD, BGA_REF = 'haasoscope_pro_max_test.kicad_pcb', 'U3'   # BGA-529 @ -90 deg


def _old_global_to_local(fp_x, fp_y, fp_rotation_deg, global_x, global_y):
    """The pre-301ee1b3 body: the TRANSPOSED rotation (both sin signs flipped).

    Kept verbatim so test 2 can assert it still FAILS. If a future refactor
    makes this agree with the real one, test 2 goes red and says so, instead of
    the whole file silently checking nothing.
    """
    rad = math.radians(fp_rotation_deg)
    cos_r, sin_r = math.cos(rad), math.sin(rad)
    dx, dy = global_x - fp_x, global_y - fp_y
    return dx * cos_r + dy * sin_r, -dx * sin_r + dy * cos_r


def _board(name):
    return os.path.join(_ROOT, 'kicad_files', name)


def _ok(name, cond):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    return bool(cond)


def _roundtrip_errors(pcb, inverse):
    """(worst error mm, pads wrong, pads total, rotated footprints) for `inverse`
    used as local->global's inverse, over every pad of the board."""
    worst, wrong, total, rotated = 0.0, 0, 0, 0
    for fp in pcb.footprints.values():
        if abs(fp.rotation % 360) > 1e-9:
            rotated += 1
        for pad in fp.pads:
            gx, gy = local_to_global(fp.x, fp.y, fp.rotation,
                                     pad.local_x, pad.local_y)
            lx, ly = inverse(fp.x, fp.y, fp.rotation, gx, gy)
            err = math.hypot(lx - pad.local_x, ly - pad.local_y)
            worst = max(worst, err)
            total += 1
            if err > TOL:
                wrong += 1
    return worst, wrong, total, rotated


def test_roundtrip_on_real_boards(results):
    """1 + 2 + 3: identity holds, the old body breaks it, and 0 deg is inert."""
    print("\n[1] _global_to_local is the exact inverse of local_to_global")
    any_rotated = False
    for name, expect_rotated in BOARDS:
        path = _board(name)
        if not os.path.exists(path):
            print(f"  SKIP  {name} (not present)")
            continue
        pcb = parse_kicad_pcb(path)
        worst, wrong, total, rotated = _roundtrip_errors(pcb, _global_to_local)
        results.append(_ok(
            f"{name}: {total} pads round-trip (worst {worst:.2e} mm, "
            f"{rotated} rotated fps)", wrong == 0))
        # The corpus must actually contain the rotated case; a board set that
        # quietly lost its rotated parts would make test 2 unfalsifiable.
        if expect_rotated:
            results.append(_ok(f"{name}: still has rotated footprints "
                               f"({rotated} >= 1)", rotated >= 1))
            any_rotated = True

        o_worst, o_wrong, _, _ = _roundtrip_errors(pcb, _old_global_to_local)
        if expect_rotated:
            print(f"\n[2] change detector on {name}")
            results.append(_ok(
                f"{name}: OLD transposed body still FAILS "
                f"({o_wrong}/{total} pads wrong, worst {o_worst:.3f} mm)",
                o_wrong > 0))
        else:
            print(f"\n[3] reduction on {name} (no rotated footprints)")
            results.append(_ok(
                f"{name}: both bodies agree when nothing is rotated "
                f"(old worst {o_worst:.2e} mm)", o_wrong == 0))

    results.append(_ok("corpus still exercises the rotated case", any_rotated))


def test_rotate_frame_roundtrip(results):
    """4: bga_fanout.rotate_frame must map balls back onto their real positions.

    `to_axis_aligned_frame` snaps the target footprint's pads to
    (cx + local_x, cy + local_y) and hands back a `back()` that undoes the
    rotation. Those two only agree when local_x IS the true inverse -- so
    `back(rotated ball) == real ball` is precisely the invariant the transposed
    body broke, and it is the one place a wrong local MOVES COPPER.

    Driven at NON-orthogonal angles because that is the only regime where
    rotate_frame runs at all (it is gated behind is_orthogonal), and no in-repo
    board has a non-orthogonal multi-pad footprint.
    """
    print("\n[4] rotate_frame ball round-trip at non-orthogonal angles")
    path = _board(BGA_BOARD)
    if not os.path.exists(path):
        print(f"  SKIP  {BGA_BOARD} (not present)")
        return

    for theta in (35.0, 45.0, -20.0):
        results.append(_ok(f"theta={theta}: rotate_frame actually runs "
                           f"(not orthogonal)", not is_orthogonal(theta)))
        for label, inverse in (("fixed", _global_to_local),
                               ("old", _old_global_to_local)):
            pcb = parse_kicad_pcb(path)
            fp = pcb.footprints[BGA_REF]
            # Re-place the part at `theta` and rebuild its pads the way the GUI
            # front does: globals from pcbnew, locals COMPUTED from them.
            fp.rotation = theta
            truth = []
            for pad in fp.pads:
                gx, gy = local_to_global(fp.x, fp.y, theta,
                                         pad.local_x, pad.local_y)
                pad.global_x, pad.global_y = gx, gy
                pad.local_x, pad.local_y = inverse(fp.x, fp.y, theta, gx, gy)
                truth.append((gx, gy))

            rp, back = to_axis_aligned_frame(pcb, BGA_REF)
            worst = 0.0
            for pad, (gx, gy) in zip(rp.footprints[BGA_REF].pads, truth):
                bx, by = back(pad.global_x, pad.global_y)
                worst = max(worst, math.hypot(bx - gx, by - gy))

            if label == "fixed":
                results.append(_ok(
                    f"theta={theta}: {len(truth)} balls round-trip through the "
                    f"frame (worst {worst:.2e} mm)", worst <= TOL))
            else:
                results.append(_ok(
                    f"theta={theta}: OLD body still misplaces balls "
                    f"(worst {worst:.3f} mm)", worst > 0.1))


def _sides(footprint):
    """{pad_number: side} from the QFN layout analyser."""
    layout = analyze_qfn_layout(footprint)
    if layout is None:
        return None
    return {p.pad_number: analyze_pad(p, layout).side for p in footprint.pads}


def test_qfn_side_classification(results):
    """5: the exact symptom -- QFN side classification must not depend on which
    front computed the locals.

    qfn_fanout classifies each pad against the footprint's LOCAL bounding box,
    so a reflected local frame relabels the package. On the 90 deg QFN-76 the
    buggy body reported bottom 17 / top 3 where the truth is bottom 3 / top 17.
    """
    print("\n[5] QFN side classification, file-true vs GUI-recomputed locals")
    path = _board(QFN_BOARD)
    if not os.path.exists(path):
        print(f"  SKIP  {QFN_BOARD} (not present)")
        return

    pcb = parse_kicad_pcb(path)
    fp = pcb.footprints[QFN_REF]
    results.append(_ok(f"{QFN_REF} is still rotated off-axis "
                       f"(rotation={fp.rotation})",
                       abs(fp.rotation % 180) > 1e-9))
    truth = _sides(fp)
    results.append(_ok(f"{QFN_REF} analyses as a QFN ({len(truth or [])} pads)",
                       bool(truth)))
    if not truth:
        return

    for label, inverse in (("fixed", _global_to_local),
                           ("old", _old_global_to_local)):
        pcb2 = parse_kicad_pcb(path)
        fp2 = pcb2.footprints[QFN_REF]
        for pad in fp2.pads:
            pad.local_x, pad.local_y = inverse(fp2.x, fp2.y, fp2.rotation,
                                               pad.global_x, pad.global_y)
        got = _sides(fp2)
        differing = sum(1 for k, v in truth.items() if got.get(k) != v)
        if label == "fixed":
            results.append(_ok(
                "GUI-recomputed locals classify every pad identically to the "
                "file's own locals", differing == 0))
        else:
            results.append(_ok(
                f"OLD body still relabels the package ({differing} pads "
                f"change side)", differing > 0))


def main():
    results = []
    test_roundtrip_on_real_boards(results)
    test_rotate_frame_roundtrip(results)
    test_qfn_side_classification(results)

    passed = sum(results)
    print(f"\n{passed}/{len(results)} rotated-frame invariant tests passed")
    print("=" * 60)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
