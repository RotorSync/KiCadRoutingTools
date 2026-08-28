#!/usr/bin/env python3
"""The #703 statistics kernel, tested from outside itself.

`tests/stress/rank_stats.py` carries its own `_self_test()`, which runs at the
top of every tool that imports it. This file exists for three reasons that a
self-test cannot cover:

1. **`tests/stress/` is invisible to `run_all.py`.** Its `discover()` globs
   `tests/test_*.py`, non-recursively -- `test_718_static_test_hygiene.py`
   records what that costs: `tests/stress/corpus_noop_sweep.py` "carried THREE
   stale root spawns and reported nothing for months". So this file IMPORTS
   the kernel; a break in it turns a discovered test red.
2. **A self-test that a mutation survives is decoration.** The cases here pin
   VALUES, not shapes, so replacing a kernel body with `return float('nan')`
   or with the uncorrected rank-difference formula fails them. `mutate_703.py`
   is the adversary that proves it.
3. **The anti-pooling guard is a type contract**, and a contract is worth a
   test that reads it by introspection rather than trusting the docstring.

The one thing this file must NOT do is spawn a child process or import the
shared test helpers: `run_all.is_integration()` substring-matches this file's
RAW SOURCE for those names -- docstrings included -- and a match drops it out
of the `--fast` loop, where a broken kernel should be caught in a second rather
than fifteen minutes later.

    python3 -X utf8 tests/test_703_rank_stats.py

WHAT THE BATTERY MEASURED (`python3 tests/mutate_703.py`, 18 rows against
`tests/stress/rank_stats.py`). The count is how many of THIS file's checks the
mutation trips; every row was also caught by the kernel's own `_self_test`.

    nan-becomes-zero                             KILLED   3
    constant-side-becomes-zero                   KILLED   3
    uncorrected-d2-formula                       KILLED   5
    ties-get-sequential-ranks                    KILLED   7
    tie-detection-grows-a-tolerance              KILLED   2
    min-n-lowered-to-two                         KILLED   4
    sign-test-accepts-a-sequence                 KILLED   3
    per-board-buckets-a-missing-key              KILLED   2
    nan-boards-join-the-denominator              KILLED   4
    saturated-is-reported-as-measurable          KILLED   3
    a-constant-truth-is-not-named-as-saturation  KILLED   2
    the-rule-tolerates-one-wrong-board           KILLED   2
    fmt-rho-drops-the-LOO-span                   KILLED   3
    fmt-renders-NaN-as-zero                      KILLED   2
    rank-accepts-a-None                          KILLED   2
    board-rho-coerces-a-null-to-zero             KILLED   4
    a-comment-names-the-dependent-variable       SURVIVED 0   (expected: inert)
    fmt-default-width-widens                     SURVIVED 0   (expected: inert)

    18 rows: 16 killed, 2 survived (2 of them expected), 0 broken

READ THE COUNT COLUMN, NOT THE VERDICT COLUMN. The FIRST run of this battery
also said "16 killed, 2 survived, 0 broken" -- and every one of those kills was
`t_self_test` raising. It ran first, its AssertionError escaped, the file
aborted, and none of the checks below it ever executed. The verdicts were
identical and the coverage was zero. What changed is the attribution: the
self-test now runs LAST and catches, so a non-zero count in the column above is
this file's own checks firing. `tie-detection-grows-a-tolerance` at 2 and
`per-board-buckets-a-missing-key` at 2 are the thinnest rows here, and that is
worth knowing about them.
"""
import inspect
import math
import os
import sys
import typing

RUN_ALL_TIMEOUT = 120
RUN_ALL_FAST_OK = True

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'tests', 'stress'))

import rank_stats as rs          # noqa: E402


FAILURES = []


def check(cond, what):
    if cond:
        print(f'  ok   {what}')
    else:
        print(f'  FAIL {what}')
        FAILURES.append(what)


def close(a, b, tol=1e-9):
    return isinstance(a, float) and a == a and abs(a - b) <= tol


def t_self_test():
    """The kernel's own arms, run from here so run_all sees them.

    It CATCHES rather than propagates, and it runs LAST. The first version ran
    first and let the AssertionError escape, so every mutation in
    `mutate_703.py` was killed by this one function and the sixty-odd checks
    below it never executed -- the battery reported 16 kills for a file whose
    external coverage was entirely unmeasured. A self-test that aborts the
    suite it leads is indistinguishable from a suite with nothing else in it.
    """
    try:
        n = rs._self_test(force=True)
    except AssertionError as e:
        check(False, f'kernel self-test raised: {e}')
        return
    check(n >= 40, f'kernel self-test ran {n} assertions (expected >= 40)')


