#!/usr/bin/env python3
"""Guards against results that depend on PROCESS HISTORY rather than inputs.

The GUI runs many routing steps in ONE long-lived process; the CLI runs each
step in a fresh one. Anything that leaks across steps -- a set iteration order,
an id()-keyed cache -- makes the GUI disagree with the CLI, and makes the GUI
disagree with ITSELF between launches. Two such bugs have already shipped:

  * `get_selected_nets()` returned `list(self._checked_nets)`. Set-of-strings
    order is randomized per process (PYTHONHASHSEED), the router routes nets in
    the order given, and three runs of one plan produced 3425 / 3433 / 3431
    segments (fixed 2df22ca with sorted()).
  * `local_to_global` composed pad coordinates in float mm while pcbnew used
    integer nm, so the two fronts' A* tie-breaks diverged (fixed 78f1731).

This file pins the remaining cases found while chasing eth_tap step 11.

Rules enforced:
  1. Layer-name lists must not come from `list(set(...))`. Layer names are
     STRINGS, so set order varies per process, and these lists become the
     router's `layers=` -- layer order decides which layer is tried first.
     Use `sorted(...)` or `dict.fromkeys(...)` (order-preserving dedupe).
  2. `obstacle_costs._MERGE_MEMO` is keyed on `id()`. Its entries must hold the
     keyed objects alive (so an address cannot be recycled into a stale hit)
     and the memo must be bounded (so keepalive cannot leak).

`list(set(...))` over grid CELLS (tuples of ints) is deliberately NOT flagged:
int hashing is not randomized, so those orders are stable.

Pure Python, no pcbnew/wx. Run: python3 tests/test_process_order_determinism.py
"""
import ast
import os
import sys

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS_DIR)
sys.path.insert(0, ROOT)

failures = []

# Files whose list(set(...)) provably builds LAYER lists. Kept explicit rather
# than pattern-matched: the same idiom over int tuples is fine.
LAYER_ORDER_FILES = ('route_planes.py', 'check_drc.py', 'route.py',
                     'route_disconnected_planes.py', 'obstacle_map.py')


def _list_of_set_calls(path):
    """Return [(lineno, source_segment)] for `list(set(...))` expressions."""
    try:
        src = open(path, encoding='utf-8').read()
    except OSError:
        return []
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    hits = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name) and node.func.id == 'list'
                and len(node.args) == 1
                and isinstance(node.args[0], ast.Call)
                and isinstance(node.args[0].func, ast.Name)
                and node.args[0].func.id == 'set'):
            seg = ast.get_source_segment(src, node) or ''
            hits.append((node.lineno, ' '.join(seg.split())[:120]))
    return hits


def test_no_set_derived_layer_lists():
    print("1. layer lists are not built from list(set(...))")
    bad = []
    for fname in LAYER_ORDER_FILES:
        path = os.path.join(ROOT, fname)
        if not os.path.exists(path):
            continue
        for lineno, seg in _list_of_set_calls(path):
            low = seg.lower()
            if 'layer' in low:
                bad.append(f"{fname}:{lineno}  {seg}")
    if bad:
        for b in bad:
            print(f"  FAIL {b}")
        failures.append(f"{len(bad)} set-derived layer list(s): {bad}")
    else:
        print(f"  ok   none in {len(LAYER_ORDER_FILES)} routing modules")


def test_check_drc_fallback_is_sorted():
    print("2. check_drc's segment-derived layer fallback is deterministic")
    src = open(os.path.join(ROOT, 'check_drc.py'), encoding='utf-8').read()
    if 'routing_layers = sorted(set(seg.layer' in src:
        print("  ok   uses sorted(set(...))")
    else:
        print("  FAIL fallback is not sorted")
        failures.append("check_drc routing_layers fallback is not sorted()")


def test_merge_memo_keepalive_and_bound():
    print("3. obstacle_costs._MERGE_MEMO has keepalive + a bound")
    import obstacle_costs
    if not hasattr(obstacle_costs, '_MERGE_MEMO_MAX'):
        print("  FAIL no _MERGE_MEMO_MAX bound")
        failures.append("_MERGE_MEMO is unbounded")
    else:
        print(f"  ok   bounded at {obstacle_costs._MERGE_MEMO_MAX}")
    src = open(os.path.join(ROOT, 'obstacle_costs.py'), encoding='utf-8').read()
    # The stored tuple must carry the keyed object so its id() cannot be reused.
    if '_MERGE_MEMO[key] = (sig, all_costs, per_net_costs' in src:
        print("  ok   entry pins per_net_costs (and arrays) alive")
    else:
        print("  FAIL entry does not keep the id()-keyed object alive")
        failures.append("_MERGE_MEMO entries lack keepalive refs")


def test_selected_nets_sorted():
    print("4. GUI net selection stays deterministically ordered (2df22ca)")
    path = os.path.join(ROOT, 'kicad_routing_plugin', 'fanout_gui.py')
    src = open(path, encoding='utf-8').read()
    if 'return list(self._checked_nets)' in src:
        print("  FAIL get_selected_nets returns list(set) again")
        failures.append("get_selected_nets returned to list(set(...))")
    elif 'return sorted(self._checked_nets)' in src:
        print("  ok   returns sorted()")
    else:
        print("  WARN could not confirm; check get_selected_nets by hand")


def main():
    for t in (test_no_set_derived_layer_lists,
              test_check_drc_fallback_is_sorted,
              test_merge_memo_keepalive_and_bound,
              test_selected_nets_sorted):
        t()
    print()
    if failures:
        print(f"FAILED ({len(failures)}):")
        for f in failures:
            print("  " + f)
        return 1
    print("PASS: no process-history-dependent ordering or id() aliasing")
    return 0


if __name__ == '__main__':
    sys.exit(main())
