"""Incremental routing: re-route only the nets a small design edit touched.

Given the CURRENT (edited) input board and a PREVIOUS routed output (with its
sibling .kicad_pro), compute the DIRTY net set -- nets whose pads/netlist/
positions changed vs what the previous output was routed from, plus nets whose
preserved copper would now collide with a changed obstacle (exact clearance
kernels). Preserve the previous copper for every clean net, rip only the dirty
nets, and route them against the preserved copper via the existing shared
engine (batch_route with the preserved copper as obstacles).

The heavy lifting stays in the shared engine path: this module only computes
the dirty set and assembles a working board (current footprints/nets + previous
copper for clean nets). batch_route then does the rip + re-route exactly as it
does for any other run, so the CLI and GUI inherit the same behavior.

Design notes / conventions honored:
  - KiCad-LOCKED and #521-protected nets are NEVER ripped, regardless of how
    dirty they look. They are excluded from the dirty set so their copper is
    preserved verbatim (batch_route's own rip machinery would refuse them too,
    but excluding them up front keeps the preserved copper intact).
  - The working board is built from the CURRENT input text (edited footprints /
    netlist) with ALL of its own copper stripped, then the previous output's
    copper for clean nets re-added. Dirty nets therefore enter batch_route with
    no copper and are routed from scratch against the preserved clean copper.
  - Net identity is by NAME (stable across boards); net_ids are remapped from
    the previous output to the current input by name when assembling copper.
"""

from __future__ import annotations

import os
import sys
from typing import Dict, List, Optional, Set, Tuple


def _pad_key(pad) -> Tuple[str, str]:
    """Stable identity for a pad across two boards: (component_ref, pad_number)."""
    return (getattr(pad, 'component_ref', '') or '',
            getattr(pad, 'pad_number', '') or '')


def _pad_geometry_sig(pad) -> Tuple:
    """Signature of a pad's copper geometry + net assignment (position, size,
    shape, net). Used to detect pads that moved / changed net / resized."""
    return (round(getattr(pad, 'global_x', 0.0), 6),
            round(getattr(pad, 'global_y', 0.0), 6),
            round(getattr(pad, 'size_x', 0.0), 6),
            round(getattr(pad, 'size_y', 0.0), 6),
            getattr(pad, 'shape', ''),
            getattr(pad, 'net_name', '') or '')


def _net_name_of(pcb, net_id: int) -> str:
    net = pcb.nets.get(net_id)
    if net is not None and net.name:
        return net.name
    return f"__net_{net_id}__"


