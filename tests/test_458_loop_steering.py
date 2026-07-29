"""
Tests for how place_route_loop steers the quench (issue #458).

The loop widens --max-displacement 1.5x after a rejected round. Since #455
gave quench a swap cap that DEFAULTS to max_displacement, that widening used
to widen swap teleports in lockstep: a 3mm base reaches 15mm after four
rejections, which is the #430 stranding failure coming back through the loop.
The loop now passes swap_max_displacement explicitly and holds it at the base
value while the nudge radius grows, and it plumbs --no-rotate / --no-swap /
--verbose through to quench.

Nothing is routed here. The routing step, the quench and the writer are
replaced with recorders, so the whole file is a unit test: no router binary,
no board is written, and it stays in the run_all.py --fast lane.
"""

import inspect
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import place_route_loop as prl
from placement.quench import quench as real_quench


def _loop_board():
    """One movable 2-pad part C1 on net NA plus a locked anchor J1 on the
    same net, inside a (100,80)-(200,120) outline. The loop only needs the
    board to parse and to yield one movable target ref for net NA."""
    body = '(kicad_pcb\n\t(version 20241229)\n'
    body += '\t(net 0 "")\n\t(net 1 "NA")\n'
    body += '''\t(gr_rect
\t\t(start 100 80)
\t\t(end 200 120)
\t\t(stroke
\t\t\t(width 0.1)
\t\t\t(type solid)
\t\t)
\t\t(layer "Edge.Cuts")
\t\t(uuid "edge1")
\t)
\t(footprint "test:CAP2P"
\t\t(layer "F.Cu")
\t\t(uuid "fp-C1")
\t\t(at 150 100)
\t\t(property "Reference" "C1"
\t\t\t(at 0 0)
\t\t)
\t\t(pad "1" smd rect
\t\t\t(at -0.5 0)
\t\t\t(size 0.8 0.8)
\t\t\t(layers "F.Cu")
\t\t\t(net 1 "NA")
\t\t\t(uuid "p1-C1")
\t\t)
\t)
\t(footprint "test:PIN"
\t\t(layer "F.Cu")
\t\t(locked yes)
\t\t(uuid "fp-J1")
\t\t(at 170 100)
\t\t(property "Reference" "J1"
\t\t\t(at 0 0)
\t\t)
\t\t(pad "1" smd rect
\t\t\t(at 0 0)
\t\t\t(size 1 1)
\t\t\t(layers "F.Cu")
\t\t\t(net 1 "NA")
\t\t\t(uuid "p1-J1")
\t\t)
\t)
)
'''
    fd, path = tempfile.mkstemp(suffix='.kicad_pcb')
    with os.fdopen(fd, 'w') as f:
        f.write(body)
    return path, tempfile.mkdtemp()


def _run_loop(extra_args, rounds=4, quench_result=None):
    """Run main() with the routing step, the quench and the writer replaced.
    Returns the list of kwargs quench was called with, one entry per round."""
    calls = []
    placements = ([{'reference': 'C1', 'new_x': 151.0, 'new_y': 100.0,
                    'new_rotation': 0.0}] if quench_result is None
                  else quench_result)

    def fake_quench(pcb_data, **kw):
        calls.append(dict(kw))
        return placements

    def fake_run_route(pcb_file, routed_file, route_args, log_file):
        # Every candidate scores exactly like round 0, so better() is False
        # and every round is REJECTED: the cap widens on every round.
        return {'failures': 2, 'failed_nets': ['NA'], 'blockers': [],
                'iterations': 1000, 'vias': 0}

    board, work = _loop_board()
    saved = (prl.quench, prl.run_route, prl.write_placed_output, sys.argv)
    prl.quench = fake_quench
    prl.run_route = fake_run_route
    prl.write_placed_output = lambda src, dst, pl: shutil.copy(src, dst)
    sys.argv = ['place_route_loop.py', board,
                os.path.join(work, 'out.kicad_pcb'),
                '--route-args', '--nets "*"', '--rounds', str(rounds),
                '--max-displacement', '3.0', '--work-dir', work] + extra_args
    try:
        prl.main()
    finally:
        (prl.quench, prl.run_route, prl.write_placed_output,
         sys.argv) = saved
        os.unlink(board)
        shutil.rmtree(work, ignore_errors=True)
    return calls


def test_swap_cap_held_while_displacement_widens():
    """The headline invariant: four rejected rounds widen the nudge cap from
    3mm to 10.1mm while the swap cap stays at the base 3mm."""
    calls = _run_loop([])
    assert len(calls) == 4, f"expected one quench per round, got {len(calls)}"
    assert [c['max_displacement'] for c in calls] == [3.0, 4.5, 6.75, 10.125]
    assert [c['swap_max_displacement'] for c in calls] == [3.0, 3.0, 3.0, 3.0]
    for c in calls:
        # quench() raises ValueError if this is ever violated.
        assert c['swap_max_displacement'] <= c['max_displacement'] + 1e-9


def test_swap_cap_held_when_quench_finds_nothing():
    """The other widening site: an empty quench result widens the cap without
    a candidate route, and must not widen swaps either."""
    calls = _run_loop([], quench_result=[])
    assert [c['max_displacement'] for c in calls] == [3.0, 4.5, 6.75, 10.125]
    assert [c['swap_max_displacement'] for c in calls] == [3.0] * 4


def test_swap_cap_flag_overrides_the_base():
    calls = _run_loop(['--swap-max-displacement', '1.0'])
    assert [c['swap_max_displacement'] for c in calls] == [1.0] * 4


def test_rotate_and_swap_flags_reach_quench():
    """--no-rotate / --no-swap / --verbose exist on the loop and arrive at
    quench; without them both move types stay enabled."""
    calls = _run_loop([])
    assert all(c['allow_rotations'] is True and c['allow_swaps'] is True
               and c['verbose'] is False for c in calls), \
        "defaults must keep both move types enabled and stay quiet"
    calls = _run_loop(['--no-rotate', '--no-swap', '--verbose'])
    assert all(c['allow_rotations'] is False and c['allow_swaps'] is False
               and c['verbose'] is True for c in calls)


def test_kwargs_match_the_real_quench_signature():
    """Guards the recorder itself: binding the captured kwargs against the
    real signature fails on a renamed keyword that a stub would swallow."""
    sig = inspect.signature(real_quench)
    for c in _run_loop([]):
        sig.bind(None, **c)


def test_swap_cap_above_displacement_is_rejected():
    """Mirrors place_optimize.py's argparse check rather than letting the
    value reach quench and raise ValueError from inside a round."""
    try:
        _run_loop(['--swap-max-displacement', '9.0'])
    except SystemExit as exc:
        assert exc.code != 0
    else:
        raise AssertionError("--swap-max-displacement 9.0 must be rejected "
                             "against a 3.0mm --max-displacement")


TESTS = [
    test_swap_cap_held_while_displacement_widens,
    test_swap_cap_held_when_quench_finds_nothing,
    test_swap_cap_flag_overrides_the_base,
    test_rotate_and_swap_flags_reach_quench,
    test_kwargs_match_the_real_quench_signature,
    test_swap_cap_above_displacement_is_rejected,
]


if __name__ == '__main__':
    for fn in TESTS:
        fn()
    print("ALL PASS")
