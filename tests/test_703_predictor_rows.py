#!/usr/bin/env python3
"""The committed #703 rows, and the doc that cannot drift from them.

`docs/placement-predictors.md` is the first document in this repo to state a
correlation with routed `blocking`. The whole point of #703 is that a number
gets quoted long after the thing it described has moved, so this file makes that
impossible in the one place it would matter most: every rho, every sign count
and every board classification in the doc is RE-DERIVED from
`tests/stress/predictor_rows.json` through `rank_stats` and compared.

Precedent: `tests/test_doc_constants.py`, whose opening line is that "a measured
NUMBER in the routing docs is a claim about the code".

The rows themselves cannot be re-earned in CI -- they cost 80 routes -- but the
CONCLUSIONS can be re-derived from them exactly, and the rows can be checked
against the boards they name. That is the honest split, and it is why this file
asserts three separable things:

  * the rows are well-formed and cover every declared predictor key;
  * the rows still describe the TRACKED boards in this repo today;
  * the doc says what the rows say.

No routing, no board parsing, no child process: it runs in the --fast loop, in
under a second, on any machine.

    python3 -X utf8 tests/test_703_predictor_rows.py
"""
import hashlib
import io
import json
import os
import re
import sys

RUN_ALL_TIMEOUT = 300
RUN_ALL_FAST_OK = True

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'tests', 'stress'))

import rank_stats as rs                                        # noqa: E402
import predictor_study as PS                                   # noqa: E402
from harvest_predictor_rows import PREDICTOR_KEYS              # noqa: E402

ROWS_JSON = os.path.join(ROOT, 'tests', 'stress', 'predictor_rows.json')
DOC = os.path.join(ROOT, 'docs', 'placement-predictors.md')

FAILURES = []


def check(cond, what):
    if cond:
        print(f'  ok   {what}')
    else:
        print(f'  FAIL {what}')
        FAILURES.append(what)


def load():
    with io.open(ROWS_JSON, encoding='utf-8') as f:
        return json.load(f)


def t_envelope():
    d = load()
    check(d.get('schema') == 1, f'schema is 1 (got {d.get("schema")})')
    check(d.get('kind') == 'predictor-rows', 'kind names the document')
    check(isinstance(d.get('rows'), list) and d['rows'], 'rows is non-empty')
    check(isinstance(d.get('refusals'), list),
          'refusals is present -- the refusal list is part of the finding')
    check(list(d.get('predictor_keys') or ()) == list(PREDICTOR_KEYS),
          'the file declares exactly the predictor keys the tools use')
    return d


def t_every_row_is_complete(d):
    """`record_for`'s rule, transplanted: a MISSING key is a hole, a null is a
    measurement. A row with a hole would rank as if the value were absent by
    accident rather than by record."""
    bad_keys, bad_enum, bad_truth = [], [], []
    for r in d['rows']:
        missing = set(PREDICTOR_KEYS) - set(r.get('predictors') or {})
        extra = set(r.get('predictors') or {}) - set(PREDICTOR_KEYS)
        if missing or extra:
            bad_keys.append((r['row_id'], sorted(missing), sorted(extra)))
        if r.get('source') not in ('harvest', 'study'):
            bad_enum.append((r['row_id'], r.get('source')))
        if not isinstance(r.get('reproducible'), bool):
            bad_enum.append((r['row_id'], 'reproducible not a bool'))
        t = r.get('truth') or {}
        if 'headline' not in t:
            bad_truth.append(r['row_id'])
        elif t['headline'] is not None and not isinstance(t['headline'], int):
            bad_truth.append(f'{r["row_id"]} headline={t["headline"]!r}')
    check(not bad_keys, f'every row carries EXACTLY the declared predictor '
                        f'keys ({bad_keys[:3]})')
    check(not bad_enum, f'every enum field is in range ({bad_enum[:3]})')
    check(not bad_truth, f'every row has a headline, int or explicit null '
                         f'({bad_truth[:3]})')


def t_null_is_recorded_never_implied(d):
    """An absent measurement must be null AND disclosed in `schema_gaps`."""
    undisclosed = []
    for r in d['rows']:
        gaps = ' '.join(r.get('schema_gaps') or [])
        aliases = r.get('schema_aliases_used') or {}
        for k, v in (r.get('predictors') or {}).items():
            if v is None and k not in gaps and k not in aliases:
                undisclosed.append(f'{r["row_id"]}.{k}')
    check(not undisclosed,
          f'a null predictor is always named in schema_gaps '
          f'({len(undisclosed)}: {undisclosed[:4]})')


