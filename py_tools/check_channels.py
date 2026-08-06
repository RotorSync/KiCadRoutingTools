#!/usr/bin/env python3
"""Are the routing channels open? The lane-ledger instrument (run-6).

Productizes the skill's per-face lane ledger (supply vs demand per escape
face of every fine-pitch part) and the anchor-pair channel widths -- the
numbers the placement gate reads before routing, and the classification
input for "is this failure floorplan-shaped": a face in deficit AT THE
FINEST LEGAL GRID is a placement/floorplan fact no routing parameter can
fix, and its `eaten_by` refs are the fix iteration's move targets.

REPORT-ONLY (exit 0 unless the board is unreadable): the corridor law is
measured descriptive, not prescriptive -- these numbers inform the loop,
they do not gate by themselves. V1 limitation, stated: supply-tap/via lane
consumption is not modeled.
"""
import _path  # noqa: F401  (py_tools -> py_router/py_placer on sys.path)

import argparse
import json
import sys


# A face with no lanes left is only news when it has something to send. Below
# this demand the finding is noise: plenty of correct boards have a blocked
# face that carries one or two nets.
GATE_MIN_DEMAND = 7


def _starved_faces(ledgers, min_demand):
    """(ref, face, demand) for faces with zero supply and real demand."""
    out = []
    for ref, rows in sorted((ledgers or {}).items()):
        for r in rows:
            if r['supply_finest_grid'] == 0 and r['demand_nets'] >= min_demand:
                out.append((ref, r['face'], r['demand_nets']))
    return out