def compute_dirty_nets(current_pcb, prev_pcb,
                       clearance: float,
                       routing_layers: Optional[List[str]] = None,
                       protected: Optional[Dict[str, str]] = None,
                       ) -> Tuple[List[str], List]:
    """Compute the DIRTY net set between the current input and the previous
    routed output.

    Returns (dirty_names, changed_pads):
      dirty_names  - net NAMES that must be ripped and re-routed.
      changed_pads - pads (from CURRENT pcb) that moved / changed net / are new;
                     used as the obstacle set for the collision pass.
    """
    if routing_layers is None:
        routing_layers = list(getattr(current_pcb.board_info, 'copper_layers', None)
                              or [])

    # ---- Build pad maps keyed by (component_ref, pad_number) ----
    cur_pads: Dict[Tuple[str, str], list] = {}
    for fp in current_pcb.footprints.values():
        for p in fp.pads:
            cur_pads.setdefault(_pad_key(p), []).append(p)
    prev_pads: Dict[Tuple[str, str], list] = {}
    for fp in prev_pcb.footprints.values():
        for p in fp.pads:
            prev_pads.setdefault(_pad_key(p), []).append(p)

    dirty: Set[str] = set()
    changed_pads: List = []

    def _mark_net(net_name: str) -> None:
        if net_name:
            dirty.add(net_name)

    # ---- Pass 1: netlist / position / geometry changes ----
    all_keys = set(cur_pads) | set(prev_pads)
    for key in all_keys:
        cur_list = cur_pads.get(key, [])
        prev_list = prev_pads.get(key, [])
        if not cur_list:
            # Pad removed from current -> its old net is dirty.
            for p in prev_list:
                _mark_net(_net_name_of(prev_pcb, p.net_id))
            continue
        if not prev_list:
            # Brand-new pad -> its net is dirty; it is a changed obstacle.
            for p in cur_list:
                _mark_net(_net_name_of(current_pcb, p.net_id))
                changed_pads.append(p)
            continue
        # Compare by geometry signature (position/size/shape/net).
        cur_sigs = [_pad_geometry_sig(p) for p in cur_list]
        prev_sigs = [_pad_geometry_sig(p) for p in prev_list]
        if cur_sigs != prev_sigs:
            for p in cur_list:
                _mark_net(_net_name_of(current_pcb, p.net_id))
                changed_pads.append(p)
            for p in prev_list:
                _mark_net(_net_name_of(prev_pcb, p.net_id))

    # ---- Pass 2: preserved copper colliding with changed obstacles ----
    # For every clean net (not already dirty), check its preserved copper
    # (from prev) against every changed pad (current position) at the routing
    # clearance. A collision means that clean net must be ripped + re-routed.
    if changed_pads:
        from check_drc import check_pad_segment_overlap, check_pad_via_overlap
        # Group preserved copper by net name.
        prev_segs_by_net: Dict[str, list] = {}
        for s in prev_pcb.segments:
            if s.net_id == 0:
                continue
            prev_segs_by_net.setdefault(_net_name_of(prev_pcb, s.net_id), []).append(s)
        prev_vias_by_net: Dict[str, list] = {}
        for v in prev_pcb.vias:
            if v.net_id == 0:
                continue
            prev_vias_by_net.setdefault(_net_name_of(prev_pcb, v.net_id), []).append(v)

        # Precompute which changed pads are copper-bearing on which layers.
        def _pad_copper_layers(pad) -> List[str]:
            layers = getattr(pad, 'layers', None) or []
            out = []
            for l in layers:
                if l == '*':
                    out.extend(routing_layers)
                elif l.endswith('.Cu'):
                    out.append(l)
            return out

        for net_name in list(prev_segs_by_net):
            if net_name in dirty:
                continue
            segs = prev_segs_by_net[net_name]
            vias = prev_vias_by_net.get(net_name, [])
            hit = False
            for pad in changed_pads:
                pad_layers = _pad_copper_layers(pad)
                if not pad_layers:
                    continue
                for s in segs:
                    if s.layer not in pad_layers:
                        continue
                    viol, _, _ = check_pad_segment_overlap(
                        pad, s, clearance, routing_layers)
                    if viol:
                        hit = True
                        break
                if hit:
                    break
                for v in vias:
                    viol, _ = check_pad_via_overlap(pad, v, clearance,
                                                    routing_layers)
                    if viol:
                        hit = True
                        break
                if hit:
                    break
            if hit:
                dirty.add(net_name)

    # ---- Exclude never-rippable nets (locked / protected) ----
    if protected:
        # 'locked' has no override; 'user'/'pair' protected nets are preserved
        # too unless explicitly overridden (there is no override here).
        dirty = {n for n in dirty if n not in protected}

    # Deterministic order.
    return sorted(dirty), changed_pads


def _copy_siblings(src_board: str, dst_board: str) -> None:
    """Copy sibling project files (.kicad_pro, .kicad_dru, ...) from src_board's
    directory to dst_board's sibling paths (#441). The DRC floor and per-layer
    rules must travel with the board or the next step resolves them from the
    looser stock netclass."""
    import shutil
    try:
        from copy_board import SIBLING_EXTS
    except Exception:
        SIBLING_EXTS = ('.kicad_pro', '.kicad_dru')
    sb = os.path.splitext(src_board)[0]
    db = os.path.splitext(dst_board)[0]
    if os.path.abspath(sb) == os.path.abspath(db):
        return
    for ext in SIBLING_EXTS:
        if os.path.isfile(sb + ext):
            try:
                shutil.copy2(sb + ext, db + ext)
            except OSError:
                pass


