"""Argparse flags shared by `place_optimize.py` and `place_route_loop.py` (#431).

Defined once so the two CLIs cannot drift: a lock advisor that reports on one
tool but not the other, or an `--allow-unplaced` that spells itself differently,
is exactly the kind of divergence CLAUDE.md's CLI/GUI section warns about --
here between two CLIs rather than between a CLI and the GUI.
"""
from __future__ import annotations


def add_board_state_args(parser) -> None:
    """`--allow-unplaced` / `--allow-routed` overrides for the two gates."""
    parser.add_argument("--allow-unplaced", action="store_true",
                        help="Run even when the board does not look placed "
                             "(parts stacked at one coordinate). Off by default: "
                             "this toolchain REFINES a placement, so on a pile "
                             "every candidate pose is illegal and the run prints "
                             "'0 parts moved' plus a legality block that looks "
                             "like a result")
    parser.add_argument("--allow-routed", action="store_true",
                        help="Run even when the board already carries copper. "
                             "Off by default: placement moves FOOTPRINTS and "
                             "not tracks, so every segment would be left behind "
                             "detached from its pad")


def add_lock_advisor_args(parser) -> None:
    """`--suggest-locks` and friends. Report-only; nothing is ever auto-locked."""
    parser.add_argument("--suggest-locks", action="store_true",
                        help="Report which parts look position-critical "
                             "(mounting holes, board-edge overhang, connectors) "
                             "with a reason each, print a paste-ready --lock "
                             "list, and exit. Writes NO board and locks nothing "
                             "-- a wrong auto-lock silently freezes a part that "
                             "needed to move, and that failure is invisible")
    parser.add_argument("--suggest-locks-json", metavar="PATH",
                        help="With --suggest-locks, also write the findings as "
                             "JSON (every measurement, fired or not)")
    parser.add_argument("--suggest-locks-globs", action="store_true",
                        help="With --suggest-locks, collapse the suggestion to "
                             "globs (J*) instead of exact refs, printing each "
                             "glob's blast radius. Exact refs are the default: "
                             "a glob you did not inspect freezes parts you "
                             "never looked at")
    parser.add_argument("--lock-confidence", default="medium",
                        choices=("high", "medium", "low"),
                        help="Minimum confidence to include in the suggested "
                             "--lock list (default: medium)")
    parser.add_argument("--lock-edge-margin", type=float, default=1.0,
                        metavar="MM",
                        help="Distance from the board edge under which a part "
                             "is flagged as possibly position-critical "
                             "(default: 1.0mm)")
