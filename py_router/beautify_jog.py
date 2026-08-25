# Sub-passes 1 (jog consolidation) + 3 (pad-entry redo) for beautify.
# Ported from carrier_lab/beautify2/jog_final2.py (pass 2b).
import math
from collections import defaultdict
from kicad_parser import Segment

def _chain_segments(segs):
    tol = 1e-3
    segs = list(segs)
    n = len(segs)
    used = [False] * n
    adj = defaultdict(list)
    for i, s in enumerate(segs):
        for p in ((s.start_x, s.start_y), (s.end_x, s.end_y)):
            adj[p].append(i)

    def find_at(p):
        for i in adj.get(p, []):
            if not used[i]:
                return i
        return None

    out = []
    for start_i in range(n):
        if used[start_i]:
            continue
        used[start_i] = True
        chain_segs = [segs[start_i]]
        chain = [(segs[start_i].start_x, segs[start_i].start_y),
                 (segs[start_i].end_x, segs[start_i].end_y)]
        cur = chain[-1]
        while True:
            nxt = find_at(cur)
            if nxt is None:
                break
            used[nxt] = True
            chain_segs.append(segs[nxt])
            p0 = (segs[nxt].start_x, segs[nxt].start_y)
            p1 = (segs[nxt].end_x, segs[nxt].end_y)
            if math.hypot(p0[0]-cur[0], p0[1]-cur[1]) <= tol:
                chain.append(p1); cur = p1
            else:
                chain.append(p0); cur = p0
        cur = chain[0]
        while True:
            nxt = find_at(cur)
            if nxt is None:
                break
            used[nxt] = True
            chain_segs.insert(0, segs[nxt])
            p0 = (segs[nxt].start_x, segs[nxt].start_y)
            p1 = (segs[nxt].end_x, segs[nxt].end_y)
            if math.hypot(p1[0]-cur[0], p1[1]-cur[1]) <= tol:
                chain.insert(0, p0); cur = p0
            else:
                chain.insert(0, p1); cur = p1
        out.append((chain, chain_segs))
    return out


def _angle_between(a1, a2):
    d = abs(a1 - a2) % 180.0
    if d > 90.0:
        d = 180.0 - d
    return d


def _polyline_bends(poly, tol_deg=2.0):
    bends = 0
    for i in range(1, len(poly) - 1):
        a = math.degrees(math.atan2(poly[i][1]-poly[i-1][1], poly[i][0]-poly[i-1][0]))
        b = math.degrees(math.atan2(poly[i+1][1]-poly[i][1], poly[i+1][0]-poly[i][0]))
        if _angle_between(a % 180.0, b % 180.0) > tol_deg:
            bends += 1
    return bends


def _minimal_bends(A, B):
    dx = abs(B[0]-A[0]); dy = abs(B[1]-A[1])
    if dx < 1e-9 and dy < 1e-9:
        return 0
    if dx < 1e-9 or dy < 1e-9:
        return 0
    if abs(dx-dy) < 1e-6:
        return 1
    return 2


def _octolinear_intermediates(A, B):
    ax, ay = A; bx, by = B
    dx, dy = bx-ax, by-ay
    adx, ady = abs(dx), abs(dy)
    sx = 1.0 if dx >= 0 else -1.0
    sy = 1.0 if dy >= 0 else -1.0
    out = []
    if adx < 1e-9 or ady < 1e-9 or abs(adx-ady) < 1e-6:
        out.append([])
    if adx >= ady:
        out.append([(round(ax + sx*ady, 4), round(by, 4))])
        out.append([(round(bx - sx*ady, 4), round(ay, 4))])
    else:
        out.append([(round(bx, 4), round(ay + sy*adx, 4))])
        out.append([(round(ax, 4), round(by - sy*adx, 4))])
    return out


def _pt_to_polyline_dist(px, py, poly):
    best = float('inf')
    for q in range(len(poly)-1):
        ax, ay = poly[q]; bx, by = poly[q+1]
        abx, aby = bx-ax, by-ay
        L2 = abx*abx + aby*aby
        t = 0.0 if L2 < 1e-12 else ((px-ax)*abx + (py-ay)*aby) / L2
        t = max(0.0, min(1.0, t))
        cx = ax + t*abx; cy = ay + t*aby
        d = math.hypot(px-cx, py-cy)
        if d < best:
            best = d
    return best


