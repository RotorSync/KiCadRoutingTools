#!/usr/bin/env python3
"""Paired A/B for placement objective terms, graded by an INDEPENDENT check.

Every term in the quench objective is a claim about routability, and a term can
always be made to improve the number it is itself computed from. This runs the
same board through the optimizer twice -- flag off, flag on -- writes both, and
then grades both with `floorplan.grade(..., with_health=True)`, which
re-derives its corridors from the FINAL poses. The optimizer minimises against
rectangles frozen at construction; the grader does not use them. So a term that
only games its own model shows up here as "improved nothing".

Table-driven on purpose (`ROWS`): the next term under test adds a row, not a
file. Adding a row does not change how any existing row is judged.

The verdict is a PAIRED, DIRECTIONAL, NON-REGRESSION rule, never a per-board
absolute:

  * the claimed signal must improve on at least N-1 BOARDS,
  * and must regress on NONE,
  * while `crossings` and `hpwl` -- the terms that already shipped -- do not
    get worse anywhere.

Unchanged boards count as neutral AND ARE PRINTED. A silently-dropped neutral
board is how a term with no effect on 3 of 4 boards reads as a clean sweep.

THE MEASURED NUMBERS LIVE IN `tests/placement_ab_baseline.json`, NOT IN PROSE.
A row's `why` records the MECHANISM; every number it once carried is in the
committed baseline, which this script re-measures and compares on every run.
That is not decoration: the `corridor-ulx3s` row spent weeks rejected on a
recorded measurement that had silently inverted -- its claimed signal went from
"62 -> 63" (the recorded claim) to 62 -> 55 (measured) -- while this gate
printed PASS, because it compared only the SIGN of one number and that sign was
held up by a criterion the prose never mentioned (#694). Prose cannot be
re-run.

Usage:
    python3 -X utf8 tests/test_placement_ab.py            # the default table
    python3 -X utf8 tests/test_placement_ab.py --row corridor-ulx3s
    python3 -X utf8 tests/test_placement_ab.py --list
    python3 -X utf8 tests/test_placement_ab.py --self-test   # gate logic only
    python3 -X utf8 tests/test_placement_ab.py --write-baseline
"""
import argparse
import json
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'py_placer'))  # placement split
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'py_router'))  # placement split
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'py_tools'))  # placement split

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOARDS = os.path.join(ROOT, 'kicad_files')

# Six full quenches (3 rows x off/on) on three large boards. Measured ~533 s;
# declared with headroom so a slower box reports FAIL, not TIME.
RUN_ALL_TIMEOUT = 1200

DEFAULT_BASELINE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                'placement_ab_baseline.json')

# The keys compared against the committed baseline.
#
# `seconds` is wall-clock and is never compared. `corridor_cut` is never
# compared either, and that is deliberate: the OFF arm is not given
# `corridor_specs` (see `run_row`), so it builds no corridor and reports 0.0.
# "0.0 -> 806.84" is not a regression, it is a comparison that was never made,
# and recording it as evidence would pin a fiction.
BASELINE_INT_KEYS = ('crossings', 'health_bus_foreign_crossings',
                     'intent_errors')
BASELINE_FLOAT_KEYS = ('hpwl', 'health_block_displacement_max_mm')
BASELINE_DICT_KEYS = ('intent_errors_by_rule',)

# CLAUDE.md's rule, now enforced here instead of only stated there. Three is
# also the floor at which the "improve on N-1, regress on none" rule can say
# anything at all: at N=3 a term with no real effect still passes 1 run in 8.
MIN_TRIAL_BOARDS = 3


