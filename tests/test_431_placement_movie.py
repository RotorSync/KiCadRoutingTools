"""The camera wired into the real movie pipeline (#431).

`test_431_camera.py` covers the shot planner as data. This covers the
INTEGRATION: that `Stage` drives the existing `Movie`/`BoardRenderer`/
`save_movie` stack, that footprints actually animate, and -- the part that
matters most -- that the ROUTING movie is untouched when no stage is passed.

Asserts on the raw PIL frame list, never on the encoded file: Pillow's GIF
writer de-duplicates identical consecutive frames, so a settle beat (which is
deliberately N identical frames) is invisible in the output file.
"""

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import ImageChops  # noqa: E402

import animate_route as A  # noqa: E402
import make_movie as MM  # noqa: E402
import route_render as RR  # noqa: E402
from kicad_parser import parse_kicad_pcb  # noqa: E402
from movie_camera import Stage, load_round_sidecars  # noqa: E402
from placement.writer import write_placed_output  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KF = os.path.join(ROOT, 'kicad_files')
SEED = os.path.join(KF, 'interf_u_unrouted.kicad_pcb')
PLACED = os.path.join(KF, 'interf_u_unrouted_placed.kicad_pcb')


def _work_dir():
    """A work dir shaped like place_route_loop's: two accepted rounds carrying a
    real 22-part placement delta, plus one REJECTED round that must be skipped."""
    d = tempfile.mkdtemp()
    shutil.copy(SEED, os.path.join(d, 'loop_round0.kicad_pcb'))
    shutil.copy(SEED, os.path.join(d, 'loop_round0_routed.kicad_pcb'))
    shutil.copy(PLACED, os.path.join(d, 'loop_round1.kicad_pcb'))
    shutil.copy(PLACED, os.path.join(d, 'loop_round2.kicad_pcb'))
    shutil.copy(PLACED, os.path.join(d, 'loop_round2_routed.kicad_pcb'))

    # Two rounds working on well-SEPARATED regions -- otherwise the camera is
    # right not to move (hysteresis), and a test asserting a pan would be
    # asserting a bug. Left cluster, then right cluster.
    b = parse_kicad_pcb(PLACED)
    by_x = sorted(b.footprints.items(), key=lambda kv: kv[1].x)
    left = [r for r, _f in by_x[:3]]
    right = [r for r, _f in by_x[-3:]]

    def _moves(refs):
        out = []
        for ref in sorted(refs):
            fp = b.footprints[ref]
            out.append({'reference': ref,
                        'from': [fp.x - 2.0, fp.y - 1.5, fp.rotation or 0.0],
                        'to': [fp.x, fp.y, fp.rotation or 0.0]})
        return out

    docs = [
        {'schema': 1, 'round': 0, 'board': 'loop_round0.kicad_pcb',
         'routed': 'loop_round0_routed.kicad_pcb', 'parent': None,
         'accepted': True, 'screened': False, 'moved': _moves(left),
         'metrics': {}},
        # REJECTED: on disk, but not the story. Must not appear in the chain.
        {'schema': 1, 'round': 1, 'board': 'loop_round1.kicad_pcb',
         'routed': None, 'parent': 'loop_round0.kicad_pcb',
         'accepted': False, 'screened': False, 'moved': _moves(left),
         'metrics': {}},
        {'schema': 1, 'round': 2, 'board': 'loop_round2.kicad_pcb',
         'routed': 'loop_round2_routed.kicad_pcb',
         'parent': 'loop_round0.kicad_pcb',      # NOT round1 -- it was rejected
         'accepted': True, 'screened': False, 'moved': _moves(right),
         'metrics': {}},
    ]
    moved = _moves(left)
    for doc in docs:
        with open(os.path.join(d, f"loop_round{doc['round']}.json"), 'w',
                  encoding='utf-8') as f:
            json.dump(doc, f)
    return d, len(moved)


# --- the routing movie must not change --------------------------------------

