"""Beautification pass 1: professional hand-routing look (stubs + pad entry).

Two conservative post-route passes that make routed output look professionally
hand-routed without changing connectivity or adding DRC violations:

  PASS A -- stub cleanup: find truly-dangling segment endpoints (reaching no
      pad, via, same-net segment junction, or same-net+layer zone -- mirroring
      quality/score.py's zone-aware stubs definition) and trim the dangling
      copper back to the last real junction (an anchor: pad/via/zone, or a
      branch point with another continuing branch). Removing genuinely dangling
      copper must not change connectivity -- each net's removal is gated on
      check_net_connectivity before/after.

  PASS B -- pad-entry normalization: where a trace's final approach enters a
      pad at an acute or odd angle, re-bend the last segment(s) so the entry is
      near-perpendicular to the pad edge (or along the pad's major axis), ONLY
      when the new geometry passes the existing exact clearance kernels at the
      routed clearance; constrained cases are skipped rather than forced.
"""
from __future__ import annotations

import math
from collections import defaultdict
from typing import List, Optional, Tuple

from kicad_parser import Segment

# Coincidence tolerance matching quality/score.py's metric_stubs (1e-3 mm).
_STUB_TOL = 1e-3


def _point_in_polygon(x: float, y: float, poly) -> bool:
    """Ray-casting point-in-polygon test (same as quality/score.py)."""
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if ((yi > y) != (yj > y)) and \
           (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def _trim_layer_dangles(segs, vias, pads, zones, layer):
    """Return segments on ``layer`` whose dangling copper should be trimmed.
    Mirrors quality/score.py's metric_stubs definition exactly: an endpoint is
    dangling if it connects to no other copper (segment endpoint / pad / via)
    within tolerance AND is not inside a same-net+layer zone. For each dangling
    endpoint we walk inward along its chain and trim back to the last real
    junction -- an anchor (pad/via/zone) or a branch point with another
    continuing branch (degree >= 3). A degree-2 node is just a continuation.
    """
    if not segs: return []
    cell = _STUB_TOL
    grid = defaultdict(list)
    for i, s in enumerate(segs):
        grid[(int(s.start_x // cell), int(s.start_y // cell))].append((i, True))
        grid[(int(s.end_x // cell), int(s.end_y // cell))].append((i, False))
    def _nearby(x, y):
        cx = int(x // cell); cy = int(y // cell)
        out = []
        for gx in range(cx - 1, cx + 2):
            for gy in range(cy - 1, cy + 2):
                for (i, st) in grid.get((gx, gy), []):
                    s = segs[i]
                    px = s.start_x if st else s.end_x; py = s.start_y if st else s.end_y
                    if math.hypot(px - x, py - y) <= _STUB_TOL: out.append((i, st))
        return out
    anchored_pts = [(v.x, v.y) for v in vias]
    for p in pads: anchored_pts.append((getattr(p, 'global_x', getattr(p, 'x', 0.0)), getattr(p, 'global_y', getattr(p, 'y', 0.0))))
    zone_polys = [z.polygon for z in zones if z.layer == layer]
    def _anchored(x, y):
        for (ax, ay) in anchored_pts:
            if math.hypot(ax - x, ay - y) <= _STUB_TOL: return True
        for poly in zone_polys:
            if _point_in_polygon(x, y, poly): return True
        return False
    degree = defaultdict(int)
    for i, s in enumerate(segs):
        for (x, y) in ((s.start_x, s.start_y), (s.end_x, s.end_y)): degree[(round(x / cell), round(y / cell))] += 1
    def _deg(x, y): return degree.get((round(x / cell), round(y / cell)), 0)
    def _near_pad(x, y):
        # within pad reach of any pad on this net -> leave copper alone so the
        # pad_entry metric (keyed on the closest segment endpoint) is untouched.
        for p in pads:
            reach = max(getattr(p, 'size_x', 0) or 0, getattr(p, 'size_y', 0) or 0) * 1.5 + 0.5
            if math.hypot(getattr(p, 'global_x', getattr(p, 'x', 0.0)) - x,
                          getattr(p, 'global_y', getattr(p, 'y', 0.0)) - y) <= reach:
                return True
        return False
    remove_ids = set()
    seen_endpoints = set()
    for i, s in enumerate(segs):
        for st in (True, False):
            x = s.start_x if st else s.end_x; y = s.start_y if st else s.end_y
            key = (round(x / cell), round(y / cell))
            if key in seen_endpoints: continue
            seen_endpoints.add(key)
            nb = _nearby(x, y)
            if len(nb) >= 2: continue  # connects another segment -> not dangling
            if _anchored(x, y): continue  # anchored -> not dangling
            cur_i = i; cur_st = st  # dangling endpoint; walk inward
            while True:
                cs = segs[cur_i]
                ox = cs.end_x if cur_st else cs.start_x; oy = cs.end_y if cur_st else cs.start_y
                # Never strip copper that lands on / reaches a pad -- that would
                # disturb the pad_entry metric (which keys on the segment whose
                # endpoint is closest to each pad center).
                if _near_pad(ox, oy): break
                if _anchored(ox, oy): remove_ids.add(id(cs)); break
                dg = _deg(ox, oy)
                if dg >= 3: remove_ids.add(id(cs)); break  # branch point with another continuing branch
                if dg <= 1: remove_ids.add(id(cs)); break  # isolated fragment / other dangling end
                others = [n for n in _nearby(ox, oy) if n[0] != cur_i]
                if not others: remove_ids.add(id(cs)); break
                remove_ids.add(id(cs))
                nxt_i, nxt_st = others[0]
                cur_i = nxt_i; cur_st = nxt_st
    return [s for s in segs if id(s) in remove_ids]


def beautify_stub_cleanup(pcb_data, scope_net_ids=None, skip_net_ids=None):
    """PASS A -- trim genuinely-dangling copper back to the last real junction.
    Returns (removed_segments, nets_changed). Each net's removals are gated on
    check_net_connectivity before/after so connectivity never degrades.
    """
    from check_connected import check_net_connectivity
    by_net = defaultdict(list)
    for s in pcb_data.segments:
        if scope_net_ids is None or s.net_id in scope_net_ids: by_net[s.net_id].append(s)
    removed_all = []
    nets_changed = 0
    for net_id, net_segs in by_net.items():
        if skip_net_ids and net_id in skip_net_ids: continue
        vias = [v for v in pcb_data.vias if v.net_id == net_id]
        pads = pcb_data.pads_by_net.get(net_id, [])
        zones = [z for z in pcb_data.zones if z.net_id == net_id]
        by_layer = defaultdict(list)
        for s in net_segs: by_layer[s.layer].append(s)
        layer_removed = []
        for layer, segs in by_layer.items(): layer_removed.extend(_trim_layer_dangles(segs, vias, pads, zones, layer))
        if not layer_removed: continue
        rem_ids = {id(s) for s in layer_removed}
        kept_segs = [s for s in net_segs if id(s) not in rem_ids]
        if not kept_segs:
            continue  # never strip a net down to zero segments (would unroute it)
        before = check_net_connectivity(net_id, net_segs, vias, pads, zones or None, pcb_data=pcb_data)
        after = check_net_connectivity(net_id, kept_segs, vias, pads, zones or None, pcb_data=pcb_data)
        b_disc = len(before.get('disconnected_pads') or [])
        a_disc = len(after.get('disconnected_pads') or [])
        b_comp = before.get('num_components') or 1; a_comp = after.get('num_components') or 1
        if a_disc > b_disc or a_comp > b_comp or (before.get('connected') and not after.get('connected')): continue
        removed_all.extend(layer_removed)
        nets_changed += 1
    return removed_all, nets_changed


def _pad_entry_info(pad):
    """Return (long_axis_dir, face_normal_dir) in degrees [0,180) for a pad."""
    rot = getattr(pad, 'rotation', 0) or 0
    sx = getattr(pad, 'size_x', 0) or 0
    sy = getattr(pad, 'size_y', 0) or 0
    if sy >= sx:
        long_axis = (rot + 90.0) % 180.0
    else:
        long_axis = rot % 180.0
    face_normal = (long_axis + 90.0) % 180.0
    return long_axis, face_normal


def _angle_between(a1, a2):
    d = abs(a1 - a2) % 180.0
    if d > 90.0:
        d = 180.0 - d
    return d


def _seg_clears(pcb_data, net_id, x1, y1, x2, y2, layer, w,
                clearance):
    """Exact clearance check for a candidate segment against foreign copper,
    mirroring single_ended_routing's clears() kernels."""
    from single_ended_routing import (_seg_foreign_pad_dist,
                                      _seg_foreign_seg_dist,
                                      _seg_foreign_via_dist,
                                      _seg_foreign_hole_dist,
                                      _scan_window)
    from routing_defaults import NPTH_TO_TRACK_CLEARANCE
    npth_clr = max(clearance, NPTH_TO_TRACK_CLEARANCE)
    win = _scan_window(pcb_data, clearance + w / 2.0)
    pd = _seg_foreign_pad_dist(pcb_data, net_id, x1, y1, x2, y2, layer,
                               base_clearance=clearance,
                               window=win)
    if pd < clearance + w / 2.0 - 1e-4:
        return False
    sd = _seg_foreign_seg_dist(pcb_data, net_id, x1, y1, x2, y2, layer,
                               net_clearances=None,
                               base_clearance=clearance,
                               window=win)
    if sd < clearance + w / 2.0 - 1e-4:
        return False
    vd = _seg_foreign_via_dist(pcb_data, net_id, x1, y1, x2, y2, layer,
                               net_clearances=None,
                               base_clearance=clearance,
                               window=win)
    if vd < clearance + w / 2.0 - 1e-4:
        return False
    hd = _seg_foreign_hole_dist(pcb_data, net_id, x1, y1, x2, y2,
                                window=_scan_window(pcb_data, npth_clr + w / 2.0))
    if hd < npth_clr + w / 2.0 - 1e-4:
        return False
    return True


def _try_rebend(pcb_data, net_id, s, pad):
    """Try to re-bend entry segment s so it enters pad perpendicular to its edge.
    Returns (removed_seg_list, added_seg_list) or (None, None) if no clean bend."""
    import math as _m
    # E = endpoint closest to pad center; F = other end
    d_start = _m.hypot(s.start_x - pad.global_x, s.start_y - pad.global_y)
    d_end = _m.hypot(s.end_x - pad.global_x, s.end_y - pad.global_y)
    if d_start <= d_end:
        E = (s.start_x, s.start_y); F = (s.end_x, s.end_y)
    else:
        E = (s.end_x, s.end_y); F = (s.start_x, s.start_y)
    ex, ey = E; fx, fy = F
    w = s.width if s.width > 0 else 0.2
    layer = s.layer
    _, face_normal = _pad_entry_info(pad)
    # face normal unit vector
    ang = _m.radians(face_normal)
    nx = _m.cos(ang); ny = _m.sin(ang)
    # candidate bend points B = E + t*n ; want F->B octolinear.
    # For axis-aligned n we solve for diagonal/axis-aligned F->B.
    candidates = []
    # t such that |ex+tx*nx-fx| == |ey+ty*ny-fy| (diagonal) or one axis zero
    for t in (-0.05,-0.1,-0.15,-0.2,-0.25,-0.3,-0.35,-0.4,-0.45,-0.5,
              0.05,0.1,0.15,0.2,0.25,0.3,0.35,0.4,0.45,0.5):
        bx = ex + t * nx; by = ey + t * ny
        dx = bx - fx; dy = by - fy
        # octolinear: dx==0 or dy==0 or |dx|==|dy|
        if abs(dx) < 1e-6 or abs(dy) < 1e-6 or abs(abs(dx)-abs(dy)) < 1e-6:
            candidates.append((t,bx,by))
        if len(candidates) >= 8:
            break
    for (t,bx,by) in candidates:
        # skip degenerate bend point too close to E or F
        if _m.hypot(bx-ex,by-ey) < 1e-4 or _m.hypot(bx-fx,by-fy) < 1e-4:
            continue
        # both new segments must clear
        if not _seg_clears(pcb_data, net_id, fx,fy,bx,by,layer,w,
                           getattr(pcb_data,'_beautify_clr',0.1)):
            continue
        if not _seg_clears(pcb_data, net_id, bx,by,ex,ey,layer,w,
                           getattr(pcb_data,'_beautify_clr',0.1)):
            continue
        # build new segments
        seg1 = Segment(start_x=fx, start_y=fy, end_x=bx, end_y=by,
                       width=w, layer=layer, net_id=net_id)
        seg2 = Segment(start_x=bx, start_y=by, end_x=ex, end_y=ey,
                       width=w, layer=layer, net_id=net_id)
        return [s], [seg1, seg2]
    return None


def beautify_pad_entry(pcb_data, config=None, scope_net_ids=None,
                       skip_net_ids=None):
    """PASS B -- normalize acute/odd-angle pad entries by re-bending the last
    segment so the entry is near-perpendicular to the pad edge (or along its
    major axis), only when the new geometry passes the exact clearance kernels.
    Returns (removed_segments, added_segments)."""
    import math as _m
    from check_connected import check_net_connectivity
    clr = getattr(config,'clearance',0.1) if config else 0.1
    pcb_data._beautify_clr = clr
    removed = []
    added = []
    nets_changed = set()
    for nid in list(pcb_data.pads_by_net.keys()):
        if skip_net_ids and nid in skip_net_ids: continue
        if scope_net_ids is not None and nid not in scope_net_ids: continue
        pads = pcb_data.pads_by_net.get(nid, [])
        segs = [s for s in pcb_data.segments if s.net_id == nid]
        if not segs: continue
        vias = [v for v in pcb_data.vias if v.net_id == nid]
        zones = [z for z in pcb_data.zones if z.net_id == nid]
        for pad in pads:
            shape = getattr(pad,'shape','')
            if shape == 'circle': continue
            # find entry segment (endpoint closest to pad center)
            best=None; best_d=1e9
            for s in segs:
                for (px,py) in ((s.start_x,s.start_y),(s.end_x,s.end_y)):
                    d=_m.hypot(px-pad.global_x,py-pad.global_y)
                    if d<best_d: best_d=d; best=s
            # only consider pads where a trace actually enters (within reach)
            reach = max(getattr(pad,'size_x',0),getattr(pad,'size_y',0))*1.5 + 0.5
            if best_d > reach: continue
            # compute entry angle vs face normal
            d_start=_m.hypot(best.start_x-pad.global_x,best.start_y-pad.global_y)
            d_end=_m.hypot(best.end_x-pad.global_x,best.end_y-pad.global_y)
            if d_start<=d_end:
                E=(best.start_x,best.start_y);F=(best.end_x,best.end_y)
            else:
                E=(best.end_x,best.end_y);F=(best.start_x,best.start_y)
            entry_dir=_m.degrees(_m.atan2(E[1]-F[1],E[0]-F[0]))%180.0
            _,face_normal=_pad_entry_info(pad)
            d=_angle_between(entry_dir%180.0,(face_normal)%180.0)
            if d<=30: continue  # already good
            res=_try_rebend(pcb_data,nid,best,pad)
            if res is None: continue
            r_segs,a_segs=res
            # Connectivity is preserved BY CONSTRUCTION: the re-bend replaces
            # segment F->E with F->B->E, keeping both endpoints F and E attached
            # to exactly what they were attached to before. No connectivity gate
            # needed (and it would dominate runtime on dense boards).
            removed.extend(r_segs); added.extend(a_segs)
            nets_changed.add(nid)
            # update segs list so subsequent pads on this net see new copper
            r_ids={id(x) for x in r_segs}
            segs=[x for x in segs if id(x) not in r_ids]+a_segs
    try:
        del pcb_data._beautify_clr
    except AttributeError:
        pass
    return removed, added


def run_beautify(pcb_data, scope_net_ids=None, skip_net_ids=None, config=None,
                 stub=True, pad_entry=True):
    """Run both beautification passes; returns (removed_segments, added_segments)."""
    removed = []
    added = []
    if stub:
        r1, _n1 = beautify_stub_cleanup(pcb_data, scope_net_ids=scope_net_ids, skip_net_ids=skip_net_ids)
        removed.extend(r1)
    if pad_entry:
        r2a, r2b = beautify_pad_entry(pcb_data, config=config, scope_net_ids=scope_net_ids, skip_net_ids=skip_net_ids)
        removed.extend(r2a); added.extend(r2b)
    return removed, added