# --- the table ------------------------------------------------------------
#
# `quench_on` is merged into the OFF kwargs to make the ON run, so a row states
# exactly one difference. `corridors` are the health.bus_corridors the intent
# declares; the grader reads the same declaration and re-derives the geometry.
#
# `signal` is the JSON_SUMMARY-style key the row claims to improve, and
# `guard` the keys that must not get worse. Lower is better for all of them.
# `expect` pins the mark that was MEASURED. Omit it for a term still on trial;
# a trial row must improve on >= N-1 BOARDS and regress on none.
#
# `rejected` marks a term that was tried, measured, and NOT adopted. Its rows
# stay in the table as the evidence and as a change detector -- they are judged
# against their recorded marks rather than against "the term must help", so a
# rejected term does not leave a permanent red mark that someone eventually
# deletes along with the finding.
#
# `why` states the MECHANISM only. Numbers belong in the baseline, which is
# re-measured and compared; a number in a comment is re-read and believed.
#
# The corridor globs name sub-buses SEPARATELY. Merging them halves the
# corridor's `cover` (address and data leave the part on different faces, so
# the endpoint average lands between them) and the resulting rectangle is a
# fiction: ulx3s SDRAM_A* scores cover 0.81, merged SDRAM_* scores 0.46. This
# is not a footnote -- the first run of this harness used the merged glob and
# reported the term INERT, because it had been pointed at a phantom.
ROWS = [
    {
        'name': 'corridor-ulx3s',
        'board': 'ulx3s.kicad_pcb',
        'corridors': [{'name': 'sdram_a', 'nets': ['SDRAM_A*'],
                       'width_mm': 8.0},
                      {'name': 'sdram_d', 'nets': ['SDRAM_D*'],
                       'width_mm': 8.0}],
        'ignore_nets': ['GND', '+3V3', '+5V'],
        'quench_on': {'corridor_weight': 20.0},
        'signal': 'health_bus_foreign_crossings',
        'guard': ('crossings', 'hpwl'),
        'expect': 'regress',
        'rejected': True,
        'why': ('MECHANISM: the term buys its signal with intent-zone '
                'containment. Both guards improve and the re-derived '
                'bus_foreign_crossings improves too, but the ON arm walks more '
                'parts out of the zones the emitted intent recorded, so the '
                'row marks REGRESS on intent errors rather than on its signal. '
                'This DIRECTION is decided by the hard pad+drill legality '
                'layer: run quench(pad_legality=False) on this board and the '
                'signal goes the other way (the pre-2026-08-22 record), while '
                'every OFF-arm number is unchanged. See '
                'docs/placement-optimization.md. Numbers: '
                'tests/placement_ab_baseline.json.'),
    },
    {
        'name': 'corridor-orangecrab',
        'board': 'orangecrab_ext_pll.kicad_pcb',
        'corridors': [{'name': 'ram_d', 'nets': ['RAM_D*'], 'width_mm': 6.0},
                      {'name': 'ram_a', 'nets': ['RAM_A*'], 'width_mm': 6.0}],
        'ignore_nets': ['GND', '+3V3', '+1V1', 'VCC*'],
        'quench_on': {'corridor_weight': 20.0},
        'signal': 'health_bus_foreign_crossings',
        'guard': ('crossings', 'hpwl'),
        'expect': 'improve',
        'rejected': True,
        'why': ('MECHANISM: the signal and both guards improve here. This row '
                'has flipped its mark once already, which is the change '
                'detector doing its job -- the flip was blamed on the '
                'pad+drill legality layer, but measured at HEAD that layer is '
                'INERT on this board (toggling quench(pad_legality=...) moves '
                'no number), so the cause is not established. It is '
                'ulx3s where that layer decides the direction. '
                'Numbers: tests/placement_ab_baseline.json.'),
    },
    {
        'name': 'corridor-coldfire',
        'board': 'kit-dev-coldfire-xilinx_5213.kicad_pcb',
        'corridors': [{'name': 'an', 'nets': ['AN*'], 'width_mm': 8.0},
                      {'name': 'bdm', 'nets': ['DDAT*', 'PST*'],
                       'width_mm': 8.0}],
        'ignore_nets': ['GND', 'VCC*', '+3.3V', '+5V'],
        'quench_on': {'corridor_weight': 20.0},
        'signal': 'health_bus_foreign_crossings',
        'guard': ('crossings', 'hpwl'),
        'expect': 'improve',
        'rejected': True,
        'why': ('MECHANISM: signal and both guards improve. Kept in the table '
                'BECAUSE a board that disagrees with the others is the whole '
                'evidence -- a term that helps on one board of three is not a '
                'term, and deleting the row that disagrees is how that gets '
                'forgotten. Numbers: tests/placement_ab_baseline.json.'),
    },
]

QUENCH_BASE = dict(
    max_displacement=3.0, step=1.0, grid_step=0.1, clearance=0.2,
    board_edge_clearance=0.55, crossing_penalty=30.0, length_weight=0.3,
    halo_base=0.5, halo_coef=0.15, halo_weight=2.0, edge_halo=2.0,
    edge_weight=2.0, max_passes=4, verbose=False)