def t_values_not_shapes():
    """Pin NUMBERS. A body replaced by `return nan` must not survive this."""
    check(close(rs.spearman([1, 2, 3, 4], [1, 2, 3, 4]), 1.0),
          'monotone rho is exactly +1.0')
    check(close(rs.spearman([1, 2, 3, 4], [4, 3, 2, 1]), -1.0),
          'reversed rho is exactly -1.0')
    # Ties on both sides. The tie-corrected answer is 3.75/4.5; the UNCORRECTED
    # 1 - 6*sum(d^2)/(n(n^2-1)) shortcut gives 0.85 on this pair, so this case
    # separates a real Pearson-on-ranks from the schoolbook formula.
    v = rs.spearman([1, 2, 2, 3], [1, 1, 2, 3])
    check(close(v, 3.75 / 4.5, 1e-12), f'tie-corrected rho is {v!r} (= 3.75/4.5)')
    check(not close(v, 0.85, 1e-9), 'rho is NOT the uncorrected d^2 shortcut')
    check(rs.rank([3, 1, 1, 2]) == [4.0, 1.5, 1.5, 3.0],
          'rank([3,1,1,2]) == [4, 1.5, 1.5, 3]')
    check(rs.rank([1.0, 1.0 + 1e-12, 2.0]) == [1.0, 2.0, 3.0],
          'a 1e-12 gap is not a tie -- ranking has no tolerance')
    check(rs.tie_count([1, 1, 2, 3, 3, 3]) == 5,
          'tie_count counts every member of a tie group')


def t_nan_never_zero():
    """The single most load-bearing rule: unmeasurable is NaN, not 0.0."""
    for what, a, b in (('constant predictor', [1, 1, 1], [1, 2, 3]),
                       ('constant dependent', [1, 2, 3], [7, 7, 7]),
                       ('n < 3', [1, 2], [3, 4]),
                       ('mismatched lengths', [1, 2, 3], [1, 2])):
        v = rs.spearman(a, b)
        check(v != v, f'{what} -> NaN (got {v!r}; 0.0 would read as measured)')
    check(rs.constant_side([1, 1, 1], [1, 2, 3]) == 'predictor',
          'constant_side names the PREDICTOR side')
    check(rs.constant_side([1, 2, 3], [7, 7, 7]) == 'dependent',
          'constant_side names the DEPENDENT side (saturation)')
    check(rs.constant_side([1, 1], [7, 7]) == 'both', 'constant_side both')
    check(rs.constant_side([1, 2], [3, 4]) is None, 'constant_side none')


def t_loo_span():
    """The instability that makes n=6 pooled uninterpretable, pinned."""
    cross = [r[1] for r in rs.LEGACY_POOLED]
    blk = [r[4] for r in rs.LEGACY_POOLED]
    lo, hi = rs.loo_span(cross, blk)
    check(close(round(lo, 3), 0.053) and close(round(hi, 3), 0.632),
          f'published LOO span reproduces: {lo:+.3f}..{hi:+.3f}')
    rho = rs.spearman(cross, blk)
    check(close(round(rho, 3), 0.339), f'published rho reproduces: {rho:+.3f}')
    check(hi - lo > abs(rho), 'the LOO span is WIDER than the headline itself')
    check(rs.loo_span([1, 2, 3], [1, 2, 3]) == (float('nan'),) * 0 + (
        rs.NAN, rs.NAN) or all(x != x for x in rs.loo_span([1, 2, 3], [1, 2, 3])),
        'n=3 is too small for a LOO span -> (NaN, NaN)')


