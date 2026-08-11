#!/usr/bin/env python3
"""Unit gate for the shared component->nets helper (issue #537).

Before this, "the nets of component X" had four independent implementations
(route.py, fanout_gui.py, ai_plan.py, check_connected.py) that disagreed about
whether 'U1' also means U10/U12/U100 -- so the same request selected different
nets on the CLI, in the GUI, and in a replayed plan. These assert the one
implementation they now share, and in particular that each front's MATCHING
POLICY is an explicit argument rather than an accident of which loop it sits in.

Run:  python3 tests/test_component_net_selection.py
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, 'py_router'))

from kicad_parser import parse_kicad_pcb  # noqa: E402
from net_queries import (  # noqa: E402
    component_ref_matches, nets_for_components, suggest_component_refs)

BOARD = os.path.join(REPO, 'kicad_files', 'splitflap_driver.kicad_pcb')

_failures = []


def check(cond, label):
    if cond:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}")
        _failures.append(label)


def test_ref_matching():
    print("\n-- reference matching policy --")
    # The whole point of the issue: a bare token means different things per front.
    check(component_ref_matches('U10', 'U1', match='substring'),
          "substring: 'U1' matches U10 (GUI's live-narrowing behaviour)")
    check(not component_ref_matches('U10', 'U1', match='glob'),
          "glob: 'U1' does NOT match U10 (CLI: a filter decides what copper lands)")
    check(component_ref_matches('U1', 'U1', match='glob'), "glob: exact hit")
    # A glob metacharacter is honoured under BOTH policies -- that is what makes
    # 'substring' a superset of the old GUI behaviour rather than a change to it.
    check(component_ref_matches('U10', 'U1*', match='glob'), "glob: 'U1*' matches U10")
    check(component_ref_matches('U10', 'U1*', match='substring'),
          "substring: glob token still globs")
    check(component_ref_matches('u1', 'U1', match='glob'), "case-insensitive")
    check(not component_ref_matches('', 'U1'), "empty ref never matches")
    check(not component_ref_matches('U1', ''), "empty pattern never matches")


def test_modes(pcb):
    print("\n-- modes --")
    refs = sorted(pcb.footprints)
    # Pick two footprints that actually share a net, so 'between' is non-vacuous.
    pair = None
    for net_id, pads in pcb.pads_by_net.items():
        if net_id <= 0:
            continue
        owners = sorted({p.component_ref for p in pads if p.component_ref})
        if len(owners) >= 2:
            pair = owners[:2]
            break
    assert pair, "fixture board has no net spanning two footprints"
    a, b = pair

    any_sel = nets_for_components(pcb, [a, b], mode='any')
    between = nets_for_components(pcb, [a, b], mode='between')
    internal = nets_for_components(pcb, [a, b], mode='internal')

    check(set(between.net_names) <= set(any_sel.net_names), "'between' subsets 'any'")
    check(set(internal.net_names) <= set(any_sel.net_names), "'internal' subsets 'any'")
    check(len(between.net_names) >= 1, f"'between' {a}+{b} is non-empty")

    # 'between' must mean DISTINCT footprints, not ">=2 matched pads" -- two pads
    # of one net on a single selected part run between nothing.
    solo = nets_for_components(pcb, [a], mode='between')
    check(not solo.net_names,
          f"'between' with one footprint ({a}) selects nothing, even for multi-pad nets")

    # 'internal' means every pad is on a matched footprint. Two checks, so this
    # cannot pass vacuously on a board where the sampled pair shares no net:
    # (1) selecting EVERY footprint makes 'internal' equal 'any' by definition,
    # (2) whatever 'internal' does return has all its pads inside the selection.
    allrefs = sorted(pcb.footprints)
    check(nets_for_components(pcb, allrefs, mode='internal').net_names
          == nets_for_components(pcb, allrefs, mode='any').net_names,
          "'internal' over ALL footprints == 'any' (no net escapes the board)")

    name_to_id = {}
    for net_id, pads in pcb.pads_by_net.items():
        net = pcb.nets.get(net_id)
        if net and net.name:
            name_to_id[net.name] = net_id
    bad = [n for n in internal.net_names
           if not all(p.component_ref in (a, b)
                      for p in pcb.pads_by_net.get(name_to_id.get(n), []))]
    check(not bad, f"'internal' {a}+{b}: every pad of every selected net is in scope "
                   f"({len(internal.net_names)} nets checked)")


def test_unmatched_and_suggestions(pcb):
    print("\n-- unmatched patterns --")
    refs = sorted(pcb.footprints)
    real = refs[0]
    bogus = 'ZZ999'
    sel = nets_for_components(pcb, [real, bogus])
    check(sel.unmatched_patterns == [bogus],
          f"typo'd ref {bogus!r} reported as unmatched, not silently ignored")
    check(real in sel.matched_refs, "the real ref still matched")
    check(not nets_for_components(pcb, []).net_names, "empty pattern list -> empty")

    # A near-miss should suggest the real reference.
    near = real + '9' if not real.endswith('9') else real[:-1]
    hint = suggest_component_refs(refs, near)
    check(hint == "" or real in hint, f"suggestion for {near!r}: {hint or '(none)'}")


def test_exclusions(pcb):
    print("\n-- power exclusion --")
    from routing_constants import POWER_NET_EXCLUSION_PATTERNS
    # Use every footprint, so power rails are certainly in scope.
    allrefs = sorted(pcb.footprints)
    plain = nets_for_components(pcb, allrefs)
    dropped = nets_for_components(pcb, allrefs,
                                  exclude_patterns=POWER_NET_EXCLUSION_PATTERNS)
    check(set(dropped.net_names) <= set(plain.net_names), "exclusion only removes")
    check(set(dropped.excluded_names) == set(plain.net_names) - set(dropped.net_names),
          "excluded_names accounts for exactly what was removed")
    check(all(not n.upper().endswith('GND') and 'GND' not in n.upper()
              for n in dropped.net_names), "no GND net survives the power drop")
    check(bool(dropped.excluded_names), "the drop is reportable, not silent")


def test_front_agreement(pcb):
    """The regression the issue is really about: CLI and GUI, same input."""
    print("\n-- CLI/GUI agreement on an exact reference --")
    for ref in sorted(pcb.footprints)[:5]:
        cli = nets_for_components(pcb, [ref], match='glob')
        gui = nets_for_components(pcb, [ref], match='substring')
        # They may legitimately differ when the ref is a prefix of another ref
        # (that is the substring policy doing its job); otherwise they must agree.
        prefix_of_other = any(r != ref and ref.upper() in r.upper()
                              for r in pcb.footprints)
        if prefix_of_other:
            check(set(cli.net_names) <= set(gui.net_names),
                  f"{ref}: substring is a superset (other refs contain it)")
        else:
            check(cli.net_names == gui.net_names,
                  f"{ref}: CLI and GUI select the identical net set")


def main():
    if not os.path.exists(BOARD):
        print(f"missing fixture board: {BOARD}")
        return 1
    pcb = parse_kicad_pcb(BOARD)
    print(f"board: {os.path.basename(BOARD)} "
          f"({len(pcb.footprints)} footprints, {len(pcb.nets)} nets)")
    test_ref_matching()
    test_modes(pcb)
    test_unmatched_and_suggestions(pcb)
    test_exclusions(pcb)
    test_front_agreement(pcb)
    print()
    if _failures:
        print(f"FAILED ({len(_failures)}): " + "; ".join(_failures))
        return 1
    print("ALL PASS")
    return 0


if __name__ == '__main__':
    sys.exit(main())