def _intent_for(board_path, corridors, workdir):
    """An intent for `board_path` with `corridors` declared.

    Emitted from the board itself rather than hand-written, so the blocks the
    health signals need are the ones that board actually has, and the row only
    has to state the bus.
    """
    from kicad_parser import parse_kicad_pcb
    from placement import floorplan
    path = os.path.join(workdir, 'intent.json')
    doc = floorplan.emit_intent(parse_kicad_pcb(board_path), board_path)
    doc['health'] = dict(doc.get('health') or {})
    doc['health']['bus_corridors'] = corridors
    with open(path, 'w') as fh:
        json.dump(doc, fh, indent=2)
    return floorplan.load_intent(path)


def _run(board_path, out_path, intent, quench_kw):
    """One quench + write + independent grade. Returns the measured row."""
    from kicad_parser import parse_kicad_pcb
    from placement.quench import quench
    from placement.writer import write_placed_output
    from placement import floorplan

    pcb = parse_kicad_pcb(board_path)
    metrics = {}
    t0 = time.time()
    placements = quench(pcb, pcb_file=board_path, metrics_out=metrics,
                        **quench_kw)
    write_placed_output(board_path, out_path, placements)
    for ext in ('.kicad_pro', '.kicad_dru'):
        src = os.path.splitext(board_path)[0] + ext
        if os.path.exists(src):
            shutil.copy2(src, os.path.splitext(out_path)[0] + ext)

    # The independent half: parse what was WRITTEN and grade it. The corridors
    # here come from the final poses, not from the frozen model the optimizer
    # minimised against.
    graded = parse_kicad_pcb(out_path)
    result = floorplan.grade(intent, graded, out_path, with_health=True)
    summary = floorplan.summary(result)
    after = metrics.get('after') or {}
    # ERRORS only, by rule. `summary()['violations_by_rule']` counts warnings
    # too, and the mark turns on the ERROR count -- a breakdown that did not
    # sum to `intent_errors` would explain the wrong number.
    by_rule = {}
    for v in result.errors:
        by_rule[v.rule] = by_rule.get(v.rule, 0) + 1
    return {
        'seconds': round(time.time() - t0, 1),
        'crossings': after.get('crossings'),
        'hpwl': round(float(after.get('hpwl') or 0.0), 2),
        'corridor_cut': round(float(after.get('corridor_cut') or 0.0), 2),
        'health_bus_foreign_crossings':
            summary.get('health_bus_foreign_crossings'),
        # NOT `health_blocks_displaced`: routability.health only computes
        # that when the intent declares `health.block_displacement_mm`, which
        # `_intent_for` does not, so it was None on every board and every run
        # -- a column of evidence that was never measured. The max is
        # threshold-free and always live.
        'health_block_displacement_max_mm':
            summary.get('health_block_displacement_max_mm'),
        'intent_errors': summary.get('errors'),
        'intent_errors_by_rule': by_rule,
    }


def _moved_rules(off, on):
    """`zone_containment 4 -> 7` for every error rule whose count changed."""
    a = off.get('intent_errors_by_rule') or {}
    b = on.get('intent_errors_by_rule') or {}
    return [f"{k} {a.get(k, 0)} -> {b.get(k, 0)}"
            for k in sorted(set(a) | set(b)) if a.get(k, 0) != b.get(k, 0)]


def _verdict(off, on, row):
    """Direction on one board. Returns (mark, notes)."""
    key = row['signal']
    a, b = off.get(key), on.get(key)
    notes = []
    if a is None or b is None:
        return 'skip', [f"{key} not measured on this board"]
    for g in row['guard']:
        ga, gb = off.get(g), on.get(g)
        if ga is not None and gb is not None and gb > ga + 1e-9:
            notes.append(f"GUARD {g} worsened {ga} -> {gb}")
    # PAIRED, not absolute. Both runs quench the board, so both walk parts out
    # of the zones the emitted intent recorded; grading the ON run against zero
    # errors would mark every row a regression for a reason the flag did not
    # cause. Only errors the flag ADDS are its fault.
    ea, eb = off.get('intent_errors') or 0, on.get('intent_errors') or 0
    if eb > ea:
        # NAME the rules that moved. A mark resting on an unattributed error
        # count is what let #694's inverted row keep reading as an intact
        # finding: the verdict came from a criterion nothing printed.
        moved = _moved_rules(off, on)
        detail = f" ({'; '.join(moved)})" if moved else ""
        notes.append(f"intent errors {ea} -> {eb}{detail}")
    if notes:
        return 'regress', notes
    if b < a:
        return 'improve', [f"{key} {a} -> {b}"]
    if b > a:
        return 'regress', [f"{key} {a} -> {b}"]
    return 'neutral', [f"{key} unchanged at {a}"]


