"""`render_placement.py` -- the placement still (#431).

Geometry and metrics are asserted as NUMBERS, separately from the drawing.
Pixels are asserted as INVARIANTS (a toggle changes the image, an overlay lands
where the transform says), never as a committed checksum: Pillow's LANCZOS and
default-font rendering drift between versions and a stored hash would red-fail a
contributor's box for reasons unrelated to this code.

The load-bearing one is `test_rects_come_from_the_optimizers_own_model`. The
renderer must not own any coordinate arithmetic -- the moment someone inlines a
courtyard transform here, the picture starts disagreeing with the grader that
decided the placement, and every later "why does the render show X but the
optimizer say Y" is unanswerable.
"""

import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import ImageChops  # noqa: E402

import render_placement as RP  # noqa: E402
from kicad_parser import parse_kicad_pcb  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KF = os.path.join(ROOT, 'kicad_files')
SEED = os.path.join(KF, 'interf_u_unrouted.kicad_pcb')
PLACED = os.path.join(KF, 'interf_u_unrouted_placed.kicad_pcb')
ULX = os.path.join(KF, 'ulx3s.kicad_pcb')


def _model(board=PLACED):
    return RP.PlacementModel(parse_kicad_pcb(board), board)


def _run(*args):
    return subprocess.run([sys.executable, '-X', 'utf8',
                           os.path.join(ROOT, 'render_placement.py')] + list(args),
                          capture_output=True, text=True, cwd=ROOT, timeout=1800)


# --- model / geometry: numbers, not pixels ----------------------------------

def test_moved_parts_finds_the_tracked_delta():
    """interf_u ships a real 22-part placement delta; C4 moves 95.654mm."""
    moves = RP.moved_parts(parse_kicad_pcb(SEED), parse_kicad_pcb(PLACED))
    assert len(moves) == 22, len(moves)
    c4 = next(m for m in moves if m['reference'] == 'C4')
    assert abs(c4['dist'] - 95.654) < 1e-3, c4['dist']
    assert [m['reference'] for m in moves] == sorted(m['reference'] for m in moves), \
        "moves must be sorted (#457)"


def test_rects_come_from_the_optimizers_own_model():
    """THE anti-duplication assertion. Fails the day a courtyard transform is
    inlined into the renderer."""
    m = _model()
    for ref in sorted(m.parts())[:8]:
        assert m.rect(ref) == m.state.parts[ref].rect(), ref


def test_metrics_are_the_quenchs_own_numbers():
    m = _model()
    cost = m.state.total_cost()
    leg = m.state.legality_metrics()
    for k in ('crossings', 'hpwl', 'length'):
        assert m.metrics[k] == cost[k], k
    for k in ('overlap_area', 'oob_count'):
        assert m.metrics[k] == leg[k], k


def test_far_side_of_a_through_hole_part_is_its_drilled_pad_box():
    """`legality.rect_on` gates on this; drawing the courtyard on both sides
    would make the picture disagree with the grader."""
    m = _model()
    tht = [r for r in sorted(m.parts())
           if getattr(m.state.parts[r], 'has_tht', False)]
    assert tht, "fixture: expected through-hole parts"
    ref = tht[0]
    far, own = m.far_rect(ref), m.rect(ref)
    assert far is not None and far != own
    assert m.sides(ref) == {'F', 'B'}


def test_union_view_pads_and_floors():
    v = RP.union_view([(10.0, 10.0, 10.2, 10.2)], pad_mm=1.0, min_size=8.0)
    assert (v[2] - v[0]) >= 8.0 and (v[3] - v[1]) >= 8.0, \
        "one nudged 0402 must not fill the screen"
    cx = (v[0] + v[2]) / 2
    assert abs(cx - 10.1) < 1e-9, "the floor must expand about the centre"
    assert RP.union_view([]) is None


