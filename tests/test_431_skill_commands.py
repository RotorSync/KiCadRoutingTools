"""Every flag the skill and docs tell Claude to pass must actually exist (#431).

Most of a skill is prose and untestable. This is the part that ROTS: a doc
telling Claude to pass a flag that was renamed or never existed produces a
confident, wrong command, and nothing catches it until a user runs it.

`tests/run_doc_examples.py` reads ```python blocks from `docs/*.md` only -- not
`.claude/skills/`, and not bash blocks -- so it cannot cover this. Precedent for
the doc-vs-code gate: `run_doc_examples.gridrouteconfig_undocumented_fields` and
`tests/gui_parity/test_cli_postpass_coverage.py`.

Explicitly NOT testable, and worth saying rather than pretending: whether Claude
*decides correctly* not to run placement on a good board. The mitigations for
that are design, not assertion -- the default-off framing in the order
rationale, the decision table, and the board-state gates that refuse the worst
case outright.
"""

import importlib.util
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Files that instruct Claude or a human to run these tools.
SOURCES = [
    '.claude/skills/plan-pcb-routing/SKILL.md',
    # The skill's reference pages carry command blocks too (#549). Without them
    # every block moved out of SKILL.md becomes flag-unchecked, which is the
    # quiet way this gate stops gating.
    '.claude/skills/plan-pcb-routing/references/evidence-map.md',
    '.claude/skills/plan-pcb-routing/references/verifier-prompts.md',
    '.claude/skills/plan-pcb-routing/references/convergence.md',
    'docs/floorplan-intent.md',
    'docs/placement-optimization.md',
    'docs/claude-skills.md',
    'placement/README.md',
    'README.md',
]

TOOLS = ('place_optimize.py', 'place_route_loop.py', 'render_placement.py',
         'check_floorplan.py', 'make_movie.py',
         # Skill-local helper, cited by its full path in the docs -- so the
         # entry is the path, which is also what _continued_blocks matches on.
         '.claude/skills/plan-pcb-routing/scripts/board_score.py')

# Flags that belong to a DIFFERENT tool on the same command line (a pipe, a
# --route-args payload). --route-args carries route.py's flags verbatim.
_ROUTE_ARGS_RE = re.compile(r"--route-args\s+(['\"])(.*?)\1", re.S)


