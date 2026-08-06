#!/usr/bin/env python3
"""The #411 recovery grader must be trustworthy before anything is measured with it.

These are all pure-function tests over pose dictionaries: no perturbation, no
routing, no subprocesses. That is deliberate -- the metric has to be provable
independently of the tool that will feed it, or a bug in one hides a bug in the
other.

The load-bearing test here is `test_the_grader_can_say_WORSE`. A grader that can
only report improvement is not a grader, and every other property in this file
is worth less if that one does not hold.

Run: python3 -X utf8 tests/test_411_recovery.py
"""
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from placement import recovery as R                       # noqa: E402

BOARD = os.path.join(ROOT, 'kicad_files', 'interf_u_unrouted.kicad_pcb')


def _load():
    from kicad_parser import parse_kicad_pcb
    pcb = parse_kicad_pcb(BOARD)
    return pcb, R.board_poses(pcb)


def _translate(poses, refs, dx, dy):
    out = dict(poses)
    for r in refs:
        x, y, rot = out[r]
        out[r] = (x + dx, y + dy, rot)
    return out


def _members(poses, n=6):
    return sorted(poses)[:n]


def test_translation_reads_exactly_the_distance_moved():
    """A rigid translate of D must read D, not 'some displacement'.

    This is what makes the dose the x-axis of a dose-response curve: if the
    metric were merely monotone in the dose the curve would still be drawn, but
    no point on it could be labelled.
    """
    pcb, poses = _load()
    mem = _members(poses)
    dx, dy = 12.0, -5.0
    moved = _translate(poses, mem, dx, dy)
    d = R.displacement_to_original(moved, poses, pcb.footprints, mem)
    want = math.hypot(dx, dy)
    assert abs(d['perturbed_pad_rms'] - want) < 1e-6, \
        f"rigid translate of {want:.4f} read as {d['perturbed_pad_rms']}"
    # Rigid: every member moved identically, so max == rms.
    assert abs(d['perturbed_pad_max'] - want) < 1e-6
    # And the untouched rest of the board must contribute nothing.
    assert d['collateral_pad_rms'] == 0.0
    assert d['parts_home_frac'] == 0.0


def test_ground_truth_scored_against_itself_is_all_zeros():
    """The instrument must read zero on a null sample."""
    pcb, poses = _load()
    mem = _members(poses)
    d = R.displacement_to_original(poses, poses, pcb.footprints, mem)
    for key in ('perturbed_pad_rms', 'perturbed_pad_max', 'collateral_pad_rms',
                'all_pad_rms', 'perturbed_centre_rms'):
        assert d[key] == 0.0, f"{key} was {d[key]} scoring the original vs itself"
    assert d['rot_mismatch'] == []
    assert d['parts_home_frac'] == 1.0
    assert R.unit_spread_ratio(poses, poses, mem) == 1.0


def test_the_grader_can_say_WORSE():
    """Score a board perturbed FURTHER against a smaller dose's record.

    P10 and P20 along the same direction: grading P20 against P10's dose must
    return exactly -1.0. Unbounded below is the property that makes this a
    grader rather than an improvement-reporter, and it is exact rather than
    approximate, so there is no tolerance to argue about.
    """
    pcb, poses = _load()
    mem = _members(poses)
    p10 = _translate(poses, mem, 10.0, 0.0)
    p20 = _translate(poses, mem, 20.0, 0.0)
    d0 = R.displacement_to_original(p10, poses, pcb.footprints, mem)['perturbed_pad_rms']
    d1 = R.displacement_to_original(p20, poses, pcb.footprints, mem)['perturbed_pad_rms']
    frac = R.recovery_fraction(d0, d1)
    assert abs(frac - (-1.0)) < 1e-9, f"expected -1.0, got {frac}"
    assert abs(d0 - 10.0) < 1e-6 and abs(d1 - 20.0) < 1e-6


def test_a_zero_dose_is_degenerate_never_perfect():
    """D0 == 0 must be None, not 1.0 and not 0.0.

    Scoring a perfect recovery for a no-op experiment is exactly how a dose-0
    row poisons an aggregate mean.
    """
    assert R.recovery_fraction(0.0, 0.0) is None
    assert R.recovery_fraction(0.0, 5.0) is None
    assert R.recovery_fraction(None, 1.0) is None
    # And a real dose still grades.
    assert R.recovery_fraction(10.0, 2.5) == 0.75