def test_no_stage_means_no_camera_and_one_renderer():
    """Structural non-regression: with stage=None the hooks are falsy branches.
    Counting constructions and set_view calls proves it without a golden image
    (Pillow's resampling drifts between versions)."""
    made = {'ctor': 0, 'set_view': 0, 'pcb_assign': 0}
    orig = RR.BoardRenderer

    class Counting(orig):
        def __init__(self, *a, **k):
            made['ctor'] += 1
            super().__init__(*a, **k)

        def set_view(self, *a, **k):
            made['set_view'] += 1
            return super().set_view(*a, **k)

    RR.BoardRenderer = Counting
    try:
        d = tempfile.mkdtemp()
        try:
            out = MM.make_movie([SEED, PLACED], out=os.path.join(d, 'r.gif'),
                                size=200, quiet=True)
            assert out
        finally:
            shutil.rmtree(d, ignore_errors=True)
    finally:
        RR.BoardRenderer = orig
    assert made['ctor'] == 1, f"{made['ctor']} renderers built (expected 1)"
    # exactly the one inside __init__; the camera never aims it
    assert made['set_view'] == 1, made['set_view']


def test_build_boards_signature_keeps_stage_optional():
    import inspect
    sig = inspect.signature(A.build_boards)
    assert sig.parameters['stage'].default is None


# --- the blocker, as an assertion -------------------------------------------

def test_moving_parts_animates_only_with_a_stage():
    """The whole reason a placement movie was impossible: build_boards points
    the renderer at the FINAL board, so parts sit at their final poses from
    frame 0. With dynamic_zones=True (which build_boards passes) frame() draws
    pads per frame from renderer.pcb, so re-pointing it animates them."""
    pcb_a, pcb_b = parse_kicad_pcb(SEED), parse_kicad_pcb(PLACED)
    r = RR.BoardRenderer(pcb_a, size=260, supersample=1, dynamic_zones=True)
    before = r.frame()
    r.pcb = pcb_b
    after = r.frame()
    assert ImageChops.difference(before, after).getbbox() is not None, \
        "re-pointing renderer.pcb did not move the parts"

    r2 = RR.BoardRenderer(pcb_a, size=260, supersample=1, dynamic_zones=False)
    b2 = r2.frame()
    r2.pcb = pcb_b
    assert ImageChops.difference(b2, r2.frame()).getbbox() is None, \
        "with dynamic_zones=False pads are baked into _base -- this is why the " \
        "animator's dynamic_zones=True is load-bearing, not incidental"


# --- the chain ---------------------------------------------------------------

def test_only_accepted_rounds_enter_the_chain():
    d, _n = _work_dir()
    try:
        chain = MM.placement_chain(d)
        assert chain is not None
        labels = [s[0] for s in chain[0]]
        assert 'round 1' not in labels, \
            f"a REJECTED round entered the chain: {labels}"
        assert labels[0] == 'round 0' and 'round 2' in labels
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_a_dir_without_sidecars_is_still_a_routing_run():
    """placement_chain must key on the SIDECARS, not a loop_round*.kicad_pcb
    glob -- --work-dir defaults to the output board's directory, which may hold
    unrelated boards."""
    d = tempfile.mkdtemp()
    try:
        shutil.copy(SEED, os.path.join(d, 'loop_round0.kicad_pcb'))
        assert MM.placement_chain(d) is None
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_sidecar_loader_orders_by_round_not_by_name():
    d = tempfile.mkdtemp()
    try:
        for n in (0, 2, 10):
            with open(os.path.join(d, f'loop_round{n}.json'), 'w',
                      encoding='utf-8') as f:
                json.dump({'schema': 1, 'round': n, 'accepted': True,
                           'board': f'loop_round{n}.kicad_pcb'}, f)
        got = [x['round'] for x in load_round_sidecars(d)]
        assert got == [0, 2, 10], f"{got} -- round 10 must not sort before 2"
    finally:
        shutil.rmtree(d, ignore_errors=True)


# --- the camera in the pipeline ---------------------------------------------

def _camera_frames(size=240):
    d, n_moved = _work_dir()
    try:
        steps, final = MM.placement_chain(d)
        stage = Stage(load_round_sidecars(d), d, tween=6)
        frames = A.build_boards(steps, final, size, 1, 150, 2, 6, stage=stage)
        return frames, stage, n_moved
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_every_frame_is_the_same_size():
    """The single highest-value assertion here. _write_mp4 fails on mixed sizes
    and the failure is SILENT -- caught, and the whole movie degraded to GIF."""
    frames, _stage, _n = _camera_frames()
    assert frames
    assert len({f.size for f in frames}) == 1, {f.size for f in frames}


