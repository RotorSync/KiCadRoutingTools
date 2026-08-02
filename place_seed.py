#!/usr/bin/env python3
"""Generate an initial placement for an UNPLACED board from its floorplan intent.

Usage:
  python place_seed.py input.kicad_pcb output.kicad_pcb --intent floorplan.json

The placement stack refines and deliberately does not place from scratch
UNAIDED -- but a declared intent carries the constraints a from-scratch run
lacks: zones, edge bands, locks, decap rules. This tool turns that intent into
a legal starting placement (see placement/seeder.py for exactly what each
construct becomes), stamps the intent's must_lock refs `(locked yes)` into the
output, runs a quench polish over the free parts, and then GRADES its own
output against the same intent -- a seed that fails the intent it was built
from is a defect, not a result.

Different --seed values produce genuinely different legal seeds (packing order
and target jitter); the same seed reproduces byte for byte. Compose with
place_portfolio.py to diversify and rank what this emits.

Exit codes: 0 seeded and graded clean; 2 bad arguments; 3 the board cannot be
seeded (no Edge.Cuts outline -- the outline is spec-owned and will not be
invented -- or the board is already placed / carries copper); 4 the seed was
written but parts could not be seated or the intent grade has errors.
"""
import argparse
import json
import os
import sys


def main():
    import routing_defaults as defaults

    p = argparse.ArgumentParser(
        description="Intent-driven initial placement for an unplaced board.",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog="""
Examples:
  python place_seed.py board.kicad_pcb seed.kicad_pcb --intent floorplan.json
  python place_seed.py board.kicad_pcb seed.kicad_pcb --intent fp.json --seed 3
""")
    p.add_argument("input_file", help="Input KiCad PCB (unplaced parts + outline)")
    p.add_argument("output_file", help="Output board with the seeded placement")
    p.add_argument("--intent", required=True, metavar="JSON",
                   help="Floorplan intent: the constraint source AND the "
                        "acceptance gate for the emitted seed")
    p.add_argument("--seed", type=int, default=0,
                   help="Packing-order/jitter seed; same seed reproduces byte "
                        "for byte (default: 0)")
    p.add_argument("--group-by", default="auto",
                   help="Block sources for resolving the intent's `group` "
                        "references (default: auto = kicad,sheet)")
    p.add_argument("--ignore-nets", nargs="+", default=None, metavar="NET",
                   help="Net patterns excluded from the polish's airwire "
                        "scoring (plane-routed rails)")
    p.add_argument("--clearance", type=float, default=defaults.CLEARANCE)
    p.add_argument("--board-edge-clearance", type=float, default=0.55)
    p.add_argument("--grid-step", type=float, default=defaults.GRID_STEP)
    p.add_argument("--max-displacement", type=float, default=3.0,
                   help="Polish displacement cap in mm (default: 3.0)")
    p.add_argument("--no-polish", action="store_true",
                   help="Skip the quench polish; emit the raw packed seed")
    p.add_argument("--force", action="store_true",
                   help="Re-seed a board that already looks placed. The "
                        "existing placement is DISCARDED; to explore around "
                        "it instead, use place_portfolio.py")
    args = p.parse_args()

    try:
        from redo_record import record_invocation
        record_invocation()
    except Exception:
        pass

    import random
    from kicad_parser import parse_kicad_pcb
    from placement import floorplan, seeder
    from placement.groups import GroupError, parse_sources
    from placement.placement_state import UNPLACED_EXIT, assess_placement
    from placement.portfolio import copy_siblings
    from placement.writer import write_placed_output

    try:
        sources = parse_sources(args.group_by)
    except GroupError as exc:
        p.error(str(exc))
    try:
        intent = floorplan.load_intent(args.intent)
    except (OSError, ValueError) as exc:
        print(f"cannot load intent {args.intent}: {exc}", file=sys.stderr)
        return 2

    print(f"Loading {args.input_file}...")
    pcb = parse_kicad_pcb(args.input_file)
    if pcb.board_info.board_bounds is None:
        print("place_seed: this board has no Edge.Cuts outline. The outline "
              "is spec-owned -- draw it (or have the repo's seeder write it "
              "from the spec) before seeding a placement.", file=sys.stderr)
        return UNPLACED_EXIT
    st = assess_placement(pcb, args.input_file)
    if st.has_copper:
        print(f"place_seed: this board carries {st.segments} segment(s) and "
              f"{st.vias} via(s); seeding moves footprints and would strand "
              f"every track. Seed the unrouted board.", file=sys.stderr)
        return UNPLACED_EXIT
    if not st.unplaced and not args.force:
        print("place_seed: this board already looks PLACED. Seeding would "
              "discard that placement; use place_portfolio.py to explore "
              "variations of it, or --force to re-seed anyway.",
              file=sys.stderr)
        return UNPLACED_EXIT

    rng = random.Random(f"{args.seed}")
    result = seeder.seed_from_intent(
        pcb, args.input_file, intent, rng, group_sources=sources,
        clearance=args.clearance,
        board_edge_clearance=args.board_edge_clearance,
        grid_step=args.grid_step)
    for note in result['notes']:
        print(f"  NOTE: {note}")
    print(f"Seeded {len(result['placements'])} part(s); "
          f"{len(result['unseated'])} unseated; "
          f"{len(result['lock_refs'])} to lock")

    write_placed_output(args.input_file, args.output_file,
                        result['placements'])
    n_locked = seeder.stamp_locked(args.output_file, result['lock_refs'])
    copy_siblings(args.input_file, args.output_file)
    print(f"Stamped (locked yes) on {n_locked} part(s)")

    ratsnest = {}
    if not args.no_polish:
        from placement.quench import quench
        pcb_seeded = parse_kicad_pcb(args.output_file)
        # Guidance weights, same as place_portfolio: the seed should be
        # polished by the objective the later steps rank with. Locks ride in
        # from the file (must_lock was just stamped); edge connectors are
        # locked per-call so the polish cannot walk them off their band.
        edge_refs = [c['ref'] for c in intent.edge_connectors]
        placements = quench(
            pcb_seeded, pcb_file=args.output_file,
            max_displacement=args.max_displacement,
            step=1.0, grid_step=args.grid_step, clearance=args.clearance,
            board_edge_clearance=args.board_edge_clearance,
            crossing_penalty=30.0, length_weight=0.3, halo_base=0.5,
            halo_coef=0.15, halo_weight=2.0, edge_halo=2.0, edge_weight=2.0,
            ignore_nets=args.ignore_nets,
            lock_refs=edge_refs or None, metrics_out=ratsnest)
        if placements:
            tmp = args.output_file + '.polish'
            write_placed_output(args.output_file, tmp, placements)
            os.replace(tmp, args.output_file)

    # ---- self-check: the seed must grade clean against its own intent ------
    pcb_out = parse_kicad_pcb(args.output_file)
    try:
        graded = floorplan.grade(intent, pcb_out, args.output_file,
                                 group_sources=sources,
                                 clearance=args.clearance,
                                 board_edge_clearance=args.board_edge_clearance)
    except floorplan.UntrustworthyOutline as exc:
        print(f"place_seed: outline cannot be trusted for grading: {exc}",
              file=sys.stderr)
        return UNPLACED_EXIT
    for v in graded.errors[:10]:
        print(f"  GRADE ERROR [{v.rule}] {v.message}")
    after = ratsnest.get('after', {})
    summary = {'placed': len(result['placements']),
               'unseated': len(result['unseated']),
               'locked': n_locked,
               'grade_errors': len(graded.errors),
               'grade_warnings': len(graded.warnings),
               'crossings': after.get('crossings'),
               'hpwl': (round(after['hpwl'], 3)
                        if after.get('hpwl') is not None else None),
               'output': args.output_file}
    print("JSON_SUMMARY: " + json.dumps(summary, sort_keys=True))
    if result['unseated'] or graded.errors:
        print("place_seed: the seed does NOT satisfy its intent -- see the "
              "errors above. It was still written, for inspection.",
              file=sys.stderr)
        return 4
    return 0


if __name__ == "__main__":
    sys.exit(main())
