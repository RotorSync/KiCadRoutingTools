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
IMPEDANCE_KEY = "net_impedance"

_GLOB_CHARS = set('*?[')

# Process-local accumulators: engines note candidates while routing; the
# writeback site (which knows the output project path) consumes them. One
# routing step runs at a time in both fronts (CLI process / GUI plan step).
_notes: Dict[str, Dict[str, object]] = {}


def _note(key: str, mapping: Dict[str, object]) -> None:
    _notes.setdefault(key, {}).update({k: v for k, v in mapping.items() if k})


def _consume(key: str) -> Dict[str, object]:
    return _notes.pop(key, {})


def note_protection_candidates(mapping: Dict[str, str]) -> None:
    """Record nets this step made protection-worthy (net name -> reason)."""
    _note(PRO_KEY, mapping)


def consume_protection_candidates() -> Dict[str, str]:
    """Return and clear the accumulated candidates (call once per step)."""
    return _consume(PRO_KEY)


def note_impedance_specs(mapping: Dict[str, dict]) -> None:
    """Record per-net impedance declarations from a step routed with
    --impedance: {net: {'ohms': 100, 'differential': True, 'pair_gap': 0.1,
    'coplanar_gap': 0.2}} -- so a later redo (rip/reroute) can recompute the
    SAME widths from the stackup instead of silently rerouting at the step's
    default width. Deliberately the DECLARATION, not the widths: widths go
    stale if the stackup changes, and copper measurement cannot distinguish
    impedance intent from necking nor recover the coplanar declaration at all
    (#486: the pour it assumes may not exist yet)."""
    _note(IMPEDANCE_KEY, mapping)


def consume_impedance_specs() -> Dict[str, dict]:
    return _consume(IMPEDANCE_KEY)


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


def _persist_map(pro_path: str, key: str, mapping: Dict[str, object],
                 label: str, verbose: bool = True) -> bool:
    """Merge ``mapping`` into the project's ``key`` map. Preserves every
    other key (plain json round-trip, same style as fix_kicad_drc_settings).
    No-op when the project file does not exist (the DRC writeback creates it
    first; without a project there is nothing for later steps to read)."""
    if not mapping or not pro_path or not os.path.isfile(pro_path):
        return False
    try:
        with open(pro_path, 'r', encoding='utf-8') as f:
            proj = json.load(f)
        ns = proj.setdefault(PRO_NAMESPACE, {})
        current = ns.get(key) or {}
        merged = dict(current)
        merged.update(mapping)
        if merged == current:
            return False
        ns[key] = merged
        with open(pro_path, 'w', encoding='utf-8') as f:
            json.dump(proj, f, indent=2)
        if verbose:
            added = len(merged) - len(current)
            print(f"  {label}: {len(merged)} recorded in {os.path.basename(pro_path)}"
                  f" (+{added} this step)")
        return True
    except Exception as e:
        if verbose:
            print(f"  (skipped {label} record: {e})")
        return False


def persist_protected_nets(pro_path: str, mapping: Dict[str, str],
                           verbose: bool = True) -> bool:
    return _persist_map(pro_path, PRO_KEY, mapping,
                        "Protected nets (not ripped by later steps)", verbose)


def persist_impedance_specs(pro_path: str, mapping: Dict[str, dict],
                            verbose: bool = True) -> bool:
    return _persist_map(pro_path, IMPEDANCE_KEY, mapping,
                        "Net impedance specs (reapplied on redo)", verbose)


def read_impedance_specs(pro_path: str) -> Dict[str, dict]:
    """Per-net impedance declarations from a .kicad_pro ({} when absent)."""
    try:
        if not pro_path or not os.path.isfile(pro_path):
            return {}
        with open(pro_path, 'r', encoding='utf-8') as f:
            proj = json.load(f)
        m = (proj.get(PRO_NAMESPACE) or {}).get(IMPEDANCE_KEY) or {}
        return {str(k): dict(v) for k, v in m.items()
                if isinstance(v, dict)} if isinstance(m, dict) else {}
    except Exception:
        return {}


def read_impedance_for_pcb_data(pcb_data, input_file: Optional[str] = None) -> Dict[str, dict]:
    path = input_file or getattr(pcb_data, 'source_path', "") or ""
    if not path:
        return {}
    return read_impedance_specs(pro_path_for_board(path))


def locked_net_names(pcb_data) -> Set[str]:
    """Nets with any KiCad-locked segment or via. The user pinned that copper;
    rip machinery must never strip the net (a partial rip would strand the
    locked fragments). Read straight from the board -- no .kicad_pro entry."""
    ids = {s.net_id for s in pcb_data.segments if getattr(s, 'locked', False)}
    ids |= {v.net_id for v in pcb_data.vias if getattr(v, 'locked', False)}
    ids.discard(0)
    return {pcb_data.nets[i].name for i in ids
            if i in pcb_data.nets and pcb_data.nets[i].name}