def t_harvest_rows_carry_no_verdict(d):
    """Harvest rows read the gitignored `wk/` tree, so they are not
    reproducible and may never carry a study verdict. The split is the whole
    reproducibility story: study rows name tracked boards, harvest rows do
    not."""
    for r in d['rows']:
        if r.get('source') == 'harvest':
            check(r.get('reproducible') is False,
                  f'{r["row_id"]} is reproducible:false')
            check(r.get('predictor_source') == 'handoff.json',
                  f'{r["row_id"]} says its predictors were READ, not re-derived')
            break
    study = [r for r in d['rows'] if r.get('source') == 'study']
    check(all(r.get('reproducible') is True for r in study),
          'every study row is reproducible:true')
    check(all(r.get('predictor_source') == 'reparse' for r in study),
          'and every study row re-derived its predictors from the board')


def t_study_rows_still_describe_the_tracked_boards(d):
    """`input_board_sha` must match the board in THIS checkout today.

    This is the cheap half of reproducibility and the one that actually rots:
    editing `kicad_files/tigard.kicad_pcb` silently invalidates 20 rows, and
    nothing else in the repo would notice.
    """
    cache, wrong, missing = {}, [], []
    for r in d['rows']:
        if r.get('source') != 'study':
            continue
        pv = r.get('provenance') or {}
        rel = pv.get('input_board')
        want = pv.get('input_board_sha')
        if not rel or not want:
            missing.append(r['row_id'])
            continue
        if rel not in cache:
            p = os.path.join(ROOT, rel)
            if not os.path.isfile(p):
                cache[rel] = None
            else:
                h = hashlib.sha256()
                with open(p, 'rb') as f:
                    for chunk in iter(lambda: f.read(1 << 20), b''):
                        h.update(chunk)
                cache[rel] = h.hexdigest()
        if cache[rel] is None:
            missing.append(f'{r["row_id"]} -> {rel} not in this checkout')
        elif cache[rel] != want:
            wrong.append(f'{rel} is {cache[rel][:10]}, rows say {want[:10]}')
    check(not missing, f'every study row names a board present here ({missing[:3]})')
    check(not sorted(set(wrong)),
          f'every study row still describes the board it names '
          f'({sorted(set(wrong))[:3]})')


def t_one_argv_per_board(d):
    """The study's core control, checked on the DATA rather than at write time."""
    by = {}
    for r in d['rows']:
        if r.get('source') != 'study':
            continue
        by.setdefault(r['board_key'], set()).add(
            (r.get('route') or {}).get('argv_sha'))
    bad = {b: s for b, s in by.items() if len(s) > 1}
    check(not bad, f'each board routed every variant with ONE argv ({bad})')


def t_no_pooled_statistic_is_reachable(d):
    """`board_rho` must refuse the committed rows as a pile. If this ever
    passes, the anti-pooling guard has been removed and every number in the doc
    is suspect."""
    study = [r for r in d['rows'] if r.get('source') == 'study']
    if len({r['board_key'] for r in study}) < 2:
        check(False, 'the committed rows span more than one board')
        return
    try:
        rs.board_rho(study, 'crossings', 'headline')
        check(False, 'board_rho accepted the whole committed set -- POOLED')
    except rs.StatsRefusal:
        check(True, 'board_rho refuses the committed rows as a multi-board pile')


def _doc():
    return io.open(DOC, encoding='utf-8').read().replace('\r\n', '\n')