def _seg_clears_prefiltered(pcb_data, net_id, x1, y1, x2, y2, layer, w,
                            clearance, field=None,
                            keepout_areas=None,
                            edge_geom=None,
                            edge_clr=None):
    from single_ended_routing import (_seg_foreign_pad_dist,
                                      _seg_foreign_seg_dist,
                                      _seg_foreign_via_dist,
                                      _seg_foreign_hole_dist,
                                      _scan_window)
    from routing_defaults import NPTH_TO_TRACK_CLEARANCE
    npth_clr = max(clearance, NPTH_TO_TRACK_CLEARANCE)
    win = _scan_window(pcb_data, clearance + w / 2.0)
    if field is not None:
        from clearance_field import field_lower_bound
        req = clearance + w / 2.0 - 1e-4
        bnd = field_lower_bound(field, x1, y1, x2, y2)
        if bnd is not None and bnd >= req:
            return True
    pd = _seg_foreign_pad_dist(pcb_data, net_id, x1, y1, x2, y2, layer,
                               base_clearance=clearance, window=win)
    if pd < clearance + w / 2.0 - 1e-4:
        return False
    sd = _seg_foreign_seg_dist(pcb_data, net_id, x1, y1, x2, y2, layer,
                               net_clearances=None, base_clearance=clearance,
                               window=win)
    if sd < clearance + w / 2.0 - 1e-4:
        return False
    vd = _seg_foreign_via_dist(pcb_data, net_id, x1, y1, x2, y2, layer,
                               net_clearances=None, base_clearance=clearance,
                               window=win)
    if vd < clearance + w / 2.0 - 1e-4:
        return False
    hd = _seg_foreign_hole_dist(pcb_data, net_id, x1, y1, x2, y2,
                                window=_scan_window(pcb_data, npth_clr + w / 2.0))
    if hd < npth_clr + w / 2.0 - 1e-4:
        return False
    # Fix 2: keepout + board-edge clearance must be in the foreign set.
    if keepout_areas is not None and \
       not _keepout_clears(keepout_areas,
                           x1,
                           y1,
                           x2,
                           y2,
                           layer,
                           w,
                           clearance):
        return False
    if edge_geom is not None and edge_clr is not None:
        rings_, outer_, cutouts_, bounds_ = edge_geom
        if not _edge_clears(rings_,
                            outer_,
                            cutouts_,
                            bounds_,
                            edge_clr,
                            x1,
                            y1,
                            x2,
                            y2,
                            w):
            return False
    return True


def _build_keepout_areas(pcb_data, config):
    areas = []
    for ko in (getattr(pcb_data.board_info, 'keepouts', None) or []):
        if ko.get('tracks_allowed', True):
            continue
        poly = ko.get('polygon') or []
        if len(poly) < 3:
            continue
        rings = [poly] + [h for h in (ko.get('holes') or []) if len(h) >= 3]
        kxs = [p[0] for r in rings for p in r]
        kys = [p[1] for r in rings for p in r]
        kls = ko.get('layers') or set()
        areas.append((rings,
                      (min(kxs), min(kys), max(kxs), max(kys)),
                      set(kls) if kls else None))
    if getattr(config, 'keepout_enabled', False):
        for kz in (getattr(pcb_data, 'keepout_zones', None) or []):
            if len(kz.points) >= 3:
                kxs = [p[0] for p in kz.points]
                kys = [p[1] for p in kz.points]
                areas.append(([list(kz.points)],
                              (min(kxs), min(kys), max(kxs), max(kys)),
                              None))
    return areas


def _ko_on_layer(kls, layer):
    if kls is None:
        return True
    return (layer in kls or '*.Cu' in kls
            or (layer in ('F.Cu', 'B.Cu') and bool({'F&B.Cu', 'F&B'} & kls)))


def _keepout_clears(areas, x1, y1, x2, y2, layer, w, clearance):
    if not areas:
        return True
    from obstacle_map import point_in_polygon, point_to_polygon_edge_distance
    margin = clearance + w / 2.0
    for rings, (kx0, ky0, kx1, ky1), kls in areas:
        if not _ko_on_layer(kls, layer):
            continue
        if (max(x1, x2) < kx0 - margin or min(x1, x2) > kx1 + margin or
                max(y1, y2) < ky0 - margin or min(y1, y2) > ky1 + margin):
            continue
        n = max(2, int(math.hypot(x2 - x1, y2 - y1) / 0.1) + 1)
        for q in range(n + 1):
            t = q / n
            px, py = x1 + t * (x2 - x1), y1 + t * (y2 - y1)
            inside = False
            for ring in rings:
                if point_in_polygon(px, py, ring):
                    inside = not inside
            if inside:
                return False
            if any(point_to_polygon_edge_distance(px, py, ring) < margin
                   for ring in rings):
                return False
    return True