def _parser_for(tool):
    """Build the tool's real argparse parser and return its option strings."""
    path = os.path.join(ROOT, tool)
    # basename, not the whole entry: a TOOLS entry may be a PATH (skill-local
    # helpers live under .claude/skills/...), and a module name carrying path
    # separators is legal but confusing in tracebacks.
    spec = importlib.util.spec_from_file_location(
        os.path.basename(tool)[:-3] + '_probe', path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    if hasattr(mod, 'build_parser'):
        p = mod.build_parser()
    else:
        # place_optimize / place_route_loop build the parser inside main(); run
        # it with --help intercepted so we get the parser without executing.
        import argparse
        got = {}
        real_parse = argparse.ArgumentParser.parse_args

        def capture(self, *a, **kw):
            got['p'] = self
            raise SystemExit(0)
        argparse.ArgumentParser.parse_args = capture
        try:
            mod.main()
        except SystemExit:
            pass
        finally:
            argparse.ArgumentParser.parse_args = real_parse
        p = got.get('p')
        assert p is not None, f"could not capture {tool}'s parser"
    return {s for a in p._actions for s in a.option_strings}


def _cited_flags(block, tool):
    """Every flag in one whole shell command that invokes `tool`.

    Scans the ENTIRE block, continuation lines included. Filtering to lines
    containing the tool name (the obvious first cut) reads only the first line
    of a backslash-continued command and silently checks almost nothing -- this
    gate found 5 flags that way instead of 20.
    """
    # strip --route-args payloads: those are route.py's flags, not this tool's
    block = _ROUTE_ARGS_RE.sub(' ', block)
    return set(re.findall(r'(--[a-z][a-z0-9-]+)', block))


def _continued_blocks(text, tool):
    """Whole shell commands (handling trailing backslashes) that run `tool`."""
    blocks, cur = [], None
    for line in text.splitlines():
        if cur is not None:
            cur.append(line)
            if not line.rstrip().endswith('\\'):
                blocks.append('\n'.join(cur))
                cur = None
            continue
        if tool in line and not line.lstrip().startswith('#'):
            cur = [line]
            if not line.rstrip().endswith('\\'):
                blocks.append('\n'.join(cur))
                cur = None
    return blocks


def test_every_documented_flag_exists():
    problems = []
    checked = 0
    for tool in TOOLS:
        try:
            valid = _parser_for(tool)
        except Exception as e:
            problems.append((tool, '<parser>', f"{type(e).__name__}: {e}"))
            continue
        for src in SOURCES:
            path = os.path.join(ROOT, src)
            if not os.path.isfile(path):
                continue
            text = open(path, encoding='utf-8', errors='replace').read()
            for block in _continued_blocks(text, tool):
                for flag in _cited_flags(block, tool):
                    checked += 1
                    if flag not in valid:
                        problems.append((tool, src, flag))
    assert not problems, "documented flags that do not exist:\n" + "\n".join(
        f"  {t}  in {s}:  {f}" for t, s, f in problems)
    # A gate that checks nothing passes for the wrong reason. The docs cite well
    # over a dozen flags across these tools; if this trips, the block/flag
    # scanner stopped matching rather than the docs becoming clean.
    assert checked >= 15, f"only {checked} flag citations found -- scanner broken?"
    print(f"  PASS: {checked} flag citations, all real")


def test_the_placement_tools_are_actually_mentioned():
    """Guards the reverse failure: the gate passing because the skill stopped
    mentioning placement at all."""
    skill = open(os.path.join(ROOT, SOURCES[0]), encoding='utf-8').read()
    for token in ('place_optimize.py', 'render_placement.py', '--suggest-locks',
                  'Step 0'):
        assert token in skill, f"{token} missing from the skill"


def test_exit_code_contract_is_documented():
    """The skill tells Claude to branch on exit 3. If the constant moves and the
    docs do not, the instruction silently becomes wrong."""
    from placement.placement_state import UNPLACED_EXIT
    assert UNPLACED_EXIT == 3
    skill = open(os.path.join(ROOT, SOURCES[0]), encoding='utf-8').read()
    assert 'exit 3' in skill or 'exits 3' in skill, \
        "the skill must state the exit-3 contract it tells Claude to rely on"


def test_skill_says_placement_is_off_by_default():
    """The single most important thing for a model to get right here."""
    skill = open(os.path.join(ROOT, SOURCES[0]), encoding='utf-8').read()
    assert 'normally SKIPPED' in skill or 'do not run it' in skill
    assert 'decision table' in skill.lower()
    # and that the render is not mistaken for the verdict (#431 limit 3)
    assert 'triage, not a verdict' in skill


def test_routing_only_stays_the_default_path():
    """#549. Placement must stay reachable only through a board-state branch or
    a post-failure branch, never on the path of "here is a board, route it".

    The structural guarantee is that placement cannot enter a plan at all. It is
    load-bearing rather than tidy: ai_plan DROPS an unknown action with a
    one-line note and RUNS THE REMAINING STEPS ANYWAY, so a `{"action":"place"}`
    step would silently route an unplaced board.
    """
    sys.path.insert(0, os.path.join(ROOT, 'kicad_routing_plugin'))
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        '_ai_plan_probe', os.path.join(ROOT, 'kicad_routing_plugin', 'ai_plan.py'))
    src = open(spec.origin, encoding='utf-8').read()
    m = re.search(r'KNOWN_ACTIONS\s*=\s*\(([^)]*)\)', src, re.S)
    assert m, "KNOWN_ACTIONS not found in ai_plan.py"
    actions = re.findall(r"['\"]([a-z_]+)['\"]", m.group(1))
    assert actions, actions
    for bad in ('place', 'placement', 'place_optimize', 'place_route_loop',
                'quench', 'floorplan'):
        assert bad not in actions, (
            f"{bad!r} became a plan action. ai_plan drops unknown actions and "
            f"runs the rest, so a placement step in a plan silently routes an "
            f"un-placed board")

    # And the skill's own plan TEMPLATE must not grow one either.
    skill = open(os.path.join(ROOT, SOURCES[0]), encoding='utf-8').read()
    fences = re.findall(r'```[^\n]*\n(.*?)```', skill, re.S)
    template = max((f for f in fences if '"action"' in f or 'Step-by-Step' in f),
                   key=len, default='')
    assert template, "the example plan template was not found"
    for tool in ('place_optimize.py', 'place_route_loop.py'):
        assert tool not in template, \
            f"{tool} appeared in the plan template; placement is CLI-only"
    print(f"  PASS: {len(actions)} plan actions, none placement; "
          f"template clean")


