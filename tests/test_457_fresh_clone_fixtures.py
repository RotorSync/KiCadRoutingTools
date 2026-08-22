"""A fresh clone must be able to run the suite (issue #457 item 3).

Two invariants, both of which were violated:

1. **Every board a test consumes must be obtainable.** Several tests read boards
   that another test produces as a side effect and that are gitignored, so on a
   clean tree they did not exist -- and `tests/run_all.py` globs alphabetically,
   which puts every consumer BEFORE its producer, so they could never appear in
   any order either. Each consumer had grown its own `if not exists: SKIP`,
   turning a missing fixture into silent non-coverage (one of them reported an
   outright FAILURE for it). `tests/fixture_boards.py` builds them on demand from
   the TRACKED roots instead.

2. **A tracked `.kicad_pro` / `.kicad_prl` must have a tracked `.kicad_pcb`.**
   Six project files were committed for boards that are generated and ignored.
   Per CLAUDE.md #441 the sibling project carries the DRC floor and must travel
   WITH its board; a tracked project for an untracked board is stale by
   construction, and regenerating the board rewrites it -- so merely running the
   suite left modified tracked files in the working tree.
"""

import ast
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'py_router'))  # #522
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'py_tools'))  # #522

import fixture_boards
from fixture_boards import _RECIPES, ROOT, ensure

SIBLINGS = ('.kicad_pro', '.kicad_prl', '.kicad_dru')


def _tracked():
    r = subprocess.run(['git', 'ls-files', 'kicad_files'],
                       capture_output=True, text=True, cwd=ROOT)
    return set(r.stdout.split())


def test_every_recipe_roots_in_a_tracked_board():
    """A fixture is only reproducible if its chain bottoms out in git."""
    tracked = _tracked()
    for name in _RECIPES:
        seen = []
        cur = name
        while cur in _RECIPES:
            seen.append(cur)
            assert len(seen) < 20, f"recipe cycle: {seen}"
            cur = _RECIPES[cur][0]
        assert f'kicad_files/{cur}' in tracked, (
            f"{name} builds from {cur}, which is NOT tracked -- the chain "
            f"cannot be reproduced on a fresh clone")
        print(f"  PASS: {name} <- {' <- '.join(reversed(seen[1:] + [cur]))}")


def test_no_tracked_project_file_without_its_board():
    """The #441 sibling rule, as a repository invariant."""
    tracked = _tracked()
    orphans = []
    for p in sorted(tracked):
        stem, ext = os.path.splitext(p)
        if ext in SIBLINGS and f'{stem}.kicad_pcb' not in tracked:
            orphans.append(p)
    assert not orphans, (
        "tracked project file(s) whose .kicad_pcb is NOT tracked:\n  "
        + "\n  ".join(orphans)
        + "\nThe project carries the DRC floor and must travel with its board. "
          "For a GENERATED board, the project is generated too -- untrack it "
          "(git rm --cached), do not commit it.")


def test_consumers_do_not_silently_skip_a_missing_fixture():
    """The band-aid this replaced: `if not os.path.exists(board): SKIP`.

    A consumer that skips its own fixture reports success while testing nothing,
    which is how these stayed broken. Each of these files must reach the fixture
    through fixture_boards instead.
    """
    consumers = {
        'test_493_interpreter_independence.py': 'fanout_starting_point.kicad_pcb',
        'test_506_507_plan_movie_tools.py': 'fanout_output1.kicad_pcb',
        'test_dru_layer_clearance_e2e.py': 'fanout_starting_point.kicad_pcb',
        'test_pad_global_nm_grid.py': 'fanout_starting_point.kicad_pcb',
    }
    here = os.path.dirname(os.path.abspath(__file__))
    for fname, board in consumers.items():
        src = open(os.path.join(here, fname), encoding='utf-8').read()
        assert 'fixture_boards' in src, (
            f"{fname} consumes {board} but does not go through fixture_boards; "
            f"on a fresh clone it will skip or fail")
        stem = board[:-len('.kicad_pcb')]
        for bad in (f"exists(os.path.join(ROOT, 'kicad_files', '{board}'))",
                    f'"{stem}"' + ' not present'):
            assert bad not in src, f"{fname} still has a skip band-aid: {bad}"
        print(f"  PASS: {fname} -> fixture_boards ({board})")