def test_clusters_are_deterministic_and_ordered():
    pts = [(0, 0), (0.5, 0), (40, 40), (40.5, 40), (41, 40)]
    a = RP.cluster_points(pts, 2.0)
    b = RP.cluster_points(list(reversed(pts)), 2.0)
    assert a == b, "cluster order must not depend on input order (#457)"
    assert len(a) == 2
    assert len(a[0]) == 3, "largest cluster first"


def test_zoom_group_resolves_exactly_like_route_py():
    """A name typed for `route.py --group` must work here unchanged."""
    from group_routing import block_refs
    from placement.groups import parse_sources
    pcb = parse_kicad_pcb(ULX)
    srcs = parse_sources('sheet')
    refs = block_refs(pcb, '58d913ec', srcs)
    assert refs and refs == block_refs(pcb, 'sheet:58d913ec', srcs)


def test_no_edge_cuts_board_reports_oob_unavailable():
    """The optimizer refuses a board with no outline, deliberately. The RENDERER
    must still work -- but must not print a 0 that reads as "clean"."""
    d = tempfile.mkdtemp()
    try:
        out = os.path.join(d, 'noedge.kicad_pcb')
        txt = open(SEED, encoding='utf-8').read()
        # drop every Edge.Cuts graphic
        import re
        txt = re.sub(r'\(gr_(line|arc|circle|rect|poly)[^()]*?'
                     r'\(layer "Edge\.Cuts"\)[\s\S]*?\)\s*\)', '', txt)
        open(out, 'w', encoding='utf-8', newline='').write(txt)
        pcb = parse_kicad_pcb(out)
        if pcb.board_info.board_bounds is not None:
            pcb.board_info.board_bounds = None      # force the path
        m = RP.PlacementModel(pcb, out)
        assert m.no_outline is True
        assert m.metrics['oob_count'] is None, "oob must be unavailable, not 0"
        assert m.metrics['crossings'] is not None, "everything else stays exact"
    finally:
        shutil.rmtree(d, ignore_errors=True)


# --- pixels: invariants -----------------------------------------------------

def _panel(**kw):
    m = _model()
    o = {'borders': True, 'labels': True, 'ratsnest': True, 'pads': True,
         'ghosts': True, 'arrows': True, 'delta_first': True,
         'ratsnest_all': False}
    o.update(kw.pop('opts', {}))
    spec = RP.PanelSpec(m, opts=o, **kw)
    return RP.render_panel(spec, size=420, supersample=1)


def test_toggles_measurably_change_the_image():
    base = _panel()
    for off in ('borders', 'labels', 'ratsnest', 'pads'):
        assert ImageChops.difference(base, _panel(opts={off: False})).getbbox() \
            is not None, f"--no-{off} changed nothing"


def test_ghosts_and_arrows_need_a_before_board():
    """`_Part.seed_x/seed_y` equals the file position for a freshly parsed
    board, so a delta is NOT free from one file -- it needs --before."""
    m = _model()
    o = dict(borders=True, labels=False, ratsnest=False, pads=False,
             ghosts=True, arrows=True, delta_first=True, ratsnest_all=False)
    plain = RP.render_panel(RP.PanelSpec(m, opts=o), size=420, supersample=1)
    moves = RP.moved_parts(parse_kicad_pcb(SEED), parse_kicad_pcb(PLACED))
    with_delta = RP.render_panel(RP.PanelSpec(m, moves=moves, opts=o),
                                 size=420, supersample=1)
    assert ImageChops.difference(plain, with_delta).getbbox() is not None


