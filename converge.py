#!/usr/bin/env python3
"""Surgical convergence: rank a move before paying for it, and step back cheaply.

A convergence run is expensive in exactly one way -- routing -- and a loop that
re-runs a full chain for every candidate spends its whole budget discovering
things a millisecond of arithmetic already knew. Measured on one board: eleven
full-chain iterations bought about eight useful moves, and the run stopped with
five nets carrying no copper.

So: a ladder, cheapest evidence first, stopping as soon as a tier discriminates.

    tier 1  legality            QuenchState.candidate_valid      ms
    tier 2  placement cost      QuenchState.total_cost           ms
    tier 3  scoped route        route.py --nets <affected>       seconds
    tier 4  full chain          the caller's own                 minutes

Tiers 1 and 2 live in pose_score.py. This module adds tier 3 -- routing only the
nets a move can affect -- and the bookkeeping that makes a step back a checkout
rather than a reconstruction.

VERBS

    converge.py poses BOARD --ref U3 [--route] [--affected NET ...]
        Rank the part's candidate poses. With --route, also run tier 3 on the
        top few and report what actually happened to the copper.

    converge.py where BOARD --nets NET ...
        What is unconnected, where the gap is, and which foreign copper is
        walling it in -- via net_forensics, which already answers this and which
        nothing in the usual chain calls.

    converge.py record --ledger L --board B --kind completion --argv ...
        Store a board by content and record what produced it.

    converge.py step-back --ledger L [--to SHA|--iteration N] --out BOARD
        Check out an earlier board. Exact, because it is addressed by content.

    converge.py replay --ledger L --iteration N
        Re-run that iteration's lever verbatim. An entry that recorded only
        prose refuses, loudly.

    converge.py status --ledger L
        Iterations spent, split completion vs systemic. A budget going to the
        instrument rather than the board is the failure this makes visible.
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.abspath(__file__))
_ROUTE_PY = os.path.join(ROOT, 'route.py')


# --------------------------------------------------------------------- tier 3

def scoped_route(board, nets, out=None, extra_args=(), timeout=None):
    """Route ONLY `nets`, and return the merged summary. Seconds, not minutes.

    This is the tier that actually discriminates: placement cost says a pose
    looks better, and only a route says the copper agrees. Scoping it to the
    affected nets is what makes it affordable enough to run per candidate.
    """
    tmp = tempfile.mkdtemp(prefix='converge_t3_')
    out = out or os.path.join(tmp, 'routed.kicad_pcb')
    js = os.path.join(tmp, 'route.json')
    argv = [sys.executable, '-X', 'utf8', _ROUTE_PY, board, out,
            '--nets'] + list(nets) + ['--json-out', js] + list(extra_args)
    r = subprocess.run(argv, capture_output=True, text=True, encoding='utf-8',
                       errors='replace', timeout=timeout, cwd=ROOT)
    summary = {}
    if os.path.isfile(js):
        with open(js, encoding='utf-8') as f:
            summary = json.load(f)
    return {'argv': argv, 'returncode': r.returncode, 'board': out,
            'json': js, 'summary': summary, 'stdout_tail': r.stdout[-1500:]}


def route_verdict(summary):
    """(failures, note) from a route summary -- the tier-3 comparison key."""
    if not summary:
        return None, 'no summary'
    failed = list(summary.get('failed_single') or [])
    fm = [d.get('net_name') if isinstance(d, dict) else d
          for d in (summary.get('failed_multipoint') or [])]
    deficit = (summary.get('multipoint_pads_total', 0)
               - summary.get('multipoint_pads_connected', 0))
    n = len(failed) + max(0, deficit)
    parts = []
    if failed or fm:
        parts.append('failed: ' + ', '.join(sorted(set(failed + fm))[:6]))
    if deficit:
        parts.append(f'{deficit} pad(s) short')
    prot = summary.get('protected_skipped')
    if prot:
        # Surfaced because a caller following the router's own retry hint would
        # otherwise loop: 'locked' has no override, ever.
        flat = {k: v for ctx in prot.values() for k, v in ctx.items()}
        parts.append('refused rips: ' + ', '.join(
            f'{k}({v})' for k, v in sorted(flat.items())[:4]))
    return n, '; '.join(parts) or 'clean'


# ------------------------------------------------------------- rip invariants

def check_rip_invariants(nets, rip_set, power_nets=(), impedance_nets=()):
    """Complaints about a proposed rip. Empty list means it is safe to run.

    Four rules, each of which cost a wasted iteration to learn:

    1. A ripped net is re-routed at the CALLING command's parameters, not the
       ones it was originally routed with. Ripping a width-bearing net without
       carrying its width brings it back at the signal default and silently
       destroys a spec geometry.
    2. One net per call. Two together let the second rip the first, reported as
       "1/2 routed" twice running with a DIFFERENT net each time.
    3. A glob never substitutes for an exact name on a protected net: the glob
       is silently skipped while the router keeps asking for that exact rip.
    4. A rip set that names the net being routed is a no-op that needs
       --force-reroute instead.
    """
    out = []
    if len(nets) > 1:
        out.append(f"routing {len(nets)} nets in one call: the second can rip "
                   f"the first and the tally will not say so -- one net per call")
    widthy = set(power_nets) | set(impedance_nets)
    unguarded = sorted(widthy & set(rip_set))
    if unguarded:
        out.append(f"rip set contains width-bearing net(s) {', '.join(unguarded)} "
                   f"-- carry --power-nets/--impedance in the SAME call or they "
                   f"come back at the signal default")
    globs = sorted(p for p in rip_set if any(c in p for c in '*?['))
    if globs:
        out.append(f"rip pattern(s) {', '.join(globs)} are globs: a protected or "
                   f"locked net matching them is skipped silently -- name it "
                   f"exactly to override, and note 'locked' has no override")
    both = sorted(set(nets) & set(rip_set))
    if both:
        out.append(f"{', '.join(both)} is in BOTH --nets and the rip set: that is "
                   f"a no-op unless you also pass --force-reroute")
    return out


# ------------------------------------------------------------------ the verbs

class _StdoutToStderr:
    """`poses` emits JSON on stdout, so nothing else may.

    Parsing the board and building the placement state print diagnostics --
    quench warns about footprints with no courtyard, for instance -- straight to
    stdout, which lands in the middle of the document a caller is piping into
    `json.load`. The diagnostics are worth keeping; they just belong on stderr.
    """

    def __enter__(self):
        self._real = sys.stdout
        sys.stdout = sys.stderr
        return self

    def __exit__(self, *exc):
        sys.stdout = self._real
        return False


def cmd_poses(a):
    from kicad_parser import parse_kicad_pcb
    import pose_score
    with _StdoutToStderr():
        pcb = parse_kicad_pcb(a.board)
        st = pose_score.make_state(pcb, a.board, clearance=a.clearance,
                                   board_edge_clearance=a.board_edge_clearance)
    with _StdoutToStderr():
        poses = pose_score.rank_poses(pcb, a.board, a.ref, radius=a.radius,
                                      step=a.step, limit=a.limit, state=st)
    if not poses:
        print(json.dumps({'ref': a.ref, 'poses': [],
                          'note': 'no legal pose, including staying put'}, indent=1))
        return 1

    if a.route:
        if not a.affected:
            print("--route needs --affected NET ... : only the caller knows "
                  "which nets a move can affect", file=sys.stderr)
            return 2
        from placement.writer import write_placed_output
        tmp = tempfile.mkdtemp(prefix='converge_poses_')
        with _StdoutToStderr():     # the writer and the router both narrate
            for p in poses[:a.route_top]:
                cand = os.path.join(
                    tmp, f"p_{p['x']}_{p['y']}_{int(p['rot'])}.kicad_pcb")
                write_placed_output(a.board, cand, [{'reference': a.ref,
                                                     'new_x': p['x'],
                                                     'new_y': p['y'],
                                                     'new_rotation': p['rot']}])
                res = scoped_route(cand, a.affected, extra_args=a.route_args or [])
                n, note = route_verdict(res['summary'])
                p['route'] = {'failures': n, 'note': note,
                              'iterations': res['summary'].get('total_iterations'),
                              'vias': res['summary'].get('total_vias')}
    print(json.dumps({'ref': a.ref, 'base_cost': poses[0]['cost'] - poses[0]['delta'],
                      'poses': poses}, indent=1))
    return 0


def cmd_where(a):
    """net_forensics already answers 'where is the gap and what is walling it
    in', per layer, nearest-first. Nothing in the usual chain calls it."""
    argv = [sys.executable, '-X', 'utf8',
            os.path.join(ROOT, 'net_forensics.py'), a.board,
            '--nets'] + list(a.nets) + ['--radius', str(a.radius)]
    return subprocess.run(argv, cwd=ROOT).returncode


