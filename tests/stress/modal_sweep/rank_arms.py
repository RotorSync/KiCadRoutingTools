#!/usr/bin/env python3
"""Rank a sweep's arms by PAIRED connectivity verdict.

    python3 tests/stress/modal_sweep/rank_arms.py <sweep_*.json> [--base ARM]

check_sweep.py answers "is this run trustworthy"; the sweep's own stdout table
prints mean completion % per arm. Neither answers the question a parameter
campaign is actually asking -- which arm finishes the most nets -- and the
obvious ways to compute it are all subtly wrong:

* **Mean completion % is not the verdict.** It weights a 40-net board equally
  with a 900-net one, so an arm that fixes two nets on a tiny board outranks
  one that fixes thirty on a big one.
* **Unpaired sums are not comparable.** Boards drop out per-arm (a chain
  breaks, a task dies), and an arm that happens to lose a hard board scores
  BETTER on the sum of what remains. Every arm is therefore scored only on
  boards where EVERY arm produced a complete chain.
* **A shorter chain grades artificially well** (RUNBOOK rule 3): nets that a
  missing step never attempted are not counted as incomplete. Arms must
  therefore agree on step count, final board, and net census -- see
  `same_chain`, and `gradeable_nets` for why the census has to be RECONSTRUCTED
  before it can be compared at all.

The verdict is `nets_incomplete` -- unrouted plus connectivity-issue nets, and
NOT `nets_incomplete + conn`, which is what modal_app's screened gate scores and
counts every connectivity-issue net twice (see `verdict`). Lower is better. DRC
is reported alongside but NOT folded in: it is a separate gate (a #590 arm that
wins on connectivity while adding real DRC must not be armed), and mixing them
into one number hides exactly that trade.

Read the W/L column, not just the total: per-board run-to-run spread is +-2..3
nets, so a total driven by one or two boards is noise wearing a verdict's
clothes (RUNBOOK rule 5 -- two defaults have been shipped and reverted on it).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def verdict(r: dict):
    """Incomplete nets, or None if this row cannot be scored.

    `nets_incomplete` ALREADY equals unrouted + connectivity-issue nets, so the
    `nets_incomplete + conn` that modal_app's screened gate uses counts every
    connectivity-issue net twice. ab_replay_grade says so where it defines the
    same verdict for its own A/B: grade on total incomplete nets, "NOT the conn
    count alone", because a net that loses its copper entirely LEAVES the conn
    bucket for the unrouted bucket -- conn can fall while the board got worse.
    Double-counting does not just rescale the score: it weights losing a net's
    copper differently from failing to connect it.
    """
    if not r.get("chain_complete"):
        return None
    return r.get("nets_incomplete")


def step_count(r: dict):
    steps = r.get("steps")
    return len(steps) if isinstance(steps, list) else steps


def gradeable_nets(r: dict):
    """Outcome-INDEPENDENT net census for this board.

    Rows carry `nets_total` as reported by check_connected, which counts only
    nets that ended up with copper -- so it shrinks on an arm that fails nets,
    and the same board reports a different total per arm (see ab_replay_grade's
    _completion, which now adds these back at grade time). Reconstruct it here
    too, so rows recorded BEFORE that fix pair correctly: the unrouted nets are
    exactly `nets_incomplete - conn`.
    """
    tot, ni, cn = r.get("nets_total"), r.get("nets_incomplete"), r.get("conn")
    if tot is None or ni is None or cn is None:
        return None
    return tot + max(0, ni - cn)


def same_chain(rows: list) -> str:
    """"" if these arms' rows are comparable, else why they are not.

    Guards the RUNBOOK's rule 3 -- a TRUNCATED chain grades artificially well,
    because nets its missing steps never attempted are never counted incomplete.
    All three signals are exact; none of them is a tolerance, because the thing
    that used to look like tolerable noise (a net total drifting a few counts
    between arms) was the outcome-dependent denominator above, not noise.
    """
    steps = {step_count(r) for r in rows}
    if len(steps) != 1:
        return f"step counts differ: {sorted(steps)}"
    finals = {r.get("final") for r in rows}
    if len(finals) != 1:
        return f"different final board: {sorted(str(f) for f in finals)}"
    census = {gradeable_nets(r) for r in rows}
    if None in census:
        return "no net census"
    if len(census) != 1:
        # Rare and real: a one-pad net counts as routed when it picks up plane
        # copper but never appears among the unrouted, so it cannot be added
        # back. Dropping the board is right -- the arms did grade different net
        # sets -- but it is a checker limitation, not a router difference.
        return f"gradeable net census differs: {sorted(census)}"
    return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("sweep", help="sweep_*.json written by modal_app")
    ap.add_argument("--base", default="",
                    help="control arm (default: the one declaring no overrides)")
    ap.add_argument("--top", type=int, default=8, help="movers to list per arm")
    ap.add_argument("--csv", default="", help="also write per-board verdicts here")
    a = ap.parse_args()

    data = json.loads(Path(a.sweep).read_text())
    rows = data.get("rows") or []
    if not rows:
        print("no rows")
        return 1

    by_arm: dict[str, dict[str, dict]] = {}
    for r in rows:
        if r and r.get("arm") and r.get("board"):
            by_arm.setdefault(r["arm"], {})[r["board"]] = r
    arms = list(by_arm)

    # The control is the arm that overrode nothing -- NOT the one named
    # "baseline". Arms files deliberately avoid that name, because the results
    # volume banks rows by arm name and a prior campaign's "baseline" rows would
    # satisfy the resume check and silently poison the anchor.
    base = a.base
    if not base:
        cands = [n for n in arms
                 if not any(r.get("env_overrides") or r.get("patched_defaults")
                            for r in by_arm[n].values())]
        if len(cands) != 1:
            print(f"cannot identify the control arm (candidates: {cands or 'none'}); "
                  f"pass --base", file=sys.stderr)
            return 2
        base = cands[0]
    if base not in by_arm:
        print(f"no rows for base arm {base!r}; have {arms}", file=sys.stderr)
        return 2

    # Pair: scoreable everywhere, and the SAME chain everywhere.
    all_boards = sorted({b for m in by_arm.values() for b in m})
    paired, dropped, wobble = [], {}, {}
    for b in all_boards:
        got = [(n, by_arm[n].get(b)) for n in arms]
        missing = [n for n, r in got if r is None]
        if missing:
            dropped[b] = f"no row in {len(missing)} arm(s)"
            continue
        unscored = [n for n, r in got if verdict(r) is None]
        if unscored:
            dropped[b] = f"chain incomplete/ungraded in {len(unscored)} arm(s)"
            continue
        why = same_chain([r for _, r in got])
        if why:
            dropped[b] = f"chains differ across arms: {why}"
            continue
        paired.append(b)
        spread = max(r.get("nets_total") or 0 for _, r in got) - \
            min(r.get("nets_total") or 0 for _, r in got)
        if spread:
            wobble[b] = spread

    print(f"{len(arms)} arms, {len(all_boards)} boards seen, "
          f"{len(paired)} PAIRED (scored) / {len(dropped)} dropped")
    if wobble:
        # Expected, and disclosed anyway: check_connected's raw "Checking N
        # routed nets" counts only nets that ended up with copper, so an arm
        # that fails nets reports a smaller one. These boards ARE comparable --
        # they matched on the reconstructed gradeable census -- and the spread
        # is a direct read on how differently the arms performed.
        print(f"    {len(wobble)} paired board(s) differ in ROUTED-net count "
              f"(max {max(wobble.values())}: {max(wobble, key=wobble.get)}) "
              f"-- expected; the gradeable census matched")
    if dropped:
        reasons: dict[str, int] = {}
        for why in dropped.values():
            reasons[why.split(":")[0]] = reasons.get(why.split(":")[0], 0) + 1
        for why, n in sorted(reasons.items(), key=lambda kv: -kv[1]):
            print(f"    {n:3} {why}")
    if not paired:
        print("\nNOTHING PAIRED -- no verdict. Check chain_complete rates first.")
        return 1

    tot = {n: sum(verdict(by_arm[n][b]) for b in paired) for n in arms}
    drc = {n: sum((by_arm[n][b].get("drc") or 0) for b in paired) for n in arms}
    nets = sum(by_arm[base][b].get("nets_total") or 0 for b in paired)

    print(f"\n{len(paired)} paired boards, {nets} nets. "
          f"verdict = incomplete nets (unrouted + connectivity issues; "
          f"LOWER is better)\n")
    print(f"{'arm':20} {'verdict':>8} {'vs base':>8} {'%':>7} "
          f"{'W':>4} {'L':>4} {'T':>4} {'DRC':>7} {'vs base':>8}")
    order = sorted(arms, key=lambda n: (n != base, tot[n]))
    for n in order:
        w = sum(1 for b in paired if verdict(by_arm[n][b]) < verdict(by_arm[base][b]))
        l = sum(1 for b in paired if verdict(by_arm[n][b]) > verdict(by_arm[base][b]))
        t = len(paired) - w - l
        d, dd = tot[n] - tot[base], drc[n] - drc[base]
        pct = (100.0 * d / tot[base]) if tot[base] else 0.0
        tag = "  <- control" if n == base else ""
        print(f"{n:20} {tot[n]:8} {d:+8} {pct:+6.1f}% "
              f"{w:4} {l:4} {t:4} {drc[n]:7} {dd:+8}{tag}")

    for n in order:
        if n == base:
            continue
        mv = sorted(((verdict(by_arm[n][b]) - verdict(by_arm[base][b]), b)
                     for b in paired), key=lambda x: x[0])
        best = [f"{b} {d:+}" for d, b in mv[:a.top] if d < 0]
        worst = [f"{b} {d:+}" for d, b in reversed(mv[-a.top:]) if d > 0]
        print(f"\n{n}: {tot[n] - tot[base]:+} overall")
        print(f"   gains : {', '.join(best) or 'none'}")
        print(f"   losses: {', '.join(worst) or 'none'}")

    if a.csv:
        import csv
        with open(a.csv, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["board", "nets_total"] + [f"{n}_verdict" for n in order]
                       + [f"{n}_drc" for n in order])
            for b in paired:
                w.writerow([b, by_arm[base][b].get("nets_total")]
                           + [verdict(by_arm[n][b]) for n in order]
                           + [by_arm[n][b].get("drc") for n in order])
        print(f"\nper-board verdicts -> {a.csv}")

    print("\nReminder: per-board spread is +-2..3 nets. A total carried by one or "
          "two boards is not a result, and connectivity does not license a "
          "default on its own -- check the DRC column too.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