def test_no_test_reads_an_untracked_board_directly():
    """DERIVED cousin of the check above -- it names no test file.

    The list in test_consumers_do_not_silently_skip_a_missing_fixture is
    hand-maintained, so it only ever covers the consumers that existed when it
    was written. test_gui_finalize_oracle_inrun.py was added later, read
    kicad_files/kit-out-plane.kicad_pcb (gitignored, and produced by NOTHING
    since #426 moved test_kit_route's outputs to a temp workdir), and sailed
    past this gate -- crashing on a fresh clone while passing locally against a
    stale leftover from the old chain.

    So derive the consumer set instead of listing it: any test that resolves a
    board out of kicad_files/ which git does not track must go through
    fixture_boards. Matching is deliberately narrow -- a literal
    'kicad_files/x.kicad_pcb' or a join of 'kicad_files', 'x.kicad_pcb' -- so
    the many tests that write x.kicad_pcb into their own tempdir are not
    swept in.
    """
    pats = [re.compile(r"['\"]kicad_files[/\\]([A-Za-z0-9_.\-]+\.kicad_pcb)['\"]"),
            re.compile(r"['\"]kicad_files['\"]\s*,\s*['\"]([A-Za-z0-9_.\-]+\.kicad_pcb)['\"]")]
    tracked = _tracked()
    here = os.path.dirname(os.path.abspath(__file__))
    offenders = []
    for fname in sorted(os.listdir(here)):
        if not (fname.startswith('test_') and fname.endswith('.py')):
            continue
        src = open(os.path.join(here, fname), encoding='utf-8').read()
        boards = set()
        for p in pats:
            boards |= set(p.findall(src))
        missing = sorted(b for b in boards if f'kicad_files/{b}' not in tracked)
        if missing and 'fixture_boards' not in src:
            offenders.append(f"{fname}: {', '.join(missing)}")
    assert not offenders, (
        "test(s) reading an UNTRACKED board straight out of kicad_files/:\n  "
        + "\n  ".join(offenders)
        + "\nOn a fresh clone that board does not exist. Add a recipe to "
          "tests/fixture_boards.py and call ensure(), or use a tracked board.")
    print("  PASS: no test reads an untracked kicad_files/ board directly")


#: Tests whose coverage depends on a board under the GITIGNORED `wk/` work
#: tree. Registered, not merely tolerated.
#:
#: #718 item 5: absent those boards each of these `skipTest()`s and the FILE
#: STILL EXITS 0 -- green while covering nothing. That is how item 3 of the same
#: issue (a sub-test pinning custom-pad tessellation that 6166a98b deliberately
#: reverted) stayed invisible on every machine but the reporter's, and it makes
#: the suite's green/red state machine-dependent in a way nothing reported.
#: `test_no_test_reads_an_untracked_board_directly` cannot see this class: its
#: regexes scan `kicad_files/` only.
#:
#: These boards are stress/placement-run OUTPUTS, not fixtures -- there is no
#: recipe to add, so the contract here is DISCLOSURE, not reproducibility. The
#: guard holds the map in BOTH directions: a new `wk/` dependency must be
#: declared, AND a registration that no longer matches the source is reported.
#: One direction is not enough -- a stale registration passed the #696
#: containment guard 28/28 while the thing it named had moved.
_WK_DEPENDENT = {
    'test_outline_prefilter.py': ['wk/run19/urchin/base.kicad_pcb'],
    'test_part_class.py': ['wk/b2/tigard__swap/d0/perturbed.control.kicad_pcb',
                           'wk/b2/tigard__swap/d0/perturbed.kicad_pcb'],
    'test_place_reconstruct.py':
        ['wk/b2/tigard__swap/d0/perturbed.control.kicad_pcb',
         'wk/b2/tigard__swap/d0/perturbed.kicad_pcb'],
    'test_placement_pad_legality.py':
        ['../wk/b2/tigard__swap/d0/perturbed.kicad_pcb'],
    'test_run20_run_watch.py': ['wk/run20'],
    'test_run4_custom_pad_circle.py': ['wk/run3/final2.kicad_pcb'],
    'test_run4_reconstruct.py': ['wk/b2/tigard__swap/d0/perturbed.kicad_pcb'],
    'test_run5_emit_guard.py': ['wk/b2/tigard__swap/d0/perturbed.kicad_pcb'],
    'test_run5_exchange.py': ['wk/b2/tigard__swap/d0/perturbed.kicad_pcb'],
    'test_run6_backlog.py': ['wk/run5/s1_pour.kicad_pcb'],
    'test_run6_body_overlap.py': ['wk/run2/original/tigard_v10.kicad_pcb',
                                  'wk/run5/final5.kicad_pcb'],
    'test_run6_check_assembly.py':
        ['wk/b2/tigard__swap/d0/perturbed.kicad_pcb',
         'wk/run2/original/tigard_v10.kicad_pcb',
         'wk/run5/final5.kicad_pcb'],
    'test_run7_vectors.py': ['wk/b2/tigard__swap/d0/perturbed.kicad_pcb'],
    'test_run8_airwire_refuted.py':
        ['wk/run7/glasgow_revC/perturbed.kicad_pcb'],
    'test_run8_oob_outline.py': ['wk/run7/glasgow_revC/perturbed.kicad_pcb'],
    'test_run8_rigid_consistency.py':
        ['wk/run7/glasgow_revC/perturbed.control.kicad_pcb',
         'wk/run7/glasgow_revC/perturbed.kicad_pcb',
         'wk/run7/glasgow_revC/rL_repair.kicad_pcb'],
    'test_run8_starved_face_gate.py': ['wk/run7/glasgow_revC'],
}

