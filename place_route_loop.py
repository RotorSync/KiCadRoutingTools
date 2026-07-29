"""
Router-in-the-loop placement repair.

Routes the board, reads the failure diagnostics (failed nets + the blocker
nets named in the router's frontier analysis), and micro-quenches ONLY the
parts that could help those routes succeed:

  - parts owning pads of the failed nets (move the endpoint out of the
    congested pocket), and
  - parts owning pads of the blocker nets (move the anchor so the blocking
    wall re-routes),

with the failed nets' airwires given extra weight in the quench cost.
High-pin-count parts are excluded from targeting (--max-target-pins) - moving
a resistor that anchors a blocker net is low-risk; dragging a 144-pin QFP to
fix one net is how placements get destroyed.

The new placement is accepted only if a real re-route improves
(failures, router iterations), otherwise it is reverted and the next round
widens the displacement cap.

Usage:
  python place_route_loop.py input.kicad_pcb output.kicad_pcb \
      --route-args '--nets "/*" "Net-*" --track-width 0.2 ...' \
      [quench options]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys

from kicad_parser import parse_kicad_pcb
import routing_defaults as defaults
from placement.quench import quench
from placement.writer import write_placed_output

_SUMMARY_RE = re.compile(r'JSON_SUMMARY: (\{.*\})')

# route.py prints this from the except around its reconciliation self-invoke
# (route.py:2294). The sub-run prints its JSON_SUMMARY BEFORE the board is
# written, so a summary followed by this marker advertises recoveries that may
# never have reached the file on disk.
_RECONCILE_ABORTED = 'final reconciliation pass failed:'

# Counters that measure WORK DONE, so they add across passes. Everything else
# in a summary is state, and state is whatever the LAST pass measured.
_EFFORT_KEYS = ('total_iterations', 'total_vias', 'total_time')


def merge_route_summaries(log: str):
    """Reduce every JSON_SUMMARY in a route.py log to one honest tally.

    route.py runs an end-of-run reconciliation pass (route.py:2144, #348)
    exactly when the first pass left failures. It self-invokes batch_route one
    level deep on the written board and prints a SECOND JSON_SUMMARY, scoped
    to the retried nets, whose failure lists route.py itself calls "the honest
    still-open set". Reading only the first summary counted every
    reconciliation recovery as a failure: the loop quenched parts anchoring
    nets that were already solved, and better() compared tallies taken at
    different points in the run.

    Per field class:

    * FAILURE STATE (failed_single / failed_multipoint / multipoint_pads_*)
      comes from the LAST summary, and is exact rather than a delta. Every net
      with a nonzero failure term is in the retry set by construction, and the
      sub-run re-derives each retried net's pad counts over ALL of that net's
      pads from the final-board union-find (route.py:1840), so the last
      summary's numbers are absolute. Summing pad counts would double-count.
    * EFFORT (total_iterations / total_vias / total_time) is SUMMED: both
      passes are work this placement cost the router, and better()'s iteration
      tiebreak should see all of it. Taking effort from the last summary would
      make a badly failing candidate look cheap, since the reconciliation pass
      only re-routes a handful of nets.
    * Anything else is last-wins.

    Degrades to the single-summary case unchanged: no failures, or a sub-run
    that returned before printing, leaves one summary that is both the first
    and the last. Returns None when the log carries no summary at all.
    """
    raw = _SUMMARY_RE.findall(log)
    if not raw:
        return None
    summaries = [json.loads(s) for s in raw]

    # A reconciliation that raised AFTER printing its summary claims
    # recoveries the board write may never have committed; fall back to the
    # first pass, which is what is definitely on disk.
    aborted = log.rfind(_RECONCILE_ABORTED) > log.rfind(raw[-1])
    merged = dict(summaries[0] if aborted else summaries[-1])

    for key in _EFFORT_KEYS:
        merged[key] = sum(s.get(key, 0) for s in summaries)

    # Coverage-gate nets (route.py:1881) have NO routed result, so their pads
    # never reach multipoint_pads_total and the caller's
    # failures = len(failed_single) + pad-deficit weighs them ZERO, though
    # they ship at broken copper. Give each one weight 1, matching what
    # failed_single gives a net that produced no result at all, by widening
    # the pad denominator. They are in neither failed_single nor the pad
    # tallies (route.py:1887 excludes single_ended_nets and routed_results),
    # so this cannot double-count. It matters most on the LAST summary: those
    # are nets the reconciliation pass ITSELF broke through its rip
    # escalation, and without this the loop can read failures=0 on a board
    # shipping disconnected copper and stop.
    gate = merged.get('coverage_gate_nets') or []
    if gate:
        merged['multipoint_pads_total'] = (
            merged.get('multipoint_pads_total', 0) + len(gate))
    return merged


def _run_route_cmd(cmd, log_file):
    """Launch route.py with stdout and stderr captured to log_file; return its
    exit code. Split out from run_route so the whole tally can be driven
    against a canned log without launching a child process."""
    with open(log_file, 'w') as f:
        return subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT).returncode


def run_route(pcb_file: str, routed_file: str, route_args: str, log_file: str):
    """Run route.py, return (metrics dict, log text)."""
    cmd = [sys.executable, 'route.py', pcb_file, routed_file] + \
        shlex.split(route_args)
    _run_route_cmd(cmd, log_file)
    log = open(log_file).read()

    summary = merge_route_summaries(log)
    if summary is None:
        raise RuntimeError(f"route.py produced no JSON_SUMMARY (see {log_file})")

    failed_nets = list(summary.get('failed_single', []))
    # failed_multipoint entries are dicts {net_name, failed_pads}; keep just the
    # name so failed_nets is uniformly net-name strings (downstream uses them as
    # dict keys -> a dict here raises "unhashable type: 'dict'").
    failed_nets += [d['net_name'] if isinstance(d, dict) else d
                    for d in summary.get('failed_multipoint', [])]
    mp_deficit = (summary.get('multipoint_pads_total', 0)
                  - summary.get('multipoint_pads_connected', 0))
    failures = len(summary.get('failed_single', [])) + mp_deficit

    # Blocker nets from frontier diagnostics:  "  1. /MD1: 46 (31.7%) ..."
    blockers = re.findall(r'^\s+\d+\.\s+(\S+?):\s+\d+\s+\(', log, re.M)

    return {
        'failures': failures,
        'failed_nets': failed_nets,
        'blockers': sorted(set(blockers)),
        'iterations': summary.get('total_iterations', 0),
        'vias': summary.get('total_vias', 0),
    }


def nets_to_refs(pcb_data, net_names, max_pins, locked_patterns):
    """Map net names to the movable component refs that own their pads."""
    import fnmatch
    name_to_id = {net.name: nid for nid, net in pcb_data.nets.items()}
    refs = set()
    for name in net_names:
        nid = name_to_id.get(name)
        if nid is None:
            continue
        for pad in pcb_data.nets[nid].pads:
            refs.add(pad.component_ref)
    out = set()
    for ref in refs:
        fp = pcb_data.footprints.get(ref)
        if fp is None:
            continue
        pins = len([p for p in fp.pads if p.net_id > 0])
        if pins > max_pins:
            continue
        if locked_patterns and any(fnmatch.fnmatch(ref, p)
                                   for p in locked_patterns):
            continue
        out.add(ref)
    return out


def better(a, b):
    """Is metrics a better than b? Failures first, then iterations."""
    if a['failures'] != b['failures']:
        return a['failures'] < b['failures']
    return a['iterations'] < b['iterations'] * 0.95


def main():
    parser = argparse.ArgumentParser(
        description="Router-in-the-loop placement repair.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input_file")
    parser.add_argument("output_file")
    parser.add_argument("--route-args", required=True,
                        help="Arguments passed to route.py (quoted string)")
    parser.add_argument("--rounds", type=int, default=5,
                        help="Max repair rounds (default: 5)")
    parser.add_argument("--max-displacement", type=float, default=3.0,
                        help="Initial displacement cap per round in mm "
                             "(default: 3; widened 1.5x after a rejected "
                             "round - nudges only, never swaps)")
    parser.add_argument("--swap-max-displacement", type=float, default=None,
                        help="Displacement cap for same-footprint swaps in mm; "
                             "must not exceed --max-displacement and is NOT "
                             "widened between rounds (default: the initial "
                             "--max-displacement)")
    parser.add_argument("--max-target-pins", type=int, default=40,
                        help="Don't move parts with more connected pins than "
                             "this (default: 40)")
    parser.add_argument("--failed-net-weight", type=float, default=3.0,
                        help="Airwire weight multiplier for failed nets "
                             "(default: 3.0)")
    parser.add_argument("--step", type=float, default=0.5,
                        help="Candidate grid step in mm (default: 0.5)")
    parser.add_argument("--length-weight", type=float, default=0.3)
    parser.add_argument("--crossing-penalty", type=float, default=30.0)
    parser.add_argument("--halo-base", type=float, default=0.5)
    parser.add_argument("--halo-coef", type=float, default=0.15)
    parser.add_argument("--halo-weight", type=float, default=2.0)
    parser.add_argument("--edge-halo", type=float, default=2.0)
    parser.add_argument("--edge-weight", type=float, default=2.0)
    parser.add_argument("--clearance", type=float, default=defaults.CLEARANCE)
    parser.add_argument("--board-edge-clearance", type=float, default=0.55)
    parser.add_argument("--grid-step", type=float, default=defaults.GRID_STEP)
    parser.add_argument("--ignore-nets", nargs="+", default=None)
    parser.add_argument("--lock", nargs="+", default=None)
    parser.add_argument("--no-rotate", action="store_true",
                        help="Disable rotation moves")
    parser.add_argument("--no-swap", action="store_true",
                        help="Disable same-footprint swap moves")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Print each accepted quench move and the "
                             "per-pass swap-capped count")
    parser.add_argument("--work-dir", default=None,
                        help="Directory for intermediate files "
                             "(default: alongside output)")
    args = parser.parse_args()

    if args.max_displacement < 0:
        parser.error("--max-displacement must be >= 0")
    if args.swap_max_displacement is not None:
        if args.swap_max_displacement < 0:
            parser.error("--swap-max-displacement must be >= 0")
        if args.swap_max_displacement > args.max_displacement:
            parser.error("--swap-max-displacement must not exceed "
                         "--max-displacement")

    work = args.work_dir or os.path.dirname(os.path.abspath(args.output_file))
    os.makedirs(work, exist_ok=True)

    cur_file = os.path.join(work, 'loop_round0.kicad_pcb')
    shutil.copy(args.input_file, cur_file)

    print("Round 0: routing initial placement...")
    best = run_route(cur_file, os.path.join(work, 'loop_round0_routed.kicad_pcb'),
                     args.route_args, os.path.join(work, 'loop_round0_route.log'))
    print(f"  failures={best['failures']} iterations={best['iterations']:,}"
          f" vias={best['vias']}")

    max_disp = args.max_displacement
    # The swap cap is pinned to the BASE displacement and never widened (#458).
    # A rejected round widens the NUDGE radius, a local search around each
    # part's own seed that a real re-route then validates. Widening the swap
    # cap in lockstep turns a 3mm budget into a 15mm teleport after four
    # rejections, which is the #430 stranding failure coming back through the
    # loop. Swaps get one fixed budget for the whole run.
    swap_cap = (args.max_displacement if args.swap_max_displacement is None
                else args.swap_max_displacement)

    for rnd in range(1, args.rounds + 1):
        if best['failures'] == 0:
            print("No failures left - stopping.")
            break

        pcb_data = parse_kicad_pcb(cur_file)
        targets = nets_to_refs(pcb_data,
                               best['failed_nets'] + best['blockers'],
                               args.max_target_pins, args.lock)
        if not targets:
            print("No movable target parts - stopping.")
            break

        name_to_id = {net.name: nid for nid, net in pcb_data.nets.items()}
        net_weights = {name_to_id[n]: args.failed_net_weight
                       for n in best['failed_nets'] if n in name_to_id}

        print(f"Round {rnd}: failed={best['failed_nets']}")
        print(f"  blockers={best['blockers'][:8]}"
              f"{'...' if len(best['blockers']) > 8 else ''}")
        print(f"  targeting {len(targets)} parts"
              f" (max_disp={max_disp:.1f}mm, swap cap={swap_cap:.1f}mm):"
              f" {', '.join(sorted(targets))}")

        placements = quench(
            pcb_data, pcb_file=cur_file,
            max_displacement=max_disp,
            swap_max_displacement=swap_cap,
            step=args.step,
            grid_step=args.grid_step, clearance=args.clearance,
            board_edge_clearance=args.board_edge_clearance,
            crossing_penalty=args.crossing_penalty,
            length_weight=args.length_weight,
            halo_base=args.halo_base, halo_coef=args.halo_coef,
            halo_weight=args.halo_weight,
            edge_halo=args.edge_halo, edge_weight=args.edge_weight,
            allow_rotations=not args.no_rotate,
            allow_swaps=not args.no_swap,
            ignore_nets=args.ignore_nets, lock_refs=args.lock,
            move_refs=targets, net_weights=net_weights,
            verbose=args.verbose,
        )

        if not placements:
            print(f"  Quench found no improving moves - widening the nudge cap"
                  f" (swap cap stays {swap_cap:.1f}mm).")
            max_disp *= 1.5
            continue

        cand_file = os.path.join(work, f'loop_round{rnd}.kicad_pcb')
        write_placed_output(cur_file, cand_file, placements)

        metrics = run_route(
            cand_file, os.path.join(work, f'loop_round{rnd}_routed.kicad_pcb'),
            args.route_args, os.path.join(work, f'loop_round{rnd}_route.log'))
        print(f"  -> failures={metrics['failures']}"
              f" iterations={metrics['iterations']:,} vias={metrics['vias']}")

        if better(metrics, best):
            print(f"  ACCEPTED (was failures={best['failures']},"
                  f" iterations={best['iterations']:,})")
            best = metrics
            cur_file = cand_file
            max_disp = args.max_displacement
        else:
            print(f"  REJECTED - reverting, widening the nudge cap"
                  f" (swap cap stays {swap_cap:.1f}mm).")
            max_disp *= 1.5

    shutil.copy(cur_file, args.output_file)
    print(f"Final: failures={best['failures']} iterations={best['iterations']:,}"
          f" vias={best['vias']}")
    print(f"Wrote {args.output_file}")


if __name__ == "__main__":
    main()
