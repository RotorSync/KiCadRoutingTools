#!/usr/bin/env python3
"""The impossibility guard, against throats of known width.

Across four recorded runs, 9 of 14 "no placement can fix this" claims were
later refuted, and EVERY claim that a pad was unroutable in principle was
wrong. They were made from renders, from a router's failure, and from
arithmetic on the wrong two edges. The rule this instrument enforces is: no
impossibility claim without a numeric field.

So the tests are built around gaps whose answer is arithmetic. A wall with a
gap of width G, graded at clearance c, admits a track of exactly `G - 2c`; the
measurement must agree to within the raster step, and must say CAGED when the
gap is too narrow and PASSABLE when it is wide enough -- including the case
where those differ by a few microns, which is the case the whole thing exists
for.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from placement import reachability
    from placement.reachability import ScipyRequired
    reachability._require_scipy()
except Exception as exc:                          # noqa: BLE001
    print(f"SKIP: {exc}")
    sys.exit(0)


class _Pad:
    def __init__(self, x, y, sx, sy, net_id, layers=('F.Cu',), drill=0.0):
        self.global_x, self.global_y = x, y
        self.size_x, self.size_y = sx, sy
        self.net_id, self.layers, self.drill = net_id, list(layers), drill
        self.pad_number, self.pad_type, self.rect_rotation = '1', 'smd', 0.0


class _Fp:
    def __init__(self, pads):
        self.pads = pads


class _Net:
    def __init__(self, name):
        self.name = name


class _Info:
    copper_layers = ['F.Cu']


class _Pcb:
    """The slice of PCBData the field reads."""
    def __init__(self, pads_by_ref, nets):
        self.footprints = {k: _Fp(v) for k, v in pads_by_ref.items()}
        self.nets = {i: _Net(n) for i, n in nets.items()}
        self.segments, self.vias = [], []
        self.board_info = _Info()


# The fixture is deliberately TINY. The raster is quadratic in the view and the
# widest-path sweep is linear in cells, so a 6x26mm view at 2.5um is 25M cells
# and minutes of wall clock; 1x4mm is 1M and seconds. Nothing about the answer
# needs the extra area -- a throat is local.
SEED = (20.0, 13.5)
TARGET = (20.0, 16.5)
WALL_Y, WALL_T = 15.0, 0.6
# Wider than the widest gap under test: a gap equal to the view width puts the
# wall entirely outside the raster, and the fixture then measures the view.
_VIEW = (19.0, 12.8, 21.0, 17.2)


def _wall_board(gap_mm, span=40.0):
    """Two pads of net 1 either side of a wall of net 2 with a gap of `gap_mm`.

    The wall is two rectangles whose facing edges are `gap_mm` apart, centred
    on x=20 and extending well past the view. Everything else is empty, so the
    ONLY constraint on a route from the lower pad to the upper one is that gap.
    """
    half = gap_mm / 2.0
    return _Pcb({
        'P1': [_Pad(SEED[0], SEED[1], 0.5, 0.5, 1)],
        'P2': [_Pad(TARGET[0], TARGET[1], 0.5, 0.5, 1)],
        'W1': [_Pad(20.0 - half - span / 2.0, WALL_Y, span, WALL_T, 2)],
        'W2': [_Pad(20.0 + half + span / 2.0, WALL_Y, span, WALL_T, 2)],
    }, {1: 'SIG', 2: 'BLOCK'})


def _measure(gap, clearance=0.15, track=0.15, step=0.005, view=_VIEW):
    pcb = _wall_board(gap)
    return reachability.pad_reachability(
        pcb, SEED, net_id=1, layers=['F.Cu'], track_mm=track,
        via_mm=0.6, base_clearance=clearance, step=step, view=view)


def test_the_measured_throat_matches_the_closed_form():
    """A gap of G at clearance c admits exactly G - 2c."""
    c = 0.15
    for gap in (0.8, 1.0, 1.2):
        r = _measure(gap, clearance=c, step=0.005)
        want = gap - 2 * c
        assert r.bottleneck_mm is not None, gap
        assert abs(r.bottleneck_mm - want) < 0.02, (gap, r.bottleneck_mm, want)
    print("  PASS: measured throat == gap - 2*clearance at 0.8/1.0/1.2mm")


def test_a_gap_too_narrow_is_CAGED_and_a_wide_one_is_PASSABLE():
    narrow = _measure(gap=0.35, clearance=0.15, track=0.15)   # admits 0.05
    wide = _measure(gap=0.60, clearance=0.15, track=0.15)     # admits 0.30
    assert narrow.caged, narrow.to_dict()
    assert not wide.caged, wide.to_dict()
    print(f"  PASS: 0.35mm gap CAGED ({narrow.bottleneck_mm:.3f}mm), "
          f"0.60mm gap PASSABLE ({wide.bottleneck_mm:.3f}mm)")


def test_it_resolves_a_few_microns_either_side_of_the_track_width():
    """The case the instrument exists for.

    A run-8 claim that a pad was sealed was refuted by a 6.4um throat, and this
    field agreed to within a micron. A tool that can only say 'tight' would
    have left that claim standing.
    """
    c, track, step = 0.15, 0.15, 0.002
    just_under = _measure(gap=2 * c + track - 0.010, clearance=c, track=track,
                          step=step)
    just_over = _measure(gap=2 * c + track + 0.010, clearance=c, track=track,
                         step=step)
    assert just_under.caged, just_under.to_dict()
    assert not just_over.caged, just_over.to_dict()
    assert just_under.margin_um < 0 < just_over.margin_um
    print(f"  PASS: {just_under.margin_um:+.1f}um CAGED vs "
          f"{just_over.margin_um:+.1f}um PASSABLE, 20um apart")


def test_a_sealed_pad_is_CAGED_with_no_path_at_any_width():
    """Not 'a very small number' -- no positive-slack path exists at all."""
    r = _measure(gap=0.05, clearance=0.15, track=0.15)
    assert r.caged
    assert r.bottleneck_mm is None or r.bottleneck_mm <= 0.0, r.to_dict()
    print("  PASS: a sealed gap yields no path at any width")


def test_clearance_is_part_of_the_verdict_not_a_detail():
    """The same copper is passable at one clearance and caged at another. A
    verdict quoted without its clearance is not a verdict."""
    # 0.6 - 2(0.10) = 0.40 clears a 0.15 track; 0.6 - 2(0.25) = 0.10 does not.
    gap = 0.6
    loose = _measure(gap, clearance=0.10, track=0.15)
    tight = _measure(gap, clearance=0.25, track=0.15)
    assert not loose.caged and tight.caged, (loose.bottleneck_mm,
                                             tight.bottleneck_mm)
    print(f"  PASS: the same 0.6mm gap is PASSABLE at 0.10mm clearance "
          f"({loose.bottleneck_mm:.3f}mm) and CAGED at 0.25mm "
          f"({tight.bottleneck_mm:.3f}mm)")


def test_wide_open_is_reported_as_bounded_by_the_view():
    """With nothing in the way, the 'bottleneck' is the size of the question
    asked, not a fact about the board -- and must say so rather than print a
    number that looks measured."""
    pcb = _Pcb({'P1': [_Pad(SEED[0], SEED[1], 0.5, 0.5, 1)],
                'P2': [_Pad(TARGET[0], TARGET[1], 0.5, 0.5, 1)]}, {1: 'SIG'})
    r = reachability.pad_reachability(pcb, SEED, net_id=1,
                                      layers=['F.Cu'], base_clearance=0.15,
                                      step=0.01, view=_VIEW)
    assert r.wide_open and not r.caged, r.to_dict()
    assert 'bounded by the VIEW' in r.format_text()
    print("  PASS: an empty board reports wide-open, not a fake measurement")


def test_the_pad_being_already_connected_is_not_a_verdict():
    """One island means there is nothing to reach. That must not be reported
    as PASSABLE -- it is a different question."""
    pcb = _Pcb({'P1': [_Pad(SEED[0], SEED[1], 0.5, 0.5, 1)]}, {1: 'SIG'})
    r = reachability.pad_reachability(pcb, SEED, net_id=1,
                                      layers=['F.Cu'], base_clearance=0.15,
                                      step=0.01, view=_VIEW)
    assert r.target_cells == 0 and r.note, r.to_dict()
    assert r.bottleneck_mm is None
    print("  PASS: 'nothing to reach' is reported as such, not as a verdict")


def test_a_seed_off_the_net_is_an_error_not_a_guess():
    pcb = _wall_board(1.0)
    try:
        reachability.pad_reachability(pcb, (19.6, 13.0), net_id=1,
                                      layers=['F.Cu'], step=0.01,
                                      view=_VIEW)
    except ValueError as exc:
        assert 'not on' in str(exc)
        print("  PASS: a seed off the net's copper raises, rather than "
              "silently answering about the wrong point")
        return
    raise AssertionError("expected a ValueError")


if __name__ == '__main__':
    for fn in (test_the_measured_throat_matches_the_closed_form,
               test_a_gap_too_narrow_is_CAGED_and_a_wide_one_is_PASSABLE,
               test_it_resolves_a_few_microns_either_side_of_the_track_width,
               test_a_sealed_pad_is_CAGED_with_no_path_at_any_width,
               test_clearance_is_part_of_the_verdict_not_a_detail,
               test_wide_open_is_reported_as_bounded_by_the_view,
               test_the_pad_being_already_connected_is_not_a_verdict,
               test_a_seed_off_the_net_is_an_error_not_a_guess):
        print(fn.__name__)
        fn()
    print("\nALL PASS")
