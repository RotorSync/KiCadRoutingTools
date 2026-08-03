#!/usr/bin/env python3
"""Opt-in: does a placement change actually ROUTE better?

`test_placement_ab.py` grades placement with placement metrics, independently
re-derived. That catches a term that games its own model, but every one of
those metrics is still a PROXY -- and the repo's own history says proxies
mislead here: one accepted placement improved `crossings` 85->60 and `hpwl`
602->596 while breaking a decap requirement, and run 6 routed MORE copper than
run 8 while scoring 29 plane-void crossings against run 8's 43.

So this tier routes. It is slower, which is why it is opt-in rather than part
of the A/B gate.

The scoping is the design decision worth stating: nets are chosen **causally,
not by which parts moved**. Routing "the nets touching moved parts" measures
whatever the optimizer happened to touch, which is circular -- a term that
moves nothing scores a perfect null. Instead it routes the union of

    (nets `net_affinity` flagged)  U  (the declared corridor nets)

which is the set the CHANGE CLAIMS to help, fixed before either board is
routed, and identical on both. If those nets do not route better, the signal is
not predictive and the change should not ship, whatever the proxies said.

Usage:
    python3 -X utf8 tests/test_placement_probe.py --off A.kicad_pcb \\
        --on B.kicad_pcb [--intent fp.json] [--extra-nets 'SDRAM_A*']
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def causal_nets(board, intent_path=None, extra=None):
    """The nets the change claims to help, as glob-free exact names.

    Derived from the OFF board only, so the ON board cannot influence which
    nets it is judged on.
    """
    from kicad_parser import parse_kicad_pcb
    from placement import floorplan
    names = set(extra or ())
    if not intent_path:
        return sorted(names)
    pcb = parse_kicad_pcb(board)
    intent = floorplan.load_intent(intent_path)
    result = floorplan.grade(intent, pcb, board, with_health=True)
    health = result.health or {}
    for row in (health.get('net_affinity') or []):
        if row.get('net'):
            names.add(row['net'])
    # Corridor nets: the buses a corridor term claims to unclutter.
    from net_queries import matches_net_filter
    for spec in ((intent.health or {}).get('bus_corridors') or []):
        pats = list(spec.get('nets') or ())
        if not pats:
            continue
        for nid, n in pcb.nets.items():
            if nid > 0 and n.name and matches_net_filter(n.name, pats):
                names.add(n.name)
    return sorted(names)


def probe(board, nets, workdir, route_args=None):
    """Route `nets` on `board` and return the connectivity/DRC verdict."""
    from converge import scoped_route
    out = os.path.join(workdir, os.path.basename(board).replace(
        '.kicad_pcb', '_probe.kicad_pcb'))
    return scoped_route(board, nets, out=out,
                        extra_args=list(route_args or ()))


def main(argv=None):
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--off', required=True, help='the baseline board')
    p.add_argument('--on', required=True, help='the changed board')
    p.add_argument('--intent', default=None,
                   help='floorplan intent; without it, only --extra-nets '
                        'scope the probe')
    p.add_argument('--extra-nets', nargs='*', default=(),
                   help='additional exact net names to include')
    # ONE string, shlex-split -- not nargs='*'. argparse refuses dash-prefixed
    # values for a list option, so `--route-args --clearance 0.2` exits 2. Same
    # trap and same fix as compare_seeds.py's forwarded args.
    p.add_argument('--route-args', default='',
                   help="forwarded to the router, IDENTICALLY on both boards, "
                        "as ONE quoted string: --route-args '--clearance 0.2'")
    p.add_argument('--workdir', default=None)
    args = p.parse_args(argv)

    import tempfile
    workdir = args.workdir or tempfile.mkdtemp(prefix='placement_probe_')
    os.makedirs(workdir, exist_ok=True)

    import shlex
    route_args = shlex.split(args.route_args) if args.route_args else []
    nets = causal_nets(args.off, args.intent, args.extra_nets)
    if not nets:
        print("no causal nets: nothing this change claims to help is "
              "identifiable. Declare health.bus_corridors, or pass "
              "--extra-nets.", file=sys.stderr)
        return 2
    print(f"probing {len(nets)} net(s) chosen from the OFF board: "
          f"{', '.join(nets[:8])}{' ...' if len(nets) > 8 else ''}\n")

    try:
        off = probe(args.off, nets, workdir, route_args)
        on = probe(args.on, nets, workdir, route_args)
    except ImportError as exc:
        print(f"probe needs converge.scoped_route: {exc}", file=sys.stderr)
        return 2

    print(f"OFF {json.dumps(off, sort_keys=True)}")
    print(f"ON  {json.dumps(on, sort_keys=True)}")

    def _get(d, *keys):
        for k in keys:
            if isinstance(d, dict) and k in d:
                return d[k]
        return None

    fa, fb = _get(off, 'failed', 'unrouted'), _get(on, 'failed', 'unrouted')
    if fa is None or fb is None:
        print("\nINCONCLUSIVE: the probe returned no failure count to compare")
        return 2
    print(f"\nfailures on the claimed nets: {fa} -> {fb}")
    if fb < fa:
        print("BETTER -- the signal is predictive on the nets it named")
        return 0
    if fb > fa:
        print("WORSE -- do not ship this change")
        return 1
    print("NO CHANGE -- the proxies moved and the routing did not. That is a "
          "reason to doubt the term, not a pass.")
    return 0


if __name__ == '__main__':
    sys.exit(main())
