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


def add_tidiness_args(parser) -> None:
    """The #548 alignment and orientation cost terms. BOTH OFF by default.

    Off because they are heuristic and aesthetic, and the router -- not a
    tidiness score -- is the judge of a placement. Turning them on by default
    would silently change every user's board to buy legibility, and would also
    corrupt the isolated fixtures in `test_458_*`, which zero every geometry
    knob they know about so the objective is clean enough to assert an exact
    total. At 0.0 both return before touching any geometry, so a default run is
    bit-identical rather than merely close.
    """
    parser.add_argument("--align-weight", type=float, default=0.0,
                        help="Reward same-footprint parts that share an axis, "
                             "so a row of decoupling caps comes out ON a line "
                             "rather than within a few tenths of one. 0 = off "
                             "(default). Try 5")
    parser.add_argument("--align-radius", type=float, default=0.5,
                        metavar="MM",
                        help="Off-axis distance past which two peers are "
                             "simply not a row, so the penalty stops growing "
                             "(default: 0.5mm). The penalty is continuous "
                             "here: a cliff would pay a part to flee the row")
    parser.add_argument("--align-span", type=float, default=20.0,
                        metavar="MM",
                        help="How far apart two same-footprint parts can SEED "
                             "and still be considered peers (default: 20.0mm)")
    parser.add_argument("--orient-weight", type=float, default=0.0,
                        help="Reward a rotation that puts a part's pads on the "
                             "side its nets leave from. 0 = off (default). The "
                             "airwire cost already uses exact pad positions, "
                             "but a ~1mm pad offset is noise against a ~20mm "
                             "net, so the signal needs its own weight. Try 1")
