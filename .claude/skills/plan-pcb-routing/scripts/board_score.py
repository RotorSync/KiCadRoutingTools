#!/usr/bin/env python3
"""One authoritative score for a board, so a loop can tell better from worse.

Why this exists
---------------
`place_route_loop.better()` compares `failures` and `iterations`, and BOTH come
out of route.py's own ``JSON_SUMMARY``. It never runs a checker. CLAUDE.md warns
about exactly this -- *"Routers can report false success. A router's own 'routed'
tally may come from a local/heuristic proxy while pads stay disconnected"* -- so a
round can be reported ACCEPTED while pads sit disconnected and DRC is dirty. That
is how a board went out at 39/44 nets with 762 DRC errors and 141/141 vias below
its own spec.

This script is the second opinion. It answers one question -- *is this board
better than that board?* -- with a number that cannot be produced by the thing
being graded.

It reimplements NO checking. Every component shells out to the real CLI, which is
deliberate and not laziness: `check_drc.main()` resolves the grading clearance
from the sibling ``.kicad_pro``, installs the per-layer ``.kicad_dru`` rules,
builds the per-netclass clearance map and derives the edge / hole-to-hole floors
(check_drc.py:2968-3045). Reimplementing that resolution here would drift, and a
grader that ignores the dru "will manufacture phantom flags on relaxed layers and
miss real ones on tightened layers" (CLAUDE.md). Subprocessing keeps the score
BY CONSTRUCTION equal to what `check_drc.py` itself reports.

The score
---------
Lexicographic, never a weighted sum -- a weighted sum lets a router buy off a
disconnected net with a lower via count::

    score = (blocking, quality)
    blocking = unrouted + broken + drc + undersized + floorplan + impedance + length
    quality  = (vias, copper_mm, segments)      # only compared once blocking == 0

`blocking` must reach 0 before a board is deliverable. `quality` orders the
boards that already got there.

Vacuity
-------
"0 violations" and "0 rules ran" are different answers, and a loop that cannot
tell them apart will happily converge on a board nothing graded. Every component
reports `ran: true|false` plus a `reason` when it did not, and `blocking` is
`None` -- not 0 -- when a component that was asked for could not run.

Exit codes (deliberately the same dialect as check_floorplan.py)
    0  graded, blocking == 0
    1  crash
    2  bad arguments
    3  board state (missing file, unparseable board)
    4  graded, blocking > 0
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile

# Violation types that mean "this copper is below the fab/spec floor" rather than
# "this copper is too close to that copper". check_drc emits both from one run;
# the split matters because they take different levers -- a size violation is a
# re-route at a different width/via, a clearance violation is a routing conflict.
# Source: check_drc.py's `by_type` grouping (:2801) over the 'type' key.
SIZE_TYPES = frozenset({'track-width', 'via-size', 'via-drill-size'})

_DRC_TOTAL = re.compile(r'^FOUND (\d+) DRC VIOLATIONS', re.M)
_DRC_TYPE = re.compile(r'^([A-Z0-9-]+) violations \((\d+)\):', re.M)
_CONN_TOTAL = re.compile(r'^FOUND (\d+) ISSUES', re.M)
_CONN_UNROUTED = re.compile(r'^\s+Unrouted nets \((\d+)\):', re.M)
_CONN_BROKEN = re.compile(r'^\s+Connectivity issues \((\d+)\):', re.M)


def krt_dir() -> str:
    """The KiCadRoutingTools clone whose checkers we grade with.

    $PCB_KICADROUTINGTOOLS wins so the skill works against an outside repo; the
    walk up from this file covers the in-repo case. Raise rather than fall back:
    scoring with a different clone's checkers would describe the wrong engine.
    """
    env = os.environ.get('PCB_KICADROUTINGTOOLS', '').strip().strip('"')
    if env:
        if not os.path.isfile(os.path.join(env, 'check_drc.py')):
            raise SystemExit(f"PCB_KICADROUTINGTOOLS={env!r} has no check_drc.py "
                             f"-- not a KiCadRoutingTools clone")
        return env
    # <krt>/.claude/skills/plan-pcb-routing/scripts/board_score.py -> four up
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(here))))
    if os.path.isfile(os.path.join(root, 'check_drc.py')):
        return root
    raise SystemExit(
        "Cannot locate KiCadRoutingTools. Set PCB_KICADROUTINGTOOLS to the clone, "
        "or run this script from inside one.")


def run_tool(root: str, tool: str, *args) -> tuple:
    """(returncode, combined output). -X utf8 for the Ω/µ the tools print."""
    cmd = [sys.executable, '-X', 'utf8', os.path.join(root, tool)] + [str(a) for a in args]
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       encoding='utf-8', errors='replace')
    return p.returncode, p.stdout


def skipped(reason: str) -> dict:
    """A component that did not run. `count` is None, never 0 (see Vacuity)."""
    return {'ran': False, 'reason': reason, 'count': None}


def score_connectivity(root: str, board: str) -> dict:
    """Unrouted and broken nets, from check_connected.py.

    This is the authoritative, zone/fill-aware answer -- it reconciles
    pour-backed nets against KiCad's exact fill in both directions, which the
    router's own tally does not do.
    """
    rc, out = run_tool(root, 'check_connected.py', board)
    if 'ALL NETS FULLY CONNECTED' in out:
        return {'ran': True, 'count': 0, 'unrouted': 0, 'broken': 0, 'nets': []}
    m = _CONN_TOTAL.search(out)
    if not m:
        return skipped(f'check_connected.py produced no summary (rc={rc})')
    unrouted = int(u.group(1)) if (u := _CONN_UNROUTED.search(out)) else 0
    broken = int(b.group(1)) if (b := _CONN_BROKEN.search(out)) else 0
    # Net names, so the ledger can say WHICH nets failed and a later round can
    # tell "the same nets every time" (parameters) from "different nets"
    # (congestion) -- the Step 9 classification needs this distinction.
    nets = re.findall(r'^\s{4}(\S+) \(\d+ pads\)', out, re.M)
    nets += re.findall(r'^\s{2}(\S+) \(net \d+\):', out, re.M)
    return {'ran': True, 'count': int(m.group(1)), 'unrouted': unrouted,
            'broken': broken, 'nets': sorted(set(nets))}


def score_drc(root: str, board: str, clearance=None, sizes=None) -> tuple:
    """(drc, undersized) -- clearance violations and sub-floor copper.

    Both come from ONE check_drc run, split on the violation type. Omitting
    --clearance is the norm and not an oversight: check_drc then reads the
    sibling .kicad_pro Default class, which is the floor the board was actually
    routed to. Passing a guessed round number manufactures phantom violations
    on legitimately tight copper (CLAUDE.md).

    `sizes` is the opposite case, and it is the one that shipped a broken board.
    check_drc defaults its size floors to the FAB minimum derived from the layer
    count -- correct when the fab is the only constraint, and blind when the
    board's own spec is TIGHTER. test-board's HW-TB-PCB08 asks for 0.6 mm vias
    with a 0.15 mm annular ring; every one of its 141 vias violated that, and
    nothing caught it, because 0.25 mm clears the 2-layer fab floor. Pass the
    spec's numbers whenever the spec has any.
    """
    args = [board]
    if clearance is not None:
        args += ['-c', str(clearance)]
    for flag, val in (sizes or {}).items():
        if val is not None:
            args += [flag, str(val)]
    # --max-print 0 prints every violation of every type, so the per-type header
    # counts are complete rather than capped at the default 20.
    args += ['--max-print', '0']
    rc, out = run_tool(root, 'check_drc.py', *args)
    if 'NO DRC VIOLATIONS FOUND' in out:
        return ({'ran': True, 'count': 0, 'by_type': {}, 'graded_at': _graded_at(out)},
                {'ran': True, 'count': 0, 'by_type': {}})
    if not _DRC_TOTAL.search(out):
        r = skipped(f'check_drc.py produced no summary (rc={rc})')
        return r, dict(r)
    by_type = {t.lower(): int(n) for t, n in _DRC_TYPE.findall(out)}
    size = {t: n for t, n in by_type.items() if t in SIZE_TYPES}
    clear = {t: n for t, n in by_type.items() if t not in SIZE_TYPES}
    return ({'ran': True, 'count': sum(clear.values()), 'by_type': clear,
             'graded_at': _graded_at(out)},
            {'ran': True, 'count': sum(size.values()), 'by_type': size})


def _graded_at(out: str):
    """The clearance check_drc actually graded at -- quote it, never assume it."""
    m = re.search(r'Grading at clearance ([\d.]+) mm', out)
    return float(m.group(1)) if m else None


def score_floorplan(root: str, board: str, intent: str, tmp: str) -> dict:
    """check_floorplan --intent. Exit 4 = graded and violated.

    Carries rules_run/rules_skipped through, because an intent that resolves to
    zero rules grades clean by vacuity -- and a typo'd block is exactly the
    failure the grader exists to catch.
    """
    if not intent:
        return skipped('no --intent given; the floorplan is ungraded')
    if not os.path.isfile(intent):
        return skipped(f'intent file not found: {intent}')
    out_json = os.path.join(tmp, 'floorplan.json')
    rc, out = run_tool(root, 'check_floorplan.py', board, '--intent', intent,
                       '--json', out_json, '-q')
    if rc in (2, 3) or not os.path.isfile(out_json):
        return skipped(f'check_floorplan.py exited {rc}: {out.strip()[-200:]}')
    with open(out_json, encoding='utf-8') as f:
        d = json.load(f)
    return {'ran': True, 'count': len(d.get('violations') or []),
            'rules_run': len(d.get('rules_run') or []),
            'rules_skipped': list((d.get('rules_skipped') or {}).keys()),
            'violations': [v.get('rule') for v in (d.get('violations') or [])]}


def score_impedance(root: str, board: str, nets, tmp: str) -> dict:
    """check_impedance -- reference-plane continuity and declared-gap audit."""
    if not nets:
        return skipped('no --impedance-nets given; impedance is ungraded')
    out_json = os.path.join(tmp, 'impedance.json')
    rc, out = run_tool(root, 'check_impedance.py', board, '--nets', *nets,
                       '--json', out_json)
    if not os.path.isfile(out_json):
        return skipped(f'check_impedance.py exited {rc}: {out.strip()[-200:]}')
    with open(out_json, encoding='utf-8') as f:
        d = json.load(f)
    tot = d.get('totals') or {}
    if not tot.get('nets_analyzed'):
        # Asked for, but nothing matched -- that is a FINDING (the globs are
        # wrong, or the nets are unrouted), not a pass.
        return skipped('no routed nets matched --impedance-nets')
    return {'ran': True, 'count': int(tot.get('nets_with_crossing') or 0),
            'nets_analyzed': tot.get('nets_analyzed'),
            'crossings': tot.get('crossings'),
            'segments_over_void': tot.get('segments_over_void')}


def score_length(board: str, groups_file: str) -> dict:
    """Length-match spread per declared group, via net_queries (no new geometry).

    `--length-groups` is a JSON file::

        {"BYTE0": {"nets": ["/DQ0", "/DQ1"], "tolerance_mm": 0.1},
         "USB":   {"nets": ["/USB_DP", "/USB_DM"], "tolerance_mm": 0.05,
                   "mode": "pin_pair"}}

    `mode: "pin_pair"` measures the driver->receiver PATH instead of total net
    copper. Use it for any multipoint or stubbed net: total copper sums every
    branch and matches no real signal path.
    """
    if not groups_file:
        return skipped('no --length-groups given; length matching is ungraded')
    if not os.path.isfile(groups_file):
        return skipped(f'length-groups file not found: {groups_file}')
    from kicad_parser import parse_kicad_pcb
    from net_queries import net_copper_lengths, pin_pair_path_length

    with open(groups_file, encoding='utf-8') as f:
        groups = json.load(f)
    pcb = parse_kicad_pcb(board)
    by_name = {n.name: nid for nid, n in pcb.nets.items()}
    failures, detail = 0, {}
    for gname, spec in groups.items():
        names = [n for n in spec.get('nets', [])]
        tol = float(spec.get('tolerance_mm', 0.1))
        ids = [by_name[n] for n in names if n in by_name]
        missing = [n for n in names if n not in by_name]
        if len(ids) < 2:
            detail[gname] = {'skipped': f'fewer than 2 of its nets exist on the '
                                        f'board (missing: {missing})'}
            failures += 1          # a group naming nets that do not exist is a
            continue               # finding about the spec, not a pass
        if spec.get('mode') == 'pin_pair':
            lengths = {}
            for n in names:
                if n not in by_name:
                    continue
                pads = pcb.pads_by_net.get(by_name[n]) or []
                L = (pin_pair_path_length(pcb, by_name[n], pads[0], pads[1])
                     if len(pads) >= 2 else None)
                if L is not None:
                    lengths[n] = L
            if len(lengths) < 2:
                detail[gname] = {'skipped': 'no track path between the pad pair '
                                            '(plane-only or broken)'}
                failures += 1
                continue
        else:
            got = net_copper_lengths(pcb, ids)
            lengths = {n: got[by_name[n]] for n in names if n in by_name}
        spread = max(lengths.values()) - min(lengths.values())
        ok = spread <= tol
        failures += 0 if ok else 1
        detail[gname] = {'spread_mm': round(spread, 4), 'tolerance_mm': tol,
                         'pass': ok, 'missing_nets': missing,
                         'worst': max(lengths, key=lengths.get)}
    return {'ran': True, 'count': failures, 'groups': detail}


def quality(board: str) -> dict:
    """Tie-breakers, compared ONLY once blocking == 0. Never a blocker itself:
    a board is not worse for having more copper if the alternative is a
    disconnected net."""
    try:
        from kicad_parser import parse_kicad_pcb
        import math
        pcb = parse_kicad_pcb(board)
        mm = sum(math.dist((s.start_x, s.start_y), (s.end_x, s.end_y))
                 for s in pcb.segments)
        return {'vias': len(pcb.vias), 'copper_mm': round(mm, 2),
                'segments': len(pcb.segments)}
    except Exception as e:
        return {'error': str(e)}


def build_parser():
    p = argparse.ArgumentParser(
        description='One authoritative (blocking, quality) score for a board',
        epilog='Exit 0 = blocking is 0; 4 = graded with blockers; '
               '3 = board state; 2 = bad arguments; 1 = crash.')
    p.add_argument('board', help='the .kicad_pcb to score')
    p.add_argument('--intent', help='floorplan intent JSON (check_floorplan '
                                    '--intent). Omitted = floorplan ungraded')
    p.add_argument('--clearance', type=float,
                   help='grade DRC at this clearance. OMIT IT unless you know '
                        'better than the board: check_drc then reads the '
                        'sibling .kicad_pro, which is the floor the board was '
                        'actually routed to')
    g = p.add_argument_group(
        'spec size floors',
        "check_drc defaults these to the FAB minimum for the layer count. Pass "
        "the board's own spec whenever it is TIGHTER than the fab -- that gap is "
        "how 141 spec-violating vias graded clean on test-board.")
    g.add_argument('--min-track-width', type=float, metavar='MM')
    g.add_argument('--min-via-diameter', type=float, metavar='MM')
    g.add_argument('--min-via-drill', type=float, metavar='MM')
    g.add_argument('--size-margin', type=float, metavar='MM',
                   help='absolute tolerance on the size checks (default: exact floor)')
    p.add_argument('--impedance-nets', nargs='+', metavar='GLOB',
                   help='route.py --nets glob syntax; enables the impedance '
                        'component')
    p.add_argument('--length-groups', metavar='JSON',
                   help='{"group": {"nets": [...], "tolerance_mm": 0.1, '
                        '"mode": "pin_pair"}} -- enables the length component')
    p.add_argument('--json', metavar='PATH', help='write the full score here')
    p.add_argument('--label', default='', help='free text carried into the JSON '
                                               '(the ledger uses it for the lever)')
    p.add_argument('--quiet', '-q', action='store_true',
                   help='print only SCORE_JSON= and the one-line summary')
    return p


def main():
    args = build_parser().parse_args()
    if not os.path.isfile(args.board):
        print(f"board not found: {args.board}", file=sys.stderr)
        return 3
    root = krt_dir()
    if root not in sys.path:
        sys.path.insert(0, root)
    sizes = {'--min-track-width': args.min_track_width,
             '--min-via-diameter': args.min_via_diameter,
             '--min-via-drill': args.min_via_drill,
             '--size-margin': args.size_margin}
    # A real temp dir, NOT a dotfile beside the board: these are intermediate
    # JSONs nobody reads twice, and scoring a board must leave nothing behind in
    # the user's project. `--json` is the copy you keep.
    with tempfile.TemporaryDirectory(prefix='board_score_') as tmp:
        conn = score_connectivity(root, args.board)
        drc, undersized = score_drc(root, args.board, args.clearance, sizes)
        floorplan = score_floorplan(root, args.board, args.intent, tmp)
        imped = score_impedance(root, args.board, args.impedance_nets, tmp)
        length = score_length(args.board, args.length_groups)

    parts = {'unrouted': {'ran': conn['ran'], 'count': conn.get('unrouted')},
             'broken': {'ran': conn['ran'], 'count': conn.get('broken')},
             'drc': drc, 'undersized': undersized, 'floorplan': floorplan,
             'impedance': imped, 'length': length}

    # A component that was ASKED for and could not run leaves blocking unknown.
    # Reporting 0 there would let the loop stop on a board nothing graded.
    counts = [v.get('count') for v in parts.values()]
    unknown = [k for k, v in parts.items()
               if v.get('count') is None and v.get('ran') is not False]
    blocking = None if unknown else sum(c for c in counts if c)

    score = {'schema': 1, 'kind': 'board-score', 'board': os.path.abspath(args.board),
             'label': args.label, 'blocking': blocking,
             'blocking_by': {k: v.get('count') for k, v in parts.items()},
             'ungraded': sorted(k for k, v in parts.items() if v.get('ran') is False),
             'unknown': sorted(unknown), 'quality': quality(args.board),
             'components': parts, 'connectivity_nets': conn.get('nets', [])}

    if args.json:
        with open(args.json, 'w', encoding='utf-8') as f:
            json.dump(score, f, indent=2, sort_keys=True)

    print('SCORE_JSON=' + json.dumps(score, sort_keys=True, separators=(',', ':')))
    bits = ' '.join(f'{k}={v}' for k, v in score['blocking_by'].items()
                    if v is not None)
    q = score['quality']
    print(f"BLOCKING={blocking}  ({bits})  "
          f"vias={q.get('vias')} copper_mm={q.get('copper_mm')}")
    if score['ungraded']:
        # Loud, because this is the difference between "clean" and "unexamined".
        print(f"UNGRADED (not scored, not passed): {', '.join(score['ungraded'])}")
    if unknown:
        print(f"UNKNOWN (asked for, could not run): {', '.join(unknown)}")
        return 4
    return 0 if blocking == 0 else 4


if __name__ == '__main__':
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:                                   # noqa: BLE001
        print(f"board_score crashed: {exc}", file=sys.stderr)
        sys.exit(1)