#: `os.path.join(<repo root>, 'a', 'b')` with every component after the base a
#: literal. Applied to `_code_only` output, which puts each call on one line;
#: the whitespace collapse keeps it working on raw text as a fallback.
_JOIN_RE = re.compile(
    r"""os\.path\.join\(\s*([A-Za-z_][A-Za-z0-9_.()'"\[\], ]*?)\s*,\s*"""
    r"""((?:'[^']*'|"[^"]*")\s*(?:,\s*(?:'[^']*'|"[^"]*")\s*)*)\)""")

#: One single- or double-quoted string literal.
_STR_RE = re.compile(r"'[^']*'" + r'|"[^"]*"')

_ROOT_BASES = ('ROOT', 'REPO', 'ROOT_DIR')

#: A subprocess argv list literal -- `[sys.executable, ..., <path>, ...]`.
_ARGV_RE = re.compile(r"\[[^\[\]]*sys\.executable[^\[\]]*\]")

#: `os.path.join(<repo root>, <bare identifier>)`: the repo root joined with a
#: script name only known at runtime.
_JOIN_VAR_RE = re.compile(
    r"os\.path\.join\(\s*(?:ROOT|REPO|ROOT_DIR)\s*,\s*"
    r"[A-Za-z_][A-Za-z0-9_]*\s*\)")

#: Tests that legitimately join the repo root with a VARIABLE inside a
#: subprocess argv. Declared with the reason, and held in both directions.
_ROOT_JOIN_ARGV_OK = {
    'test_431_skill_commands.py':
        'its `tool` values come from discovered_tools(), which yields paths '
        'ALREADY qualified by directory ("py_router/route.py") straight out '
        'of the skill text and filters them through os.path.isfile -- so the '
        'join is over a relative path, not a bare basename.',
}


def _code_only(src):
    """`src` with comments and docstrings gone, so PROSE cannot be scanned.

    These gates match path expressions, and a path expression written inside a
    docstring to EXPLAIN the defect is not an instance of it -- the first draft
    of test_no_test_spawns_a_script_that_moved flagged its own docstring. Round
    tripping through ast drops every comment and, once the docstring nodes are
    removed, every docstring; real string literals in code survive, which is
    the point. On a syntax error, fall back to the raw text: a gate that goes
    quiet on a file it cannot parse is worse than one that is slightly noisy.
    """
    try:
        tree = ast.parse(src)
    except SyntaxError:                                    # pragma: no cover
        return src
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
            continue
        body = node.body
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            node.body = body[1:] or [ast.Pass()]
    ast.fix_missing_locations(tree)
    return ast.unparse(tree)