def _strip_all_copper(content: str) -> str:
    """Remove every top-level (segment ...) and (via ...) block from a board's
    text. Footprints carry (pad ...) blocks, never (segment ...)/(via ...), so
    this only touches routed copper."""
    import re

    def _strip_blocks(text: str, token: str) -> str:
        # Match a block starting with a tab-indented '(' + token and ending at
        # its matching close paren. Blocks are balanced s-exprs; we scan.
        out = []
        i = 0
        n = len(text)
        start_marker = '(' + token
        while i < n:
            # Find next occurrence of start_marker at a block start.
            idx = text.find(start_marker, i)
            if idx == -1:
                out.append(text[i:])
                break
            # Ensure it's a block start (preceded by whitespace/newline).
            if idx > 0 and text[idx - 1] not in ' \t\n':
                out.append(text[i:idx + len(start_marker)])
                i = idx + len(start_marker)
                continue
            out.append(text[i:idx])
            # Scan to matching close paren.
            depth = 0
            j = idx
            while j < n:
                c = text[j]
                if c == '(':
                    depth += 1
                elif c == ')':
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            # Skip past the block (and trailing newline).
            i = j + 1
            while i < n and text[i] in '\r\n':
                i += 1
        return ''.join(out)

    content = _strip_blocks(content, 'segment')
    content = _strip_blocks(content, 'via')
    return content


def build_working_board(current_input_path: str,
                        prev_output_path: str,
                        dirty_names: List[str],
                        work_path: str,
                        ) -> Dict:
    """Build a working board file: current footprints/nets + previous copper for
    every CLEAN net. Dirty nets enter with no copper (batch_route re-routes them).

    Returns a summary dict with counts."""
    from kicad_parser import parse_kicad_pcb
    from kicad_writer import add_tracks_and_vias_to_pcb

    current_pcb = parse_kicad_pcb(current_input_path)
    prev_pcb = parse_kicad_pcb(prev_output_path)

    dirty_set = set(dirty_names)

    # Map prev net_ids -> current net_ids by name.
    cur_name_to_id = {n.name: nid for nid, n in current_pcb.nets.items()}
    prev_name_to_id = {n.name: nid for nid, n in prev_pcb.nets.items()}

    def _cur_nid(prev_nid: int) -> Optional[int]:
        name = _net_name_of(prev_pcb, prev_nid)
        return cur_name_to_id.get(name)

    # Collect clean-net copper from prev.
    tracks = []
    vias = []
    clean_seg_count = 0
    clean_via_count = 0
    for s in prev_pcb.segments:
        if s.net_id == 0:
            continue
        name = _net_name_of(prev_pcb, s.net_id)
        if name in dirty_set:
            continue
        nid = _cur_nid(s.net_id)
        if nid is None:
            continue
        tracks.append({
            'start': (s.start_x, s.start_y),
            'end': (s.end_x, s.end_y),
            'width': s.width,
            'layer': s.layer,
            'net_id': nid,
        })
        clean_seg_count += 1
    for v in prev_pcb.vias:
        if v.net_id == 0:
            continue
        name = _net_name_of(prev_pcb, v.net_id)
        if name in dirty_set:
            continue
        nid = _cur_nid(v.net_id)
        if nid is None:
            continue
        vias.append({
            'x': v.x,
            'y': v.y,
            'size': v.size,
            'drill': v.drill,
            'layers': v.layers,
            'net_id': nid,
        })
        clean_via_count += 1

    # Build the working board: current input text stripped of ALL its own copper,
    # then prev's clean-net copper added back.
    with open(current_input_path, 'r', encoding='utf-8') as f:
        content = f.read()
    stripped = _strip_all_copper(content)

    # Write the stripped board to a temp file first (add_tracks_and_vias_to_pcb
    # reads from a path).
    tmp_stripped = work_path + '.stripped.kicad_pcb'
    with open(tmp_stripped, 'w', encoding='utf-8') as f:
        f.write(stripped)

    add_tracks_and_vias_to_pcb(
        tmp_stripped, work_path,
        tracks=tracks,
        vias=vias,
        net_id_to_name=current_pcb.net_id_to_name or None,
    )
    if os.path.exists(tmp_stripped):
        try:
            os.remove(tmp_stripped)
        except OSError:
            pass

    # Carry the PREVIOUS output's sibling project files (DRC floor, per-layer
    # rules, protected-nets record) to the working board so batch_route resolves
    # the same floor it routed to originally (#441). The working board is built
    # from CURRENT input text, whose own .kicad_pro may predate or differ from
    # the routed floor.
    _copy_siblings(prev_output_path, work_path)

    return {
        'dirty_names': sorted(dirty_set),
        'clean_segments': clean_seg_count,
        'clean_vias': clean_via_count,
        'work_path': work_path,
    }


