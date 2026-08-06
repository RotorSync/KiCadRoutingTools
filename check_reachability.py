#!/usr/bin/env python3
"""Is this pad reachable, or is the router being blamed for the geometry?

    python3 check_reachability.py board.kicad_pcb --net GND --at 142.5,88.1
    python3 check_reachability.py board.kicad_pcb --pad U3.23
    python3 check_reachability.py board.kicad_pcb --pad U3.23 --track 0.15 --json

Measures the widest track that can reach the rest of the net from that pad, by
Euclidean distance transform plus an exact Kruskal widest path (see
`placement/reachability.py`). Two verdicts, and they mean different things:

    PASSABLE -- a route EXISTS at this track width. If the router failed, that
                is a finding about the ROUTER (grid, ordering, ripup budget),
                not about the board.
    CAGED    -- no route exists at ANY grid. This one IS geometry, and the fix
                is placement or a spec change.

Why it exists: across four recorded runs, 9 of 14 "no placement can fix this"
claims were later refuted, and every claim that a pad was unroutable in
principle was wrong. The rule this tool enforces is simple -- **no impossibility
claim without a numeric field.**

Clearances are read from the BOARD (netclasses via the sibling `.kicad_pro`,
then `--clearance` as the base), so the verdict is graded at what the board
would actually route at rather than at a guessed round number.

Exit codes: 0 PASSABLE, 1 CAGED, 2 usage/geometry error.
"""
import argparse
import json
import os
import sys


def _resolve_pad(pcb, spec):
    """'U3.23' -> (x, y, net_id, layer). The layer is the pad's own."""
    if '.' not in spec:
        raise SystemExit(f"--pad wants REF.PADNUM, got {spec!r}")
    ref, num = spec.rsplit('.', 1)
    fp = pcb.footprints.get(ref)
    if fp is None:
        raise SystemExit(f"no footprint {ref!r} on this board")
    for p in fp.pads:
        if str(p.pad_number) == num:
            layers = [l for l in (p.layers or []) if l.endswith('.Cu')]
            layer = None
            if p.drill and p.drill > 0:
                layer = None                      # any; caller picks the first
            elif layers and layers[0] != '*.Cu':
                layer = layers[0]
            return p.global_x, p.global_y, p.net_id, layer
    raise SystemExit(f"{ref} has no pad {num!r} "
                     f"(has: {', '.join(str(p.pad_number) for p in fp.pads[:12])})")


def main(argv=None):
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('board')
    p.add_argument('--pad', help="REF.PADNUM, e.g. U3.23 -- sets --at and "
                                 "--net from the pad itself")
    p.add_argument('--at', help="x,y of the stranded pad (mm)")
    p.add_argument('--net', help="net name; inferred from --pad when given")
    p.add_argument('--layers', default=None,
                   help="comma list, the seed pad's layer FIRST "
                        "(default: the board's copper layers)")
    p.add_argument('--track', type=float, default=None,
                   help="track width to test (default: the board's Default "
                        "netclass track width)")
    p.add_argument('--via', type=float, default=None,
                   help="via diameter for layer changes (default: the board's "
                        "Default netclass via)")
    p.add_argument('--clearance', type=float, default=None,
                   help="base clearance (default: the board's Default "
                        "netclass clearance). Per-net classes are read from "
                        "the sibling .kicad_pro on top of this")
    p.add_argument('--step', type=float, default=0.01,
                   help="raster step in mm (default 0.01). A throat is only "
                        "resolved to +/- one step, so halve it if the margin "
                        "comes out inside a step of zero")
    p.add_argument('--margin', type=float, default=4.0,
                   help="half-size of the view around the seed, mm "
                        "(default 4.0). Reachability is LOCAL; a whole board "
                        "at 10um costs memory for nothing")
    p.add_argument('--view', default=None, help="x0,y0,x1,y1 to override")
    p.add_argument('--json', action='store_true')
    args = p.parse_args(argv)

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from kicad_parser import parse_kicad_pcb
    from placement import reachability

    pcb = parse_kicad_pcb(args.board)

    layer_first = None
    if args.pad:
        x, y, nid, layer_first = _resolve_pad(pcb, args.pad)
        seed = (x, y)
        net_id = nid
        if not net_id:
            raise SystemExit(f"{args.pad} has no net -- nothing to reach")
    else:
        if not (args.at and args.net):
            raise SystemExit("give --pad REF.NUM, or both --at x,y and --net")
        seed = tuple(float(v) for v in args.at.split(','))
        net_id = None

    # Knobs from the BOARD, not from round numbers.
    import list_nets
    try:
        dr = list_nets.read_design_rules(args.board)
    except Exception:                             # noqa: BLE001
        dr = None
    def _board(key, fallback):
        v = list_nets.board_default_netclass_param(args.board, key, dr)
        return float(v) if v else fallback
    clearance = args.clearance if args.clearance is not None else _board(
        'clearance', 0.2)
    track = args.track if args.track is not None else _board('track_width',
                                                             0.15)
    via = args.via if args.via is not None else _board('via_diameter', 0.6)
    try:
        net_clearances = list_nets.net_clearance_map_by_id(
            args.board, {i: n.name for i, n in pcb.nets.items()}, dr)
    except Exception:                             # noqa: BLE001
        net_clearances = {}

    layers = (args.layers.split(',') if args.layers
              else list(pcb.board_info.copper_layers or ('F.Cu', 'B.Cu')))
    if layer_first and layer_first in layers:     # the seed's own layer first
        layers = [layer_first] + [l for l in layers if l != layer_first]
    view = ([float(v) for v in args.view.split(',')] if args.view else None)

    try:
        r = reachability.pad_reachability(
            pcb, seed, net_name=args.net, net_id=net_id, layers=layers,
            view=view, track_mm=track, via_mm=via, base_clearance=clearance,
            net_clearances=net_clearances, step=args.step,
            margin_mm=args.margin)
    except reachability.ScipyRequired as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    source = 'from --flags' if args.clearance is not None else 'from the board'
    if args.json:
        print(json.dumps(r.to_dict(), indent=2))
    else:
        print(f"board      {os.path.basename(args.board)}")
        print(f"knobs      clearance {clearance}mm, track {track}mm, "
              f"via {via}mm ({source})")
        print(r.format_text())
        if r.target_cells and not r.caged:
            print("\nPASSABLE means a route EXISTS at this width. If the "
                  "router failed here, that is a finding about the router "
                  "(grid, ordering, ripup budget) -- not about the board.")
    # "nothing to reach inside the view" is not a verdict, and must not be
    # reported as one: exit 2 so a script cannot read it as PASSABLE.
    if r.target_cells == 0:
        return 2
    return 1 if r.caged else 0


if __name__ == '__main__':
    sys.exit(main())