def _test_files():
    here = os.path.dirname(os.path.abspath(__file__))
    for fname in sorted(os.listdir(here)):
        if fname.startswith('test_') and fname.endswith('.py'):
            with open(os.path.join(here, fname), encoding='utf-8') as fh:
                yield fname, _code_only(fh.read())


def _joins(src):
    """(base_expr, [literal, ...]) for every literal os.path.join in `src`."""
    flat = re.sub(r'\s+', ' ', src)
    for m in _JOIN_RE.finditer(flat):
        lits = [x[1:-1] for x in _STR_RE.findall(m.group(2))]
        yield m.group(1).strip(), lits


def _scan_wk_deps():
    """file -> sorted board paths it resolves out of the REPO's `wk/`.

    Only joins rooted at the repo count. A test that builds a `wk` inside its
    own tempdir (`os.path.join(td, 'wk')` -- test_provenance_audit,
    test_blind_stage_identity, test_run15_handback_contract) depends on nothing
    external and must NOT be swept in.
    """
    found = {}
    for fname, src in _test_files():
        for base, lits in _joins(src):
            if 'wk' not in lits:
                continue
            if base not in _ROOT_BASES and '__file__' not in base:
                continue
            found.setdefault(fname, set()).add('/'.join(lits))
    return {k: sorted(v) for k, v in found.items()}


def test_wk_dependent_tests_are_declared():
    """#718 item 5: a test that silently skips is not coverage -- name it."""
    found = _scan_wk_deps()
    unregistered = sorted(set(found) - set(_WK_DEPENDENT))
    departed = sorted(set(_WK_DEPENDENT) - set(found))
    changed = sorted(f for f in set(found) & set(_WK_DEPENDENT)
                     if found[f] != sorted(_WK_DEPENDENT[f]))
    assert not unregistered, (
        "test(s) reading a board out of the gitignored wk/ work tree without "
        "declaring it:\n  " + "\n  ".join(
            f"{f}: {', '.join(found[f])}" for f in unregistered)
        + "\nAbsent that board the test skipTest()s and the file still exits "
          "0 -- green while covering nothing. Add it to _WK_DEPENDENT so the "
          "dependency is on the record, or use a tracked board.")
    assert not departed, (
        "_WK_DEPENDENT names file(s) that no longer read a wk/ board:\n  "
        + "\n  ".join(departed) + "\nA stale registration is a guard that "
        "passes while covering nothing -- drop the entry.")
    assert not changed, (
        "_WK_DEPENDENT is out of date for:\n  " + "\n  ".join(
            f"{f}: declared {sorted(_WK_DEPENDENT[f])} != found {found[f]}"
            for f in changed))
    absent = sorted(
        f for f, boards in found.items()
        if any(not os.path.exists(
            os.path.normpath(os.path.join(ROOT, b))) for b in boards))
    print(f"  PASS: {len(found)} wk/-dependent test file(s) declared")
    if absent:
        print(f"  NOTE: {len(absent)} of them are INERT on this machine (the "
              f"wk/ board is absent) -- they report PASS while covering "
              f"nothing:")
        for f in absent:
            print(f"          {f}")