def prepare_incremental(current_input_path: str,
                        prev_output_path: str,
                        work_path: str,
                        clearance: Optional[float] = None,
                        ) -> Dict:
    """Main entry point: compute the dirty net set and build the working board.

    Returns a dict with:
      dirty_names   - nets to rip + re-route.
      work_path     - path to the assembled working board.
      clean_segments / clean_vias - preserved copper counts.
      clearance     - the clearance used for the collision pass.
      protected_excluded - nets excluded because they are locked/protected.
      empty         - True when there is nothing to route (NULL-EDIT).
    """
    from kicad_parser import parse_kicad_pcb

    current_pcb = parse_kicad_pcb(current_input_path)
    prev_pcb = parse_kicad_pcb(prev_output_path)

    if clearance is None:
        from list_nets import board_default_netclass_clearance
        clearance = board_default_netclass_clearance(current_input_path)
        if clearance is None:
            from routing_defaults import CLEARANCE
            clearance = CLEARANCE

    # Protection map from the PREVIOUS output's sibling .kicad_pro (the routed
    # board carries the protected-nets record) plus locked copper.
    protected: Dict[str, str] = {}
    try:
        from protected_nets import protection_map
        protected = protection_map(prev_pcb, prev_output_path) or {}
    except Exception:
        protected = {}

    routing_layers = list(getattr(current_pcb.board_info, 'copper_layers', None)
                          or [])

    dirty_names, changed_pads = compute_dirty_nets(
        current_pcb, prev_pcb, clearance,
        routing_layers=routing_layers,
        protected=protected,
    )

    protected_excluded = sorted(
        n for n in dirty_names if n in protected)

    summary = build_working_board(
        current_input_path, prev_output_path, dirty_names, work_path)

    summary['dirty_names'] = dirty_names
    summary['work_path'] = work_path
    summary['clearance'] = clearance
    summary['protected_excluded'] = protected_excluded
    summary['changed_pads'] = len(changed_pads)
    summary['empty'] = not dirty_names

    return summary


if __name__ == '__main__':
    # CLI probe: python incremental_routing.py <current> <prev> <work>
    if len(sys.argv) < 4:
        print("usage: incremental_routing.py <current.kicad_pcb> <prev.kicad_pcb> <work.kicad_pcb>")
        sys.exit(2)
    res = prepare_incremental(sys.argv[1], sys.argv[2], sys.argv[3])
    print("dirty:", len(res['dirty_names']))
    print("dirty_names:", res['dirty_names'][:20])
    print("clean_segments:", res['clean_segments'], "clean_vias:", res['clean_vias'])
    print("changed_pads:", res['changed_pads'])
    print("protected_excluded:", res['protected_excluded'])
    print("empty:", res['empty'])