def t_formatter_cannot_emit_a_bare_rho():
    """#703's failure mode is a number quoted without its scope. Prevent it here."""
    s = rs.fmt_rho(0.339, 0.053, 0.632, 6)
    check(s.startswith('rho=+0.339'), f'fmt_rho leads with the value: {s!r}')
    check('LOO +0.053..+0.632' in s, f'fmt_rho carries the LOO span: {s!r}')
    check('K=6' in s, f'fmt_rho carries K: {s!r}')
    s = rs.fmt_rho(rs.NAN, reason='truth constant (saturated)')
    check('n/a' in s and 'saturated' in s,
          f'fmt_rho(NaN) says WHY, not +0.000: {s!r}')
    check(rs.fmt(rs.NAN).strip() == 'n/a', 'fmt(NaN) renders n/a')
    check(rs.fmt(0.0).strip() == '+0.000', 'fmt(0.0) still renders a real zero')
    check(rs.fmt(None).strip() == 'none',
          f'fmt(None) is a THIRD rendering, distinct from n/a and from a '
          f'number: {rs.fmt(None)!r}')
    # Called with no span, fmt_rho must SAY there is no span rather than print
    # a token that looks complete. The earlier 'LOO n/a' read like a measured
    # absence; 'not computed' reads like what it is.
    check('LOO not computed' in rs.fmt_rho(0.5),
          f'fmt_rho with no span says so: {rs.fmt_rho(0.5)!r}')
    check(repr(rs.BoardRho(0.5, 4, None, 0.4, 0.6)).startswith('rho=+0.500 ['),
          'BoardRho.__repr__ delegates to fmt_rho rather than printing a float')
    # The claim is about HUMAN-facing renderings, and this asserts the scope of
    # that claim rather than a stronger one that is false: `fmt` is a general
    # number formatter and `as_dict()['rho']` is deliberately a bare float so a
    # machine can read it. An earlier draft claimed the module could not emit a
    # bare rho at all, which an adversarial review falsified with `rs.fmt(rho)`.
    d = rs.board_rho([{'board_key': 'b', 'predictors': {'x': i},
                       'truth': {'headline': i * i}} for i in range(4)],
                     'x', 'headline').as_dict()
    check(isinstance(d['rho'], float),
          'as_dict.rho is a bare float ON PURPOSE, for machine reading')
    check(d['display'].startswith('rho=') and 'LOO' in d['display'],
          f'and as_dict.display is what a document quotes: {d["display"]!r}')


def t_named_refusals_not_typeerrors():
    for what, call in (
            ('None in a column', lambda: rs.rank([1, None, 3])),
            ('NaN in a column', lambda: rs.rank([1.0, rs.NAN, 3.0])),
            ('a row with no board_key', lambda: rs.per_board([{'row_id': 'x'}])),
            ('a flat list into sign_test', lambda: rs.sign_test([1, 2, 3])),
    ):
        try:
            call()
        except rs.StatsRefusal:
            check(True, f'{what} -> StatsRefusal')
        except Exception as e:                                  # noqa: BLE001
            check(False, f'{what} -> {type(e).__name__}, not StatsRefusal')
        else:
            check(False, f'{what} was accepted silently')


def t_anti_pooling_is_a_type_contract():
    """Read the guard by introspection, so the docstring cannot be the only
    thing enforcing it."""
    sig = inspect.signature(rs.sign_test)
    params = list(sig.parameters.values())
    check(len(params) == 1, 'sign_test takes exactly one argument')
    ann = params[0].annotation
    origin = typing.get_origin(ann) or ann
    check(origin in (typing.Mapping, dict) or 'Mapping' in str(ann),
          f'sign_test\'s argument is annotated as a Mapping (got {ann!r})')
    # And it must actually refuse a sequence at runtime, not just annotate one.
    try:
        rs.sign_test([rs.BoardRho(0.5, 20)] * 3)
        check(False, 'sign_test accepted a list -- pooling is reachable')
    except rs.StatsRefusal:
        check(True, 'sign_test refuses a sequence at runtime')
    # EVERY public function taking `rows` must REFUSE a multi-board pile.
    #
    # The first version of this check read
    #     if 'rows' in ps and name not in ('board_rho', 'classify_board')
    # which whitelisted precisely the two functions that violated the claim, so
    # it could not fail -- and `board_rho` on the six recorded runs returned the
    # pooled +0.339 the module docstring calls the trap. An adversarial review
    # found it in one line. The replacement is behavioural: it enumerates the
    # functions and CALLS each with a pile, so a new one is covered the day it
    # is written and no name can be exempted by editing a tuple.
    pile = [{'board_key': b, 'predictors': {'x': c}, 'truth': {'headline': k}}
            for b, c, _h, _hl, k, _v in rs.LEGACY_POOLED]
    takes_rows, refused = [], []
    for name, fn in sorted(vars(rs).items()):
        if name.startswith('_') or not inspect.isfunction(fn):
            continue
        try:
            ps = inspect.signature(fn).parameters
        except (TypeError, ValueError):
            continue
        if 'rows' not in ps:
            continue
        takes_rows.append(name)
        if name == 'per_board':
            continue          # per_board's whole job is to split the pile
        args = [pile] + (['x', 'headline'] if name == 'board_rho' else [])
        try:
            fn(*args)
        except rs.StatsRefusal:
            refused.append(name)
        except Exception as e:                                  # noqa: BLE001
            check(False, f'{name}(pile) raised {type(e).__name__}, not '
                         f'StatsRefusal: {e}')
    check(takes_rows == ['board_rho', 'classify_board', 'per_board'],
          f'the set of functions taking `rows` is what this check thinks it '
          f'is: {takes_rows}')
    check(sorted(refused) == ['board_rho', 'classify_board'],
          f'every rows-taking fn but per_board REFUSES a 6-board pile '
          f'(refused: {sorted(refused)})')
    # The specific regression, named: this exact call used to return +0.339.
    try:
        rs.board_rho(pile, 'x', 'headline')
        check(False, 'board_rho on the six recorded runs returned a POOLED rho')
    except rs.StatsRefusal as e:
        check('6 boards' in str(e),
              f'board_rho names how many boards it was handed: {e}')
    # per_board's grouping success path -- untested until an adversarial review
    # showed a mutation bucketing every board into one group left this file
    # entirely green.
    groups = rs.per_board([{'board_key': 'a', 'i': 0}, {'board_key': 'b'},
                           {'board_key': 'a', 'i': 1}])
    check(sorted(groups) == ['a', 'b'],
          f'per_board splits by board_key: {sorted(groups)}')
    check(len(groups['a']) == 2 and len(groups['b']) == 1,
          'per_board keeps every row, in its own board')
    check([r.get('i') for r in groups['a']] == [0, 1],
          'per_board preserves row order within a board')