def test_rendering_is_reproducible_in_this_environment():
    """The checksum test worth having: identical bytes twice, and across two
    subprocesses with different PYTHONHASHSEED. Catches set-iteration order
    reaching the drawing layer (#457) without pinning a Pillow version."""
    assert _panel().tobytes() == _panel().tobytes()
    d = tempfile.mkdtemp()
    try:
        outs = []
        for seed in ('0', '12345'):
            env = dict(os.environ, PYTHONHASHSEED=seed)
            p = os.path.join(d, f'r{seed}.png')
            r = subprocess.run(
                [sys.executable, '-X', 'utf8',
                 os.path.join(ROOT, 'render_placement.py'), PLACED,
                 '-o', p, '--size', '300', '--supersample', '1', '--quiet'],
                capture_output=True, text=True, cwd=ROOT, env=env, timeout=1800)
            assert r.returncode == 0, r.stderr[-400:]
            outs.append(open(p, 'rb').read())
        assert outs[0] == outs[1], "render differs under a different hash seed"
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_per_side_panels_differ_on_a_two_sided_board():
    d = tempfile.mkdtemp()
    try:
        r = _run(ULX, '--per-side', '--size', '320', '--supersample', '1',
                 '-o', d, '--quiet')
        assert r.returncode == 0, r.stderr[-500:]
        pngs = sorted(f for f in os.listdir(d) if f.endswith('.png'))
        assert len(pngs) == 2, pngs
        a = open(os.path.join(d, pngs[0]), 'rb').read()
        b = open(os.path.join(d, pngs[1]), 'rb').read()
        assert a != b, "F and B panels are identical"
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_the_caption_carries_the_verdict_not_just_a_title():
    """#431 limit 3: geometry is not causality. Without these numbers a reader
    adopts the wrong heuristic -- "lots moved, looks broken" and "barely moved,
    looks safe" are both wrong."""
    m = _model()
    moves = RP.moved_parts(parse_kicad_pcb(SEED), parse_kicad_pcb(PLACED))
    cap = RP.caption(RP.PanelSpec(m, moves=moves, label='x'),
                     {'failures': 3, 'iterations': 1234, 'vias': 7})
    for token in ('crossings', 'hpwl', 'overlap', 'failures 3', '22 moved'):
        assert token in cap, (token, cap)


def test_cli_writes_a_png_and_a_machine_readable_summary():
    d = tempfile.mkdtemp()
    try:
        out = os.path.join(d, 'delta.png')
        r = _run(PLACED, '--before', SEED, '-o', out, '--json',
                 '--size', '320', '--supersample', '1')
        assert r.returncode == 0, r.stderr[-500:]
        assert os.path.getsize(out) > 1000
        line = [l for l in r.stdout.splitlines() if l.startswith('JSON_SUMMARY:')]
        assert line, r.stdout[-400:]
        import json
        doc = json.loads(line[0].split('JSON_SUMMARY:', 1)[1])
        assert doc['moved'] == 22
        assert doc['metrics']['crossings'] > 0
        assert doc['panels'][0]['path'] == out
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_an_unplaced_board_still_renders_and_says_so():
    """The renderer WARNS where the placement CLIs refuse -- seeing the state is
    the point -- and frames the parts, or a pile renders as a dot in a corner."""
    d = tempfile.mkdtemp()
    try:
        from placement.writer import write_placed_output
        src = os.path.join(KF, 'lvds_converter_dualclk.kicad_pcb')
        pcb = parse_kicad_pcb(src)
        bb = pcb.board_info.board_bounds
        pile = os.path.join(d, 'pile.kicad_pcb')
        write_placed_output(src, pile, [{'reference': r,
                                         'new_x': (bb[0] + bb[2]) / 2,
                                         'new_y': (bb[1] + bb[3]) / 2,
                                         'new_rotation': 0.0}
                                        for r in pcb.footprints])
        out = os.path.join(d, 'pile.png')
        r = _run(pile, '-o', out, '--size', '320', '--supersample', '1', '--json')
        assert r.returncode == 0, r.stderr[-500:]
        assert 'does not look PLACED' in r.stdout
        assert os.path.getsize(out) > 500
        import json
        doc = json.loads([l for l in r.stdout.splitlines()
                          if l.startswith('JSON_SUMMARY:')][0].split(':', 1)[1])
        assert doc['unplaced'] is True
        assert doc['panels'][0]['view'] is not None, \
            "an unplaced board must be framed to the PARTS, not the outline"
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_ratsnest_nets_draws_only_the_named_nets():
    """Between "the nets that moved" and "every net on the board" is the case
    that actually comes up: the handful you are chasing. Naming them also makes
    their parts prominent -- a set of wires with nothing identified is not much
    of an answer."""
    m = _model()
    all_ids = {n.net_id for n in m.pcb.nets.values()
               if n.net_id > 0 and len(n.pads) > 1}
    pick = m.net_ids_matching(['/BIT*'])
    assert pick, "fixture: expected some matching nets"
    assert pick < all_ids, "the pattern must select a strict subset"

    o = dict(borders=False, labels=False, ratsnest=True, pads=False,
             ghosts=False, arrows=False, delta_first=True, ratsnest_all=False)
    plain = RP.render_panel(RP.PanelSpec(m, opts=o), size=420, supersample=1)
    named = RP.render_panel(RP.PanelSpec(m, pick_nets=pick, opts=o),
                            size=420, supersample=1)
    assert ImageChops.difference(plain, named).getbbox() is not None
    # the picked colour must actually appear, and only when asked for
    assert RP.C_AIR_PICK in {c for _n, c in named.convert('RGB').getcolors(1 << 20)}
    assert RP.C_AIR_PICK not in {c for _n, c in plain.convert('RGB').getcolors(1 << 20)}


