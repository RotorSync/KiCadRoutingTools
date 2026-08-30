"""Beautification pass 3: stub repair (Class B extend + gap-pair completion +
safe spur trim).

PHASE-1 taxonomy (see carrier_lab/stub_pass_findings.md):

  Class A -- dead-end traces rooted at anchors on BROKEN single-chain nets
      (orangecrab/routed_output/fanout fixtures). Trimming them strips whole
      nets, which collapses score.py's 'vias'/'pad_entry' denominators
      (n_routed_nets drops while vias stay) -- a >2 sub-score regression on
      those pathological half-routed fixtures. NOT handled here; documented.
  Class B -- off-center landings INSIDE own-net pad/via copper (>1e-3 mm from
      center): metric_stubs false positives (electrically connected). Fixed by
      EXTENDING the owner segment along its own direction so the endpoint lands
      at the pad/via center (collinear within a tiny tolerance -> no new bend,
      no off-angle joint).
  Near-miss -- two dangling endpoints of the SAME net+layer facing each other
      within GAP mm: genuine disconnections. Completed with a minimal
      octolinear connector (exact-clearance-gated incl keepout/edge).
  Spur -- a dangling chain whose junction is a BRANCH point (other copper
      continues) or a FREE isolated fragment: trimmable back to the junction
      without creating a new dangling end.

Every removal/addition is gated per-net on check_net_connectivity equal-or-
better, and every added segment on the exact clearance kernels (foreign pad/
seg/via/hole + keepout + board edge) at the routed clearance -- so connectivity
never degrades and DRC never increases. Skips protected/impedance nets like the
other beautify passes.
"""
from __future__ import annotations

import math
from collections import defaultdict
from typing import List, Optional, Tuple

from kicad_parser import Segment

_TOL = 1e-3