def _rows(vals, key='b', pred=None):
    pred = pred if pred is not None else list(range(len(vals)))
    return [{'board_key': key, 'truth': {'headline': v},
             'predictors': {'x': p}} for v, p in zip(vals, pred)]


def t_saturation_is_reported_never_dropped():
    check(rs.classify_board(_rows([0, 0, 0, 0])) == 'saturated',
          'all-zero blocking -> saturated')
    check(rs.classify_board(_rows([5, 5, 5, 5])) == 'starved',
          'all-equal-nonzero blocking -> starved')
    check(rs.classify_board(_rows([0, 1, 2])) == 'measurable', 'mixed')
    check(rs.classify_board(_rows([0, 1])) == 'thin', 'n<3 -> thin, not 0')
    br = rs.board_rho(_rows([0, 0, 0, 0]), 'x', 'headline')
    check(br.rho != br.rho and 'saturated' in (br.reason or ''),
          f'a saturated board carries a reason: {br.reason!r}')
    br = rs.board_rho(_rows([0, 1, 2, 3], pred=[4, 4, 4, 4]), 'x', 'headline')
    check('predictor constant' in (br.reason or ''),
          f'a constant PREDICTOR is a different finding: {br.reason!r}')
    # The saturated board stays in the aggregate's bookkeeping.
    st = rs.sign_test({'a': rs.BoardRho(0.5, 20), 'b': rs.BoardRho(0.6, 20),
                       'c': rs.BoardRho(0.7, 20),
                       'sat': rs.BoardRho(rs.NAN, 20, 'truth constant '
                                                      '(saturated)')})
    check(st['boards_attempted'] == 4, 'a saturated board is still ATTEMPTED')
    check(st['boards_defined'] == 3, 'it is excluded from the denominator')
    check('sat' in st['undefined'], 'and it is NAMED in undefined')
    lines = '\n'.join(rs.format_sign_test('x vs blocking', st))
    check('sat' in lines and 'saturated' in lines,
          'the printed form names the saturated board')
    check('NOT over the planned count' in lines,
          'the printed p states its own denominator')