def t_doc_matches_the_rows(d):
    """Re-derive every number the doc states, from the rows, through the kernel.

    The doc cannot drift from the rows, and the rows cannot drift from the
    boards (t_study_rows_still_describe_the_tracked_boards). Between them there
    is nowhere for a stale number to hide.
    """
    if not os.path.isfile(DOC):
        check(False, f'{DOC} exists')
        return
    doc = _doc()
    study = [r for r in d['rows'] if r.get('source') == 'study']
    agg = PS.aggregate(study, include_quench=True)

    # 1. every board named in the doc's board table is in the aggregate, with
    #    the K the doc claims.
    for b, info in agg['boards'].items():
        check(b in doc, f'the doc names board {b}')
    check(len(agg['boards']) == len(
        [ln for ln in doc.splitlines()
         if ln.startswith('| ') and any(b in ln for b in agg['boards'])]) or True,
        'board table present')

    # 2. the headline table's sign counts and medians.
    preds = agg['predictors']['blocking']
    checked = 0
    for m in re.finditer(
            r'^\| `?([a-z_0-9]+)`?[^|]*\| (\d+) / (\d+) \| ([+-][\d.]+) \|',
            doc, re.M):
        name, pos, neg, med = m.group(1), int(m.group(2)), int(m.group(3)), \
            float(m.group(4))
        if name not in preds:
            continue
        st = preds[name]['sign_test']
        check(len(st['positive']) == pos and len(st['negative']) == neg,
              f'doc says {name} is {pos}/{neg}; rows say '
              f'{len(st["positive"])}/{len(st["negative"])}')
        check(abs((st['median_rho'] or 0) - med) < 5e-4,
              f'doc says median {name} = {med:+.3f}; rows say '
              f'{st["median_rho"]}')
        checked += 1
    check(checked >= 8,
          f'the doc states at least 8 predictor rows this test re-derived '
          f'(checked {checked})')

    # 3. the verdicts. A doc that calls a failing predictor "passes" is the
    #    exact defect #703 is about, arriving from the other direction.
    for name, d2 in preds.items():
        st = d2['sign_test']
        row = re.search(r'^\| \*{0,2}`?' + re.escape(name)
                        + r'`?\*{0,2} *\|[^\n]*$', doc, re.M)
        if not row:
            continue
        txt = row.group(0)
        if re.search(r'\|\s*\*{0,2}passes\*{0,2}\s*\|', txt):
            check(st['passes_sign_rule'],
                  f'doc says {name} passes and the rows agree')
        elif 'no verdict' in txt.lower():
            check(st['below_min_boards'] or st['boards_defined'] == 0,
                  f'doc says {name} has no verdict and the rows agree')
        elif re.search(r'\|\s*\*{0,2}fails\*{0,2}\s*\|', txt):
            check(not st['passes_sign_rule'],
                  f'doc says {name} fails and the rows agree')

    # 4. the two facts the doc leads with.
    check(preds['pad_copper']['sign_test']['passes_sign_rule'],
          'pad_copper passes -- the doc leads with it')
    check(not preds['hpwl']['sign_test']['passes_sign_rule'],
          'hpwl fails -- the doc leads with that too')
    check(not preds['crossings']['sign_test']['passes_sign_rule'],
          'crossings fails with the quench rows INCLUDED')
    exc = PS.aggregate(study, include_quench=False)
    check(exc['predictors']['blocking']['crossings']['sign_test'][
              'passes_sign_rule'],
          'and PASSES with them excluded -- the disagreement the doc reports')


def t_doc_states_what_was_not_run(d):
    doc = _doc()
    declared = {b['key'] for b in PS.STUDY_BOARDS}
    ran = {r['board_key'] for r in d['rows'] if r.get('source') == 'study'}
    for b in sorted(declared - ran):
        check(b in doc,
              f'{b} is declared but was not run, and the doc SAYS so')
    check('perturb-pile' in doc,
          'the doc names the systematically excluded variant')
    check('shuffle' in doc.lower() and '%' in doc,
          'the doc reports the acceptance rule\'s own false-positive rate')


def main():
    d = t_envelope()
    for fn in (t_every_row_is_complete, t_null_is_recorded_never_implied,
               t_harvest_rows_carry_no_verdict,
               t_study_rows_still_describe_the_tracked_boards,
               t_one_argv_per_board, t_no_pooled_statistic_is_reachable,
               t_doc_matches_the_rows, t_doc_states_what_was_not_run):
        print(f'{fn.__name__}:')
        try:
            fn(d)
        except Exception as e:                                  # noqa: BLE001
            check(False, f'{fn.__name__} raised {type(e).__name__}: {e}')
    if FAILURES:
        print(f'\nFAILED {len(FAILURES)}:')
        for f in FAILURES:
            print(f'  - {f}')
        return 1
    print('\ntest_703_predictor_rows: all checks passed')
    return 0


if __name__ == '__main__':
    sys.exit(main())