def test_ratsnest_nets_uses_the_shared_net_filter():
    """Same glob semantics as route.py --nets, exclusions included. A second
    matcher in a viewer would be a second set of surprises."""
    m = _model()
    from net_queries import matches_net_filter
    pats = ['*', '!/BIT*']
    got = m.net_ids_matching(pats)
    want = {n.net_id for n in m.pcb.nets.values()
            if n.net_id > 0 and n.name and matches_net_filter(n.name, pats)}
    assert got == want and got, "exclusion patterns must behave as elsewhere"
    assert not (got & m.net_ids_matching(['/BIT*']))


def test_ratsnest_nets_cli_reports_an_empty_match():
    """Silently rendering nothing would read as "this net has no airwires"."""
    d = tempfile.mkdtemp()
    try:
        r = _run(PLACED, '-o', os.path.join(d, 'x.png'), '--size', '260',
                 '--supersample', '1', '--ratsnest-nets', 'NOSUCHNET*')
        assert r.returncode == 0, r.stderr[-300:]
        assert 'no nets match' in (r.stdout + r.stderr)
    finally:
        shutil.rmtree(d, ignore_errors=True)


TESTS = [
    test_moved_parts_finds_the_tracked_delta,
    test_rects_come_from_the_optimizers_own_model,
    test_metrics_are_the_quenchs_own_numbers,
    test_far_side_of_a_through_hole_part_is_its_drilled_pad_box,
    test_union_view_pads_and_floors,
    test_clusters_are_deterministic_and_ordered,
    test_zoom_group_resolves_exactly_like_route_py,
    test_no_edge_cuts_board_reports_oob_unavailable,
    test_toggles_measurably_change_the_image,
    test_ghosts_and_arrows_need_a_before_board,
    test_rendering_is_reproducible_in_this_environment,
    test_per_side_panels_differ_on_a_two_sided_board,
    test_the_caption_carries_the_verdict_not_just_a_title,
    test_cli_writes_a_png_and_a_machine_readable_summary,
    test_an_unplaced_board_still_renders_and_says_so,
    test_ratsnest_nets_draws_only_the_named_nets,
    test_ratsnest_nets_uses_the_shared_net_filter,
    test_ratsnest_nets_cli_reports_an_empty_match,
]


if __name__ == '__main__':
    for t in TESTS:
        print(f"--- {t.__name__}")
        t()
    print("ALL PASS")