def _edge_clears(edge_rings, edge_outer, edge_cutouts,
                 board_bounds, edge_clr,
                 x1, y1, x2, y2, w):
    required = edge_clr + w / 2.0 - 1e-4
    if edge_rings:
        from check_drc import _point_on_board, _segment_to_rings_distance
        if not _point_on_board(x1, y1, edge_outer, edge_cutouts) or            not _point_on_board(x2, y2, edge_outer, edge_cutouts):
            return False
        return _segment_to_rings_distance(x1, y1, x2, y2,
                                          edge_rings) >= required
    if board_bounds:
        min_x, min_y, max_x, max_y = board_bounds
        return all(min(x - min_x, max_x - x,
                       y - min_y, max_y - y) >= required
                   for x, y in ((x1, y1), (x2, y2)))
    return True


def _build_edge_geometry(pcb_data):
    from check_drc import board_edge_geometry
    try:
        rings, outer, cutouts = board_edge_geometry(pcb_data.board_info)
    except Exception:
        rings = []
        outer = None
        cutouts = []
    return (rings or [], outer or None,
            cutouts or [], pcb_data.board_info.board_bounds)


# ---------------------------------------------------------------------------
# Fix 3: robust connectivity gate.
# ---------------------------------------------------------------------------

def _near_corunning_foreign(grid, cell, pcb_data, net_id, layer,
                            x1, y1, x2, y2, guard_mm):
    """True if the segment runs within guard_mm mm of a co-running (<=10 deg)
    foreign segment on the same layer. Used to avoid disturbing tidy parallel
    bundles (which would regress metric_parallel_coherence)."""
    x0, xa = min(x1, x2), max(x1, x2)
    y0, ya = min(y1, y2), max(y1, y2)
    ang = math.degrees(math.atan2(y2 - y1, x2 - x1)) % 180.0
    for cx in range(int(x0 / cell) - 1, int(xa / cell) + 2):
        for cy in range(int(y0 / cell) - 1, int(ya / cell) + 2):
            for o in grid.get((cx, cy), []):
                if o.net_id == net_id:
                    continue  # same-net copper is not a parallel-bundle neighbour
                oang = math.degrees(math.atan2(o.end_y - o.start_y,
                                               o.end_x - o.start_x)) % 180.0
                if _angle_between(ang, oang) > 10.0:
                    continue
                d = _seg_seg_dist(x1, y1, x2, y2,
                                  o.start_x, o.start_y, o.end_x, o.end_y)
                if d < guard_mm:
                    return True
    return False


def _build_foreign_index(pcb_data, layer):
    """Grid index of ALL segments on a layer (same-net filtering happens at
    query time in _near_corunning_foreign). Built once per layer and cached."""
    cell = 1.0
    grid = defaultdict(list)
    for s in pcb_data.segments:
        if s.layer != layer:
            continue
        x0, x1 = min(s.start_x, s.end_x), max(s.start_x, s.end_x)
        y0, y1 = min(s.start_y, s.end_y), max(s.start_y, s.end_y)
        for cx in range(int(x0 / cell) - 1, int(x1 / cell) + 2):
            for cy in range(int(y0 / cell) - 1, int(y1 / cell) + 2):
                grid[(cx, cy)].append(s)
    return grid, cell


def _seg_seg_dist(ax1, ay1, ax2, ay2, bx1, by1, bx2, by2):
    def pt_seg(px, py, x1, y1, x2, y2):
        abx, aby = x2 - x1, y2 - y1
        L2 = abx * abx + aby * aby
        t = 0.0 if L2 < 1e-12 else ((px - x1) * abx + (py - y1) * aby) / L2
        t = max(0.0, min(1.0, t))
        return math.hypot(px - (x1 + t * abx), py - (y1 + t * aby))
    best = min(pt_seg(ax1, ay1, bx1, by1, bx2, by2),
               pt_seg(ax2, ay2, bx1, by1, bx2, by2),
               pt_seg(bx1, by1, ax1, ay1, ax2, ay2),
               pt_seg(bx2, by2, ax1, ay1, ax2, ay2))
    return best



# ---------------------------------------------------------------------------
# Fix 2: keepout + board-edge clearance in the foreign set.
# ---------------------------------------------------------------------------