def run_row(row, workdir):
    board = os.path.join(BOARDS, row['board'])
    if not os.path.exists(board):
        print(f"  SKIP {row['name']}: no {row['board']} in kicad_files/")
        return 'skip', [], None, None
    d = os.path.join(workdir, row['name'])
    os.makedirs(d, exist_ok=True)
    intent = _intent_for(board, row['corridors'], d)

    kw_off = dict(QUENCH_BASE)
    kw_off['ignore_nets'] = list(row.get('ignore_nets') or ())
    kw_on = dict(kw_off)
    kw_on.update(row['quench_on'])
    # The ON run needs the corridors the flag prices; the OFF run must NOT get
    # them, or "off" would mean "built and multiplied by zero" rather than
    # "the objective that shipped".
    if 'corridor_weight' in row['quench_on']:
        kw_on['corridor_specs'] = row['corridors']
    # A row that states no difference measures nothing, and reads exactly like
    # a flag that never reached the engine.
    if kw_on == kw_off:
        raise AssertionError(
            f"{row['name']}: quench_on {row['quench_on']} leaves the ON kwargs "
            f"identical to OFF -- the row would measure the same run twice")

    off = _run(board, os.path.join(d, 'off.kicad_pcb'), intent, kw_off)
    on = _run(board, os.path.join(d, 'on.kicad_pcb'), intent, kw_on)
    mark, notes = _verdict(off, on, row)
    expected = row.get('expect')
    tag = mark.upper()
    if expected and mark == expected:
        tag = f"{mark.upper()} (as measured)"
    elif expected:
        tag = f"{mark.upper()} != expected {expected.upper()}"
    print(f"  {row['name']:<24} {tag:<32} "
          f"({off['seconds']}s / {on['seconds']}s)")
    for k in ('crossings', 'hpwl', row['signal']):
        print(f"      {k:<32} {off.get(k)!s:>12} -> {on.get(k)!s:>12}")
    # Printed apart from the real deltas: OFF is deliberately given no
    # corridor, so this pair is not a before/after of the same quantity.
    print(f"      {'corridor_cut':<32} {off.get('corridor_cut')!s:>12} -> "
          f"{on.get('corridor_cut')!s:>12}   [OFF builds no corridor -- not a "
          f"comparison]")
    for n in notes:
        print(f"      {n}")
    if expected and mark != expected and row.get('why'):
        print(f"      recorded reason: {row['why']}")
    return mark, notes, off, on


# --- the committed baseline ------------------------------------------------
#
# Mirrors tests/stress/corpus_noop_sweep.py: `--baseline` reads the committed
# expectation, `--baseline ""` skips, `--write-baseline` re-records and returns
# WITHOUT comparing, so recording can never fail -- the burden is on the human
# to read the table first.

def _sign(a, b, tol=0.0):
    """-1 / 0 / +1 for the move from `a` to `b` (lower is better everywhere)."""
    if a is None or b is None:
        return None
    d = b - a
    if abs(d) <= tol:
        return 0
    return 1 if d > 0 else -1


BASELINE_KEYS = BASELINE_INT_KEYS + BASELINE_FLOAT_KEYS + BASELINE_DICT_KEYS


def record_for(row, mark, off, on):
    """The serializable evidence for one row.

    Refuses a measurement that is MISSING a compared key rather than writing a
    baseline with a hole in it. A key that quietly stops being produced is the
    same failure as a number that quietly inverts: the evidence still looks
    complete. (`health_blocks_displaced` was None on every board for months
    because nothing ever asked whether it had a value.) A key present and None
    is fine -- that is a measurement, and the comparator handles it.
    """
    def keep(arm, d):
        missing = [k for k in BASELINE_KEYS if k not in (d or {})]
        if missing:
            raise AssertionError(
                f"{row['name']}: the {arm} measurement is missing "
                f"{', '.join(missing)} -- _run no longer produces "
                f"{'it' if len(missing) == 1 else 'them'}, or the compared-key "
                f"lists and _run have drifted apart")
        return {k: v for k, v in d.items() if k in BASELINE_KEYS}
    return {'board': row['board'], 'mark': mark,
            'off': keep('OFF', off), 'on': keep('ON', on)}


