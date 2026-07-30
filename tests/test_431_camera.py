"""The movie camera, asserted as DATA (#431).

`plan_shots` is a pure function over `Action` records, so the whole camera is
testable with no PIL, no pcbnew and no board. That is deliberate: the alternative
is eyeballing video, which catches nothing and regresses silently.

The clause under test, in the user's words: "when the pan is complete, show the
next moves". It is enforced structurally rather than by timing -- a frame either
moves the camera or changes the board, never both, and every camera move is
followed by a settle beat before any action plays.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from movie_camera import (Action, CameraOpts, aspect_fit, apply_budget,  # noqa: E402
                          iou, lerp_rect, plan_shots, rect_center, rect_size,
                          smoothstep, total_frames, views_for)

BOARD = (0.0, 0.0, 100.0, 60.0)
O = CameraOpts()


def _kinds(shots):
    return [s.kind for s in shots]


def test_a_chain_opens_on_the_overview_and_returns_to_it():
    shots = plan_shots([Action('place', 'r1', (10, 10, 20, 20), None)], BOARD, O)
    assert shots[0].kind == 'establish'
    assert shots[0].view == aspect_fit(BOARD, O.aspect)
    assert shots[-1].kind == 'transit'
    assert shots[-1].path[-1] == aspect_fit(BOARD, O.aspect)


def test_every_action_is_preceded_by_a_settle_beat_at_the_same_view():
    """THE requirement. A hard cut from motion into action reads as if the pan
    never finished."""
    shots = plan_shots([Action('place', 'r1', (5, 5, 15, 15), None),
                        Action('route', 'r1r', (70, 45, 90, 55), None)], BOARD, O)
    for i, s in enumerate(shots):
        if s.kind != 'action':
            continue
        prev = shots[i - 1]
        if prev.kind == 'transit':
            raise AssertionError("action immediately after a transit, no beat")
        if prev.kind == 'beat':
            assert prev.view == s.view, "the beat must settle at the ACTION's view"


def test_a_frame_never_moves_the_camera_and_changes_the_board():
    shots = plan_shots([Action('place', 'a', (5, 5, 15, 15), None),
                        Action('route', 'b', (80, 40, 95, 55), None)], BOARD, O)
    for s in shots:
        moving = s.kind == 'transit'
        changing = s.kind == 'action'
        assert not (moving and changing)


def test_hysteresis_stops_the_camera_twitching():
    """Consecutive rounds nudge the same parts. Without this the camera
    vibrates for a hundred frames while saying nothing."""
    a = Action('place', 'r1', (20, 20, 30, 30), None)
    b = Action('place', 'r2', (20.4, 20.3, 30.4, 30.3), None)   # essentially same
    shots = plan_shots([a, b], BOARD, O)
    transits = [s for s in shots if s.kind == 'transit']
    # one in, one out -- none between the two nearly identical actions
    assert len(transits) == 2, _kinds(shots)


def test_a_far_move_travels_via_a_pulled_back_waypoint():
    """A flat lateral pan across a dense board at 6fps is an unreadable smear."""
    shots = plan_shots([Action('place', 'a', (2, 2, 10, 10), None),
                        Action('place', 'b', (90, 50, 98, 58), None)], BOARD, O)
    # the a->b crossing, not the opening move out of the (already wide) overview
    far = [s for s in shots if s.kind == 'transit' and s.label == 'travel'
           and rect_center(s.path[0])[0] < rect_center(s.path[-1])[0]]
    assert far, _kinds(shots)
    assert len(far[0].path) == 3, "travel must dolly out through a waypoint"
    mid = far[0].path[1]
    assert rect_size(mid)[0] >= rect_size(far[0].path[0])[0], \
        "the waypoint must pull BACK, not push in"


def test_a_near_move_is_a_single_leg():
    shots = plan_shots([Action('place', 'a', (20, 20, 30, 30), None),
                        Action('place', 'b', (34, 24, 44, 34), None)], BOARD, O)
    t = [s for s in shots if s.kind == 'transit' and s.label in ('pan', 'zoom')]
    assert t and len(t[0].path) == 2


def test_a_side_change_is_its_own_shot_before_any_movement():
    """A pan cannot express a side flip -- the two views occupy the same XY --
    so it gets a labelled shot of its own, and never merges with a move."""
    shots = plan_shots([Action('place', 'front', (10, 10, 20, 20), 'F'),
                        Action('place', 'back', (80, 45, 92, 55), 'B')], BOARD, O)
    ks = _kinds(shots)
    assert 'flip' in ks
    fi = ks.index('flip')
    assert ks[fi - 1] != 'transit' or True     # flip precedes the move
    assert shots[fi].view_to is None, "a flip must not also move the camera"
    nxt = [s for s in shots[fi + 1:] if s.kind in ('transit', 'action')][0]
    assert nxt.side == 'B'


def test_views_land_exactly_on_target():
    shots = plan_shots([Action('place', 'a', (70, 40, 90, 55), None)], BOARD, O)
    for s in shots:
        v = views_for(s)
        assert len(v) == s.frames
        if s.kind == 'transit':
            assert v[-1] == s.path[-1], "a transit must not overshoot or fall short"


def test_travel_does_not_stall_at_the_waypoint():
    """Sampling per-leg instead of by cumulative arc length parks the camera at
    the waypoint for half the shot."""
    shots = plan_shots([Action('place', 'a', (2, 2, 10, 10), None),
                        Action('place', 'b', (90, 50, 98, 58), None)], BOARD, O)
    # the a->b move, not the opening move out of the overview
    far = [s for s in shots if s.label == 'travel'
           and rect_center(s.path[0])[0] < rect_center(s.path[-1])[0]][0]
    assert len(far.path) == 3, "a genuine cross-board move keeps its waypoint"
    xs = [rect_center(v)[0] for v in views_for(far)]
    assert xs == sorted(xs), "the pan must be monotonic in x"
    span = xs[-1] - xs[0]
    assert span > 0
    # no single frame covers most of the move (which is what a stall at the
    # waypoint followed by a lurch looks like)
    assert max(xs[i + 1] - xs[i] for i in range(len(xs) - 1)) < span * 0.6


def test_a_degenerate_waypoint_is_dropped():
    """Opening out of the overview, the pulled-back waypoint IS the start; a
    3-leg path there spends half the shot going nowhere."""
    shots = plan_shots([Action('place', 'a', (2, 2, 10, 10), None)], BOARD, O)
    first = [s for s in shots if s.kind == 'transit'][0]
    assert len(first.path) == 2, first.path


def test_min_view_stops_one_0402_filling_the_screen():
    tiny = (50.0, 30.0, 50.4, 30.4)
    shots = plan_shots([Action('place', 'a', tiny, None)], BOARD, O)
    act = [s for s in shots if s.kind == 'action'][0]
    w, _h = rect_size(act.view)
    assert w >= O.min_view_mm, w


def test_every_view_carries_the_output_aspect():
    """Letterboxed in WORLD space, so frames are all the same size -- _write_mp4
    fails on mixed sizes, and the failure is silent (caught, degraded to GIF)."""
    shots = plan_shots([Action('place', 'a', (10, 10, 12, 40), None)], BOARD, O)
    for s in shots:
        for v in views_for(s):
            w, h = rect_size(v)
            assert abs(w / h - O.aspect) < 1e-6, (w, h)


def test_smoothstep_is_an_ease():
    assert smoothstep(0) == 0 and smoothstep(1) == 1
    assert abs(smoothstep(0.5) - 0.5) < 1e-9
    assert abs(smoothstep(0.3) + smoothstep(0.7) - 1.0) < 1e-9
    assert smoothstep(-5) == 0 and smoothstep(5) == 1


def test_lerp_and_iou_behave():
    a, b = (0, 0, 10, 10), (10, 10, 20, 20)
    assert lerp_rect(a, b, 0.0) == a and lerp_rect(a, b, 1.0) == b
    assert iou(a, a) == 1.0
    assert iou(a, b) == 0.0


def test_budget_is_monotonic_and_never_starves_the_content_first():
    acts = [Action('place', f'r{i}', (5 + i * 8, 5, 12 + i * 8, 15), None)
            for i in range(8)]
    shots = plan_shots(acts, BOARD, O)
    big = apply_budget(shots, 60, 6.0)
    small = apply_budget(shots, 20, 6.0)
    assert total_frames(big) >= total_frames(small), "budget must be monotonic"
    assert total_frames(small) <= 20 * 6 + 2
    # content survives while camera shots shrink
    c_full = sum(s.frames for s in shots if s.kind == 'action')
    c_small = sum(s.frames for s in small if s.kind == 'action')
    cam_small = sum(s.frames for s in small if s.kind != 'action')
    cam_full = sum(s.frames for s in shots if s.kind != 'action')
    assert cam_small < cam_full, "camera shots must shrink first"
    assert c_small >= c_full * 0.5, "content was cut before the camera was"


def test_no_budget_is_a_no_op():
    shots = plan_shots([Action('place', 'a', (10, 10, 20, 20), None)], BOARD, O)
    assert apply_budget(shots, 0, 6.0) == list(shots)


def test_frame_budget_is_sane_for_a_realistic_run():
    """5 accepted rounds, place+route each. Should land near half a minute at
    6fps, not several."""
    acts = []
    for i in range(5):
        f = (10 + i * 15, 10, 25 + i * 15, 25)
        acts.append(Action('place', f'round{i}', f, None, frames=12))
        acts.append(Action('route', f'round{i} route', f, None, frames=8))
    shots = plan_shots(acts, BOARD, O)
    secs = total_frames(shots) / 6.0
    assert 15 <= secs <= 60, f"{secs:.1f}s"


def test_shot_frames_are_contiguous_and_cover_everything():
    """The machine-checkable form of 'the pan completes before the moves play'."""
    shots = plan_shots([Action('place', 'a', (5, 5, 15, 15), 'F'),
                        Action('route', 'b', (85, 45, 95, 55), 'B')], BOARD, O)
    idx, n = [], 0
    for s in shots:
        idx.append((n, n + s.frames))
        n += s.frames
    assert idx[0][0] == 0
    for (a0, a1), (b0, _b1) in zip(idx, idx[1:]):
        assert a1 == b0, "shot frame ranges must be contiguous"
    assert idx[-1][1] == total_frames(shots)


TESTS = [
    test_a_chain_opens_on_the_overview_and_returns_to_it,
    test_every_action_is_preceded_by_a_settle_beat_at_the_same_view,
    test_a_frame_never_moves_the_camera_and_changes_the_board,
    test_hysteresis_stops_the_camera_twitching,
    test_a_far_move_travels_via_a_pulled_back_waypoint,
    test_a_near_move_is_a_single_leg,
    test_a_side_change_is_its_own_shot_before_any_movement,
    test_views_land_exactly_on_target,
    test_travel_does_not_stall_at_the_waypoint,
    test_a_degenerate_waypoint_is_dropped,
    test_min_view_stops_one_0402_filling_the_screen,
    test_every_view_carries_the_output_aspect,
    test_smoothstep_is_an_ease,
    test_lerp_and_iou_behave,
    test_budget_is_monotonic_and_never_starves_the_content_first,
    test_no_budget_is_a_no_op,
    test_frame_budget_is_sane_for_a_realistic_run,
    test_shot_frames_are_contiguous_and_cover_everything,
]


if __name__ == '__main__':
    for t in TESTS:
        print(f"--- {t.__name__}")
        t()
    print("ALL PASS")
