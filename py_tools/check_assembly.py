#!/usr/bin/env python3
"""Is this placement physically BUILDABLE? The assembly gate (run-6).

One command, one verdict: the body-overlap channel (blocking = cross-
footprint pad intersections, corpus-calibrated to zero on healthy boards;
advisory = fab/courtyard pairs with class/intent waiver labels) plus the
pad/hole/oob legality echo -- both conjuncts in one JSON.

Needs NO intent to be meaningful (unlike check_floorplan's legality rule,
which skips without a budget): a bare board grades honestly. --intent adds
authored overlap waivers only.

--baseline <board> computes the loop currency: advisory pairs NEW relative
to the baseline board (dense real boards ship hundreds of by-design
courtyard kisses -- the corpus measured 235 -- so the placement fix loop
targets the pairs OUR moves introduced, never a shipped design's own).

Exit codes: 0 = no blocking pair, 2 = usage/load error, 4 = blocking > 0.
"""
import _path  # noqa: F401  (py_tools -> py_router/py_placer on sys.path)

import argparse
import json
import sys


def main():
    p = argparse.ArgumentParser(
        description="Assembly (body-overlap) audit of a placed board.")
    p.add_argument("board")
    p.add_argument("--intent", default=None, metavar="JSON",
                   help="Floorplan intent; only its overlap_waivers are read")
    p.add_argument("--clearance", type=float, default=None,
                   help="Pad-model clearance (default: routing_defaults)")
    p.add_argument("--baseline", default=None, metavar="BOARD",
                   help="Report advisory pairs NEW relative to this board "
                        "(the placement-loop currency)")
    p.add_argument("--json", default=None, metavar="PATH",
                   help="Write the full grade as JSON")
    args = p.parse_args()

    import routing_defaults as defaults
    from kicad_parser import parse_kicad_pcb
    from placement.legality import grade_body_overlap, grade_pad_legality

    clearance = (args.clearance if args.clearance is not None
                 else defaults.CLEARANCE)

    waivers = ()
    if args.intent:
        try:
            from placement.floorplan import load_intent
            waivers = load_intent(args.intent).waiver_pairs()
        except Exception as exc:
            print(f"cannot load intent {args.intent}: {exc}", file=sys.stderr)
            return 2

    try:
        pcb = parse_kicad_pcb(args.board)
    except Exception as exc:
        print(f"cannot parse {args.board}: {exc}", file=sys.stderr)
        return 2

    g = grade_body_overlap(pcb, clearance, intent_waivers=waivers,
                           pcb_file=args.board)
    leg = grade_pad_legality(pcb, clearance, worst_n=0)

    new_advisory = None
    if args.baseline:
        try:
            base_pcb = parse_kicad_pcb(args.baseline)
        except Exception as exc:
            print(f"cannot parse baseline {args.baseline}: {exc}",
                  file=sys.stderr)
            return 2
        gb = grade_body_overlap(base_pcb, clearance, intent_waivers=waivers,
                                pcb_file=args.baseline)
        base_keys = {(q.a, q.b, q.kind) for q in gb['pairs']}
        new_advisory = [q for q in g['advisory_pairs']
                        if (q.a, q.b, q.kind) not in base_keys]

    print(f"Assembly audit of {args.board} (clearance {clearance}):")
    print(f"  blocking {g['blocking']}  advisory {g['advisory']}"
          f"  waived {g['waived']}"
          + (f"  new-vs-baseline {len(new_advisory)}"
             if new_advisory is not None else ""))
    for q in g['pairs']:
        label = ('BLOCKING' if q.kind == 'pad_intersection'
                 else (f'waived:{q.waiver}' if q.waived else 'advisory'))
        star = ''
        if new_advisory is not None and q in new_advisory:
            star = '  <-- NEW vs baseline'
        print(f"    {q.a} <-> {q.b}  {q.kind}  {q.area_mm2}mm2 "
              f"side {q.side}  {label}{star}")
    print(f"  pad/hole/oob echo: {leg['pad_conflicts']} pad pair(s), "
          f"{leg['hole_conflicts']} hole conflict(s), "
          f"{leg['oob_pad_count']} part(s) with pad copper off-board")
    verdict = ('NOT BUILDABLE' if g['blocking']
               else 'buildable (blocking 0)')
    print(f"  VERDICT: {verdict}")

    if args.json:
        doc = {
            'board': args.board,
            'clearance': clearance,
            'blocking': g['blocking'],
            'advisory': g['advisory'],
            'waived': g['waived'],
            'pairs': [q._asdict() for q in g['pairs']],
            'blocking_pairs': [q._asdict() for q in g['blocking_pairs']],
            'advisory_pairs': [q._asdict() for q in g['advisory_pairs']],
            'pad_conflicts': leg['pad_conflicts'],
            'hole_conflicts': leg['hole_conflicts'],
            'oob_pad_count': leg['oob_pad_count'],
            'oob_pad_amount': leg['oob_pad_amount'],
        }
        if new_advisory is not None:
            doc['baseline'] = args.baseline
            doc['new_advisory_pairs'] = [q._asdict() for q in new_advisory]
        with open(args.json, 'w', encoding='utf-8') as f:
            json.dump(doc, f, indent=1, sort_keys=True)
        print(f"  JSON -> {args.json}")

    return 4 if g['blocking'] else 0


if __name__ == "__main__":
    import cli_banner
    cli_banner.install()   # CMD/EXIT self-echo (run-3 B1)
    sys.exit(main())
