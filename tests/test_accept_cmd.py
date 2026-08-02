#!/usr/bin/env python3
"""`place_route_loop --accept-cmd`: an external, spec-aware accept test.

The built-in comparator is `failures`, then `iterations`. That is all it can
see. A board with a real specification is decided by things it has no term for
-- a maximum routed length, a via ban, a required track width, a
decap-proximity limit -- so a loop driven by it will accept a round that broke
one of them and call the result an improvement.

`--accept-cmd` hands that judgement to the caller, who is the only party that
knows what the spec says. The contract is deliberately small so a judge can be a
five-line shell script:

    CMD <placed.kicad_pcb> <routed.kicad_pcb> <route.json>  ->  SCORE=<float>

Lower is better. Anything that is not a parseable SCORE line on a zero exit is a
REJECT, because a judge that cannot answer is not evidence that the round was
good.
"""
import os
import stat
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from place_route_loop import accept_score  # noqa: E402


def _judge(td, body, name='judge.py'):
    """A judge as a real subprocess, since that is how the loop runs one."""
    p = os.path.join(td, name)
    with open(p, 'w', encoding='utf-8') as f:
        f.write(body)
    os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)
    return f'"{sys.executable}" "{p}"'


def test_a_score_is_read_and_lower_is_better():
    with tempfile.TemporaryDirectory() as td:
        cmd = _judge(td, "print('noise on stdout')\nprint('SCORE=12.5')\n")
        score, note = accept_score(cmd, 'p.kicad_pcb', 'r.kicad_pcb', 'r.json')
        assert score == 12.5, note
        assert '12.5' in note
    print("  PASS: SCORE= parsed out of ordinary stdout")


def test_the_judge_receives_all_three_paths():
    """It cannot grade a spec without the board AND the routing result."""
    with tempfile.TemporaryDirectory() as td:
        cmd = _judge(td, "import sys\n"
                         "assert len(sys.argv) == 4, sys.argv\n"
                         "print('SCORE=%d' % len(sys.argv[1] + sys.argv[2] + sys.argv[3]))\n")
        score, note = accept_score(cmd, 'AA', 'BB', 'CC')
        assert score == 6, note
    print("  PASS: judge is called as CMD <placed> <routed> <json>")


def test_every_way_of_not_answering_is_a_reject():
    """A judge that cannot answer must never be read as approval."""
    with tempfile.TemporaryDirectory() as td:
        cases = [
            ("import sys\nprint('boom')\nsys.exit(3)\n", 'exit 3'),
            ("print('all good!')\n", 'no SCORE'),
            ("print('SCORE=banana')\n", 'unparseable'),
        ]
        for body, why in cases:
            score, note = accept_score(_judge(td, body), 'p', 'r', 'j')
            assert score is None, f"{why}: expected reject, got {score}"
            assert note, f"{why}: a rejection must say why"
        # A command that does not exist at all.
        score, note = accept_score('definitely-not-a-real-binary-xyz', 'p', 'r', 'j')
        assert score is None and 'could not run' in note
    print("  PASS: non-zero exit, missing SCORE, bad float, missing binary all reject")


def test_the_flag_exists_and_defaults_to_the_old_behaviour():
    """No flag must mean byte-identical behaviour to before."""
    import place_route_loop as prl
    import inspect
    src = inspect.getsource(prl)
    assert '--accept-cmd' in src
    # The built-in comparator is still reachable, and is still what runs when
    # no judge is supplied.
    assert 'else:\n            _accepted = better(metrics, best)' in src, (
        "better() must remain the default path -- reworking it is out of scope, "
        "the flag BYPASSES it")
    assert 'def better(' in src, "better() must not have been removed"
    print("  PASS: better() is untouched and remains the default")


def test_strictly_better_wins_and_ties_do_not():
    """The promotion rule, spelled out: a tie is not an improvement."""
    import re, inspect
    import place_route_loop as prl
    src = inspect.getsource(prl)
    m = re.search(r'_accepted = _score is not None and \(best_score is None\s*\n\s*or _score < best_score\)', src)
    assert m, "the accept rule should be: scored, and strictly below the incumbent"
    print("  PASS: strictly-lower-than-incumbent, and an unscoreable round loses")


if __name__ == '__main__':
    for k, v in sorted(globals().items()):
        if k.startswith('test_'):
            print(f"--- {k}")
            v()
    print("ALL PASS")