def compare_baseline(current, expected, float_tol=1e-6, scope=None):
    """Compare measured records against the committed baseline.

    Returns a list of problem strings. Two classes, both fatal, kept distinct
    because they mean different things:

      INVERTED -- the DIRECTION of a key reversed, or the row's mark changed.
                  This is #694: the finding the row records is no longer the
                  finding the code produces.
      DRIFT    -- same direction, moved value. Integers compare exactly; floats
                  at a relative tolerance.

    `scope` is the set of row names this run actually ran, so `--row X` does
    not report every other baseline row as missing.
    """
    problems = []
    for name in sorted(current):
        cur = current[name]
        exp = expected.get(name)
        if exp is None:
            problems.append(f"NEW ROW {name}: not in the baseline")
            continue
        if cur.get('mark') != exp.get('mark'):
            problems.append(
                f"INVERTED {name}: mark {exp.get('mark')} -> {cur.get('mark')}")
        for key in BASELINE_INT_KEYS + BASELINE_FLOAT_KEYS:
            tol = 0.0 if key in BASELINE_INT_KEYS else float_tol
            ca, cb = cur.get('off', {}).get(key), cur.get('on', {}).get(key)
            ea, eb = exp.get('off', {}).get(key), exp.get('on', {}).get(key)
            if ca is None or cb is None or ea is None or eb is None:
                if (ca is None) != (ea is None) or (cb is None) != (eb is None):
                    problems.append(f"DRIFT {name}.{key}: baseline "
                                    f"{ea!r} -> {eb!r}, measured "
                                    f"{ca!r} -> {cb!r}")
                continue
            # Relative for floats: a tolerance on hpwl means "the same number",
            # not "close enough to ignore a move".
            scale = max(abs(ea), abs(eb), 1.0) * tol
            if _sign(ea, eb, scale) != _sign(ca, cb, scale):
                problems.append(
                    f"INVERTED {name}.{key}: baseline {ea} -> {eb}, "
                    f"measured {ca} -> {cb}")
            elif (abs(ca - ea) > max(abs(ea), 1.0) * tol
                    or abs(cb - eb) > max(abs(eb), 1.0) * tol):
                problems.append(
                    f"DRIFT {name}.{key}: baseline {ea} -> {eb}, "
                    f"measured {ca} -> {cb}")
        for key in BASELINE_DICT_KEYS:
            for arm in ('off', 'on'):
                c = (cur.get(arm) or {}).get(key) or {}
                e = (exp.get(arm) or {}).get(key) or {}
                for rule in sorted(set(c) | set(e)):
                    if c.get(rule, 0) != e.get(rule, 0):
                        problems.append(
                            f"DRIFT {name}.{arm}.{key}[{rule}]: baseline "
                            f"{e.get(rule, 0)}, measured {c.get(rule, 0)}")
    for name in sorted(expected):
        if name in current:
            continue
        if scope is None or name in scope:
            problems.append(
                f"MISSING {name}: in the baseline, not measured in this run")
    return problems


# --- the gate --------------------------------------------------------------

def gate(rows, marks):
    """The pass rule. Pure: no I/O, so `_self_test` can reach every branch."""
    lines = []
    pinned = {r['name']: r['expect'] for r in rows if r.get('expect')}
    trial = [r for r in rows if not r.get('expect')]
    mismatched = [n for n, e in pinned.items() if marks.get(n) != e]
    rejected = [r['name'] for r in rows if r.get('rejected')]

    ok = not mismatched
    if rejected:
        lines.append(
            f"rejected: {len(rejected)} row(s) record a term that was tried "
            f"and NOT adopted ({', '.join(rejected)}). They are judged "
            f"against their recorded marks, not against 'the term helps'.")
    if trial:
        # Judged per BOARD, not per row. Counting rows lets three rows on one
        # board satisfy a rule that says three boards -- and `--row` makes
        # running exactly one of them the convenient path.
        by_board = {}
        for r in trial:
            m = marks.get(r['name'])
            if m == 'skip' or m is None:
                continue
            by_board.setdefault(r['board'], []).append(m)
        boards = sorted(by_board)
        b_reg = [b for b in boards if 'regress' in by_board[b]]
        b_imp = [b for b in boards
                 if 'improve' in by_board[b] and b not in b_reg]
        n = len(boards)
        if n < MIN_TRIAL_BOARDS:
            ok = False
            lines.append(
                f"REFUSED: a term on trial is judged on >= {MIN_TRIAL_BOARDS} "
                f"DISTINCT boards; this run judged {n} "
                f"({', '.join(boards) if boards else 'none'}). Add rows on "
                f"other boards, or run the whole table.")
        ok = ok and not b_reg and len(b_imp) >= max(1, n - 1)
        lines.append(f"on trial: improved {len(b_imp)}/{n} board(s), "
                     f"regressed {len(b_reg)} "
                     f"(rule: improve >= N-1, regress == 0)")
        lines.append(f"          N={n}: a term with no real effect still "
                     f"passes that rule 1 run in {2 ** n} by chance")
    if pinned:
        lines.append(
            f"pinned:   {len(pinned) - len(mismatched)}/{len(pinned)} rows "
            f"match their measured verdict"
            + (f"; MISMATCH: {', '.join(mismatched)}" if mismatched else ""))
    return ok, lines