def cmd_record(a):
    from board_store import BoardStore, Ledger
    store = BoardStore(a.store or os.path.join(os.path.dirname(a.ledger), 'boards'))
    sha = store.put(a.board)
    lg = Ledger(a.ledger)
    prev = lg.last_accepted()
    e = lg.append({'iteration': len(lg.entries()), 'kind': a.kind,
                   'parent_sha': (prev or {}).get('result_sha'),
                   'result_sha': sha, 'lever': a.lever,
                   'lever_argv': list(a.argv) if a.argv else None,
                   'score': json.loads(a.score) if a.score else None,
                   'accepted': not a.rejected})
    print(json.dumps(e, indent=1, sort_keys=True))
    return 0


def cmd_step_back(a):
    from board_store import BoardStore, Ledger
    lg = Ledger(a.ledger)
    store = BoardStore(a.store or os.path.join(os.path.dirname(a.ledger), 'boards'))
    if a.to:
        sha = a.to
    elif a.iteration is not None:
        m = [e for e in lg.entries() if e.get('iteration') == a.iteration]
        if not m:
            print(f"no iteration {a.iteration} in {a.ledger}", file=sys.stderr)
            return 2
        sha = m[-1]['result_sha']
    else:
        last = lg.last_accepted()
        if not last:
            print("no accepted iteration to step back to", file=sys.stderr)
            return 2
        sha = last['result_sha']
    store.get(sha, a.out)
    print(f"checked out {sha[:12]} -> {a.out}")
    return 0