def test_skill_states_the_board_outline_is_not_editable():
    """#549. True today only by construction -- no writer emits an Edge.Cuts
    primitive -- and stated nowhere, so nothing stops a future change or a
    confident model from resizing a board to make parts fit."""
    skill = open(os.path.join(ROOT, SOURCES[0]), encoding='utf-8').read()
    low = skill.lower()
    # AND, not OR. Written as `or` first, this passed with either phrase
    # deleted -- both were present, so neither was actually pinned.
    assert 'outline is not yours to change' in low, \
        "the skill must state that the board outline is the user's, not ours"
    assert 'never resize a board' in low, \
        "the skill must carry the imperative, not only the heading"
    # and must name the three tools that DO rewrite Edge.Cuts, as things not to run
    for tool in ('fix_outline_gaps.py', 'strip_routing.py', 'prep_set2.py'):
        assert tool in skill, f"{tool} rewrites Edge.Cuts and is not warned about"
    assert 'oob_area' in skill, \
        "the cutout-blind metric must be called out where oob is discussed"
    print("  PASS: outline rule present, all 3 rewriting tools named")


def test_verdict_lines_do_not_collide_with_the_gui_result_contract():
    """ai_backend.extract_result_line takes the LAST `RESULT=` line and ai_gui
    parses it as the plan JSON. A verifier verdict spelled `RESULT=` would be
    read as a malformed plan."""
    src = open(os.path.join(ROOT, 'kicad_routing_plugin', 'ai_backend.py'),
               encoding='utf-8').read()
    assert 'RESULT=' in src, "the host contract moved; re-check this gate"
    for rel in (SOURCES[0],
                '.claude/skills/plan-pcb-routing/references/verifier-prompts.md'):
        path = os.path.join(ROOT, rel)
        if not os.path.isfile(path):
            continue
        text = open(path, encoding='utf-8').read()
        for line in text.splitlines():
            st = line.strip().strip('`')
            if st.startswith('RESULT=') and ('PASS' in st or 'FAIL' in st
                                             or 'lens=' in st):
                raise AssertionError(
                    f"{rel}: verifier verdict spelled RESULT=, which the GUI "
                    f"parses as the plan JSON: {st[:60]}")
        assert 'VERDICT=' in text, f"{rel}: no VERDICT= contract found"
    print("  PASS: verdicts use VERDICT=; RESULT= left to the host")