def _new_seg_overlaps_kept(new_seg,
                           kept_same_layer_segs):
    """True if new_seg exactly duplicates (endpoints within ~5um) any kept
    same-net same-layer segment -- the stacked-copper definition."""
    a_new = (round(new_seg.start_x,3), round(new_seg.start_y,3))
    b_new = (round(new_seg.end_x,3), round(new_seg.end_y,3))
    lo_new = min(a_new,b_new); hi_new=max(a_new,b_new)
    for o in kept_same_layer_segs:
        a_o=(round(o.start_x,3),round(o.start_y,3))
        b_o=(round(o.end_x,3),round(o.end_y,3))
        lo_o=min(a_o,b_o); hi_o=max(a_o,b_o)
        if lo_new==lo_o and hi_new==hi_o:
            return True
    return False


def _bundle_blocked(grid,
                    cell,
                    pcb_data,
                    net_id,
                    layer,
                    pts,
                    guard_mm):
    for q in range(len(pts)-1):
        if _near_corunning_foreign(grid,
                                   cell,
                                   pcb_data,
                                   net_id,
                                   layer,
                                   pts[q][0],
                                   pts[q][1],
                                   pts[q+1][0],
                                   pts[q+1][1],
                                   guard_mm):
            return True
    return False


# ---------------------------------------------------------------------------
# Fix 1: same-net overlap detection.
# ---------------------------------------------------------------------------

def _net_gate_passes(nid,
                     net_segs_all,
                     vias,
                     pads,
                     zones,
                     net_removed_ids,
                     net_added,
                     pcb_data):
    from check_connected import check_net_connectivity
    before = check_net_connectivity(nid,
                                    net_segs_all,
                                    vias,
                                    pads,
                                    zones or None,
                                    pcb_data=pcb_data)
    trial = [s for s in net_segs_all if id(s) not in net_removed_ids] + net_added
    after = check_net_connectivity(nid,
                                   trial,
                                   vias,
                                   pads,
                                   zones or None,
                                   pcb_data=pcb_data)
    b_disc = len(before.get('disconnected_pads') or [])
    a_disc = len(after.get('disconnected_pads') or [])
    b_comp = before.get('num_components') or 1
    a_comp = after.get('num_components') or 1
    if a_disc > b_disc or a_comp > b_comp or        (before.get('connected') and not after.get('connected')):
        return False

    # Every endpoint of every NEW segment must land on kept copper / pad / via
    # within a tight tolerance -- catches a re-embedded path that floats a hair
    # off its anchor (which check_net_connectivity's permissive tolerance can
    # miss but the authoritative check_connected CLI grades as disconnected).
    kept_ids = {id(s) for s in net_segs_all if id(s) not in net_removed_ids}
    kept_segs = [s for s in net_segs_all if id(s) in kept_ids]
    pad_pts = [(getattr(p,'global_x',getattr(p,'x',0)),
                getattr(p,'global_y',getattr(p,'y',0))) for p in pads]
    via_pts = [(v.x,v.y) for v in vias]
    tol_endpoint = 5e-3   # 5um

    def _anchored(x,y):
        for (ax_,ay_) in pad_pts:
            if math.hypot(ax_-x, ay_-y) <= tol_endpoint:
                return True
        for (vx_,vy_) in via_pts:
            if math.hypot(vx_-x, vy_-y) <= tol_endpoint:
                return True
        for s in kept_segs:
            if math.hypot(s.start_x-x,s.start_y-y) <= tol_endpoint or                math.hypot(s.end_x-x,s.end_y-y) <= tol_endpoint:
                return True
            d = _pt_to_polyline_dist(x,y,[(s.start_x,s.start_y),
                                          (s.end_x,s.end_y)])
            if d <= tol_endpoint:
                return True
        return False

    # Only PATH ENDPOINTS must land on kept copper: a point shared by two NEW
    # segments is an interior vertex of the re-embedded path and is anchored by
    # the new copper itself. A point appearing in exactly one new segment is a
    # path endpoint and must sit on kept copper / pad / via.
    from collections import Counter
    pt_count = Counter()
    for s in net_added:
        pt_count[(round(s.start_x,3), round(s.start_y,3))] += 1
        pt_count[(round(s.end_x,3), round(s.end_y,3))] += 1
    for s in net_added:
        for (x,y) in ((s.start_x,s.start_y),(s.end_x,s.end_y)):
            if pt_count[(round(x,3), round(y,3))] >= 2:
                continue
            if not _anchored(x,y):
                return False
    return True


# ---------------------------------------------------------------------------
# Fix 4: parallel-bundle guard.
# ---------------------------------------------------------------------------

def _pad_info(pad):
    rot = getattr(pad,'rotation',0) or 0
    sx = getattr(pad,'size_x',0) or 0
    sy = getattr(pad,'size_y',0) or 0
    if sy >= sx:
        long_axis = (rot + 90.0) % 180.0
    else:
        long_axis = rot % 180.0
    face_normal = (long_axis + 90.0) % 180.0
    return long_axis, face_normal