def test_no_test_spawns_a_script_that_moved():
    """#718 item 1: spawning a CLI at a path the #522 reorg vacated.

    CPython writes `can't open file ...: [Errno 2]` to STDERR and exits 2, so a
    test asserting on STDOUT fails as a bare AssertionError that reads like a
    product bug, and one asserting `returncode == 0` reads exit 2 as the tool's
    own. test_run6_backlog and test_run5_exchange were correct on their
    branches and went stale on MERGE -- which is why ed779096's sweep missed
    them, and why this is a standing gate rather than another sweep.

    TWO forms, because one of them is what actually shipped:

    * LITERAL -- `os.path.join(ROOT, 'route.py')`. Decided exactly: an offender
      is a name that is NOT at the root but IS where the shipped resolver
      looks. A literal that resolves nowhere (test_krt_capabilities' deliberate
      `no_such_file.py`) is a negative control, not a stale path.

    * VARIABLE -- `os.path.join(ROOT, script)` inside a subprocess argv, which
      is the form BOTH #718 offenders used. `run_utils.tool`'s docstring says a
      static lint cannot do this job, and it is right that the *value* is only
      knowable at runtime -- but the SHAPE is not. Joining the repo root with a
      bare script name is the assumption #522 falsified, so the shape is the
      defect, and the few legitimate uses are declared below. Scoped to
      subprocess argv on purpose: `os.path.join(ROOT, _p522)` for a sys.path
      insert is the same shape and is not a spawn (46 such joins across the
      suite, 1 of them an argv).
    """
    sys.path.insert(0, ROOT)
    from krt_capabilities import _tool_path
    offenders = []
    argv_var = set()
    for fname, src in _test_files():
        flat = re.sub(r'\s+', ' ', src)
        for base, lits in _joins(src):
            if base not in _ROOT_BASES or len(lits) != 1:
                continue
            name = lits[0]
            if not name.endswith('.py'):
                continue
            if os.path.isfile(os.path.join(ROOT, name)):
                continue
            resolved = _tool_path(ROOT, name)
            if os.path.isfile(resolved):
                offenders.append(
                    f"{fname}: spawns ROOT/{name} (literal), which lives at "
                    f"{os.path.relpath(resolved, ROOT)}")
        for m in _ARGV_RE.finditer(flat):
            if _JOIN_VAR_RE.search(m.group(0)):
                argv_var.add(fname)
    undeclared = sorted(argv_var - set(_ROOT_JOIN_ARGV_OK))
    departed = sorted(set(_ROOT_JOIN_ARGV_OK) - argv_var)
    offenders += [
        f"{f}: spawns os.path.join(ROOT, <variable>) -- the shape #522 "
        f"falsified" for f in undeclared]
    assert not offenders, (
        "test(s) spawning a CLI at a path the #522 reorg vacated:\n  "
        + "\n  ".join(offenders)
        + "\nUse tests/run_utils.tool() / tool_env(), which resolve the tool "
          "wherever it is and RAISE by name when it is absent, instead of "
          "dying into an empty stdout three frames away. If the variable "
          "genuinely carries a directory-qualified path, declare it in "
          "_ROOT_JOIN_ARGV_OK with the reason.")
    assert not departed, (
        "_ROOT_JOIN_ARGV_OK declares file(s) that no longer join ROOT with a "
        "variable in an argv:\n  " + "\n  ".join(departed)
        + "\nDrop the entry -- an exemption for something that stopped "
          "happening is a hole nobody is watching.")
    print(f"  PASS: no test spawns a root-level script the reorg moved "
          f"({len(_ROOT_JOIN_ARGV_OK)} declared exemption)")


def test_fixtures_build_from_a_clean_state():
    """End to end: every fixture is producible right now."""
    for name in _RECIPES:
        path = ensure(name)
        assert os.path.exists(path), f"{name} not produced"
        assert os.path.getsize(path) > 1000, f"{name} looks truncated"
    print(f"  PASS: all {len(_RECIPES)} fixtures present/buildable")


def test_building_a_fixture_leaves_no_tracked_file_modified():
    """Running the suite must not dirty the working tree."""
    r = subprocess.run(['git', 'status', '--porcelain', 'kicad_files'],
                       capture_output=True, text=True, cwd=ROOT)
    dirty = [ln for ln in r.stdout.splitlines()
             if ln[:2] not in ('??',) and not ln[:2].strip() == 'D']
    dirty = [ln for ln in dirty if ' M ' in ln[:3] or ln.startswith(' M')
             or ln.startswith('M ')]
    assert not dirty, (
        "building fixtures modified tracked file(s):\n  " + "\n  ".join(dirty))
    print("  PASS: working tree unmodified by fixture builds")


TESTS = [
    test_every_recipe_roots_in_a_tracked_board,
    test_no_tracked_project_file_without_its_board,
    test_consumers_do_not_silently_skip_a_missing_fixture,
    test_no_test_reads_an_untracked_board_directly,
    test_wk_dependent_tests_are_declared,
    test_no_test_spawns_a_script_that_moved,
    test_fixtures_build_from_a_clean_state,
    test_building_a_fixture_leaves_no_tracked_file_modified,
]


if __name__ == '__main__':
    for t in TESTS:
        print(f"--- {t.__name__}")
        t()
    print("ALL PASS")