def test_the_score_is_the_gate_and_the_router_is_not_the_judge():
    """The board that prompted this shipped at 39/44 nets and 762 DRC errors
    with every tool reporting success, because the only thing being consulted
    was the router's own tally. Two claims have to stay in the skill or that
    recurs: the score exists and is the gate, and place_route_loop's ACCEPTED
    is not a verdict."""
    skill = open(os.path.join(ROOT, SOURCES[0]), encoding='utf-8').read()
    low = skill.lower()

    assert 'board_score.py' in skill, \
        "the skill must name the score helper -- it is the only number not " \
        "produced by the thing being graded"
    # The router's self-report must be explicitly demoted. Without this the
    # skill reads ACCEPTED as 'this round is good', which is how a disconnected
    # board survives an 'improving' loop.
    assert 'not a quality verdict' in low, \
        "the skill must state that place_route_loop's ACCEPTED is not a verdict"
    assert 'better()' in skill and 'place_route_loop.py:358' in skill, \
        "cite where the router-self-report comparison actually lives"
    # The loop has to be bounded, or 'keep going until fixed' is unbounded.
    assert '100 iterations per board' in low or '100 per board' in low, \
        "the convergence budget must be stated in the skill"
    # ...but bounded is not the only failure mode. A run stopped at 11 of 20 and
    # called it "budget exhausted" while its own ledger said the levers were not
    # exhausted, so the skill must also say what is NOT a stop condition.
    assert 'not a stop condition' in low or 'not stop conditions' in low, \
        "the skill must name the non-reasons for stopping (wall-clock, fatigue, " \
        "'the score stopped moving') -- bounding the loop from above is useless " \
        "if it can be abandoned from below"
    # The lever must be chosen by connectivity, not by whichever number is
    # biggest: `drc` can be ~90% grading artifact on a multi-class board, and a
    # run that let it pick spent eleven iterations on clearances while five nets
    # carried no copper.
    assert 'connectivity first' in low, \
        "the skill must rank unrouted/broken above drc when choosing the lever"
    # Re-entering at the failing step is what makes a 100-iteration budget cheap.
    assert 'rip-existing-nets' in skill, \
        "ripping blocking nets must be documented as a sanctioned lever"
    # Vacuity: ungraded must never read as clean.
    assert 'ungraded' in low, "the skill must distinguish ungraded from passed"

    conv = os.path.join(ROOT, '.claude/skills/plan-pcb-routing/references/convergence.md')
    assert os.path.isfile(conv), "references/convergence.md is missing"
    ctext = open(conv, encoding='utf-8').read()
    for key in ('ledger.jsonl', 'parent_board', 'stopped_by', 'blocking'):
        assert key in ctext, f"convergence.md does not document `{key}`"
    # The ledger has to be the one the TOOLS read. `board_store.Ledger` is
    # append-only JSONL and `converge.py record` is its only writer, so a
    # hand-written single JSON document leaves step-back, replay, status and
    # make_film --from-ledger all unreachable -- which is what the skill used
    # to prescribe.
    for verb in ('record', 'status'):
        assert f'converge.py {verb}' in ctext or f'converge.py {verb}' in skill, \
            f"neither the skill nor convergence.md names `converge.py {verb}`"
    assert '"limit": 100' in ctext, \
        "the ledger template must carry the same budget the prose states (100)"
    print("  PASS: score is the gate, router self-report demoted, loop bounded")


def test_routed_board_lenses_exist_and_reenter_the_loop():
    """A verifier fan-out that only reports is not a gate. The three
    routed-board lenses must exist, and a FAIL must be documented as
    re-entering the loop rather than becoming a caveat on a shipped board."""
    rel = '.claude/skills/plan-pcb-routing/references/verifier-prompts.md'
    text = open(os.path.join(ROOT, rel), encoding='utf-8').read()
    for lens in ('connectivity', 'drc', 'spec'):
        assert f'`{lens}`' in text, f"routed-board lens `{lens}` is missing"
    assert 're-enters the loop' in text.lower(), \
        "a FAIL must be documented as re-entering the loop, not as a footnote"
    assert 'do not re-word a fail into a caveat' in text.lower(), \
        "the caveat-laundering failure mode must be refused by name"
    print("  PASS: 3 routed lenses present, FAIL re-enters the loop")


TESTS = [
    test_every_documented_flag_exists,
    test_the_score_is_the_gate_and_the_router_is_not_the_judge,
    test_routed_board_lenses_exist_and_reenter_the_loop,
    test_the_placement_tools_are_actually_mentioned,
    test_exit_code_contract_is_documented,
    test_routing_only_stays_the_default_path,
    test_skill_states_the_board_outline_is_not_editable,
    test_verdict_lines_do_not_collide_with_the_gui_result_contract,
    test_skill_says_placement_is_off_by_default,
]


if __name__ == '__main__':
    for t in TESTS:
        print(f"--- {t.__name__}")
        t()
    print("ALL PASS")