def _point_in_polygon(x: float, y: float, poly) -> bool:
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if ((yi > y) != (yj > y)) and            (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def _dangling_endpoints(pcb_data):
    """Mirror quality/score.py metric_stubs: endpoints connecting to no other
    copper (segment endpoint / pad / via within tolerance) and not inside a
    same-net+layer zone. Returns list of (x, y).

    Uses a coarse spatial hash so the near-neighbour count is O(1) per endpoint
    instead of O(N) -- this is the hot path (called once per phase).
    """
    cell = 0.02  # mm; > 2*TOL so any within-tolerance neighbour shares a cell
    grid = defaultdict(list)

    def gkey(px, py):
        return (int(math.floor(px / cell)), int(math.floor(py / cell)))

    def add_pt(px, py):
        grid[gkey(px, py)].append((px, py))

    for s in pcb_data.segments:
        add_pt(s.start_x, s.start_y)
        add_pt(s.end_x, s.end_y)
    for pads in pcb_data.pads_by_net.values():
        for p in pads:
            add_pt(p.global_x, p.global_y)
    for v in pcb_data.vias:
        add_pt(v.x, v.y)

    def count_near(px, py):
        c = 0
        kx, ky = gkey(px, py)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for (qx, qy) in grid.get((kx + dx, ky + dy), ()):
                    if math.hypot(px - qx, py - qy) <= _TOL:
                        c += 1
                        if c >= 2:
                            return c
        return c

    zone_index = defaultdict(list)
    for z in pcb_data.zones:
        if z.net_id == 0:
            continue
        zone_index[(z.net_id, z.layer)].append(z.polygon)

    def in_same_net_zone(px, py, net_id, layer):
        for poly in zone_index.get((net_id, layer), []):
            if _point_in_polygon(px, py, poly):
                return True
        return False

    out = []
    seen = set()
    for s in pcb_data.segments:
        for ep in ((s.start_x, s.start_y), (s.end_x, s.end_y)):
            key = (round(ep[0], 4), round(ep[1], 4))
            if key in seen:
                continue
            seen.add(key)
            if count_near(*ep) < 2 and not in_same_net_zone(ep[0], ep[1], s.net_id, s.layer):
                out.append(ep)
    return out


def _build_seg_index(pcb_data):
    """Spatial hash of segment endpoints -> list of segments touching them.
    Returns (index, cell). Cell size > 2*TOL so within-tolerance endpoints
    share a cell."""
    cell = 0.02
    index = defaultdict(list)

    def gkey(px, py):
        return (int(math.floor(px / cell)), int(math.floor(py / cell)))

    for s in pcb_data.segments:
        for ep in ((s.start_x, s.start_y), (s.end_x, s.end_y)):
            index[gkey(*ep)].append(s)
    return index, cell


def _owner_of(pcb_data, x, y, seg_index=None, cell=0.02):
    if seg_index is not None:
        kx = int(math.floor(x / cell))
        ky = int(math.floor(y / cell))
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for s in seg_index.get((kx + dx, ky + dy), ()):
                    if math.hypot(s.start_x - x, s.start_y - y) <= _TOL or                        math.hypot(s.end_x - x, s.end_y - y) <= _TOL:
                        return s
        return None
    for s in pcb_data.segments:
        if math.hypot(s.start_x - x, s.start_y - y) <= _TOL or            math.hypot(s.end_x - x, s.end_y - y) <= _TOL:
            return s
    return None


def _in_pad(p, x, y):
    sx = p.size_x / 2.0
    sy = p.size_y / 2.0
    return abs(x - p.global_x) <= sx and abs(y - p.global_y) <= sy


def _in_own_pad_or_via(pcb_data, x, y, nid):
    for p in pcb_data.pads_by_net.get(nid, []):
        if _in_pad(p, x, y):
            return True
    for v in pcb_data.vias:
        if v.net_id == nid and math.hypot(x - v.x, y - v.y) <= v.size / 2.0:
            return True
    return False


def _nbrs_at(segs, x, y, nid, layer):
    out = []
    for s in segs:
        if s.net_id != nid or s.layer != layer:
            continue
        if math.hypot(s.start_x - x, s.start_y - y) <= _TOL or            math.hypot(s.end_x - x, s.end_y - y) <= _TOL:
            out.append(s)
    return out


def _other_end(s, x, y):
    if math.hypot(s.start_x - x, s.start_y - y) <= _TOL:
        return (s.end_x, s.end_y)
    return (s.start_x, s.start_y)


def _anchored_at(pcb_data, x, y, nid):
    for p in pcb_data.pads_by_net.get(nid, []):
        if math.hypot(p.global_x - x, p.global_y - y) <= _TOL:
            return True
    for v in pcb_data.vias:
        if v.net_id == nid and math.hypot(v.x - x, v.y - y) <= _TOL:
            return True
    return False


def _walk_chain(pcb_data, x, y, seg_index=None, cell=0.02):
    """Walk inward from a dangling endpoint through degree-2 chain to the first
    junction. Returns (chain_segments_ordered_free_end_first, junction_type)
    where junction_type is 'anchor' | 'branch' | 'free' | 'no_owner'."""
    segs = pcb_data.segments
    owner = _owner_of(pcb_data, x, y, seg_index=seg_index, cell=cell)
    if owner is None:
        return [], 'no_owner'
    nid, layer = owner.net_id, owner.layer
    cur = (x, y)
    visited = set()
    chain = []
    while True:
        nbrs = [s for s in _nbrs_at(segs, *cur, nid, layer) if id(s) not in visited]
        if not nbrs:
            return chain, 'free'
        s = nbrs[0]
        visited.add(id(s))
        ox, oy = _other_end(s, *cur)
        chain.append(s)
        cur = (ox, oy)
        if _anchored_at(pcb_data, *cur, nid):
            return chain, 'anchor'
        deg = len(_nbrs_at(segs, *cur, nid, layer))
        if deg >= 3:
            return chain, 'branch'
        if deg <= 1:
            return chain, 'free'


def _octolinear_path(A, B):
    """Minimal octolinear path points from A to B (axis-aligned or single 45)."""
    ax, ay = A
    bx, by = B
    dx = bx - ax
    dy = by - ay
    adx = abs(dx)
    ady = abs(dy)
    sx = 1.0 if dx >= 0 else -1.0
    sy = 1.0 if dy >= 0 else -1.0
    pts = [A]
    if adx < 1e-9 or ady < 1e-9 or abs(adx - ady) < 1e-6:
        pts.append(B)
        return pts
    if adx >= ady:
        pts.append((round(ax + sx * ady, 4), round(by, 4)))
    else:
        pts.append((round(bx, 4), round(ay + sy * adx, 4)))
    pts.append(B)
    return pts


def beautify_stub_repair(pcb_data,
                         config=None,
                         scope_net_ids=None,
                         skip_net_ids=None,
                         gap=0.15,
                         max_extend=0.05):
    """PASS 3 -- stub repair: Class B extend + gap-pair completion + safe spur trim.

    Returns (removed_segments, added_segments). Conservative: every removal is
    gated per-net on check_net_connectivity equal-or-better; every addition on
    the exact clearance kernels (foreign pad/seg/via/hole + keepout + edge).
    """
    clr = getattr(config, 'clearance', 0.1) if config else 0.1
    bec = getattr(config, 'board_edge_clearance', 0.0) or 0.0
    edge_clr = max(clr, bec)
    pcb_data._beautify_clr = clr
    pcb_data._beautify_bec = bec

    from beautify_jog import (_seg_clears_prefiltered,
                              _build_keepout_areas,
                              _build_edge_geometry)
    keepout_areas = _build_keepout_areas(pcb_data, config or type('C', (), {})())
    edge_geom = _build_edge_geometry(pcb_data)

    def clears(nid, x1, y1, x2, y2, layer, w):
        try:
            return _seg_clears_prefiltered(
                pcb_data, nid, x1, y1, x2, y2, layer, w,
                clr,
                field=None,
                keepout_areas=keepout_areas,
                edge_geom=edge_geom,
                edge_clr=edge_clr)
        except Exception:
            return False

    removed = []
    added = []
    nets_changed = set()

    seg_index, seg_cell = _build_seg_index(pcb_data)

    # ---- Phase 1: Class B extend (collinear within max_extend) ----
    dang = _dangling_endpoints(pcb_data)
    done = set()
    replaced_ids = set()
    for (x, y) in dang:
        key = (round(x, 4), round(y, 4))
        if key in done or key in replaced_ids:
            continue
        o = _owner_of(pcb_data, x, y, seg_index=seg_index, cell=seg_cell)
        if o is None or id(o) in replaced_ids:
            continue
        nid = o.net_id
        if skip_net_ids and nid in skip_net_ids:
            continue
        if scope_net_ids is not None and nid not in scope_net_ids:
            continue
        layer = o.layer
        w = o.width if o.width > 0 else 0.25
        tgt = None
        for p in pcb_data.pads_by_net.get(nid, []):
            if _in_pad(p, x, y):
                tgt = (p.global_x, p.global_y)
                break
        if tgt is None:
            for v in pcb_data.vias:
                if v.net_id == nid and math.hypot(x - v.x, y - v.y) <= v.size / 2.0:
                    tgt = (v.x, v.y)
                    break
        if tgt is None:
            continue
        tx_, ty_ = tgt
        fx, fy = (o.end_x, o.end_y) if math.hypot(o.start_x - x, o.start_y - y) <= _TOL             else (o.start_x, o.start_y)
        dx = x - fx
        dy = y - fy
        L = math.hypot(dx, dy)
        if L < 1e-9:
            continue
        cx = tx_ - fx
        cy = ty_ - fy
        cross = abs(dx * cy - dy * cx) / L   # perpendicular dist of center from line F->(x,y)
        proj = (cx * dx + cy * dy) / L       # projection of center along line from F
        ext_len = proj - L                   # how far beyond (x,y) the center projects
        if cross > max_extend or ext_len < -1e-6 or ext_len > max_extend:
            continue
        ns = Segment(start_x=fx, start_y=fy,
                     end_x=tx_, end_y=ty_,
                     width=w, layer=layer, net_id=nid)
        if not clears(nid, fx, fy, tx_, ty_, layer, w):
            continue
        removed.append(o)
        added.append(ns)
        replaced_ids.add(id(o))
        nets_changed.add(nid)
        done.add(key)
        done.add((round(tx_, 4), round(ty_, 4)))

    # ---- Phase 2: gap-pair completion (same-net+layer dangling pairs) ----
    cur_segs = [s for s in pcb_data.segments if id(s) not in {id(x) for x in removed}]
    cur_segs = list(cur_segs) + list(added)
    saved_segs = pcb_data.segments
    pcb_data.segments = cur_segs

    dang = _dangling_endpoints(pcb_data)
    done = set()
    added_ids = {id(a) for a in added}
    rem_ids = {id(r) for r in removed}
    ep_owner = {}
    for (x, y) in dang:
        o = _owner_of(pcb_data, x, y, seg_index=seg_index, cell=seg_cell)
        if o is not None:
            ep_owner[(round(x, 4), round(y, 4))] = (o, x, y)
    eps_list = list(ep_owner.keys())
    used = set()
    for i in range(len(eps_list)):
        k1 = eps_list[i]
        if k1 in used or k1 in done or k1 in replaced_ids or k1 in added_ids or k1 in rem_ids:
            continue
        o1, x1, y1 = ep_owner[k1]
        nid1 = o1.net_id
        if skip_net_ids and nid1 in skip_net_ids:
            continue
        if scope_net_ids is not None and nid1 not in scope_net_ids:
            continue
        layer1 = o1.layer
        w1 = o1.width if o1.width > 0 else 0.25
        best = None
        best_d = gap + 1.0
        for j in range(i + 1, len(eps_list)):
            k2 = eps_list[j]
            if k2 in used or k2 in done or k2 in replaced_ids or k2 in added_ids or k2 in rem_ids:
                continue
            o2, x2, y2 = ep_owner[k2]
            if o2.net_id != nid1 or o2.layer != layer1:
                continue
            d = math.hypot(x1 - x2, y1 - y2)
            if d < best_d:
                best_d = d
                best = (k2, o2, x2, y2)
        if best is None or best_d > gap or best_d <= _TOL + 5e-4:
            continue
        k2, o2, x2, y2 = best
        pts = _octolinear_path((x1, y1), (x2, y2))
        ok = True
        newsegs = []
        for q in range(len(pts) - 1):
            ns = Segment(start_x=pts[q][0], start_y=pts[q][1],
                         end_x=pts[q + 1][0], end_y=pts[q + 1][1],
                         width=w1, layer=layer1, net_id=nid1)
            if not clears(nid1,
                          pts[q][0], pts[q][1], pts[q + 1][0], pts[q + 1][1],
                          layer1, w1):
                ok = False
                break
            newsegs.append(ns)
        if ok and newsegs:
            added.extend(newsegs)
            nets_changed.add(nid1)
            used.add(k1)
            used.add(k2)
            done.add(k1)
            done.add(k2)

    # ---- Phase 3: safe spur trim (branch/free chains only) ----
    # Each candidate is judged against the EVOLVING board state: cur_segs /
    # rem_ids / pcb_data.segments are refreshed after every accepted removal, so
    # a later candidate's connectivity gate and chain walk see the copper that
    # earlier trims already removed -- never a stale pre-loop snapshot. Without
    # this, two individually-safe trims on one net (e.g. redundant parallel
    # paths to a pad left by rip-up/retry) could each pass against the original
    # board and together remove a through-path.
    dang = _dangling_endpoints(pcb_data)
    done = set()
    for (x, y) in dang:
        key = (round(x, 4), round(y, 4))
        if key in done or key in replaced_ids or key in added_ids or key in rem_ids:
            continue
        o = _owner_of(pcb_data, x, y, seg_index=seg_index, cell=seg_cell)
        if o is None or id(o) in rem_ids or id(o) in added_ids:
            continue
        nid = o.net_id
        if skip_net_ids and nid in skip_net_ids:
            continue
        if scope_net_ids is not None and nid not in scope_net_ids:
            continue
        layer = o.layer
        chain, jtype = _walk_chain(pcb_data, x, y, seg_index=seg_index, cell=seg_cell)
        if not chain or jtype == 'no_owner':
            continue
        safe = (jtype == 'branch' or jtype == 'free')
        if not safe:
            continue
        rem_ids_local = {id(c) for c in chain}
        rem_ids_local = {i for i in rem_ids_local if i not in rem_ids}
        if not rem_ids_local:
            continue
        kept = [s for s in cur_segs if id(s) not in rem_ids_local]
        from check_connected import check_net_connectivity as cnc
        vv = [v for v in pcb_data.vias if v.net_id == nid]
        pp = pcb_data.pads_by_net.get(nid, [])
        zz = [z for z in pcb_data.zones if z.net_id == nid]
        before = cnc(nid,
                     [s for s in cur_segs if s.net_id == nid],
                     vv, pp,
                     zz or None,
                     pcb_data=pcb_data)
        after = cnc(nid,
                    [s for s in kept if s.net_id == nid],
                    vv, pp,
                    zz or None,
                    pcb_data=pcb_data)
        bd = len(before.get('disconnected_pads') or [])
        ad = len(after.get('disconnected_pads') or [])
        bc = before.get('num_components') or 1
        ac = after.get('num_components') or 1
        if ad > bd or ac > bc or (before.get('connected') and not after.get('connected')):
            continue
        removed.extend([s for s in cur_segs if id(s) in rem_ids_local])
        nets_changed.add(nid)
        done.add(key)
        # Refresh the evolving state so later candidates are judged against the
        # copper that remains after THIS trim (not a stale pre-loop snapshot).
        cur_segs = kept
        rem_ids |= rem_ids_local
        pcb_data.segments = cur_segs

    pcb_data.segments = saved_segs
    try:
        del pcb_data._beautify_clr
        del pcb_data._beautify_bec
    except AttributeError:
        pass

    # Deduplicate removals (a segment may be walked from two endpoints).
    seen_r = set()
    removed_dedup = []
    for s in removed:
        if id(s) not in seen_r:
            seen_r.add(id(s))
            removed_dedup.append(s)
    return removed_dedup, added


def run_beautify_stub_repair(pcb_data,
                             scope_net_ids=None,
                             skip_net_ids=None,
                             config=None,
                             gap=0.15,
                             max_extend=0.05):
    """Convenience wrapper matching run_beautify's signature shape."""
    return beautify_stub_repair(pcb_data,
                                config=config,
                                scope_net_ids=scope_net_ids,
                                skip_net_ids=skip_net_ids,
                                gap=gap,
                                max_extend=max_extend)