def main():
    p = argparse.ArgumentParser(
        description="Per-face lane ledger + anchor channel widths.")
    p.add_argument("board")
    p.add_argument("--clearance", type=float, default=None)
    p.add_argument("--track-width", type=float, default=None)
    p.add_argument("--grid-step", type=float, default=None)
    p.add_argument("--refs", nargs="*", default=None,
                   help="Parts to ledger (default: auto -- QFN/QFP/BGA "
                        "or pad pitch below 2x the lane pitch)")
    p.add_argument("--min-extent", type=float, default=3.5,
                   help="Anchor size floor for the channel table (mm)")
    p.add_argument("--json", default=None, metavar="PATH")
    p.add_argument("--baseline", default=None, metavar="BOARD",
                   help="the board this one was derived from (typically the "
                        "damaged input). Enables the E8 gate: faces that lose "
                        "ALL escape supply while carrying real demand, and did "
                        "not already do so on the baseline.")
    p.add_argument("--gate", action="store_true",
                   help="exit 4 when the --baseline comparison finds a NEW "
                        "starved face (default: report only)")
    p.add_argument("--min-demand", type=int, default=GATE_MIN_DEMAND,
                   help="nets a face must carry before zero supply counts as "
                        "starvation (default: %(default)s)")
    args = p.parse_args()

    import routing_defaults as defaults
    from kicad_parser import parse_kicad_pcb, detect_package_type
    from placement import routability

    clearance = (args.clearance if args.clearance is not None
                 else defaults.CLEARANCE)
    track = (args.track_width if args.track_width is not None
             else defaults.TRACK_WIDTH)
    grid = (args.grid_step if args.grid_step is not None
            else defaults.GRID_STEP)

    try:
        pcb = parse_kicad_pcb(args.board)
    except Exception as exc:
        print(f"cannot parse {args.board}: {exc}", file=sys.stderr)
        return 2

    refs = args.refs
    if not refs:
        pitch_floor = 2 * (track + clearance)
        refs = []
        for ref, fp in sorted((pcb.footprints or {}).items()):
            kind = None
            try:
                kind = detect_package_type(fp)
            except Exception:
                kind = None
            if kind in ('QFN', 'QFP', 'BGA'):
                refs.append(ref)
                continue
            xs = sorted({round(p.global_x, 3) for p in (fp.pads or [])})
            gaps = [b - a for a, b in zip(xs, xs[1:]) if b - a > 1e-3]
            if gaps and min(gaps) < pitch_floor and len(fp.pads or []) >= 8:
                refs.append(ref)
        if not refs:
            print("  (no fine-pitch parts auto-detected; pass --refs)")

    print(f"Lane ledger of {args.board} "
          f"(track {track} clearance {clearance} grid {grid}; "
          f"taps NOT modeled -- v1):")
    ledgers = {}
    for ref in refs:
        rows = routability.face_lane_ledger(
            pcb, ref, clearance=clearance, track_width=track,
            grid_step=grid, pcb_file=args.board)
        if not rows:
            continue
        ledgers[ref] = rows
        for r in rows:
            flag = ''
            if r['deficit_finest_grid'] > 0:
                flag = '  <-- DEFICIT AT FINEST GRID (floorplan-shaped)'
            elif r['deficit_routed_grid'] > 0:
                flag = '  <-- deficit at routed grid (try finer grid first)'
            eaten = (' eaten_by ' + ', '.join(
                f'{n}({v})' for n, v in r['eaten_by'][:3])
                if r['eaten_by'] else '')
            print(f"  {ref} {r['face']}: demand {r['demand_nets']} "
                  f"supply {r['supply_routed_grid']}@routed"
                  f"/{r['supply_finest_grid']}@finest{eaten}{flag}")

    starved = _starved_faces(ledgers, args.min_demand)
    new_starved = None
    if args.baseline:
        try:
            base_pcb = parse_kicad_pcb(args.baseline)
        except Exception as exc:
            print(f"cannot parse baseline {args.baseline}: {exc}",
                  file=sys.stderr)
            return 2
        base_ledgers = {}
        for ref in refs:
            rows = routability.face_lane_ledger(
                base_pcb, ref, clearance=clearance, track_width=track,
                grid_step=grid, pcb_file=args.baseline)
            if rows:
                base_ledgers[ref] = rows
        was = {(r, f) for r, f, _d in _starved_faces(base_ledgers,
                                                     args.min_demand)}
        new_starved = [t for t in starved if (t[0], t[1]) not in was]
        print(f"Starved faces (zero supply at the finest grid, demand >= "
              f"{args.min_demand}): {len(starved)} now, {len(was)} on the "
              f"baseline, {len(new_starved)} NEW")
        for ref, face, dem in new_starved:
            print(f"  NEW: {ref} {face}: demand {dem}, supply 0 -- nothing "
                  f"leaves this face")
        if new_starved:
            print("  A face that carries real demand and has no lane left is "
                  "a placement the router cannot rescue, readable now rather "
                  "than after the retries. Move what ate the span (see "
                  "eaten_by above) or reconsider the arrangement.")

    channels = routability.pair_channel_widths(
        pcb, clearance=clearance, min_extent_mm=args.min_extent,
        pcb_file=args.board)
    print(f"Anchor channels (narrowest first, {len(channels)} pair(s)):")
    for row in channels[:12]:
        parts = (' parts: ' + ', '.join(row['parts_in_channel'])
                 if row['parts_in_channel'] else '')
        print(f"  {row['a']} <-> {row['b']}: {row['channel_mm']}mm{parts}")

    if args.json:
        with open(args.json, 'w', encoding='utf-8') as f:
            json.dump({'board': args.board, 'clearance': clearance,
                       'track_width': track, 'grid_step': grid,
                       'taps_not_modeled': True,
                       'ledgers': ledgers, 'channels': channels,
                       'starved_faces': starved,
                       'baseline': args.baseline,
                       'new_starved_faces': new_starved},
                      f, indent=1, sort_keys=True)
        print(f"  JSON -> {args.json}")
    if args.gate and new_starved:
        return 4
    return 0


if __name__ == "__main__":
    import cli_banner
    cli_banner.install()   # CMD/EXIT self-echo (run-3 B1)
    sys.exit(main())