def test_rotation_counts_and_the_footprint_ORIGIN_does_not():
    """The trap this repo already paid for, as an assertion.

    A part back at its original x/y but rotated 180 degrees is NOT recovered.
    The recorded field case: crossings 85->60 and hpwl 602->596 both "better"
    while a decap requirement went 2.04 -> 9.57 mm, because the quench rotated
    the part it served. The footprint origin never moved, so a position diff
    showed 0.00 mm and the delta render drew no arrow.
    """
    pcb, poses = _load()
    ref = 'U5'
    x, y, rot = poses[ref]
    flipped = dict(poses)
    flipped[ref] = (x, y, (rot + 180.0) % 360.0)
    d = R.displacement_to_original(flipped, poses, pcb.footprints, [ref])
    assert d['perturbed_centre_rms'] == 0.0, \
        "the footprint origin must show no movement -- that is the trap"
    assert d['perturbed_pad_rms'] > 40.0, \
        f"a 180 rotation of U5 must cost real millimetres, got {d['perturbed_pad_rms']}"
    assert d['rot_mismatch'] == [ref]
    assert d['parts_home_frac'] == 0.0, \
        "a rotated part must not count as home"

    # A symmetric two-pad passive is the index-matching case: its pads swap
    # ends, so it reads 2*pad_offset rather than 0.
    fp = pcb.footprints['C2']
    d2 = R.part_displacement(fp, (0.0, 0.0, 0.0), (0.0, 0.0, 180.0))
    assert d2 > 0.0, "a flipped 2-pad passive must not read 0"


def test_pose_distance_mm_is_NOT_what_this_module_uses():
    """portfolio.pose_distance_mm adds a unitless flat 1.0 for a rotation.

    Fine for its own job (are these two candidates the same placement?), wrong
    for a millimetre metric. Asserted rather than trusted to a comment, because
    'simplify one into the other' is exactly the change someone will propose.
    """
    from placement.portfolio import pose_distance_mm
    pcb, poses = _load()
    ref = 'U5'
    x, y, rot = poses[ref]
    a = {ref: (x, y, rot)}
    b = {ref: (x, y, (rot + 180.0) % 360.0)}
    theirs = pose_distance_mm(a, b, [ref])
    ours = R.part_displacement(pcb.footprints[ref], a[ref], b[ref])
    assert abs(theirs - 1.0) < 1e-9, \
        f"pose_distance_mm should charge a flat 1.0 here, got {theirs}"
    assert ours > 40.0
    assert ours != theirs


def _compact_unit(poses, n=6):
    """The n mutually-closest parts -- a stand-in for a real block.

    A real block is spatially coherent; that is what makes it a block. Picking
    `sorted(poses)[:n]` instead gives a set scattered across the whole board,
    which is a different object and has different metric behaviour (see the
    insensitivity test below).
    """
    import itertools
    refs = sorted(poses)
    best, best_span = None, float('inf')
    for anchor in refs:
        ax, ay, _ = poses[anchor]
        near = sorted(refs, key=lambda r: math.hypot(poses[r][0] - ax,
                                                     poses[r][1] - ay))[:n]
        span = max(math.hypot(poses[a][0] - poses[b][0],
                              poses[a][1] - poses[b][1])
                   for a, b in itertools.combinations(near, 2))
        if span < best_span:
            best, best_span = near, span
    return sorted(best)


def test_unit_spread_ratio_catches_a_torn_block():
    """A block dragged home member-by-member is not a recovered block.

    With --group-by none a router-in-the-loop can pull the passives back while
    the IC stays put. A centroid-only recovery ratio reads that as partial
    success; the radius of gyration reads it as the tear it is.
    """
    pcb, poses = _load()
    mem = _compact_unit(poses, 6)
    # Rigid move: the shape is preserved, so the ratio is 1.0.
    rigid = _translate(poses, mem, 15.0, 0.0)
    assert abs(R.unit_spread_ratio(rigid, poses, mem) - 1.0) < 1e-9
    # Tear: move half the members and leave the rest.
    torn = _translate(poses, mem[:3], 40.0, 0.0)
    ratio = R.unit_spread_ratio(torn, poses, mem)
    assert ratio > 1.5, f"a torn block should blow the ratio, got {ratio}"


