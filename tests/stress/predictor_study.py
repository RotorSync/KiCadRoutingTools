#!/usr/bin/env python3
"""Do pre-route placement predictors rank the routed outcome? (#703)

WHAT THIS ANSWERS, AND WHAT THE REPO HAD INSTEAD

Every correlation number quoted in this repo's skills and drivers is measured
against distance-to-the-correct-placement or against the gap a human left.
`r(crossings) = +0.780` is 29 candidates on ONE board against distance-to-truth;
the corridor law's `r = +0.41..+0.90` is against the human's gap. CLAUDE.md's
own "What a placement run is FOR" says the headline is `blocking`
(`unrouted + broken + ...`), and no predictor here had ever been correlated
with it. `harvest_predictor_rows.py` shows the recorded runs cannot supply the
answer -- one placement per board is n=1 per board, which is below the
arithmetic floor. So this generates the placements and pays for the routes.

THE ORDER IS THE EXPERIMENT, AND THE DRIVER ENFORCES IT

Four circularity traps, each of which this repo has already been bitten by:

  1. **Route argv is frozen per board BEFORE any variant exists.**
     `route_argv_for` derives it from the board alone, hashes it into
     `<out>/<board>/ARGV.json`, and REFUSES to generate or route anything if
     that file already exists at a different hash. Per-variant tuning is not
     representable in this tool, and the aggregator refuses a board whose rows
     disagree about `argv_sha`.

  2. **The sampler optimises none of the predictors.** The bad end comes from
     `placement/perturb.py` (the #411 damage rig) and the realistic end from
     `portfolio.generate`, whose quench DOES minimise crossings and hpwl -- so
     those rows carry `generator: "portfolio_quench"` and every statistic is
     reported twice, with and without them. If the two disagree in sign the
     report says so and calls neither the answer. Nothing here ever calls
     `rank_key` or `select_best`: selecting with a predictor and then
     correlating that predictor is the circle this issue exists to break.

  3. **Predictors are re-derived from the WRITTEN board.** Structurally, not by
     convention: `generate_variant` returns a PATH and `predictors_for` takes a
     path and nothing else, so the optimizer's live `QuenchState` is
     unreachable from the measurement.

  4. **A fresh output path per route.** `route.py` reads back a sibling
     `.kicad_pro` DRC floor, so re-running to the same path silently changes
     the routing -- it looks like non-determinism and is not (CLAUDE.md).

THE STATISTIC IS WITHIN A BOARD, NEVER POOLED

`rank_stats` will not let it be otherwise: `board_rho` refuses rows from more
than one board, and `sign_test` takes a mapping. Pooling measures board size --
on this repo's own corpus `rho(crossings, vias)` is +0.714 pooled across six
boards and -0.400 within one board's slate, opposite signs for the same two
quantities. Boards are aggregated by a SIGN TEST, the shape
`test_placement_ab.gate()` already uses: right direction on >= N-1 boards,
wrong direction on none, with the `1 in 2^N` coin-flip null printed beside it.

`blocking` and `vias` are reported as SEPARATE dependent variables. The recorded
evidence says crossings may predict cost and not completion, and collapsing the
two would hide exactly that.

SATURATION IS REPORTED, NEVER DROPPED

A board where every variant reaches `blocking 0` has no headroom and can rank
nothing; a board where every variant is equally broken is the same problem at
the other end. Both are printed with their constant value, counted in "boards
attempted", excluded from the sign test's denominator -- and BOTH numbers are
printed, because a p-value labelled with the planned board count when saturation
reduced the real one is a verdict resting on a criterion nobody printed.

    python3 -X utf8 tests/stress/predictor_study.py --calibrate --out wk/703
    python3 -X utf8 tests/stress/predictor_study.py --plan
    python3 -X utf8 tests/stress/predictor_study.py --out wk/703 -j 4
    python3 -X utf8 tests/stress/predictor_study.py --task esp_prog:authored --out wk/703
    python3 -X utf8 tests/stress/predictor_study.py --from-rows tests/stress/predictor_rows.json
    python3 -X utf8 tests/stress/predictor_study.py --from-rows ... --shuffle-control 200
    python3 -X utf8 tests/stress/predictor_study.py --verify-row tigard:perturb-swap-d1

The run is hours and a session can die inside it, so rows are appended to a
JSONL as each task finishes and `--resume` skips any (board, variant) already
present. `--from-rows` re-derives every statistic with no routing at all, so the
answer never has to be bought twice.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import glob
import hashlib
import json
import os
import subprocess
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(_HERE))
sys.path.insert(0, _HERE)
for _p in ('py_router', 'py_placer', 'py_tools'):
    sys.path.insert(0, os.path.join(ROOT, _p))

import rank_stats as rs                                        # noqa: E402
from harvest_predictor_rows import (METRIC_KEYS, PREDICTOR_KEYS,  # noqa: E402
                                    TRUTH_BY_KEYS, sha256_file, git_describe)

SCHEMA = 1
BOARD_SCORE = os.path.join(
    ROOT, '.claude', 'skills', 'plan-pcb-placement-and-routing', 'scripts',
    'board_score.py')
ROUTE_PY = os.path.join(ROOT, 'py_router', 'route.py')

#: THE BOARD SET, fixed in source before any number was seen.
#:
#: Every board is git-TRACKED, which is the property that makes this study
#: reproducible rather than a report from one machine: with the board, the seed
#: and the frozen argv, any reviewer can regenerate a variant byte-identically
#: (`portfolio.generate(..., only=i)` is deterministic by contract) and re-route
#: it. The research note that proposed this study named `neo6502`, which lives
#: only in an external corpus; it is dropped for exactly that reason.
#:
#: `route_seconds` is MEASURED by `--calibrate`, not estimated. Boards are
#: chosen to span part count and layer count within a budget that a laptop can
#: actually pay.
STUDY_BOARDS = [
    # key, board file, measured authored route seconds, authored blocking
    {'key': 'esp_prog', 'file': 'kicad_files/esp_prog.kicad_pcb'},
    {'key': 'splitflap_driver', 'file': 'kicad_files/splitflap_driver.kicad_pcb'},
    {'key': 'watchy', 'file': 'kicad_files/watchy.kicad_pcb'},
    {'key': 'tigard', 'file': 'kicad_files/tigard.kicad_pcb'},
    {'key': 'sonde_u', 'file': 'kicad_files/sonde_u.kicad_pcb'},
    {'key': 'kit-dev-coldfire-xilinx_5213',
     'file': 'kicad_files/kit-dev-coldfire-xilinx_5213.kicad_pcb'},
]

CALIBRATION_CANDIDATES = STUDY_BOARDS + [
    {'key': 'ulx3s', 'file': 'kicad_files/ulx3s.kicad_pcb'},
    {'key': 'glasgow_revC', 'file': 'kicad_files/glasgow_revC.kicad_pcb'},
]

#: K = 20 per board. At K=20 a per-board Spearman resolves |rho| >= 0.44 from
#: zero; at K=12 only |rho| >= 0.6, which is why it is not 12. The split spans
#: the range on purpose -- an all-realistic slate has no headroom and an
#: all-damaged one has no ceiling.
PERTURB_KINDS = ('translate', 'wrong_side', 'swap', 'scatter', 'pile')
#: Doses as a FRACTION of the board diagonal, so a 30 mm board and a 200 mm one
#: receive comparable damage rather than comparable millimetres.
DOSE_FRACTIONS = (0.05, 0.15, 0.30)
PORTFOLIO_INDICES = (1, 2, 3, 4)
#: `swap` is excluded from portfolio strategies: it is barren without resolved
#: blocks, and `perturb.py`'s swap re-picks the pair itself, so the damage end
#: covers it properly.
PORTFOLIO_STRATEGIES = ('jitter', 'poses')


def variant_names():
    """The K=20 variant list, in a fixed order, identical for every board."""
    out = ['authored']
    for k in PERTURB_KINDS:
        for i, _f in enumerate(DOSE_FRACTIONS):
            out.append(f'perturb-{k}-d{i}')
    out += [f'portfolio-{i}' for i in PORTFOLIO_INDICES]
    return out


VARIANTS = variant_names()
K = len(VARIANTS)


# ---------------------------------------------------------------------------
# the frozen argv
# ---------------------------------------------------------------------------

def route_argv_for(board_file, out_board, json_out):
    """The route command for a board, derived from THE BOARD ALONE.

    No variant, no index, no measurement is in scope here, which is what makes
    "identical argv per board" a property of the code rather than a promise.
    Floors are deliberately NOT passed: with no `--clearance` every net routes
    at its own net-class clearance and the writeback preserves it, so all of a
    board's variants are graded on the same terms without this tool inventing
    a number (CLAUDE.md's `--clearance` ceiling rules).
    """
    return [sys.executable, '-X', 'utf8', ROUTE_PY, board_file,
            '--output', out_board, '--json-out', json_out]


def argv_signature(board_file):
    """The hash of the argv SHAPE, with the per-variant paths blanked out."""
    argv = route_argv_for(board_file, '<OUT>', '<JSON>')
    argv = [os.path.basename(a) if a.endswith('.py') else a for a in argv]
    argv[0] = 'python'
    return hashlib.sha256(json.dumps(argv).encode()).hexdigest(), argv


def freeze_argv(board_key, board_file, out_dir):
    """Write (or check) ARGV.json. Refuses a changed argv mid-study."""
    d = os.path.join(out_dir, board_key)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, 'ARGV.json')
    sig, argv = argv_signature(board_file)
    if os.path.isfile(path):
        old = json.load(open(path, encoding='utf-8'))
        if old.get('argv_sha') != sig:
            raise SystemExit(
                f'REFUSING: {board_key} was frozen at argv_sha '
                f'{old.get("argv_sha")[:12]} and this run would use '
                f'{sig[:12]}.\n  frozen: {old.get("argv")}\n  now:    {argv}\n'
                f'Identical argv per board is the study\'s control. Delete '
                f'{path} and re-run the WHOLE board, or keep the argv.')
        return old['argv_sha']
    with open(path, 'w', encoding='utf-8') as f:
        json.dump({'board': board_file, 'argv_sha': sig, 'argv': argv,
                   'frozen_at': 'before any variant was generated'}, f,
                  indent=1)
    return sig


# ---------------------------------------------------------------------------
# variant generation -- returns a PATH and nothing else
# ---------------------------------------------------------------------------

def _board_diagonal(board_file):
    from kicad_parser import parse_kicad_pcb
    pcb = parse_kicad_pcb(board_file)
    b = getattr(pcb.board_info, 'board_bounds', None)
    if not b:
        return 100.0
    import math
    return math.hypot(b[2] - b[0], b[3] - b[1])


def generate_variant(board_file, variant, work_dir, seed=0):
    """Write the variant's board and return (path, recipe, note).

    Returns a PATH. It deliberately returns no state object: the predictor
    function takes a path and nothing else, so there is no way for the
    optimizer's live model to reach the measurement.
    """
    os.makedirs(work_dir, exist_ok=True)
    if variant == 'authored':
        return board_file, {'generator': 'authored'}, ''

    if variant.startswith('perturb-'):
        from placement import perturb as P
        _, kind, dose_tag = variant.split('-', 2)
        di = int(dose_tag[1:])
        frac = DOSE_FRACTIONS[di]
        dose = frac * _board_diagonal(board_file)
        out = os.path.join(work_dir, f'{variant}.kicad_pcb')
        truth = os.path.join(work_dir, '_truth')
        os.makedirs(truth, exist_ok=True)
        rec = P.perturb(board_file, out, kind=kind, dose_mm=dose, seed=seed,
                        write_record=False,
                        control_out=os.path.join(truth, f'{variant}.ctl.kicad_pcb'))
        if rec.get('status') != 'ok':
            return None, {'generator': 'perturb', 'kind': kind,
                          'dose_fraction': frac,
                          'status': rec.get('status')}, rec.get('reason', '')
        recipe = {'generator': 'perturb', 'kind': kind, 'dose_fraction': frac,
                  'dose_mm_requested': rec.get('dose_mm_requested'),
                  'dose_mm_applied': rec.get('dose_mm_applied'),
                  'clipped': rec.get('clipped'),
                  'max_feasible_dose_mm': rec.get('max_feasible_dose_mm'),
                  'seed': seed, 'block': (rec.get('block') or {}).get('name')}
        return out, recipe, ('dose CLIPPED to the feasible travel'
                             if rec.get('clipped') else '')

    if variant.startswith('portfolio-'):
        from placement import portfolio as PF
        i = int(variant.split('-')[1])
        sub = os.path.join(work_dir, f'pf_{i}')
        res = PF.generate(board_file, sub, seed=seed, n_candidates=i + 1,
                          strategies=PORTFOLIO_STRATEGIES, only=i)
        cands = res.get('candidates') or []
        if not cands or not cands[0].board:
            return None, {'generator': 'portfolio_quench', 'only': i,
                          'seed': seed}, (cands[0].note if cands else 'barren')
        c = cands[0]
        return c.board, {'generator': 'portfolio_quench', 'only': i,
                         'seed': seed, 'strategy': c.strategy,
                         'strategies': list(PORTFOLIO_STRATEGIES),
                         'displacement_rms': round(c.displacement_rms, 4)}, ''
    raise ValueError(f'unknown variant {variant!r}')


def poses_sha(board_file):
    """sha256 over the sorted poses, quantised to 1 nm.

    64 bytes that answer "is this the same placement?" without the board. Two
    variants sharing one is a sampler that produced a duplicate, which is
    n-inflation rather than a sample.
    """
    from kicad_parser import parse_kicad_pcb
    pcb = parse_kicad_pcb(board_file)
    rows = sorted(
        f'{r}|{round(f.x, 6):.6f}|{round(f.y, 6):.6f}|'
        f'{round(float(f.rotation or 0.0) % 360, 6):.6f}|{f.layer}'
        for r, f in pcb.footprints.items())
    return hashlib.sha256('\n'.join(rows).encode()).hexdigest()


# ---------------------------------------------------------------------------
# predictors -- from the WRITTEN board, by a second parse
# ---------------------------------------------------------------------------

def predictors_for(board_path):
    """Every pre-route number, re-derived from the board on disk.

    Takes a PATH and nothing else. `PlacementModel(exact=True)` is used at the
    weights `render_placement._build_state` uses -- crossing_penalty 10.0,
    halo_base 0.5, halo_coef 0.25, length_weight 1.0 -- because
    `pose_score.make_state`'s 0.15/30.0/0.3 produce a halo that is NOT
    comparable to the one every recorded handoff carries. That knob trap is
    documented in `tests/stress/calibrate_congestion_ratio.py` and it is the
    reason this does not simply call the cheapest available state builder.

    A predictor that raises is recorded null with its exception. Never 0.
    """
    from kicad_parser import parse_kicad_pcb
    from render_placement import PlacementModel

    pred = {k: None for k in PREDICTOR_KEYS}
    gaps = []
    pcb = parse_kicad_pcb(board_path)
    model = PlacementModel(pcb, board_path, exact=True)
    m = dict(model.metrics or {})
    for k in METRIC_KEYS:
        if k in m:
            pred[k] = m[k]
        else:
            gaps.append(f'metrics.{k}')

    # The legality checklist, from THE SAME model, through the very function
    # `render_placement` uses to build its own `checklist` block -- so a study
    # row and a recorded handoff are the same quantity rather than two
    # implementations of one name.
    from render_placement import legality_findings
    try:
        fnd = legality_findings(model)
    except Exception as e:                                      # noqa: BLE001
        fnd = None
        gaps.append(f'legality:{type(e).__name__}: {e}')
    if isinstance(fnd, dict):
        def _n(key):
            v = fnd.get(key)
            if v is None:
                gaps.append(f'legality.{key}')
                return None
            return len(v) if isinstance(v, (list, tuple)) else v
        pred['pad_copper'] = _n('oob_refs_pad_copper')
        pred['courtyard_off_outline'] = _n('oob_refs_courtyard')
        pred['body_overlap_pairs'] = _n('body_overlap_pairs_refs')
        pred['pad_clearance_pairs'] = _n('pad_conflict_pairs_refs')
        # The run-23 rename: the recorded handoffs carry
        # `b_courtyard_overlap_pairs` and the current engine calls the same
        # channel `courtyard_overlap_pairs_refs`. The harvester normalises the
        # document side under the same canonical name.
        pred['courtyard_advisory_pairs'] = _n('courtyard_overlap_pairs_refs')
        pred['courtyard_blocking_pairs'] = _n('courtyard_blocking_pairs_refs')
        pred['cross_side_stacks'] = _n('cross_side_stacks')
        pred['hole_conflicts'] = _n('hole_conflict_pairs_refs')
        pred['courtyard_overlap_mm2'] = fnd.get('courtyard_overlap_mm2')
        if fnd.get('courtyard_census_error'):
            gaps.append(f'legality.courtyard_census_error='
                        f'{fnd["courtyard_census_error"]}')
    for k, v in list(pred.items()):
        if v is None and f'metrics.{k}' not in gaps:
            gaps.append(f'predictor.{k}')
    return pred, sorted(set(gaps))


# ---------------------------------------------------------------------------
# one task -> one row
# ---------------------------------------------------------------------------

def run_task(board_key, board_file, variant, out_dir, seed=0, timeout=3600):
    work = os.path.join(out_dir, board_key, variant)
    os.makedirs(work, exist_ok=True)
    argv_sha = freeze_argv(board_key, board_file, out_dir)

    row = {
        'schema': SCHEMA, 'kind': 'predictor-row',
        'row_id': f'study:{board_key}:{variant}',
        'source': 'study', 'reproducible': True,
        'board_key': board_key, 'variant': variant, 'generator': None,
        'predictor_source': 'reparse',
        'provenance': {
            'input_board': board_file,
            'input_board_sha': sha256_file(os.path.join(ROOT, board_file))
            if not os.path.isabs(board_file) else sha256_file(board_file),
            'seed': seed, 'k_declared': K,
            'measured_git': git_describe(ROOT),
        },
        'route': {'argv': None, 'argv_sha': argv_sha, 'returncode': None,
                  'seconds': None},
        'predictors': {k: None for k in PREDICTOR_KEYS},
        'truth': {'headline': None, 'blocking': None, 'blocking_by': {},
                  'quality': {}},
        'schema_gaps': [], 'schema_aliases_used': {}, 'notes': [],
    }

    t0 = time.time()
    try:
        path, recipe, note = generate_variant(board_file, variant, work, seed)
    except Exception as e:                                      # noqa: BLE001
        row['notes'].append(f'generate failed: {type(e).__name__}: {e}')
        return row
    row['generator'] = recipe.get('generator')
    row['provenance']['recipe'] = recipe
    if note:
        row['notes'].append(note)
    if not path:
        row['notes'].append('the sampler produced no board for this variant')
        return row
    row['provenance']['variant_board_sha'] = sha256_file(path)
    row['provenance']['poses_sha256'] = poses_sha(path)

    try:
        pred, gaps = predictors_for(path)
    except Exception as e:                                      # noqa: BLE001
        row['notes'].append(f'predictors failed: {type(e).__name__}: {e}')
        return row
    row['predictors'] = pred
    row['schema_gaps'] = gaps

    routed = os.path.join(work, 'routed.kicad_pcb')
    rjson = os.path.join(work, 'route.json')
    argv = route_argv_for(
        path if os.path.isabs(path) else os.path.join(ROOT, path),
        routed, rjson)
    row['route']['argv'] = [os.path.basename(a) if a.endswith('.py') else a
                            for a in argv]
    r0 = time.time()
    try:
        p = subprocess.run(argv, capture_output=True, text=True,
                           encoding='utf-8', errors='replace', cwd=ROOT,
                           timeout=timeout)
        row['route']['returncode'] = p.returncode
    except subprocess.TimeoutExpired:
        row['route']['returncode'] = -9
        row['notes'].append(f'route TIMED OUT after {timeout}s')
        row['route']['seconds'] = round(time.time() - r0, 1)
        return row
    row['route']['seconds'] = round(time.time() - r0, 1)
    if os.path.isfile(rjson):
        try:
            js = json.load(open(rjson, encoding='utf-8'))
            row['route']['min_clearance_used'] = js.get('min_clearance_used')
            row['route']['failed_single'] = len(js.get('failed_single') or [])
            row['route']['open_single'] = len(js.get('open_single') or [])
        except Exception:                                       # noqa: BLE001
            pass
    if not os.path.isfile(routed):
        row['notes'].append('route wrote no board')
        return row

    sjson = os.path.join(work, 'score.json')
    sp = subprocess.run(
        [sys.executable, '-X', 'utf8', BOARD_SCORE, routed, '--json', sjson,
         '--label', row['row_id'], '-q'],
        capture_output=True, text=True, encoding='utf-8', errors='replace',
        cwd=ROOT, timeout=timeout)
    if os.path.isfile(sjson):
        s = json.load(open(sjson, encoding='utf-8'))
        by = s.get('blocking_by') or {}
        q = s.get('quality') or {}
        row['truth'] = {
            'headline': s.get('blocking'), 'blocking': s.get('blocking'),
            'blocking_by': {k: by.get(k) for k in TRUTH_BY_KEYS},
            'quality': {'vias': q.get('vias'), 'copper_mm': q.get('copper_mm'),
                        'segments': q.get('segments')},
            'ungraded': s.get('ungraded'),
            'routed_board_sha': s.get('board_sha'),
        }
        if s.get('blocking') is None:
            # board_score's vacuity rule. None is "a component that was asked
            # for could not run", never zero.
            row['notes'].append(
                f'blocking is None (ungraded: {s.get("ungraded")}) -- this row '
                f'is excluded from every statistic, not counted as 0')
    else:
        row['notes'].append(f'board_score wrote no json (exit {sp.returncode})')
    row['provenance']['total_seconds'] = round(time.time() - t0, 1)
    return row


# ---------------------------------------------------------------------------
# aggregation
# ---------------------------------------------------------------------------

DEPENDENTS = (('blocking', 'headline'), ('vias', None))


def _truth_col(row, dep):
    if dep == 'headline':
        return row['truth'].get('headline')
    return (row['truth'].get('quality') or {}).get('vias')


def aggregate(rows, include_quench=True):
    """Per-board rho for every predictor, then a sign test across boards."""
    rows = [r for r in rows if r.get('source') == 'study']
    if not include_quench:
        rows = [r for r in rows if r.get('generator') != 'portfolio_quench']
    groups = rs.per_board(rows)

    # The frozen-argv control, checked on the DATA and not only at write time.
    argv_bad = {b: sorted({r['route'].get('argv_sha') for r in rr})
                for b, rr in groups.items()
                if len({r['route'].get('argv_sha') for r in rr}) > 1}

    out = {'boards': {}, 'predictors': {}, 'argv_disagreement': argv_bad,
           'include_quench': include_quench}
    for b, rr in sorted(groups.items()):
        out['boards'][b] = {
            'k': len(rr),
            'classification': rs.classify_board(rr, 'headline'),
            'blocking_values': sorted(
                {r['truth'].get('headline') for r in rr
                 if r['truth'].get('headline') is not None}),
            'excluded': [r['variant'] for r in rr
                         if r['truth'].get('headline') is None],
        }
    for name, dep_key in DEPENDENTS:
        per_pred = {}
        for pred in PREDICTOR_KEYS:
            by_board = {}
            for b, rr in sorted(groups.items()):
                if dep_key == 'headline':
                    br = rs.board_rho(rr, pred, 'headline')
                else:
                    sub = [{'board_key': b, 'predictors': r['predictors'],
                            'truth': {'v': _truth_col(r, 'vias')}} for r in rr]
                    br = rs.board_rho(sub, pred, 'v')
                by_board[b] = br
            per_pred[pred] = {
                'by_board': {b: br.as_dict() for b, br in by_board.items()},
                'sign_test': rs.sign_test(by_board),
            }
        out['predictors'][name] = per_pred
    return out


def report(agg, dep='blocking', top=None):
    lines = []
    lines.append('=' * 78)
    lines.append(f'PREDICTORS vs {dep.upper()} -- Spearman WITHIN each board, '
                 f'never pooled')
    lines.append('=' * 78)
    if agg['argv_disagreement']:
        lines.append('REFUSED -- these boards carry more than one argv_sha, so '
                     'their variants were not routed on the same terms:')
        for b, shas in agg['argv_disagreement'].items():
            lines.append(f'  {b}: {shas}')
        return lines
    lines.append(f"portfolio_quench rows "
                 f"{'INCLUDED' if agg['include_quench'] else 'EXCLUDED'}")
    lines.append('')
    lines.append('boards:')
    for b, info in agg['boards'].items():
        lines.append(f"  {b:30s} K={info['k']:<3d} {info['classification']:11s} "
                     f"blocking values {info['blocking_values'][:6]}"
                     + (f"  ({len(info['excluded'])} row(s) excluded: "
                        f"{', '.join(info['excluded'][:4])})"
                        if info['excluded'] else ''))
    lines.append('')
    preds = agg['predictors'][dep]
    order = sorted(preds, key=lambda k: -abs(
        preds[k]['sign_test'].get('median_rho') or 0.0))
    if top:
        order = order[:top]
    for pred in order:
        st = preds[pred]['sign_test']
        lines.append(f'  {pred}')
        for b, d in preds[pred]['by_board'].items():
            lines.append(f'      {b:30s} {d["display"]}')
        lines += ['    ' + ln for ln in rs.format_sign_test('across boards', st)]
        lines.append('')
    return lines


def shuffle_control(rows, n=200, seed=12345):
    """How often does a predictor with NO signal pass our own acceptance rule?

    Truth is permuted WITHIN each board, so board size, K and the predictor
    columns are all preserved and only the pairing is destroyed. If a shuffled
    run passes the >= N-1 sign rule at any appreciable rate, the rule is not
    honest and no amount of prose fixes that. Free over --from-rows.
    """
    import random
    rows = [r for r in rows if r.get('source') == 'study']
    groups = rs.per_board(rows)
    rng = random.Random(seed)
    passes = {p: 0 for p in PREDICTOR_KEYS}
    trials = 0
    for _ in range(n):
        shuffled = {}
        for b, rr in groups.items():
            truths = [r['truth'].get('headline') for r in rr]
            rng.shuffle(truths)
            shuffled[b] = [
                {'board_key': b, 'predictors': r['predictors'],
                 'truth': {'headline': t}} for r, t in zip(rr, truths)]
        trials += 1
        for p in PREDICTOR_KEYS:
            by_board = {b: rs.board_rho(rr, p, 'headline')
                        for b, rr in shuffled.items()}
            if rs.sign_test(by_board)['passes_sign_rule']:
                passes[p] += 1
    return {'trials': trials,
            'rate': {p: round(c / trials, 4) for p, c in sorted(passes.items())}}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def load_rows(path):
    d = json.load(open(path, encoding='utf-8'))
    return d.get('rows') or []


def read_jsonl(path):
    out = []
    if os.path.isfile(path):
        with open(path, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--out', default=os.path.join(ROOT, 'wk', '703'),
                    help='work dir (gitignored)')
    ap.add_argument('--boards', nargs='+', default=None,
                    help='NARROW the study to these board keys (never widen)')
    ap.add_argument('--variants', nargs='+', default=None)
    ap.add_argument('-k', type=int, default=None,
                    help='first K variants only (a smoke run, not a study)')
    ap.add_argument('-j', type=int, default=1, help='parallel tasks')
    ap.add_argument('--seed', type=int, default=0)
    ap.add_argument('--route-timeout', type=int, default=3600)
    ap.add_argument('--task', default=None, metavar='BOARD:VARIANT',
                    help='run ONE task and print its row as json')
    ap.add_argument('--plan', action='store_true')
    ap.add_argument('--calibrate', action='store_true')
    ap.add_argument('--from-rows', default=None, metavar='ROWS_JSON')
    ap.add_argument('--shuffle-control', type=int, default=0, metavar='N')
    ap.add_argument('--verify-row', default=None, metavar='BOARD:VARIANT')
    ap.add_argument('--append', default=None, metavar='ROWS_JSON',
                    help='merge the run\'s rows into this committed file')
    ap.add_argument('--list', action='store_true')
    a = ap.parse_args(argv)

    rs._self_test()

    if a.list:
        for v in VARIANTS:
            print(v)
        return 0

    if a.plan:
        print(f'{len(STUDY_BOARDS)} boards x K={K} = '
              f'{len(STUDY_BOARDS) * K} routes')
        print(f'variants (identical for every board): {", ".join(VARIANTS)}')
        cal = os.path.join(a.out, 'calibration.json')
        if os.path.isfile(cal):
            c = json.load(open(cal, encoding='utf-8'))
            tot = 0.0
            for b in STUDY_BOARDS:
                s = (c.get(b['key']) or {}).get('seconds')
                print(f"  {b['key']:30s} {s if s is not None else '?':>8} s "
                      f"authored -> {K} routes ~ "
                      f"{(s * K / 60.0) if s else float('nan'):.1f} min")
                tot += (s or 0) * K
            print(f'  TOTAL ~ {tot / 3600.0:.1f} h serial, '
                  f'~{tot / 3600.0 / 4:.1f} h at -j 4  (from MEASURED '
                  f'authored routes; damaged boards route slower)')
        else:
            print(f'  no calibration at {cal} -- run --calibrate first. This '
                  f'tool does not estimate a runtime it has not measured.')
        return 0

    if a.from_rows:
        rows = load_rows(a.from_rows)
        study = [r for r in rows if r.get('source') == 'study']
        if not study:
            print(f'{a.from_rows} carries no study rows (only '
                  f'{len(rows)} harvest row(s)). The harvest is one placement '
                  f'per board, which is n=1 per board: no correlation is '
                  f'computable from it, and that is the finding.')
            return 0
        for inc in (True, False):
            agg = aggregate(study, include_quench=inc)
            for dep, _ in DEPENDENTS:
                print('\n'.join(report(agg, dep)))
        if a.shuffle_control:
            sc = shuffle_control(study, a.shuffle_control)
            print(f'\nSHUFFLE CONTROL -- truth permuted WITHIN each board, '
                  f'{sc["trials"]} trials')
            print('  how often a predictor with NO signal passes our own '
                  'sign rule:')
            for p, r in sorted(sc['rate'].items(), key=lambda kv: -kv[1])[:10]:
                print(f'    {p:28s} {r:6.1%}')
        return 0

    if a.calibrate:
        os.makedirs(a.out, exist_ok=True)
        cal_path = os.path.join(a.out, 'calibration.json')
        cal = json.load(open(cal_path, encoding='utf-8')) if os.path.isfile(
            cal_path) else {}
        for b in CALIBRATION_CANDIDATES:
            if a.boards and b['key'] not in a.boards:
                continue
            row = run_task(b['key'], b['file'], 'authored',
                           os.path.join(a.out, 'cal'), a.seed, a.route_timeout)
            cal[b['key']] = {
                'seconds': row['route'].get('seconds'),
                'blocking': row['truth'].get('headline'),
                'vias': (row['truth'].get('quality') or {}).get('vias'),
                'returncode': row['route'].get('returncode'),
            }
            print(f"  {b['key']:30s} {cal[b['key']]}")
            with open(cal_path, 'w', encoding='utf-8') as f:
                json.dump(cal, f, indent=1, sort_keys=True)
        print(f'wrote {cal_path}')
        return 0

    if a.task:
        bk, _, variant = a.task.partition(':')
        board = next((b for b in CALIBRATION_CANDIDATES if b['key'] == bk), None)
        if board is None:
            print(f'no such board: {bk}', file=sys.stderr)
            return 2
        if variant not in VARIANTS:
            print(f'no such variant: {variant}', file=sys.stderr)
            return 2
        row = run_task(bk, board['file'], variant, a.out, a.seed,
                       a.route_timeout)
        print('ROW_JSON=' + json.dumps(row))
        return 0

    # --- the study itself -------------------------------------------------
    boards = STUDY_BOARDS
    if a.boards:
        unknown = [b for b in a.boards
                   if b not in {x['key'] for x in CALIBRATION_CANDIDATES}]
        if unknown:
            print(f'no such board(s): {unknown}; try --plan', file=sys.stderr)
            return 2
        boards = [b for b in CALIBRATION_CANDIDATES if b['key'] in a.boards]
    variants = a.variants or VARIANTS
    if a.k:
        variants = variants[:a.k]

    os.makedirs(a.out, exist_ok=True)
    jsonl = os.path.join(a.out, 'rows.jsonl')
    done = {r['row_id'] for r in read_jsonl(jsonl)}
    tasks = [(b['key'], b['file'], v) for b in boards for v in variants
             if f'study:{b["key"]}:{v}' not in done]
    print(f'{len(tasks)} task(s) to run ({len(done)} already in {jsonl})')
    if a.k or a.boards or a.variants:
        print('NOTE: this is a NARROWED run. A verdict requires the whole '
              'declared table.')

    def _one(t):
        bk, bf, v = t
        p = subprocess.run(
            [sys.executable, '-X', 'utf8', os.path.abspath(__file__),
             '--task', f'{bk}:{v}', '--out', a.out, '--seed', str(a.seed),
             '--route-timeout', str(a.route_timeout)],
            capture_output=True, text=True, encoding='utf-8', errors='replace',
            cwd=ROOT)
        for line in (p.stdout or '').splitlines():
            if line.startswith('ROW_JSON='):
                return json.loads(line[len('ROW_JSON='):])
        return {'row_id': f'study:{bk}:{v}', 'source': 'study',
                'board_key': bk, 'variant': v,
                'notes': [f'task subprocess exit {p.returncode}: '
                          f'{(p.stderr or "")[-300:]}'],
                'predictors': {k: None for k in PREDICTOR_KEYS},
                'truth': {'headline': None}, 'route': {}}

    n_done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, a.j)) as ex:
        for row in ex.map(_one, tasks):
            with open(jsonl, 'a', encoding='utf-8') as f:
                f.write(json.dumps(row) + '\n')
            n_done += 1
            print(f'  PROGRESS {n_done}/{len(tasks)}  {row["row_id"]}  '
                  f'blocking={row["truth"].get("headline")}  '
                  f'{row.get("route", {}).get("seconds")}s')

    rows = read_jsonl(jsonl)
    if a.append:
        doc = (json.load(open(a.append, encoding='utf-8'))
               if os.path.isfile(a.append)
               else {'schema': SCHEMA, 'kind': 'predictor-rows', 'rows': [],
                     'refusals': []})
        keep = [r for r in doc['rows'] if r.get('source') != 'study']
        doc['rows'] = sorted(keep + rows, key=lambda r: r['row_id'])
        doc['predictor_keys'] = list(PREDICTOR_KEYS)
        doc['study_boards'] = [b['key'] for b in STUDY_BOARDS]
        doc['study_variants'] = VARIANTS
        with open(a.append, 'w', encoding='utf-8') as f:
            json.dump(doc, f, indent=1, sort_keys=True)
            f.write('\n')
        print(f'merged {len(rows)} study row(s) into {a.append}')
    print('ALL DONE')
    return 0


if __name__ == '__main__':
    sys.exit(main())
