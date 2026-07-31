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
    'docs/floorplan-intent.md',
    'docs/placement-optimization.md',
    'docs/claude-skills.md',
    'placement/README.md',
    'README.md',
]

TOOLS = ('place_optimize.py', 'place_route_loop.py', 'render_placement.py',
         'check_floorplan.py')

# Flags that belong to a DIFFERENT tool on the same command line (a pipe, a
# --route-args payload). --route-args carries route.py's flags verbatim.
_ROUTE_ARGS_RE = re.compile(r"--route-args\s+(['\"])(.*?)\1", re.S)


def _parser_for(tool):
    """Build the tool's real argparse parser and return its option strings."""
    path = os.path.join(ROOT, tool)
    spec = importlib.util.spec_from_file_location(tool[:-3] + '_probe', path)
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


TESTS = [
    test_every_documented_flag_exists,
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
