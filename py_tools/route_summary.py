#!/usr/bin/env python3
"""One honest tally from route.py's one-or-two JSON_SUMMARY emissions.

route.py runs an end-of-run reconciliation pass exactly when the first pass left
failures. It self-invokes batch_route one level deep on the written board and
prints a SECOND JSON_SUMMARY, scoped to the retried nets, whose failure lists
route.py itself calls "the honest still-open set". Reading only the first summary
counts every reconciliation recovery as a failure.

This module owns that reduction, in two forms:

    merge_summaries(summaries, aborted)  -- from dicts, for an in-process caller
    merge_route_summaries(log)           -- from a log, for a subprocess caller

`route.py --json-out` uses the first (it has the dicts and knows whether the
reconciliation raised, so it needs neither a regex nor a marker string).
`place_route_loop.py` uses the second, because it only ever sees a log file.
Both were the same 110 lines in two places until they were not.
"""
import _path  # noqa: F401  (py_tools -> py_router/py_placer on sys.path)
import json
import re
from typing import Dict, List, Optional

__all__ = ['merge_summaries', 'merge_route_summaries',
           'SUMMARY_RE', 'RECONCILE_ABORTED', 'EFFORT_KEYS']

SUMMARY_RE = re.compile(r'JSON_SUMMARY: (\{.*\})')

# route.py prints this from the except around its reconciliation self-invoke.
# The sub-run prints its JSON_SUMMARY BEFORE the board is written, so a summary
# followed by this marker advertises recoveries that may never have reached the
# file on disk.
RECONCILE_ABORTED = 'final reconciliation pass failed:'

# Counters that measure WORK DONE, so they add across passes. Everything else in
# a summary is state, and state is whatever the LAST pass measured.
EFFORT_KEYS = ('total_iterations', 'total_vias', 'total_time')


def merge_summaries(summaries: List[Dict], aborted: bool = False) -> Optional[Dict]:
    """Reduce one-or-more summary dicts to a single tally.

    Per field class:

    * FAILURE STATE (failed_single / open_single / failed_multipoint /
      multipoint_pads_*) comes from the LAST summary, and is exact rather
      than a delta. Every net
      with a nonzero failure term is in the retry set by construction, and the
      sub-run re-derives each retried net's pad counts over ALL of that net's
      pads from the final-board union-find, so the last summary's numbers are
      absolute. Summing pad counts would double-count.
    * EFFORT is SUMMED: both passes are work this run cost the router, and an
      iteration tiebreak should see all of it. Taking effort from the last
      summary would make a badly failing candidate look cheap, since the
      reconciliation pass only re-routes a handful of nets.
    * The pad-pair keys are REBUILT, because they are whole-board in the first
      summary but reconcile-subset-scoped in the sub-run's.
    * Anything else is last-wins.

    `aborted` means the reconciliation raised AFTER printing its summary: it
    claims recoveries the board write may never have committed, so fall back to
    the first pass, which is what is definitely on disk.

    Degrades to the single-summary case unchanged. Returns None for an empty
    list.
    """
    if not summaries:
        return None
    merged = dict(summaries[0] if aborted else summaries[-1])

    for key in EFFORT_KEYS:
        merged[key] = sum(s.get(key, 0) for s in summaries)

    # Rebuild the pad-pair tallies and the blockers key, which last-wins would
    # silently narrow to the reconcile subset (a 50/40 whole-board reading
    # becomes the sub-run's 3/2, or vanishes entirely). Skipped when aborted:
    # merged is already pass 1 wholesale, which is what is on disk.
    if len(summaries) > 1 and not aborted:
        first, last = summaries[0], summaries[-1]
        if 'pad_pairs_total' in first:
            if 'pad_pairs_total' in last:
                # Denominator: pass 1's whole board. Connected: that total minus
                # what is STILL open at end of run. A net the reconciliation
                # itself broke that pass 1 never graded subtracts its deficit
                # without widening the denominator -- conservative, same spirit
                # as the coverage-gate widening below.
                _deficit = sum(
                    e.get('pairs_total', 0) - e.get('pairs_connected', 0)
                    for e in (last.get('pad_pairs_open') or []))
                _total = first['pad_pairs_total']
                merged['pad_pairs_total'] = _total
                merged['pad_pairs_connected'] = max(0, _total - _deficit)
            else:
                # The sub-run printed a summary without pad-pair keys (its
                # emission is defensively try/except'd): pass 1's numbers are
                # the newest that exist.
                merged['pad_pairs_total'] = first['pad_pairs_total']
                merged['pad_pairs_connected'] = first.get('pad_pairs_connected', 0)
                if 'pad_pairs_open' in first:
                    merged['pad_pairs_open'] = first['pad_pairs_open']
        if 'blockers' in first and 'blockers' not in merged:
            _failed = set(merged.get('failed_single') or [])
            _failed |= {d.get('net_name') if isinstance(d, dict) else d
                        for d in (merged.get('failed_multipoint') or [])}
            merged['blockers'] = [e for e in first['blockers']
                                  if e.get('net') in _failed]

    # Coverage-gate nets have NO routed result, so their pads never reach
    # multipoint_pads_total and a caller's
    # failures = len(failed_single) + pad-deficit weighs them ZERO, though they
    # ship at broken copper. Give each one weight 1, matching what failed_single
    # gives a net that produced no result at all, by widening the pad
    # denominator. They are in neither failed_single nor the pad tallies, so
    # this cannot double-count. It matters most on the LAST summary: those are
    # nets the reconciliation pass ITSELF broke through its rip escalation, and
    # without this a loop can read failures=0 on a board shipping disconnected
    # copper and stop.
    gate = merged.get('coverage_gate_nets') or []
    if gate:
        merged['multipoint_pads_total'] = (
            merged.get('multipoint_pads_total', 0) + len(gate))
    return merged


def merge_route_summaries(log: str) -> Optional[Dict]:
    """`merge_summaries` for a caller that only has the log text.

    Returns None when the log carries no summary at all.
    """
    raw = SUMMARY_RE.findall(log)
    if not raw:
        return None
    summaries = [json.loads(s) for s in raw]
    aborted = log.rfind(RECONCILE_ABORTED) > log.rfind(raw[-1])
    return merge_summaries(summaries, aborted)
