#!/usr/bin/env python3
"""Reconstruct a PLACED-BUT-WRONG board: the puzzle solver.

Usage:
  python place_reconstruct.py damaged.kicad_pcb repaired.kicad_pcb [--intent fp.json]

For boards whose placement is structurally damaged (swapped regions, dragged
selections, re-imported netlists) -- the case the nudge-scale quench and the
from-scratch seeder both fail. Pipeline (placement/reconstruct.py):
classify tiers (locked/mechanical frame -> anchors -> smalls), corner-inset
pattern fit on mounting holes (propose-only), rigid +/-v vector detection, an
EXACT simultaneous candidate assignment (small ILP via scipy.optimize.milp;
breakout-descent fallback), then a violation-driven minimal-move legalize
sweep. Every stage applies only if the legality gate tuple (pad conflicts,
hole shortfall, pad off-board, courtyard overlap, hpwl) does not worsen.

Exit codes: 0 wrote a board that improved (or matched) every gate axis with
no residual pad conflicts; 3 the board cannot be reconstructed here (no
outline / carries copper); 4 residual violations remain (board still written
for inspection).
"""
import argparse
import json
import math
import sys


def main():
    import routing_defaults as defaults

    p = argparse.ArgumentParser(
        description="Structure-level placement reconstruction.",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog="""
Examples:
  python place_reconstruct.py damaged.kicad_pcb repaired.kicad_pcb
  python place_reconstruct.py damaged.kicad_pcb repaired.kicad_pcb \\
      --intent floorplan.json --dry-run
""")
    p.add_argument("input_file")
    p.add_argument("output_file")
    p.add_argument("--intent", default=None, metavar="JSON",
                   help="Optional floorplan intent (edge connectors exempt "
                        "from off-board repair; zones constrain re-seating)")
    p.add_argument("--stages", default="classify,fit,vector,assign,legalize",
                   help="Comma list of stages to run (default: all)")
    p.add_argument("--anchor-extent", default="auto",
                   help="Anchor tier threshold in mm, or 'auto' = "
                        "max(3.5, P75 of pad-extent diagonals)")
    p.add_argument("--move-penalty", type=float, default=0.75,
                   help="Assignment objective: flat cost (mm-equivalent) per "
                        "moved part -- the fewest-parts-moved term "
                        "(default: 0.75)")
    p.add_argument("--assign-rounds", type=int, default=1,
                   help="Gated assign+prune passes (default 1). Round 1's "
                        "homecomings make the net-anchor centroids TRUER, so "
                        "a second round can justify moves the first could "
                        "not: a self-consistent displaced ISLAND (members "
                        "conflict-free where they sit, anchored to each "
                        "other) presents no per-member gradient until its "
                        "boundary partners are home (run-4 F2/F4). Each "
                        "round is gated and pruned; the loop stops early "
                        "when a round moves nothing")
    p.add_argument("--max-move", type=float, default=5.0,
                   help="Legalize-stage displacement cap ladder tops out here "
                        "(default: 5.0)")
    p.add_argument("--clearance", type=float, default=defaults.CLEARANCE)
    p.add_argument("--board-edge-clearance", type=float, default=0.55)
    p.add_argument("--grid-step", type=float, default=defaults.GRID_STEP)
    p.add_argument("--dry-run", action="store_true",
                   help="Print the stage reports and move list; write nothing")
    args = p.parse_args()
    stages = {s.strip() for s in args.stages.split(',') if s.strip()}

    if not args.dry_run:
        try:
            from redo_record import record_invocation
            record_invocation()
        except Exception:
            pass

    import pose_score
    from kicad_parser import parse_kicad_pcb
    from placement import floorplan, reconstruct, seeder
    from placement.legality import grade_pad_legality
    from placement.placement_state import UNPLACED_EXIT, assess_placement
    from placement.portfolio import copy_siblings
    from placement.writer import write_placed_output

    intent = None
    if args.intent:
        try:
            intent = floorplan.load_intent(args.intent)
        except (OSError, ValueError) as exc:
            print(f"cannot load intent {args.intent}: {exc}", file=sys.stderr)
            return 2

    print(f"Loading {args.input_file}...")
    pcb = parse_kicad_pcb(args.input_file)
    if pcb.board_info.board_bounds is None:
        print("place_reconstruct: no Edge.Cuts outline -- the outline is "
              "spec-owned and will not be invented.", file=sys.stderr)
        return UNPLACED_EXIT
    st = assess_placement(pcb, args.input_file)
    if st.has_copper:
        print(f"place_reconstruct: board carries {st.segments} segment(s) / "
              f"{st.vias} via(s); reconstruction moves footprints and would "
              f"strand them. Strip or re-run from the unrouted board.",
              file=sys.stderr)
        return UNPLACED_EXIT

    state = pose_score.make_state(
        pcb, args.input_file, clearance=args.clearance,
        board_edge_clearance=args.board_edge_clearance,
        grid_step=args.grid_step)
    if state.legality_ctx is None:
        print("place_reconstruct: pad legality layer unavailable on this "
              "state; refusing to run blind.", file=sys.stderr)
        return 2

    notes = []
    report = {}

    tiers = reconstruct.classify(state, intent, args.anchor_extent)
    report['tiers'] = tiers.as_dict()
    print(f"Tiers: {len(tiers.locked)} locked, {len(tiers.zero_net)} "
          f"zero-net (frame), {len(tiers.anchors)} anchors "
          f"(extent >= {tiers.threshold}mm), {len(tiers.smalls)} smalls")

    # Run-4 F2: declared edge parts may overhang up to their band -- in the
    # candidate cull AND in the gate tuple (both sites, or the gate reverts
    # the homecoming the cull just allowed). Printed up front so the declared
    # set is part of the run's record.
    edge_bands = {}
    if intent is not None:
        for c in intent.edge_connectors:
            if c['ref'] not in state.parts:
                continue
            # EVERY banded entry keeps its allowance, suspect or not: with
            # banded_pad_oob ABOVE hpwl in the gate tuple, charging a
            # suspect's observed overhang would hand the ILP a strict tuple
            # improvement for pulling a healthy edge part (a false-positive
            # suspect) inboard off its correct seat. The exchange stage does
            # not need oob pressure -- it accepts on the hpwl homecoming.
            band = c.get('overhang_mm') or {}
            edge_bands[c['ref']] = float(band.get('max') or 2.0)
    if edge_bands:
        print("Declared edge parts (band max mm): "
              + ", ".join(f"{r} ({m:g})"
                          for r, m in sorted(edge_bands.items())))
    report['edge_bands'] = {r: m for r, m in sorted(edge_bands.items())}

    base = reconstruct.measure(state, edge_bands)
    print(f"Gate before: pad_pairs={base[0]} hole={base[1]} oob={base[2]} "
          f"hpwl={base[3]} overlap={base[4]}")
    report['gate_before'] = list(base)

    proposals = {}
    if 'fit' in stages:
        proposals = reconstruct.fit_corner_insets(state, tiers)
        for ref, cands in sorted(proposals.items()):
            print(f"  fit: {ref} proposed at {cands}")
        report['fit_proposals'] = {r: c for r, c in proposals.items()}

    vectors = []
    if 'vector' in stages:
        vectors = reconstruct.rigid_vectors(state, proposals)
        if vectors:
            print(f"  rigid vectors (up to sign): {vectors}")
        report['vectors'] = vectors

    # F2: declared edge entries whose edge could not be named (implausible
    # pose) -- their objective term is the EDGE metric, not the net-anchor
    # proxy (R1: an edge part's position is not a netlist question; the
    # proxy would anchor them to their possibly-misplaced partners).
    edge_pref = {}
    if intent is not None:
        for c in intent.edge_connectors:
            # Receptacles ONLY (run-5): the edge metric is the right
            # objective for a part whose class says the mating face must
            # reach the edge. An edge-less ACTUATOR entry (a suspect
            # overhang, run-5 suspect-and-derive) makes no seat claim --
            # its true home may be interior, and pinning it to an edge
            # would re-manufacture the damage; the exchange stage owns it.
            if (c['ref'] in state.parts and not c.get('edge')
                    and c.get('class') == 'edge_receptacle'
                    and not state.parts[c['ref']].locked):
                edge_pref[c['ref']] = edge_bands.get(c['ref'], 2.0)
    if edge_pref:
        print("Edge-preference refs (no declared edge; class outranks the "
              "netlist proxy): " + ", ".join(sorted(edge_pref)))

    moved = []
    all_pruned = []
    if 'assign' in stages and (vectors or proposals):
        for rnd in range(max(1, args.assign_rounds)):
            cands, pattern = reconstruct.build_candidates(
                state, tiers, vectors, proposals, edge_bands=edge_bands)
            n_multi = sum(1 for c in cands.values() if len(c) > 1)
            if rnd == 0:
                print(f"  assign: {n_multi} part(s) with a real candidate set")
            choice = reconstruct.solve_assignment(state, cands, tiers,
                                                  args.move_penalty, notes,
                                                  pattern=pattern,
                                                  edge_pref=edge_pref)
            old = {r: (state.parts[r].x, state.parts[r].y,
                       state.parts[r].rot)
                   for r in choice}
            for ref, k in sorted(choice.items()):
                if k > 0:
                    x, y = cands[ref][k]
                    state.apply_move(ref, x, y, state.parts[ref].rot)
            after = reconstruct.measure(state, edge_bands)
            if after <= base:
                # Run-4 F3(b): the gate is one board-wide tuple, so an
                # accepted assignment can smuggle individual mis-moves past
                # it (run 3's spurious vector carried J7 WORSE than its
                # input inside a hugely-improving set). Per-part revert
                # sweep, gated on strict tuple improvement -- monotone,
                # board-only.
                pruned = reconstruct.prune_assignment(state, old, notes,
                                                      edge_bands=edge_bands,
                                                      exempt=set(edge_pref))
                all_pruned.extend(pruned)
                after = reconstruct.measure(state, edge_bands)
                base = after
                rmoved = [r for r, k in choice.items()
                          if k > 0 and r not in set(pruned)]
                moved = sorted(set(moved) | set(rmoved))
                print(f"  assign round {rnd + 1} APPLIED: {len(rmoved)} "
                      f"part(s) moved"
                      + (f" ({len(pruned)} pruned back)" if pruned else "")
                      + f"; gate now pad_pairs={after[0]} hole={after[1]} "
                      f"oob={after[2]} hpwl={after[3]} overlap={after[4]}")
                if not rmoved:
                    break     # a round that moved nothing: converged
            else:
                for ref, (x, y, rot) in old.items():
                    state.apply_move(ref, x, y, rot)
                notes.append(f"assign round {rnd + 1} REVERTED: gate "
                             f"worsened {base} -> {after}")
                print(f"  assign round {rnd + 1} REVERTED "
                      f"(gate {base} -> {after})")
                break
    report['assign_pruned'] = sorted(set(all_pruned))
    report['assign_moved'] = sorted(moved)
    report['gate_after_assign'] = list(base)
    if edge_pref:
        report['edge_pref'] = sorted(edge_pref)
        report['edge_seated'] = {
            r: [state.parts[r].x, state.parts[r].y]
            for r in sorted(edge_pref) if r in set(moved)}

    placements = [{'reference': r, 'new_x': pt.x, 'new_y': pt.y,
                   'new_rotation': pt.rot}
                  for r, pt in state.parts.items()
                  if (abs(pt.x - pt.seed_x) > 1e-9
                      or abs(pt.y - pt.seed_y) > 1e-9
                      or pt.rot != pt.orig_rot % 360)]
    for n in notes:
        print(f"  NOTE: {n}")

    if args.dry_run:
        report['dry_run'] = True
        report['would_move'] = sorted(p_['reference'] for p_ in placements)
        print("JSON_SUMMARY: " + json.dumps(report, sort_keys=True))
        return 0

    write_placed_output(args.input_file, args.output_file, placements)
    copy_siblings(args.input_file, args.output_file)

    if 'legalize' in stages:
        pcb2 = parse_kicad_pcb(args.output_file)
        caps = [c for c in seeder.REPAIR_CAPS_MM if c <= args.max_move]
        if not caps or caps[-1] < args.max_move:
            caps = list(caps) + [args.max_move]
        rep = seeder.repair_placement(
            pcb2, args.output_file, intent, group_sources=(),
            clearance=args.clearance,
            board_edge_clearance=args.board_edge_clearance,
            grid_step=args.grid_step, caps=caps)
        for n in rep['notes']:
            print(f"  NOTE: {n}")
        if rep['moves']:
            tmp = args.output_file + '.legalize'
            write_placed_output(args.output_file, tmp, rep['moves'])
            import os
            os.replace(tmp, args.output_file)
        print(f"  legalize: {len(rep['repaired'])} repaired, "
              f"{len(rep['unrepairable'])} unrepairable")
        report['legalize'] = {'repaired': rep['repaired'],
                              'unrepairable': rep['unrepairable']}

    final = grade_pad_legality(parse_kicad_pcb(args.output_file),
                               args.clearance)
    report['final'] = {k: final[k] for k in
                       ('pad_conflicts', 'pad_shortfall', 'hole_conflicts',
                        'oob_pad_count', 'oob_pad_amount', 'exact')}
    report['output'] = args.output_file
    print(f"Final (exact): {final['pad_conflicts']} pad conflict pair(s), "
          f"{final['hole_conflicts']} hole conflict(s), "
          f"{final['oob_pad_count']} part(s) with pad copper off-board")
    if final['oob_pad_count']:
        print("  (off-board residue that no cap could repair: if it is a "
              "by-design overhang -- a card edge, a switch actuator -- "
              "declare it in an intent's edge_connectors; it is then exempt)")
    print("JSON_SUMMARY: " + json.dumps(report, sort_keys=True))
    # Exit 4 = residual PAD/HOLE conflicts -- the defect classes this tool
    # exists to remove. Off-board residue is reported (and by-design overhang
    # is indistinguishable from defect without an intent), not an exit code.
    return 4 if (final['pad_conflicts'] or final['hole_conflicts']) else 0


if __name__ == "__main__":
    import cli_banner; cli_banner.install()  # CMD/EXIT self-echo (run-3 B1)
    sys.exit(main())