def test_the_tear_detector_is_BLIND_on_a_scattered_unit():
    """Pinning a real limitation so it cannot be silently relied on.

    Measured while writing the test above: displacing 3 of 6 members by 40 mm
    moves the ratio only 1.00 -> ~1.04 when those six are scattered across the
    board, because their original gyration is already ~50 mm. The ratio is
    therefore only readable BESIDE the original gyration, which is why
    `score_board` reports `unit_gyration_orig_mm`. A ratio near 1.0 against a
    large original gyration means "cannot tell", not "not torn".
    """
    pcb, poses = _load()
    scattered = sorted(poses)[:6]
    g = R._gyration(poses, scattered)
    assert g > 20.0, "fixture assumption: these six really are scattered"
    torn = _translate(poses, scattered[:3], 40.0, 0.0)
    ratio = R.unit_spread_ratio(torn, poses, scattered)
    assert ratio < 1.5, (
        f"expected the detector to be blind here (got {ratio}); if this now "
        f"fires, the metric improved and this test should be re-derived rather "
        f"than deleted")


def test_recovery_radius_is_monotone_from_below():
    """One lucky cell at a large dose must not claim a large radius."""
    rows = [
        {'dose_mm_applied': 5.0, 'recovery_fraction': 0.9},
        {'dose_mm_applied': 10.0, 'recovery_fraction': 0.6},
        {'dose_mm_applied': 20.0, 'recovery_fraction': 0.1},   # fails here
        {'dose_mm_applied': 40.0, 'recovery_fraction': 0.8},   # lucky
    ]
    assert R.recovery_radius(rows) == 10.0, \
        "the radius must stop at the first failing dose, not at the last passing one"
    # Nothing recovers -> None, which is a result, not a zero.
    assert R.recovery_radius([{'dose_mm_applied': 5.0,
                               'recovery_fraction': 0.0}]) is None
    # Rows with no verdict are not counted as failures.
    assert R.recovery_radius([{'dose_mm_applied': 5.0, 'recovery_fraction': None}]) is None


def test_the_denominator_keeps_nets_that_produced_no_copper():
    """OmniRouting: 'missing outputs remain in both routability denominators'.

    A net with no copper at all must contribute 0 connected pairs over its full
    (P-1) share of the denominator -- not be skipped, which is what
    check_connected.run_connectivity_check does and why this module does not
    call it.
    """
    from kicad_parser import parse_kicad_pcb
    unrouted = parse_kicad_pcb(BOARD)          # zero segments, zero vias
    nets = R.frozen_net_ids(unrouted)
    assert nets, "fixture should have multi-pad nets"
    tally = R.connectivity_tally(unrouted, nets)
    assert tally['prr_denominator'] > 0, \
        "a board with no copper must still have a denominator"
    assert tally['pairs_connected'] == 0, \
        f"no copper must connect no pairs, got {tally['pairs_connected']}"
    assert tally['PRR_connected'] == 0.0
    # The DRC half must announce that it did not run rather than passing the
    # connectivity number off as DRC-clean.
    assert tally['PRR'] is None and tally['drc_clean_ran'] is False
    assert tally['drc_clean_reason']


def test_the_denominator_is_frozen_not_recomputed_per_arm():
    """A supplied denominator must be used verbatim.

    The denominator is computed once on the original. If an arm silently
    recomputed it, an arm that lost a net would be graded against a smaller
    denominator and look better for having done less.
    """
    from kicad_parser import parse_kicad_pcb
    pcb = parse_kicad_pcb(BOARD)
    nets = R.frozen_net_ids(pcb)
    natural = R.connectivity_tally(pcb, nets)['prr_denominator']
    forced = R.connectivity_tally(pcb, nets, prr_denominator=natural * 2)
    assert forced['prr_denominator'] == natural * 2
    assert forced['PRR_connected'] == 0.0


TESTS = [
    test_translation_reads_exactly_the_distance_moved,
    test_ground_truth_scored_against_itself_is_all_zeros,
    test_the_grader_can_say_WORSE,
    test_a_zero_dose_is_degenerate_never_perfect,
    test_rotation_counts_and_the_footprint_ORIGIN_does_not,
    test_pose_distance_mm_is_NOT_what_this_module_uses,
    test_unit_spread_ratio_catches_a_torn_block,
    test_the_tear_detector_is_BLIND_on_a_scattered_unit,
    test_recovery_radius_is_monotone_from_below,
    test_the_denominator_keeps_nets_that_produced_no_copper,
    test_the_denominator_is_frozen_not_recomputed_per_arm,
]


if __name__ == '__main__':
    failed = 0
    for t in TESTS:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception as exc:                       # noqa: BLE001
            failed += 1
            print(f"FAIL {t.__name__}: {exc}")
    sys.exit(1 if failed else 0)