def cmd_replay(a):
    from board_store import Ledger, replay_command
    lg = Ledger(a.ledger)
    m = [e for e in lg.entries() if e.get('iteration') == a.iteration]
    if not m:
        print(f"no iteration {a.iteration}", file=sys.stderr)
        return 2
    try:
        argv = replay_command(m[-1])
    except ValueError as e:
        # A message, not a traceback: "this iteration is not replayable" is an
        # ordinary answer about the ledger, not a crash.
        print(str(e), file=sys.stderr)
        print("Record lever_argv when you write an entry and this becomes a "
              "one-liner instead of a reconstruction.", file=sys.stderr)
        return 4
    print('replaying: ' + ' '.join(argv))
    return subprocess.run(argv, cwd=ROOT).returncode


def cmd_status(a):
    from board_store import Ledger
    lg = Ledger(a.ledger)
    c = lg.counts()
    print(json.dumps(c, indent=1, sort_keys=True))
    if c['total'] and c['systemic'] * 2 >= c['total']:
        print("NOTE: at least half of this budget went to SYSTEMIC iterations -- "
              "changes to how the chain measures or grades itself, not to the "
              "copper. Check what is still unrouted before spending more.",
              file=sys.stderr)
    return 0


def build_parser():
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest='verb', required=True)

    q = sub.add_parser('poses', help='rank a part\'s candidate poses')
    q.add_argument('board')
    q.add_argument('--ref', required=True)
    q.add_argument('--radius', type=float, default=2.0)
    q.add_argument('--step', type=float, default=0.5)
    q.add_argument('--limit', type=int, default=12)
    q.add_argument('--clearance', type=float, default=0.25)
    q.add_argument('--board-edge-clearance', type=float, default=0.55)
    q.add_argument('--route', action='store_true',
                   help='also run tier 3 (a scoped route) on the top poses')
    q.add_argument('--route-top', type=int, default=2,
                   help='how many ranked poses to actually route (default 2)')
    q.add_argument('--affected', nargs='+', default=None,
                   help='nets a move of this part can affect; required by --route')
    q.add_argument('--route-args', nargs='+', default=None)
    q.set_defaults(fn=cmd_poses)

    w = sub.add_parser('where', help='islands, gaps and the copper walling them in')
    w.add_argument('board')
    w.add_argument('--nets', nargs='+', required=True)
    w.add_argument('--radius', type=float, default=1.0)
    w.set_defaults(fn=cmd_where)

    r = sub.add_parser('record', help='store a board and record what produced it')
    r.add_argument('--ledger', required=True)
    r.add_argument('--board', required=True)
    r.add_argument('--store', default=None)
    r.add_argument('--kind', choices=('completion', 'systemic'), default='completion')
    r.add_argument('--lever', default=None)
    r.add_argument('--score', default=None, help='JSON')
    r.add_argument('--rejected', action='store_true')
    r.add_argument('--argv', nargs=argparse.REMAINDER, default=None,
                   help='the command that produced it -- what makes replay possible')
    r.set_defaults(fn=cmd_record)

    s = sub.add_parser('step-back', help='check out an earlier board, exactly')
    s.add_argument('--ledger', required=True)
    s.add_argument('--store', default=None)
    s.add_argument('--to', default=None, help='a board sha')
    s.add_argument('--iteration', type=int, default=None)
    s.add_argument('--out', required=True)
    s.set_defaults(fn=cmd_step_back)

    y = sub.add_parser('replay', help="re-run an iteration's lever verbatim")
    y.add_argument('--ledger', required=True)
    y.add_argument('--iteration', type=int, required=True)
    y.set_defaults(fn=cmd_replay)

    t = sub.add_parser('status', help='budget spent, completion vs systemic')
    t.add_argument('--ledger', required=True)
    t.set_defaults(fn=cmd_status)
    return p


def main(argv=None):
    a = build_parser().parse_args(argv)
    return a.fn(a)


if __name__ == '__main__':
    sys.exit(main())