def beautify_jog_consolidation(pcb_data, config=None, scope_net_ids=None,
                               skip_net_ids=None):
    clr = getattr(config,'clearance',0.1) if config else 0.1
    bec = getattr(config,'board_edge_clearance',0.0) or 0.0
    edge_clr = max(clr, bec)
    pcb_data._beautify_clr = clr
    removed = []
    added = []
    nets_changed = set()
    max_dev = 0.2   # mm: keep re-embedding local so parallel bundles stay tidy
    max_span = 8    # vertices: only collapse LOCAL micro-jog clusters

    # Fix 4: bundle guard distance ~3x trace pitch (computed per-net below).
    bundle_guard_mm = getattr(config, 'bundle_guard_mm', None)
    # Fix 2: build keepout areas + edge geometry once.
    keepout_areas = _build_keepout_areas(pcb_data, config or type('C', (), {})())
    edge_geom = _build_edge_geometry(pcb_data)

    from check_connected import check_net_connectivity

    # Fix 4: foreign index is built once per layer and cached (same-net
    # filtering happens at query time), not once per net.
    _foreign_cache = {}

    by_net_layer = defaultdict(list)
    for s in pcb_data.segments:
        if scope_net_ids is None or s.net_id in scope_net_ids:
            by_net_layer[(s.net_id, s.layer)].append(s)

    for (nid, layer), segs in by_net_layer.items():
        if skip_net_ids and nid in skip_net_ids:
            continue
        widths = [s.width for s in segs]
        w = max(widths) if widths else 0.25
        # #498: honor per-layer .kicad_dru clearance rules (replacement, may be
        # tighter than clr) so re-embedded copper grades clean at the rule.
        layer_clr = clr
        if config is not None and hasattr(config, 'layer_clearance'):
            layer_clr = config.layer_clearance(layer, clr)
        net_segs_all = [s for s in pcb_data.segments if s.net_id == nid]
        vias = [v for v in pcb_data.vias if v.net_id == nid]
        pads = pcb_data.pads_by_net.get(nid, [])

        # No clearance field: building one per net is far more expensive than
        # the exact foreign-dist checks it would only fast-path, so pass None
        # and let _seg_clears_prefiltered do the precise per-segment checks.
        field = None

        via_pts = {(round(v.x,3), round(v.y,3)) for v in vias}
        pad_pts = set()
        for p in pads:
            pad_pts.add((round(getattr(p,'global_x',getattr(p,'x',0)),3),
                         round(getattr(p,'global_y',getattr(p,'y',0)),3)))
        inc = defaultdict(int)
        for s in segs:
            inc[(round(s.start_x,3), round(s.start_y,3))] += 1
            inc[(round(s.end_x,3), round(s.end_y,3))] += 1
        branch_pts = {k for k,v in inc.items() if v > 2}
        pad_reach = {}
        for p in pads:
            reach = max(getattr(p,'size_x',0) or 0, getattr(p,'size_y',0) or 0) * 1.5 + 0.5
            pad_reach[(round(getattr(p,'global_x',getattr(p,'x',0)),3),
                       round(getattr(p,'global_y',getattr(p,'y',0)),3))] = reach

        # Fix 4: bundle guard distance ~3x trace pitch (per-net).
        if bundle_guard_mm is None:
            bg = max((w + clr) * 3.0, 0.5)
        else:
            bg = bundle_guard_mm
        # Fix 4: foreign index on this layer for the bundle guard (cached).
        # NOTE: setdefault would evaluate its default eagerly, so build only
        # when the layer is not already cached.
        if layer not in _foreign_cache:
            _foreign_cache[layer] = _build_foreign_index(pcb_data, layer)
        fgrid, fcell = _foreign_cache[layer]

        net_removed_ids = set()
        net_added = []
        for poly, chain_segs in _chain_segments(segs):
            n = len(poly)
            if n < 3:
                continue
            i = 0
            while i < n - 2:
                best_j = None
                best_path = None
                best_new = None
                for j in range(n - 1, i + 1, -1):
                    if j - i > max_span:
                        continue
                    blocked = False
                    for k in range(i+1, j):
                        key = (round(poly[k][0],3), round(poly[k][1],3))
                        if key in via_pts or key in pad_pts or key in branch_pts:
                            blocked = True
                            break
                        for (pcx,pcy),reach in pad_reach.items():
                            if math.hypot(poly[k][0]-pcx, poly[k][1]-pcy) <= reach:
                                blocked = True
                                break
                        if blocked:
                            break
                    if blocked:
                        continue
                    cur_bends = _polyline_bends(poly[i:j+1])
                    min_bends = _minimal_bends(poly[i], poly[j])
                    if cur_bends <= min_bends:
                        continue
                    span_poly = poly[i:j+1]
                    chosen_path = None
                    chosen_new = None
                    for inter in _octolinear_intermediates(poly[i], poly[j]):
                        pts = [poly[i]] + inter + [poly[j]]
                        ok = True
                        # stay-local constraint: each new intermediate point must be
                        # within max_dev of the original span (preserve parallel spacing)
                        for q in range(1, len(pts)-1):
                            if _pt_to_polyline_dist(pts[q][0], pts[q][1], span_poly) > max_dev:
                                ok = False; break
                        if not ok:
                            continue
                        # Fix 4: bundle guard -- skip windows whose new path runs
                        # near a co-running foreign segment.
                        if _bundle_blocked(fgrid, fcell, pcb_data, nid, layer, pts, bg):
                            ok = False; continue
                        new_segs = []
                        for q in range(len(pts)-1):
                            if math.hypot(pts[q+1][0]-pts[q][0], pts[q+1][1]-pts[q][1]) < 1e-5:
                                ok = False; break
                            ns = Segment(start_x=pts[q][0], start_y=pts[q][1],
                                         end_x=pts[q+1][0], end_y=pts[q+1][1],
                                         width=w, layer=layer, net_id=nid)
                            new_segs.append(ns)
                        if not ok:
                            continue
                        # Fix 2: clearance incl keepout/edge.
                        for ns in new_segs:
                            if not _seg_clears_prefiltered(
                                    pcb_data, nid,
                                    ns.start_x, ns.start_y,
                                    ns.end_x, ns.end_y,
                                    layer, w, layer_clr,
                                    field=field,
                                    keepout_areas=keepout_areas,
                                    edge_geom=edge_geom,
                                    edge_clr=edge_clr):
                                ok = False; break
                        if not ok:
                            continue
                        chosen_path = pts
                        chosen_new = new_segs
                        break
                    if chosen_path is not None:
                        best_j = j
                        best_path = chosen_path
                        best_new = chosen_new
                        break
                if best_j is not None and best_new is not None:
                    # Fix 4: bundle guard on the final chosen path.
                    if _bundle_blocked(fgrid, fcell, pcb_data, nid, layer,
                                       best_path,
                                       bg):
                        i += max(1, best_j - i)
                        continue
                    # Fix 1: reject if any new segment duplicates a kept same-net
                    # segment (stacked-copper). Kept = same-net same-layer segs not
                    # being removed.
                    kept_same_layer = [s for s in net_segs_all
                                       if s.layer == layer and id(s) not in net_removed_ids]
                    dup = False
                    for ns in best_new:
                        if _new_seg_overlaps_kept(ns, kept_same_layer):
                            dup = True; break
                    if dup:
                        i += max(1, best_j - i)
                        continue
                    for e in range(i, best_j):
                        net_removed_ids.add(id(chain_segs[e]))
                    net_added.extend(best_new)
                    i = best_j
                else:
                    i += 1

        if not net_added:
            continue
        rem_segs = [s for s in net_segs_all if id(s) in net_removed_ids]
        # Fix 3: robust connectivity gate (catches tiny gaps).
        zones_nid = [z for z in pcb_data.zones if z.net_id == nid]
        if not _net_gate_passes(nid, net_segs_all, vias, pads,
                                zones_nid,
                                net_removed_ids, net_added,
                                pcb_data):
            continue
        removed.extend(rem_segs)
        added.extend(net_added)
        nets_changed.add(nid)

    try:
        del pcb_data._beautify_clr
    except AttributeError:
        pass
    return removed, added