def protection_map(pcb_data, input_file: Optional[str] = None) -> Dict[str, str]:
    """Full protection map for a board: the .kicad_pro list, THIS run's
    --protect-nets matches (stashed on pcb_data by batch_route as
    `_user_protected_nets`, reason 'user' -- exact-name overridable like the
    .pro reasons), plus nets with KiCad-locked copper. 'locked' wins where
    several apply -- unlike the other reasons it has NO exact-name override
    (locked means never)."""
    m = read_for_pcb_data(pcb_data, input_file)
    m.update(getattr(pcb_data, '_user_protected_nets', None) or {})
    m.update({n: 'locked' for n in locked_net_names(pcb_data)})
    return m


def cached_protection_map(pcb_data, input_file: Optional[str] = None) -> Dict[str, str]:
    """protection_map, cached on pcb_data for the in-run rip ladders (they can
    consult it hundreds of times in a cascade). Safe to cache for a run: the
    .kicad_pro does not change mid-run, KiCad-locked copper is parsed state,
    and --protect-nets is stashed once before routing starts."""
    cache = getattr(pcb_data, '_protection_map_cache', None)
    if cache is None:
        cache = protection_map(pcb_data, input_file)
        pcb_data._protection_map_cache = cache
    return cache


def stash_user_protection(pcb_data, patterns: Optional[List[str]]) -> Dict[str, str]:
    """Resolve --protect-nets patterns against the board ONCE and stash the
    matches on pcb_data, where protection_map() overlays them for every
    consumer of this run (the rip-existing filter, the in-run phase-3 ladder,
    the seam re-ask). Also notes them as candidates so the caller's existing
    persist site writes them to the output .kicad_pro -- from the next step on
    they are protected without the flag. Shared by batch_route,
    batch_route_diff_pairs and the GUI call sites (CLAUDE.md parity rule).
    Returns the resolved {name: 'user'} map."""
    if not patterns:
        return {}
    from net_queries import matches_net_filter
    user_prot = {n.name: 'user' for n in pcb_data.nets.values()
                 if n.name and matches_net_filter(n.name, patterns)}
    if user_prot:
        pcb_data._user_protected_nets = dict(
            getattr(pcb_data, '_user_protected_nets', None) or {},
            **user_prot)
        # The in-run ladders read a per-run cache; drop any stale one so
        # this call's protections are in it.
        if hasattr(pcb_data, '_protection_map_cache'):
            del pcb_data._protection_map_cache
        note_protection_candidates(user_prot)
        print(f"  --protect-nets: {len(user_prot)} net(s) protected for "
              f"this run ({', '.join(sorted(user_prot)[:6])}"
              f"{'...' if len(user_prot) > 6 else ''})")
    else:
        print(f"  --protect-nets matched no nets: {patterns}")
    return user_prot


def exact_names(patterns: Optional[Iterable[str]]) -> Set[str]:
    """The non-glob entries of a pattern list: naming a net exactly is the
    deliberate-override signal that lifts its protection for this step."""
    if not patterns:
        return set()
    return {p for p in patterns if p and not (_GLOB_CHARS & set(p))}


def stash_rip_overrides(pcb_data, patterns: Optional[Iterable[str]]) -> Set[str]:
    """Record the exact-name rip overrides on pcb_data so the IN-RUN ladders
    can honor them (run-6 z2 fix). The pre-run filters (--rip-existing-nets /
    --force-reroute) already lift 'user' protection for exactly-named nets,
    but the in-run ladders re-consult cached_protection_map, which still
    lists them -- so the phase-3 tap cascade refused a net the operator had
    explicitly named ('protected_skipped {"phase3 tap cascade":
    {USB_DM_R: user}}' while --rip-existing-nets named it). 'locked' is
    never overridable, here or anywhere."""
    names = exact_names(patterns)
    if names:
        pcb_data._rip_override_names = set(
            getattr(pcb_data, '_rip_override_names', None) or set()) | names
    return getattr(pcb_data, '_rip_override_names', None) or set()


def rip_override_names(pcb_data) -> Set[str]:
    """The exact-name rip overrides stashed for this run (empty set if none)."""
    return getattr(pcb_data, '_rip_override_names', None) or set()