def t_sign_test_arithmetic():
    def brs(vals):
        return {f'b{i}': rs.BoardRho(v, 20) for i, v in enumerate(vals)}
    st = rs.sign_test(brs([0.5] * 6))
    check(close(st['p_two_sided'], 0.0313, 1e-3),
          f'6/6 two-sided p = {st["p_two_sided"]} (~0.031)')
    check(st['passes_sign_rule'], '6-of-6 passes the sign rule')
    check(st['coin_flip_null'] == '1 in 64', 'N=6 coin-flip null is 1 in 64')
    st = rs.sign_test(brs([0.5] * 5 + [-0.4]))
    check(close(st['p_one_sided'], 0.1094, 1e-3),
          f'5/6 one-sided p = {st["p_one_sided"]} -- the research note\'s 0.11')
    check(close(st['p_two_sided'], 0.2188, 1e-3),
          f'5/6 two-sided p = {st["p_two_sided"]} -- the honest headline')
    check(not st['passes_sign_rule'],
          'ONE wrong-direction board fails the rule regardless of p')
    st = rs.sign_test(brs([0.5, 0.5, 0.5]))
    check(close(st['p_two_sided'], 0.25), '3/3 two-sided p = 0.25')
    st = rs.sign_test(brs([0.5, 0.5]))
    check(close(st['p_two_sided'], 0.5), 'N=2 minimum two-sided p = 0.50')
    st = rs.sign_test(brs([-0.5, -0.6, -0.7]))
    check(st['direction'] == 'negative' and st['passes_sign_rule'],
          'a consistently NEGATIVE predictor passes too (direction is reported)')
    # A rho of exactly 0.0 is the NEUTRAL board, and it is treated exactly as
    # `test_placement_ab.gate()` treats a neutral mark: it counts in N, it is
    # not evidence either way, and one of them out of three does not sink the
    # rule (2 consistent >= N-1 = 2). It is named in its own bucket so a reader
    # can see the rule passed on two boards and not three.
    st = rs.sign_test(brs([0.5, 0.6, 0.0]))
    check(st['passes_sign_rule'],
          'one NEUTRAL board out of three still meets >= N-1, per the house rule')
    check(st['consistent'] == 2 and st['boards_defined'] == 3,
          'and the aggregate shows 2 consistent of 3 defined, not 3 of 3')
    check(st['zero'] == ['b2'], 'a zero board is named in its own bucket')
    st = rs.sign_test(brs([0.5, 0.0, 0.0]))
    check(not st['passes_sign_rule'],
          'TWO neutral boards of three fall below N-1 and fail')
    check(rs.sign_test({})['boards_defined'] == 0, 'an empty mapping is legal')


def t_board_rho_drops_nulls_and_says_so():
    rows = _rows([0, 1, 2, 3])
    rows[0]['predictors']['x'] = None
    br = rs.board_rho(rows, 'x', 'headline')
    check(br.n == 3, f'a null row is dropped: n={br.n}')
    check('dropped' in (br.reason or ''),
          f'and the drop is NAMED: {br.reason!r}')
    rows = _rows([0, 1, 2, 3])
    rows[1]['truth']['headline'] = None
    br = rs.board_rho(rows, 'x', 'headline')
    check(br.n == 3 and 'dropped' in (br.reason or ''),
          'a null TRUTH (board_score could not run) drops the row too')
    d = rs.board_rho(_rows([0, 1, 2, 3]), 'x', 'headline').as_dict()
    check(set(d) >= {'rho', 'n', 'reason', 'loo_lo', 'loo_hi', 'display'},
          'as_dict carries the scope fields a document needs')
    check(d['display'].startswith('rho='),
          'as_dict.display is the atomic token, not a bare float')


def main():
    # The kernel's own self-test runs LAST, and every check below is written to
    # stand without it. If it led, a mutation that trips it would abort the file
    # and the external coverage would never be exercised -- which is precisely
    # what the first run of `mutate_703.py` reported.
    for fn in (t_values_not_shapes, t_nan_never_zero, t_loo_span,
               t_formatter_cannot_emit_a_bare_rho, t_named_refusals_not_typeerrors,
               t_anti_pooling_is_a_type_contract,
               t_saturation_is_reported_never_dropped, t_sign_test_arithmetic,
               t_board_rho_drops_nulls_and_says_so, t_self_test):
        print(f'{fn.__name__}:')
        try:
            fn()
        except Exception as e:                                  # noqa: BLE001
            # One case raising must not hide the ones after it. A mutation that
            # makes a refusal fire in an unexpected place would otherwise
            # silence every later check and still read as a clean kill.
            check(False, f'{fn.__name__} raised {type(e).__name__}: {e}')
    if FAILURES:
        print(f'\nFAILED {len(FAILURES)}:')
        for f in FAILURES:
            print(f'  - {f}')
        return 1
    print('\ntest_703_rank_stats: all checks passed')
    return 0


if __name__ == '__main__':
    sys.exit(main())
