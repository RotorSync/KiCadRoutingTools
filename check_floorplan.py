#!/usr/bin/env python3
"""Grade a board against a declared floorplan intent (#549).

Placement is judged here by `crossings` and `hpwl`, and those are indifferent
between a sensible layout and a scattered one with the same wirelength. Nothing
declares where parts BELONG, so nothing can check whether they went there, and a
render only moves the judgement from a number to a vibe.

This makes the intent falsifiable. Write what the floorplan is meant to be, and
this measures the board against it and exits non-zero with the number that broke.

    # start from what the board already is, then edit it
    python3 check_floorplan.py board.kicad_pcb --emit-intent floorplan.json

    # grade
    python3 check_floorplan.py board.kicad_pcb --intent floorplan.json
    python3 check_floorplan.py board.kicad_pcb --intent floorplan.json --json out.json

THE BOARD OUTLINE IS NOT EDITABLE BY THIS TOOLCHAIN. `envelope` is READ from the
board; a part outside it is a finding about the PART. Board size, cutouts and
slots are mechanical decisions -- enclosure fit, panel rails, connector
apertures -- that belong to the user. The honest response to a board that is
genuinely too small is to say so with the measured number and stop, never to
resize it. Nothing here writes Edge.Cuts.

Exit codes:

    0   graded, no error-severity violations (or --exit-zero)
    1   crash
    2   argparse, including an unreadable or malformed intent file
    3   the board is not in a state this tool can work on (unplaced / no
        trustworthy outline) -- the placement-family convention
    4   graded successfully, error-severity violations found

4 rather than 1 because 3 is already taken and 1 is ambiguous with a crash: a
caller has to be able to tell "the floorplan is wrong" from "the grader is
broken", and that distinction is the entire product. (`check_drc.py` uses 1 for
its violations; this diverges knowingly.)
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from kicad_parser import parse_kicad_pcb
from placement.cli_gates import add_board_state_args
from placement.floorplan import (UntrustworthyOutline, emit_intent, format_text,
                                 grade, load_intent, summary, to_json)
from placement.placement_state import UNPLACED_EXIT, gate_or_exit
from placement.groups import GroupError, parse_sources
from placement.floorplan import IntentError

VIOLATIONS_EXIT = 4


def build_parser():
    p = argparse.ArgumentParser(
        description="Grade a board against a declared floorplan intent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split('Exit codes:')[0].strip())
    p.add_argument('board', help='the .kicad_pcb to grade')
    p.add_argument('--intent', metavar='PATH',
                   help='the floorplan intent JSON to grade against')
    p.add_argument('--emit-intent', metavar='PATH',
                   help='write a starter intent READ OFF this board and exit. '
                        'It grades clean by construction -- a baseline to '
                        'tighten, with the real block names filled in')
    p.add_argument('--json', metavar='PATH',
                   help='write the full findings (every measurement) as JSON')
    p.add_argument('--group-by', default='auto', metavar='SOURCES',
                   help="how blocks are derived when an intent block names a "
                        "`group`: comma list of kicad,sheet,netprefix,decap; "
                        "'auto' = kicad,sheet (default); 'none' to disable. "
                        "Deriving is read-only here, which is why this defaults "
                        "ON unlike on the placement CLIs")
    p.add_argument('--clearance', type=float, metavar='MM',
                   help='clearance the legality model grades at (default: the '
                        'routing default). Pass the floor the board was routed '
                        'to, or halo and overlap are graded at the wrong gap')
    p.add_argument('--board-edge-clearance', type=float, metavar='MM',
                   help='board-edge clearance for the outline gate '
                        '(default: 0.55)')
    p.add_argument('--exit-zero', action='store_true',
                   help='report violations but exit 0 anyway')
    p.add_argument('-q', '--quiet', action='store_true',
                   help='suppress the text report; keep JSON_SUMMARY')
    add_board_state_args(p)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    parser_error = build_parser().error

    if not args.intent and not args.emit_intent:
        parser_error('one of --intent or --emit-intent is required')
    if not os.path.exists(args.board):
        parser_error(f"{args.board}: no such file")

    try:
        sources = parse_sources(args.group_by)
    except GroupError as exc:
        parser_error(str(exc))

    pcb = parse_kicad_pcb(args.board)

    # The unplaced gate first: grading a pile of parts at the origin fires every
    # rule at once, which is noise rather than a verdict.
    gate_or_exit(pcb, args.board, 'check_floorplan',
                 allow_unplaced=args.allow_unplaced,
                 allow_routed=True)          # copper is irrelevant to a floorplan

    if args.emit_intent:
        try:
            doc = emit_intent(pcb, args.board, group_sources=sources or ())
        except UntrustworthyOutline as exc:
            print(f"ERROR: {args.board}: {exc}", file=sys.stderr)
            return UNPLACED_EXIT
        with open(args.emit_intent, 'w', encoding='utf-8') as fh:
            json.dump(doc, fh, indent=1, sort_keys=True)
            fh.write('\n')
        if not args.quiet:
            zoned = sum(1 for b in doc['blocks'] if 'zone' in b)
            print(f"Wrote {args.emit_intent}")
            print(f"  {len(doc['blocks'])} block(s), {zoned} with a zone, "
                  f"{len(doc['edge_connectors'])} edge connector(s), "
                  f"{len(doc['must_lock'])} locked part(s)")
            print(f"  envelope {doc['envelope']['rect']} -- read from the "
                  f"board. The outline is not editable by this toolchain")
        return 0

    try:
        intent = load_intent(args.intent)
    except IntentError as exc:
        parser_error(str(exc))

    try:
        result = grade(intent, pcb, args.board, group_sources=sources or (),
                       clearance=args.clearance,
                       board_edge_clearance=args.board_edge_clearance)
    except UntrustworthyOutline as exc:
        print(f"ERROR: {args.board}: {exc}", file=sys.stderr)
        print("  Refused rather than graded: with no usable outline every "
              "containment check silently degrades to the bounding box, and a "
              "clean report would mean 'stopped checking'.", file=sys.stderr)
        return UNPLACED_EXIT

    if not args.quiet:
        print(format_text(result))

    if args.json:
        with open(args.json, 'w', encoding='utf-8') as fh:
            json.dump(to_json(result), fh, indent=1, sort_keys=True)
            fh.write('\n')
        if not args.quiet:
            print(f"  wrote {args.json}")

    print("JSON_SUMMARY: " + json.dumps(summary(result), sort_keys=True))
    if result.errors and not args.exit_zero:
        return VIOLATIONS_EXIT
    return 0


if __name__ == '__main__':
    sys.exit(main())