def beautify_pad_entry_redo(pcb_data, config=None, scope_net_ids=None,
                            skip_net_ids=None):
    """Sub-pass 3: re-embed the last few mm of each bad-angle pad approach so
    the entry is near-perpendicular AND stays jog-clean (single clean corner,
    no bend increase over the window). Replaces pass-1's append-a-jog."""
    clr = getattr(config,'clearance',0.1) if config else 0.1
    bec = getattr(config,'board_edge_clearance',0.0) or 0.0
    edge_clr = max(clr, bec)
    pcb_data._beautify_clr = clr
    removed = []
    added = []
    nets_changed = set()
    max_dev = 0.4

    # Fix 2: build keepout areas + edge geometry once.
    keepout_areas = _build_keepout_areas(pcb_data, config or type('C', (), {})())
    edge_geom = _build_edge_geometry(pcb_data)

    from check_connected import check_net_connectivity

    for nid in list(pcb_data.pads_by_net.keys()):
        if skip_net_ids and nid in skip_net_ids:
            continue
        if scope_net_ids is not None and nid not in scope_net_ids:
            continue
        pads = pcb_data.pads_by_net.get(nid, [])
        segs = [s for s in pcb_data.segments if s.net_id == nid]
        if not segs:
            continue
        vias = [v for v in pcb_data.vias if v.net_id == nid]
        zones = [z for z in pcb_data.zones if z.net_id == nid]
        chains = _chain_segments(segs)
        net_segs_all = segs

        # Spatial grid of chain endpoints built once per net so the per-pad
        # nearest-endpoint search is O(nearby cells) instead of O(all segs).
        _cell = 1.0
        _ep_grid = defaultdict(list)
        for _ci, (_poly, _csegs) in enumerate(chains):
            for _si, _s in enumerate(_csegs):
                for (_px, _py) in ((_s.start_x, _s.start_y),
                                   (_s.end_x, _s.end_y)):
                    _ep_grid[(int(_px / _cell), int(_py / _cell))].append(
                        (_px, _py, _ci, _si))

        net_removed_ids = set()
        net_added = []
        for pad in pads:
            shape = getattr(pad,'shape','')
            if shape == 'circle':
                continue
            # find entry endpoint closest to pad center (grid-scoped)
            reach = max(getattr(pad,'size_x',0), getattr(pad,'size_y',0))*1.5 + 0.5
            best_E = None; best_F = None; best_d = 1e9
            best_poly = None; best_csegs = None
            _gx0 = int((pad.global_x - reach) / _cell) - 1
            _gx1 = int((pad.global_x + reach) / _cell) + 1
            _gy0 = int((pad.global_y - reach) / _cell) - 1
            _gy1 = int((pad.global_y + reach) / _cell) + 1
            for _gx in range(_gx0, _gx1 + 1):
                for _gy in range(_gy0, _gy1 + 1):
                    for (_px, _py, _ci, _si) in _ep_grid.get((_gx, _gy), []):
                        d = math.hypot(_px - pad.global_x, _py - pad.global_y)
                        if d < best_d:
                            best_d = d; best_E = (_px, _py)
                            _poly, _csegs = chains[_ci]
                            _s = _csegs[_si]
                            best_F = ((_s.end_x, _s.end_y)
                                      if (_px == _s.start_x and _py == _s.start_y)
                                      else (_s.start_x, _s.start_y))
                            best_poly = _poly; best_csegs = _csegs
            if best_d > reach or best_E is None:
                continue
            _, face_normal = _pad_info(pad)
            entry_dir = math.degrees(math.atan2(best_E[1]-best_F[1],
                                                best_E[0]-best_F[0])) % 180.0
            if _angle_between(entry_dir % 180.0, face_normal % 180.0) <= 30:
                continue
            # window: last up-to-3 segments ending at E within ~4mm of pad center
            w = max((s.width for s in best_csegs), default=0.2)
            layer = best_csegs[0].layer
            # #498: honor per-layer .kicad_dru clearance rules.
            layer_clr = clr
            if config is not None and hasattr(config, 'layer_clearance'):
                layer_clr = config.layer_clearance(layer, clr)
            # build polyline of chain; find index of E vertex
            poly = best_poly
            n = len(poly)
            eidx = None
            for k in range(n):
                if math.hypot(poly[k][0]-best_E[0], poly[k][1]-best_E[1]) < 1e-4:
                    eidx = k; break
            if eidx is None or eidx < 2:
                continue
            # window start: walk back until cumulative length ~4mm or max 3 segs
            start = eidx - 1
            acc = math.hypot(poly[eidx][0]-poly[eidx-1][0],
                             poly[eidx][1]-poly[eidx-1][1])
            cnt = 1
            while start > 0 and acc < 4.0 and cnt < 3:
                start -= 1
                acc += math.hypot(poly[start+1][0]-poly[start][0],
                                  poly[start+1][1]-poly[start][1])
                cnt += 1
            # skip if window contains a branch point (would strand a stub)
            inc2 = defaultdict(int)
            for s2 in segs:
                inc2[(round(s2.start_x,3),round(s2.start_y,3))] += 1
                inc2[(round(s2.end_x,3),round(s2.end_y,3))] += 1
            skip = False
            for k in range(start+1, eidx):
                if inc2[(round(poly[k][0],3),round(poly[k][1],3))] > 2:
                    skip = True; break
            if skip:
                continue
            A = poly[start]; Ept = poly[eidx]
            span_poly = poly[start:eidx+1]
            cur_bends = _polyline_bends(span_poly)
            # candidate paths: A -> ... -> Ept where final segment enters along face normal
            ang = math.radians(face_normal)
            nx = math.cos(ang); ny = math.sin(ang)
            chosen_path = None
            chosen_new = None
            for t in (-0.05,-0.1,-0.15,-0.2,-0.25,-0.3,-0.35,-0.4,-0.45,-0.5,
                      0.05,0.1,0.15,0.2,0.25,0.3,0.35,0.4,0.45,0.5):
                bx = Ept[0] + t*nx; by = Ept[1] + t*ny
                for inter in _octolinear_intermediates(A,(bx,by)):
                    pts = [A] + inter + [(bx,by), Ept]
                    ok = True
                    for q in range(1,len(pts)-1):
                        if _pt_to_polyline_dist(pts[q][0],pts[q][1],span_poly) > max_dev:
                            ok=False; break
                    if not ok:
                        continue
                    new_bends = _polyline_bends(pts)
                    if new_bends > cur_bends:
                        continue   # net local jog count must not increase
                    jw = max(2.0, 8*w)
                    bend_pos = []
                    for q in range(1, len(pts)-1):
                        aa = math.degrees(math.atan2(pts[q][1]-pts[q-1][1],
                                                     pts[q][0]-pts[q-1][0])) % 180
                        bb = math.degrees(math.atan2(pts[q+1][1]-pts[q][1],
                                                     pts[q+1][0]-pts[q][0])) % 180
                        if _angle_between(aa, bb) > 2.0:
                            bend_pos.append(q)
                    bad = False
                    for bi in range(len(bend_pos)):
                        for bj in range(bi+1, len(bend_pos)):
                            qi=bend_pos[bi]; qj=bend_pos[bj]
                            dd=0.0
                            for q in range(qi,qj):
                                dd += math.hypot(pts[q+1][0]-pts[q][0],
                                                 pts[q+1][1]-pts[q][1])
                            if dd < jw:
                                bad=True; break
                        if bad: break
                    if bad:
                        continue
                    fd = math.degrees(math.atan2(Ept[1]-by,Ept[0]-bx)) % 180.0
                    if _angle_between(fd % 180.0, face_normal % 180.0) > 30:
                        continue
                    new_segs=[]
                    for q in range(len(pts)-1):
                        if math.hypot(pts[q+1][0]-pts[q][0],pts[q+1][1]-pts[q][1]) < 1e-5:
                            ok=False; break
                        ns=Segment(start_x=pts[q][0],start_y=pts[q][1],
                                   end_x=pts[q+1][0],end_y=pts[q+1][1],
                                   width=w,layer=layer,net_id=nid)
                        new_segs.append(ns)
                    if not ok:
                        continue
                    # Fix 2: clearance incl keepout/edge.
                    for ns in new_segs:
                        if not _seg_clears_prefiltered(
                                pcb_data,nid,
                                ns.start_x,ns.start_y,
                                ns.end_x,ns.end_y,
                                layer,w,layer_clr,
                                keepout_areas=keepout_areas,
                                edge_geom=edge_geom,
                                edge_clr=edge_clr):
                            ok=False; break
                    if not ok:
                        continue
                    chosen_path=pts; chosen_new=new_segs; break
                if chosen_path is not None:
                    break
            if chosen_path is None or chosen_new is None:
                continue
            # Fix 1: reject if any new segment duplicates a kept same-net segment.
            kept_same_layer=[s for s in net_segs_all
                             if s.layer==layer and id(s) not in net_removed_ids]
            dup=False
            for ns in chosen_new:
                if _new_seg_overlaps_kept(ns, kept_same_layer):
                    dup=True; break
            if dup:
                continue
            # remove window segments [start..eidx) from chain segs
            for e in range(start, eidx):
                net_removed_ids.add(id(best_csegs[e]))
            net_added.extend(chosen_new)

        if not net_added:
            continue
        rem_segs=[s for s in net_segs_all if id(s) in net_removed_ids]
        # Fix 3: robust connectivity gate.
        if not _net_gate_passes(nid, net_segs_all, vias, pads,
                                zones,
                                net_removed_ids, net_added,
                                pcb_data):
            continue
        removed.extend(rem_segs); added.extend(net_added); nets_changed.add(nid)

    try:
        del pcb_data._beautify_clr
    except AttributeError:
        pass
    return removed, added



