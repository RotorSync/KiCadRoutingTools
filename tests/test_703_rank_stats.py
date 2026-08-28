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
    # There must be no way to get a value-only rendering out of the module.
    src = inspect.getsource(rs.fmt_rho)
    check('LOO' in src, 'fmt_rho itself is what carries the span')


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
    # No public function may take a flat multi-board row list.
    offenders = []
    for name, fn in vars(rs).items():
        if name.startswith('_') or not callable(fn) or not inspect.isfunction(fn):
            continue
        if name in ('per_board',):
            continue
        try:
            ps = inspect.signature(fn).parameters
        except (TypeError, ValueError):
            continue
        if 'rows' in ps and name not in ('board_rho', 'classify_board'):
            offenders.append(name)
    check(not offenders,
          f'no public fn but per_board/board_rho/classify_board takes `rows` '
          f'(offenders: {offenders})')


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
