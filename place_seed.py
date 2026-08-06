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
    p.add_argument("--corridor-weight", type=float, default=0.0, metavar="W",
                   help="Price the length each foreign airwire cuts through "
                        "the intent's health.bus_corridors during the polish, "
                        "at W per mm. 0 = off (default). See "
                        "place_portfolio.py --corridor-weight")
    p.add_argument("--force", action="store_true",
                   help="Re-seed a board that already looks placed. The "
                        "existing placement is DISCARDED; to explore around "
                        "it instead, use place_portfolio.py")
    p.add_argument("--anchors-first", action="store_true",
                   help="Seed the ANCHOR tier (pad-extent >= the P75 "
                        "threshold, the same tiering reconstruct uses) by "
                        "descending extent BEFORE any small part -- the "
                        "default queue is pin-count descending, which seeds "
                        "a large low-pin connector late, after the smalls "
                        "claimed its space. The smalls are parked as "
                        "non-obstacles either way (the existing exclude "
                        "mechanism); this changes only WHO goes first "
                        "(run-4 C)")
    p.add_argument("--anchor-rounds", type=int, default=1,
                   help="With --anchors-first: gated re-seat passes after "
                        "the first full placement (default 1 = none). Each "
                        "round re-seats anchors then smalls at their partner "
                        "centroids over the FULL placement and keeps the "
                        "round only if the legality/hpwl gate tuple does not "
                        "worsen; stops early when a round moves nothing")
    p.add_argument("--repair", action="store_true",
                   help="Violation-driven minimal-move repair of a PLACED "
                        "board: only parts violating the intent or pad/hole "
                        "legality move, worst first, each seated nearest its "
                        "current pose with an escalating displacement cap. "
                        "The opposite contract of --force")
    p.add_argument("--dry-run", action="store_true",
                   help="With --repair: print the move list and grades, "
                        "write nothing")
    args = p.parse_args()
    if args.repair and args.force:
        p.error("--repair and --force are mutually exclusive (repair moves "
                "only violators; force re-derives everything)")
    if args.dry_run and not args.repair:
        p.error("--dry-run only applies to --repair")

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
    if args.repair:
        if st.unplaced:
            print("place_seed: --repair needs a PLACED board (this one is "
                  "unplaced -- seed it instead).", file=sys.stderr)
            return UNPLACED_EXIT
        result = seeder.repair_placement(
            pcb, args.input_file, intent, group_sources=sources,
            clearance=args.clearance,
            board_edge_clearance=args.board_edge_clearance,
            grid_step=args.grid_step)
        for note in result['notes']:
            print(f"  NOTE: {note}")
        max_move = 0.0
        for mv in result['moves']:
            fp = pcb.footprints.get(mv['reference'])
            if fp is not None:
                import math as _math
                max_move = max(max_move, _math.hypot(mv['new_x'] - fp.x,
                                                     mv['new_y'] - fp.y))
        print(f"Repair: {len(result['violators'])} violator(s), "
              f"{len(result['repaired'])} repaired "
              f"({len(result['moves'])} moved, max {max_move:.2f}mm), "
              f"{len(result.get('unresolved') or [])} unresolved, "
              f"{len(result['unrepairable'])} unrepairable")
        summary = {'repaired': len(result['repaired']),
                   'unrepairable': len(result['unrepairable']),
                   'moved_refs': [m['reference'] for m in result['moves']],
                   'max_move_mm': round(max_move, 3),
                   'dry_run': args.dry_run,
                   'output': None if args.dry_run else args.output_file}
        summary.update({f'{k}_before': v
                        for k, v in result['pad_report_before'].items()})
        if not args.dry_run:
            write_placed_output(args.input_file, args.output_file,
                                result['moves'])
            copy_siblings(args.input_file, args.output_file)
            from placement.legality import grade_pad_legality
            pcb_out = parse_kicad_pcb(args.output_file)
            pads_after = grade_pad_legality(pcb_out, args.clearance)
            graded = floorplan.grade(intent, pcb_out, args.output_file,
                                     group_sources=sources,
                                     clearance=args.clearance,
                                     board_edge_clearance=args.board_edge_clearance)
            for v in graded.errors[:10]:
                print(f"  GRADE ERROR [{v.rule}] {v.message}")
            summary['grade_errors'] = len(graded.errors)
            summary['pad_conflicts_after'] = pads_after['pad_conflicts']
            summary['hole_conflicts_after'] = pads_after['hole_conflicts']
            print("JSON_SUMMARY: " + json.dumps(summary, sort_keys=True))
            return 4 if (result['unrepairable'] or graded.errors) else 0
        print("JSON_SUMMARY: " + json.dumps(summary, sort_keys=True))
        return 4 if result['unrepairable'] else 0

    if not st.unplaced and not st.partially_unplaced and not args.force:
        # PARTIALLY unplaced boards (a stacked pile beside real placements --
        # a netlist re-import, or a seeder that pinned only the spec-fixed
        # parts) are a legitimate seeding target, not a refusal: locked parts
        # are treated as authoritative and the pile is what gets placed.
        print("place_seed: this board already looks PLACED. Seeding would "
              "discard that placement; use place_portfolio.py to explore "
              "variations of it, or --force to re-seed anyway.",
              file=sys.stderr)
        return UNPLACED_EXIT

    # Partially unplaced without --force: seed ONLY the stacked pile. The
    # genuinely-placed unlocked parts are someone's work, not this tool's to
    # re-derive; --force widens the scope back to everything unlocked.
    seed_refs = None
    if st.partially_unplaced and not st.unplaced and not args.force:
        seed_refs = set(st.stacked_refs)
        print(f"place_seed: partially unplaced -- seeding only the "
              f"{len(seed_refs)} stacked part(s); the rest stand as placed "
              f"(--force re-seeds everything unlocked)")

    rng = random.Random(f"{args.seed}")
    result = seeder.seed_from_intent(
        pcb, args.input_file, intent, rng, group_sources=sources,
        clearance=args.clearance,
        board_edge_clearance=args.board_edge_clearance,
        grid_step=args.grid_step, seed_refs=seed_refs,
        anchors_first=args.anchors_first,
        anchor_rounds=args.anchor_rounds)
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
            lock_refs=edge_refs or None, metrics_out=ratsnest,
            corridor_weight=args.corridor_weight,
            corridor_specs=list((intent.health or {}).get('bus_corridors')
                                or ()) or None)
        if placements:
            tmp = args.output_file + '.polish'
            write_placed_output(args.output_file, tmp, placements)
            os.replace(tmp, args.output_file)

    # ---- self-check: the seed must grade clean against its own intent ------
    def _grade():
        pcb_out = parse_kicad_pcb(args.output_file)
        return floorplan.grade(intent, pcb_out, args.output_file,
                               group_sources=sources,
                               clearance=args.clearance,
                               board_edge_clearance=args.board_edge_clearance)

    try:
        graded = _grade()
        # The quench has no zone term, so a polish nudge can walk a declared
        # zone member past its tolerance (measured: a crystal load cap,
        # 0.86mm past its zone's edge at one seed). A plain revert to the
        # seeded pose is not enough -- the polish moved NEIGHBORS into that
        # space too (measured: 0.784mm2 of new overlap) -- so the part is
        # RE-SEATED: the seeder's own search, targeted at its seeded pose,
        # constrained to its zone, against the post-polish board.
        if not args.no_polish:
            broke = sorted({v.ref for v in graded.errors
                            if v.rule == 'zone_containment' and v.ref})
            if broke:
                import pose_score
                pcb_cur = parse_kicad_pcb(args.output_file)
                st = pose_score.make_state(
                    pcb_cur, args.output_file, clearance=args.clearance,
                    board_edge_clearance=args.board_edge_clearance,
                    grid_step=args.grid_step)
                blocks2, _p = floorplan.resolve_blocks(intent, pcb_cur,
                                                       sources)
                zone_of = {}
                for z in intent.blocks:
                    if z.rect is None:
                        continue
                    for r in blocks2.get(z.name, ()):
                        zone_of.setdefault(r, z)
                seeded_pose = {p['reference']: p for p in result['placements']}
                fixes = []
                for ref in broke:
                    z = zone_of.get(ref)
                    sp = seeded_pose.get(ref)
                    if z is None or sp is None or ref not in st.parts:
                        continue
                    clr = seeder._try_place(
                        st, ref, sp['new_x'], sp['new_y'], set(),
                        constraint=z.rect, tol=intent.zone_tolerance(z))
                    if clr is not None:
                        p2 = st.parts[ref]
                        fixes.append({'reference': ref, 'new_x': p2.x,
                                      'new_y': p2.y, 'new_rotation': p2.rot})
                if fixes:
                    print(f"  polish walked "
                          f"{', '.join(f['reference'] for f in fixes)} out "
                          f"of a declared zone; re-seated in-zone against "
                          f"the polished board")
                    tmp = args.output_file + '.reseat'
                    write_placed_output(args.output_file, tmp, fixes)
                    os.replace(tmp, args.output_file)
                    graded = _grade()
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
    import cli_banner; cli_banner.install()  # CMD/EXIT self-echo (run-3 B1)
    sys.exit(main())