def blocker_never_rip_ids(pcb_data, plane_net_ids: Set[int],
                          input_file: Optional[str] = None,
                          max_pads: Optional[int] = None,
                          exclude_patterns: Optional[Iterable[str]] = None,
                          allow_names: Optional[Iterable[str]] = None,
                          announce: bool = True) -> Set[int]:
    """The never-pick-as-blocker id set for the plane scripts' tap/join rip
    ladders (shared by route_planes AND route_disconnected_planes so the
    create side stops being protection-blind -- it used to pass only its own
    plane nets). Union of:

      * the plane nets themselves,
      * #521-protected nets (.kicad_pro record, --protect-nets, KiCad-locked),
      * (run-6 guard) nets with more than ``max_pads`` pads -- a rail has the
        most copper near any pad, so every blocker selector preferentially
        picks it, and ripping it opens all its pads at once with an in-step
        reconnect that measured 0/2 at restoring them. Lifted per net by
        naming it EXACTLY in ``allow_names`` (--rip-blocker-allow),
      * any net matching ``exclude_patterns`` (--rip-blocker-exclude).
    """
    from net_queries import matches_net_filter
    if max_pads is None:
        from routing_defaults import PLANE_BLOCKER_MAX_PADS
        max_pads = PLANE_BLOCKER_MAX_PADS
    allow = set(allow_names or ())
    never = set(plane_net_ids)
    prot = {}
    try:
        prot = protection_map(pcb_data, input_file)
    except Exception:
        pass
    prot_ids = {nid for nid, n in pcb_data.nets.items()
                if n.name and n.name in prot} - set(plane_net_ids)
    rail_ids = set()
    if max_pads and max_pads > 0:
        for nid, pads in (getattr(pcb_data, 'pads_by_net', None) or {}).items():
            if nid in never or nid in prot_ids:
                continue
            name = pcb_data.nets[nid].name if nid in pcb_data.nets else None
            if name and name in allow:
                continue
            if len(pads) > max_pads:
                rail_ids.add(nid)
    excl_ids = set()
    if exclude_patterns:
        excl_ids = {nid for nid, n in pcb_data.nets.items()
                    if n.name and matches_net_filter(n.name, list(exclude_patterns))}
        excl_ids -= never | prot_ids | rail_ids
    if announce:
        def _names(ids, cap=4):
            xs = sorted(pcb_data.nets[i].name for i in ids if i in pcb_data.nets)
            return f"{', '.join(xs[:cap])}{'...' if len(xs) > cap else ''}"
        if prot_ids:
            print(f"  {len(prot_ids)} PROTECTED net(s) excluded from blocker "
                  f"rip-up ({_names(prot_ids)})")
        if rail_ids:
            print(f"  {len(rail_ids)} multi-pad net(s) (> {max_pads} pads) "
                  f"excluded from blocker rip-up ({_names(rail_ids)}) -- "
                  f"name one exactly in --rip-blocker-allow to rip it "
                  f"deliberately")
        if excl_ids:
            print(f"  {len(excl_ids)} net(s) excluded from blocker rip-up by "
                  f"--rip-blocker-exclude ({_names(excl_ids)})")
    return never | prot_ids | rail_ids | excl_ids


# What the last run's rip filters refused, and why: {context: {net: reason}}.
# The print below is for a human reading a log; a PROGRAM driving the router
# cannot see it, and the router's own failure hint tells that program to retry
# with --rip-existing-nets naming exactly the net that was just refused. A
# caller following that advice loops forever. route.py drains this into
# JSON_SUMMARY['protected_skipped'] so the refusal is machine-readable, and so a
# caller can tell "name it exactly to override" from "locked, no override ever".
PROTECTED_SKIPPED: Dict[str, Dict[str, str]] = {}


def clear_skipped() -> None:
    """Reset the record. route.py calls this once per run."""
    PROTECTED_SKIPPED.clear()


def filter_rippable_names(names: List[str], protected: Dict[str, str],
                          override_patterns: Optional[Iterable[str]] = None,
                          context: str = "rip-up") -> List[str]:
    """Drop protected names (minus exact-name overrides), printing what was
    excluded and why. Returns the surviving names in input order. A net whose
    reason is 'locked' (KiCad-locked copper) has NO override -- ever."""
    if not protected:
        return list(names)
    overrides = exact_names(override_patterns)
    kept, blocked = [], []
    for n in names:
        if n in protected and (protected[n] == 'locked' or n not in overrides):
            blocked.append(n)
        else:
            kept.append(n)
    if blocked:
        PROTECTED_SKIPPED.setdefault(context, {}).update(
            {n: protected[n] for n in blocked})
        by_reason: Dict[str, List[str]] = {}
        for n in blocked:
            by_reason.setdefault(protected[n], []).append(n)
        det = '; '.join(f"{r}: {', '.join(ns[:4])}{'...' if len(ns) > 4 else ''}"
                        for r, ns in sorted(by_reason.items()))
        print(f"  {len(blocked)} PROTECTED net(s) excluded from {context} ({det})"
              f" -- name a net exactly (no glob) to override")
    return kept