def test_the_camera_moves_and_then_holds():
    """Motion during a transit, stillness during a settle beat. Asserted on the
    RAW frames: the GIF writer de-duplicates identical consecutive frames, so a
    beat is invisible in the encoded file."""
    frames, stage, _n = _camera_frames()
    log = stage.frame_log()
    assert log, "the stage recorded no shots"
    kinds = [k for k, _a, _b in log]
    assert 'establish' in kinds or 'transit' in kinds, kinds

    def changed(i):
        return ImageChops.difference(frames[i], frames[i + 1]).getbbox() is not None

    moved_any = False
    for kind, a, b in log:
        if kind == 'transit' and b - a >= 3:
            assert any(changed(i) for i in range(a, min(b, len(frames)) - 1)), \
                "a transit produced no motion"
            moved_any = True
        if kind == 'beat' and b - a >= 2:
            assert not any(changed(i) for i in range(a, min(b, len(frames)) - 1)), \
                "a settle beat moved -- the board or camera changed during it"
    assert moved_any, "no transit shot emitted at all"


def test_the_parts_actually_glide():
    frames, stage, n_moved = _camera_frames()
    assert n_moved == 3, n_moved
    acts = [(a, b) for k, a, b in stage.frame_log() if k == 'action']
    assert acts, "no tween emitted"
    a, b = acts[-1]
    assert b - a >= 2
    assert ImageChops.difference(frames[a], frames[b - 1]).getbbox() is not None, \
        "the tween's first and last frames are identical -- nothing moved"


def test_the_movie_writes_and_reports_its_path():
    d, _n = _work_dir()
    out = os.path.join(d, 'placement.gif')
    try:
        got = MM.make_movie([d], out=out, camera='auto', size=200, quiet=True)
        assert got and os.path.exists(got), got
        assert os.path.getsize(got) > 1000
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_camera_defaults_off_and_the_env_knob_turns_it_on():
    """One variable covers the GUI recorder, run_plan.py --movie and the stress
    renderer at once -- and OFF is the default everywhere, because every GUI
    movie is a routing movie."""
    import env_knobs
    old = os.environ.get('KICAD_MOVIE_CAMERA')
    try:
        os.environ.pop('KICAD_MOVIE_CAMERA', None)
        env_knobs.refresh()
        assert env_knobs.MOVIE_CAMERA == 'off'
        os.environ['KICAD_MOVIE_CAMERA'] = 'auto'
        env_knobs.refresh()
        assert env_knobs.MOVIE_CAMERA == 'auto'
    finally:
        if old is None:
            os.environ.pop('KICAD_MOVIE_CAMERA', None)
        else:
            os.environ['KICAD_MOVIE_CAMERA'] = old
        env_knobs.refresh()


def test_camera_on_a_dir_with_no_sidecars_degrades_with_a_note():
    """Asking for a camera where there is nothing to drive must still produce a
    movie, not an exception."""
    d = tempfile.mkdtemp()
    try:
        shutil.copy(SEED, os.path.join(d, 'step01_route.kicad_pcb'))
        shutil.copy(PLACED, os.path.join(d, 'step02_route.kicad_pcb'))
        got = MM.make_movie([d], out=os.path.join(d, 'm.gif'), camera='auto',
                            size=200, quiet=True)
        assert got and os.path.exists(got)
    finally:
        shutil.rmtree(d, ignore_errors=True)


TESTS = [
    test_no_stage_means_no_camera_and_one_renderer,
    test_build_boards_signature_keeps_stage_optional,
    test_moving_parts_animates_only_with_a_stage,
    test_only_accepted_rounds_enter_the_chain,
    test_a_dir_without_sidecars_is_still_a_routing_run,
    test_sidecar_loader_orders_by_round_not_by_name,
    test_every_frame_is_the_same_size,
    test_the_camera_moves_and_then_holds,
    test_the_parts_actually_glide,
    test_the_movie_writes_and_reports_its_path,
    test_camera_defaults_off_and_the_env_knob_turns_it_on,
    test_camera_on_a_dir_with_no_sidecars_degrades_with_a_note,
]


if __name__ == '__main__':
    for t in TESTS:
        print(f"--- {t.__name__}")
        t()
    print("ALL PASS")
