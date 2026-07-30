"""Protected nets: routed invariants that later chain steps must not rip (#521).

Length matching and coupled diff-pair routing produce copper whose VALUE is not
just connectivity -- meander trains and coupled P/N geometry embody an
invariant a later generic step cannot reproduce. The allwinner_h3_ddr3 chain
showed the failure: a retry step ran ``--rip-existing-nets '/DDR3 16x1/*'``
with no ``--length-match-group``, ripped the whole matched group, rerouted a
subset at natural length, and left the "matched" board 40/41 nets unmatched
(and one net stranded entirely).

The protection list lives in the sibling ``.kicad_pro`` under a tool-namespaced
key, exactly like the DRC-floor writeback: ``fix_project_for_output`` copies the
input project to each step's output, so protection flows down the chain for
free, and ``copy_board.py`` carries it as a sibling.

    {"kicad_routing_tools": {"protected_nets": {"/DDR3 16x1/SA7": "length-matched", ...}}}

**Policy** (no CLI flag, no GUI control -- deliberate, like the #498 .kicad_dru
auto-read): protection guards against COLLATERAL damage. A net named EXACTLY
(no glob metacharacters) in ``--nets`` or ``--rip-existing-nets`` is being
deliberately targeted and stays rippable; glob matches over protected nets are
filtered with a printed exclusion. Consumers:

  * route.py's ``--rip-existing-nets`` expansion (collateral rip of non-target
    nets) skips protected nets unless exactly named.
  * route_disconnected_planes' ``--rip-blocker-nets`` tap rip never selects a
    protected net as a blocker.

**Writers** (engine-side, so the GUI/AI-plan path inherits them): length/time
match groups mark their members 'length-matched'/'time-matched';
batch_route_diff_pairs marks routed coupled pair members 'diff-pair'. Writers
call ``note_protection_candidates``; the per-step persistence happens next to
the DRC-floor writeback (CLI mains, ai_plan executor) via
``consume_protection_candidates`` + ``persist_protected_nets``.
"""
import json
import os
from typing import Dict, Iterable, List, Optional, Set

PRO_NAMESPACE = "kicad_routing_tools"
PRO_KEY = "protected_nets"

_GLOB_CHARS = set('*?[')

# Process-local accumulator: engines note candidates while routing; the
# writeback site (which knows the output project path) consumes them. One
# routing step runs at a time in both fronts (CLI process / GUI plan step).
_candidates: Dict[str, str] = {}


def note_protection_candidates(mapping: Dict[str, str]) -> None:
    """Record nets this step made protection-worthy (net name -> reason)."""
    _candidates.update({k: v for k, v in mapping.items() if k})


def consume_protection_candidates() -> Dict[str, str]:
    """Return and clear the accumulated candidates (call once per step)."""
    global _candidates
    out, _candidates = _candidates, {}
    return out


def pro_path_for_board(board_path: str) -> str:
    return os.path.splitext(board_path)[0] + '.kicad_pro'


def read_protected_nets(pro_path: str) -> Dict[str, str]:
    """Protection map from a .kicad_pro ({} when absent/unreadable)."""
    try:
        if not pro_path or not os.path.isfile(pro_path):
            return {}
        with open(pro_path, 'r', encoding='utf-8') as f:
            proj = json.load(f)
        m = (proj.get(PRO_NAMESPACE) or {}).get(PRO_KEY) or {}
        return {str(k): str(v) for k, v in m.items()} if isinstance(m, dict) else {}
    except Exception:
        return {}


def read_for_pcb_data(pcb_data, input_file: Optional[str] = None) -> Dict[str, str]:
    """Protection map for the board an engine is working on. ``input_file``
    when the caller has one; engines without it (GUI builds PCBData from the
    live board) discover the board file via PCBData.source_path (#498)."""
    path = input_file or getattr(pcb_data, 'source_path', "") or ""
    if not path:
        return {}
    return read_protected_nets(pro_path_for_board(path))


def persist_protected_nets(pro_path: str, mapping: Dict[str, str],
                           verbose: bool = True) -> bool:
    """Merge ``mapping`` into the project's protection list. Preserves every
    other key (plain json round-trip, same style as fix_kicad_drc_settings).
    No-op when the project file does not exist (the DRC writeback creates it
    first; without a project there is nothing for later steps to read)."""
    if not mapping or not pro_path or not os.path.isfile(pro_path):
        return False
    try:
        with open(pro_path, 'r', encoding='utf-8') as f:
            proj = json.load(f)
        ns = proj.setdefault(PRO_NAMESPACE, {})
        current = ns.get(PRO_KEY) or {}
        merged = dict(current)
        merged.update(mapping)
        if merged == current:
            return False
        ns[PRO_KEY] = merged
        with open(pro_path, 'w', encoding='utf-8') as f:
            json.dump(proj, f, indent=2)
        if verbose:
            added = len(merged) - len(current)
            print(f"  Protected nets: {len(merged)} recorded in {os.path.basename(pro_path)}"
                  f" (+{added} this step) -- later steps will not rip them"
                  f" unless named exactly")
        return True
    except Exception as e:
        if verbose:
            print(f"  (skipped protected-nets record: {e})")
        return False


def exact_names(patterns: Optional[Iterable[str]]) -> Set[str]:
    """The non-glob entries of a pattern list: naming a net exactly is the
    deliberate-override signal that lifts its protection for this step."""
    if not patterns:
        return set()
    return {p for p in patterns if p and not (_GLOB_CHARS & set(p))}


def filter_rippable_names(names: List[str], protected: Dict[str, str],
                          override_patterns: Optional[Iterable[str]] = None,
                          context: str = "rip-up") -> List[str]:
    """Drop protected names (minus exact-name overrides), printing what was
    excluded and why. Returns the surviving names in input order."""
    if not protected:
        return list(names)
    overrides = exact_names(override_patterns)
    kept, blocked = [], []
    for n in names:
        if n in protected and n not in overrides:
            blocked.append(n)
        else:
            kept.append(n)
    if blocked:
        by_reason: Dict[str, List[str]] = {}
        for n in blocked:
            by_reason.setdefault(protected[n], []).append(n)
        det = '; '.join(f"{r}: {', '.join(ns[:4])}{'...' if len(ns) > 4 else ''}"
                        for r, ns in sorted(by_reason.items()))
        print(f"  {len(blocked)} PROTECTED net(s) excluded from {context} ({det})"
              f" -- name a net exactly (no glob) to override")
    return kept