def _self_test():
    """Every branch of the gate and the comparator, without quenching.

    Runs at the top of EVERY invocation: it costs milliseconds, and the live
    table cannot reach any of it (all three rows are pinned, so the trial
    branch never executes on a real run). A gate whose own logic is untested
    is how a documented rule stays documentation -- which is exactly what
    #694 found: CLAUDE.md's ">= 3 boards" rule was never in the code.
    """
    def row(name, board, **kw):
        r = {'name': name, 'board': board, 'signal': 's', 'guard': (),
             'quench_on': {'x': 1}}
        r.update(kw)
        return r

    # 1. one improving trial row on one board is REFUSED on board count --
    #    the exact hole #694 names (`max(1, judged - 1) == 1` passed it).
    rows = [row('a', 'b1.kicad_pcb')]
    ok, lines = gate(rows, {'a': 'improve'})
    assert not ok, "one board must not pass"
    assert any('REFUSED' in x for x in lines), lines

    # 2. three trial rows on the SAME board is still one board.
    rows = [row(n, 'b1.kicad_pcb') for n in ('a', 'b', 'c')]
    ok, lines = gate(rows, {'a': 'improve', 'b': 'improve', 'c': 'improve'})
    assert not ok, "three rows on one board must not pass"
    assert any('REFUSED' in x for x in lines), lines

    # 3. three distinct boards, 2 improve + 1 neutral, none regress -> passes.
    rows = [row(n, f'b{i}.kicad_pcb')
            for i, n in enumerate(('a', 'b', 'c'))]
    ok, _ = gate(rows, {'a': 'improve', 'b': 'improve', 'c': 'neutral'})
    assert ok, "improve on N-1 boards with no regress must pass"

    # 3b. two neutral boards is only N-2 improved -- the N-1 rule must FAIL.
    #     Without this case, deleting the improve count entirely still passes
    #     every other assertion here.
    ok, _ = gate(rows, {'a': 'improve', 'b': 'neutral', 'c': 'neutral'})
    assert not ok, "improve on N-2 boards must fail"
    ok, _ = gate(rows, {'a': 'neutral', 'b': 'neutral', 'c': 'neutral'})
    assert not ok, "an inert term must fail"

    # 4. any regress fails, however many improve.
    ok, _ = gate(rows, {'a': 'improve', 'b': 'improve', 'c': 'regress'})
    assert not ok, "a regressing board must fail"

    # 5. a skipped board does not count toward N (and must not pass at 2).
    ok, lines = gate(rows, {'a': 'improve', 'b': 'improve', 'c': 'skip'})
    assert not ok and any('REFUSED' in x for x in lines), lines

    # 6. pinned rows are judged against their recorded mark.
    rows = [row('p', 'b1.kicad_pcb', expect='regress', rejected=True)]
    assert gate(rows, {'p': 'regress'})[0]
    assert not gate(rows, {'p': 'improve'})[0]

    # --- the comparator ---
    base = {'r': {'board': 'b.kicad_pcb', 'mark': 'regress',
                  'off': {'crossings': 100, 'hpwl': 10.0,
                          'intent_errors': 5,
                          'intent_errors_by_rule': {'zone_containment': 5}},
                  'on': {'crossings': 90, 'hpwl': 9.0,
                         'intent_errors': 7,
                         'intent_errors_by_rule': {'zone_containment': 7}}}}
    same = json.loads(json.dumps(base))
    assert compare_baseline(same, base) == [], compare_baseline(same, base)

    # 7. a reversed direction is INVERTED, not DRIFT. This is #694 itself.
    inv = json.loads(json.dumps(base))
    inv['r']['on']['crossings'] = 110
    probs = compare_baseline(inv, base)
    assert any(p.startswith('INVERTED r.crossings') for p in probs), probs
    assert not any(p.startswith('DRIFT r.crossings') for p in probs), probs

    # 8. a changed mark is INVERTED.
    m = json.loads(json.dumps(base))
    m['r']['mark'] = 'improve'
    assert any('INVERTED r: mark' in p for p in compare_baseline(m, base))

    # 9. integers compare exactly; direction unchanged, so DRIFT not INVERTED.
    d = json.loads(json.dumps(base))
    d['r']['on']['crossings'] = 91
    probs = compare_baseline(d, base)
    assert any(p.startswith('DRIFT r.crossings') for p in probs), probs

    # 10. floats: inside the tolerance is silence, outside it is DRIFT.
    f = json.loads(json.dumps(base))
    f['r']['on']['hpwl'] = 9.0 + 1e-9
    assert compare_baseline(f, base, float_tol=1e-6) == []
    f['r']['on']['hpwl'] = 9.05
    assert any(p.startswith('DRIFT r.hpwl')
               for p in compare_baseline(f, base, float_tol=1e-6))

    # 10b. --float-tol is for floats ONLY. A loose tolerance must not start
    #      waving integer counts through: "crossings 90 -> 94" is a real move
    #      at every tolerance, and the --help promises exactly that.
    t = json.loads(json.dumps(base))
    t['r']['on']['hpwl'] = 9.0001
    t['r']['on']['crossings'] = 94
    probs = compare_baseline(t, base, float_tol=0.05)
    assert not any('r.hpwl' in p for p in probs), probs
    assert any(p.startswith('DRIFT r.crossings') for p in probs), probs

    # 11. an error rule whose count moved is named BY RULE.
    r = json.loads(json.dumps(base))
    r['r']['on']['intent_errors_by_rule'] = {'zone_containment': 7,
                                             'keepout': 1}
    probs = compare_baseline(r, base)
    assert any('intent_errors_by_rule[keepout]' in p for p in probs), probs

    # 12. scope: a baseline row outside this run is not reported missing.
    assert compare_baseline({}, base, scope=set()) == []
    assert any(p.startswith('MISSING r')
               for p in compare_baseline({}, base, scope={'r'}))

    # 13. a row the baseline has never seen is named, not silently accepted.
    assert any(p.startswith('NEW ROW r') for p in compare_baseline(base, {}))

    # --- _verdict and the record it produces ---
    vrow = {'name': 'v', 'board': 'b.kicad_pcb', 'quench_on': {'x': 1},
            'signal': 'health_bus_foreign_crossings',
            'guard': ('crossings', 'hpwl')}
    voff = {'crossings': 100, 'hpwl': 10.0, 'corridor_cut': 0.0, 'seconds': 1,
            'health_bus_foreign_crossings': 62,
            'health_block_displacement_max_mm': 17.95,
            'intent_errors': 14,
            'intent_errors_by_rule': {'block_unresolved': 10,
                                      'zone_containment': 4}}
    von = {'crossings': 90, 'hpwl': 9.0, 'corridor_cut': 800.0, 'seconds': 1,
           'health_bus_foreign_crossings': 55,
           'health_block_displacement_max_mm': 18.09,
           'intent_errors': 17,
           'intent_errors_by_rule': {'block_unresolved': 10,
                                     'zone_containment': 7}}

    # 14. #694 in miniature: signal and both guards improve, yet the mark is
    #     REGRESS -- and the note must NAME the rule that decided it, or the
    #     verdict is again resting on a criterion nobody printed.
    mark, notes = _verdict(voff, von, vrow)
    assert mark == 'regress', (mark, notes)
    assert any('zone_containment 4 -> 7' in n for n in notes), notes

    # 15. a guard that worsens is named as a guard, not as the signal.
    g = dict(von, crossings=101)
    mark, notes = _verdict(voff, g, vrow)
    assert mark == 'regress' and any('GUARD crossings' in n for n in notes)

    # 16. with the errors equal, the signal decides.
    e = dict(von, intent_errors=14,
             intent_errors_by_rule={'block_unresolved': 10,
                                    'zone_containment': 4})
    assert _verdict(voff, e, vrow)[0] == 'improve'
    assert _verdict(voff, dict(e, health_bus_foreign_crossings=62),
                    vrow)[0] == 'neutral'
    assert _verdict(voff, dict(e, health_bus_foreign_crossings=70),
                    vrow)[0] == 'regress'

    # 17. the record carries every compared key and NOTHING that is not
    #     comparable -- `seconds` is wall-clock, and `corridor_cut` has no OFF
    #     arm to compare against.
    rec = record_for(vrow, 'regress', voff, von)
    for arm in ('off', 'on'):
        assert 'seconds' not in rec[arm] and 'corridor_cut' not in rec[arm], rec
        for k in BASELINE_INT_KEYS + BASELINE_FLOAT_KEYS + BASELINE_DICT_KEYS:
            assert k in rec[arm], (k, rec[arm])
    assert compare_baseline({'v': rec}, {'v': rec}) == []

    # 18. a measurement that has LOST a compared key refuses, instead of
    #     recording a baseline with a hole in it.
    try:
        record_for(vrow, 'regress', {k: v for k, v in voff.items()
                                     if k != 'intent_errors'}, von)
    except AssertionError as exc:
        assert 'intent_errors' in str(exc), exc
    else:
        raise AssertionError('a missing compared key must refuse')


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--row', action='append',
                   help='Run only these table rows (repeatable)')
    p.add_argument('--list', action='store_true', help='List rows and exit')
    p.add_argument('--workdir', default=None,
                   help='Where to write boards (default: a temp dir)')
    p.add_argument('--json', '--json-out', dest='json', default=None,
                   help='Write the full per-row report here (not committed)')
    p.add_argument('--baseline', default=DEFAULT_BASELINE,
                   help='Committed measurements to compare against '
                        '(default: %(default)s); "" to skip the comparison')
    p.add_argument('--write-baseline', action='store_true',
                   help='Record CURRENT measurements as the expectation. Only '
                        'after reading the table and agreeing with every row.')
    p.add_argument('--float-tol', type=float, default=1e-6,
                   help='Relative tolerance for float keys (default: '
                        '%(default)s). Integers always compare exactly.')
    p.add_argument('--self-test', action='store_true',
                   help='Run the gate/comparator logic checks and exit')
    args = p.parse_args(argv)

    # Always, and first: a broken gate now fails in a second instead of after
    # nine minutes of quenching.
    _self_test()
    if args.self_test:
        print('self-test OK')
        return 0

    if args.list:
        for r in ROWS:
            print(f"{r['name']:<24} {r['board']:<28} "
                  f"{r['quench_on']} -> {r['signal']}")
        return 0

    rows = [r for r in ROWS if not args.row or r['name'] in args.row]
    if not rows:
        print("no such row; try --list", file=sys.stderr)
        return 2

    workdir = args.workdir or tempfile.mkdtemp(prefix='placement_ab_')
    os.makedirs(workdir, exist_ok=True)
    print(f"A/B in {workdir}\n")

    marks, records = {}, {}
    for r in rows:
        mark, _notes, off, on = run_row(r, workdir)
        marks[r['name']] = mark
        if off is not None and on is not None:
            records[r['name']] = record_for(r, mark, off, on)

    print()
    tally = {m: [n for n, v in marks.items() if v == m]
             for m in ('improve', 'neutral', 'regress', 'skip')}
    # Printed, not dropped: a term with no effect on 3 of 4 boards must not
    # read as a clean sweep.
    for m in ('improve', 'neutral', 'regress', 'skip'):
        if tally[m]:
            print(f"{m:<9} {len(tally[m])}: {', '.join(tally[m])}")

    if args.json:
        with open(args.json, 'w') as fh:
            json.dump({'rows': records, 'marks': marks}, fh, indent=1,
                      sort_keys=True)
        print(f"wrote report: {args.json}")

    if args.write_baseline:
        target = args.baseline or DEFAULT_BASELINE
        with open(target, 'w') as fh:
            json.dump(records, fh, indent=1, sort_keys=True)
        print(f"\nwrote baseline: {target} ({len(records)} row(s)). Read the "
              f"table above and agree with every row before committing it.")
        return 0

    ok, lines = gate(rows, marks)
    for ln in lines:
        print(ln)

    problems, compared = [], False
    if args.baseline:
        expected = None
        try:
            with open(args.baseline) as fh:
                expected = json.load(fh)
        except Exception as exc:                       # noqa: BLE001
            print(f"baseline unreadable: {exc}")
        if expected is None:
            print("no baseline recorded. Read the table above, then "
                  "--write-baseline.")
        else:
            compared = True
            problems = compare_baseline(records, expected,
                                        float_tol=args.float_tol,
                                        scope={r['name'] for r in rows})
    else:
        print('baseline comparison SKIPPED (--baseline "")')

    if problems:
        ok = False
        print(f"\nbaseline: {len(problems)} problem(s) vs "
              f"{os.path.basename(args.baseline)}")
        for pr in problems:
            print(f"  {pr}")
        print("  INVERTED means the recorded finding no longer holds -- read "
              "it before re-recording.\n"
              "  An intended change re-records with --write-baseline, in the "
              "same commit as the change.")
    elif compared:
        print(f"baseline: {len(records)} row(s) match "
              f"{os.path.basename(args.baseline)}")

    print(f"\n{'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
