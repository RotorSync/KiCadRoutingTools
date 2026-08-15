#!/usr/bin/env python3
"""Headless DEFAULTS-parity gate: no CLI argparse default may silently disagree
with the shared routing_defaults constant it shadows.

This is the Class-3 half of CLI/GUI parity (CLAUDE.md: "A changed default must
match in both places -- the GUI sets its own values from UI controls and does
not inherit argparse defaults"). The GUI initializes its controls from
`routing_defaults`, so a CLI flag carrying a stale LITERAL routes at a different
value than the GUI does, with nothing to announce it.

It has already happened twice, and neither the reviewer nor the parity audit
caught it, because nothing compared the two:

  1af3096 "PROXIMITY_HEURISTIC_FACTOR 0.02 -> 0" changed routing_defaults and
    the batch_route() SIGNATURE default -- but not the argparse default, which
    passes an explicit 0.02 and therefore OVERRIDES the signature. route.py and
    route_diff.py kept routing at 0.02 while the GUI moved to 0.0.
  c3ec3c3 "VIA_COST 50 -> 75" converted route_diff.py's --heuristic-weight but
    missed the adjacent --proximity-heuristic-factor and the --via-cost above it.

The symptom was gate 4 (test_gui_engine_parity) reporting a small copper-set
divergence -- the CLI placing an extra via and two fewer segments than the GUI on
the same board. Fixing the defaults made the copper sets identical again.

Needs NEITHER wx NOR pcbnew.

Run:  python3 tests/gui_parity/test_cli_defaults_parity.py
Exit code 1 on any disagreement.
"""
import os
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "py_router"))
import routing_defaults as D  # noqa: E402

CLIS = [
    "py_router/route.py",
    "py_router/route_diff.py",
    "py_router/route_planes.py",
    "py_router/repair_planes.py",
    "py_router/bga_fanout/__init__.py",
    "py_router/qfn_fanout/__init__.py",
]

# A flag resolves to a tool-scoped constant first, then the generic one: the
# fanout tools deliberately run tighter geometry than the signal router, and
# `BGA_CLEARANCE` is a different knob from `CLEARANCE`.
TOOL_PREFIX = {
    "bga_fanout/__init__.py": "BGA_",
    "qfn_fanout/__init__.py": "QFN_",
}

# Literals with NO shared constant to track. Each needs a reason; this is not a
# place to silence a real disagreement.
EXEMPT = {
    ("qfn_fanout/__init__.py", "--via-size"): "qfn-local underpad escape geometry; no QFN_VIA_SIZE constant",
    ("qfn_fanout/__init__.py", "--via-drill"): "qfn-local underpad escape geometry; no QFN_VIA_DRILL constant",
}


def add_argument_statements(src):
    """Yield (flag, full statement text) for each add_argument call."""
    for m in re.finditer(r'add_argument\(', src):
        depth, i = 0, m.end() - 1
        while i < len(src):
            if src[i] == '(':
                depth += 1
            elif src[i] == ')':
                depth -= 1
                if depth == 0:
                    break
            i += 1
        stmt = src[m.start():i + 1]
        fm = re.search(r'["\'](--[a-z0-9-]+)["\']', stmt)
        if fm:
            yield fm.group(1), stmt


def resolve_constant(rel, flag):
    """The routing_defaults constant a flag shadows, tool-scoped first."""
    base = flag.lstrip('-').replace('-', '_').upper()
    for suffix, prefix in TOOL_PREFIX.items():
        if rel.endswith(suffix) and hasattr(D, prefix + base):
            return prefix + base
    return base if hasattr(D, base) else None


def main():
    failures, latent, checked = [], [], 0

    for rel in CLIS:
        path = REPO / rel
        if not path.exists():
            failures.append(f"{rel}: MISSING (update this gate's CLI list)")
            continue
        src = path.read_text()
        for flag, stmt in add_argument_statements(src):
            dm = re.search(r'default\s*=\s*([^,\)]+)', stmt)
            if not dm:
                continue
            dexpr = dm.group(1).strip()
            # Correctly sourced from the shared module -- cannot drift.
            if dexpr.startswith(('defaults.', 'routing_defaults.')):
                checked += 1
                continue
            const = resolve_constant(rel, flag)
            if not const:
                continue
            try:
                got = eval(dexpr, {"__builtins__": {}}, {})
            except Exception:
                continue
            # `None` is the deliberate #439 sentinel: omitted -> resolve the
            # value from the board's own netclass / constraints, not from a
            # default. Not a drift candidate.
            if got is None:
                continue
            key = (rel.split('py_router/')[-1], flag)
            if key in EXEMPT:
                continue
            checked += 1
            want = getattr(D, const)
            if got != want:
                failures.append(
                    f"{rel}  {flag}: argparse default {got!r} DISAGREES with "
                    f"routing_defaults.{const} = {want!r}. The GUI initializes "
                    f"its control from the constant, so the two fronts route "
                    f"differently. Fix: default=defaults.{const}")
            else:
                latent.append(f"{rel}  {flag} = {got!r} (== {const}; "
                              f"reference the constant so it cannot drift)")

    print(f"CLI defaults parity: {checked} default(s) checked across "
          f"{len(CLIS)} CLI(s), {len(failures)} disagreement(s).")
    if latent:
        print(f"\n  {len(latent)} literal(s) that currently AGREE with their constant "
              f"(latent traps, not failures):")
        for row in latent:
            print(f"    {row}")
    if failures:
        print(f"\nFAILURES ({len(failures)}):")
        for f in failures:
            print(f"  {f}")
        print("\nVERDICT: CLI/GUI DEFAULTS DIVERGE")
        return 1
    print("\nVERDICT: no CLI default disagrees with its shared constant")
    return 0


if __name__ == '__main__':
    sys.exit(main())
