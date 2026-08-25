"""Batched clearance kernels (Phase 2.5 Task 2).

Vectorized analogues of the single-segment _seg_foreign_* kernels in
single_ended_routing.py. They evaluate a SET of segments against the foreign
obstacles in ONE set of numpy matrix ops (broadcasting N segments x M
obstacles), returning an array of per-segment distances -- bit-for-bit the
same exact geometry as calling the single-segment kernel once per segment.
Used by the smooth candidate loop to evaluate candidate sets in batches
instead of one clears() call per candidate from Python.
"""

import math
import numpy as np

from single_ended_routing import (
    _FOREIGN_PAD_WINDOW,
    _foreign_pad_arrays,
    _foreign_seg_arrays,
    _foreign_via_arrays,
    _foreign_hole_capsules,
    _custom_pad_min_dist,
)


def _capsule_batch_dist(x1s, y1s, x2s, y2s, ax, ay, bx, by):
    """Exact distance from each of N query segments to each of M capsule AXIS
    segments -- the batched analogue of _seg_capsule_axis_dist. Returns an
    (N,M) array of segment-to-segment distances (0 on proper crossing)."""
    def pt_to_capsule(px, py):
        # distance from N query points to M capsule axes -> (N,M)
        dx = bx - ax; dy = by - ay
        L2 = dx * dx + dy * dy
        safe = np.where(L2 > 0, L2, 1.0)
        t = np.clip(((px[:, None] - ax[None, :]) * dx[None, :] +
                     (py[:, None] - ay[None, :]) * dy[None, :]) / safe[None, :],
                    0.0, 1.0)
        return np.hypot(px[:, None] - (ax[None, :] + t * dx[None, :]),
                        py[:, None] - (ay[None, :] + t * dy[None, :]))

    def capsule_pt_to_seg(px, py):
        # distance from M capsule-endpoint points to N query segments -> (N,M)
        sdx = x2s - x1s; sdy = y2s - y1s
        L2 = sdx * sdx + sdy * sdy
        safe = np.where(L2 > 0, L2, 1.0)[:, None]
        t = np.clip(((px[None, :] - x1s[:, None]) * sdx[:, None] +
                     (py[None, :] - y1s[:, None]) * sdy[:, None]) / safe,
                    0.0, 1.0)
        return np.hypot(px[None, :] - (x1s[:, None] + t * sdx[:, None]),
                        py[None, :] - (y1s[:, None] + t * sdy[:, None]))

    d = np.minimum(capsule_pt_to_seg(ax, ay), capsule_pt_to_seg(bx, by))
    d = np.minimum(d, pt_to_capsule(x1s, y1s))
    d = np.minimum(d, pt_to_capsule(x2s, y2s))
    sdx = x2s - x1s; sdy = y2s - y1s
    hdx = bx - ax; hdy = by - ay
    o1 = sdx[:, None] * (ay[None, :] - y1s[:, None]) - sdy[:, None] * (ax[None, :] - x1s[:, None])
    o2 = sdx[:, None] * (by[None, :] - y1s[:, None]) - sdy[:, None] * (bx[None, :] - x1s[:, None])
    o3 = hdx[None, :] * (y1s[:, None] - ay[None, :]) - hdy[None, :] * (x1s[:, None] - ax[None, :])
    o4 = hdx[None, :] * (y2s[:, None] - ay[None, :]) - hdy[None, :] * (x2s[:, None] - ax[None, :])
    crossing = (o1 * o2 < 0) & (o3 * o4 < 0)
    return np.where(crossing, 0.0, d)


def _seg_foreign_pad_dist_batch(pcb_data, net_ids, x1s, y1s, x2s, y2s, layer,
                                base_clearance=None, net_clearances=None,
                                window=_FOREIGN_PAD_WINDOW):
    """Batched _seg_foreign_pad_dist: per-segment min foreign-pad edge distance."""
    nids_arr = np.asarray(net_ids)
    N = len(x1s)
    nids_pad, cx, cy, hx, hy, crr_, rc_, rs_, ex_, ey_, plc_, custom = \
        _foreign_pad_arrays(pcb_data, layer)
    best_custom = np.full(N, 1e9)
    if custom:
        for i in range(N):
            n = max(2, int(math.hypot(x2s[i] - x1s[i], y2s[i] - y1s[i]) / 0.02) + 1)
            _t = np.linspace(0.0, 1.0, n + 1)
            sx = x1s[i] + (x2s[i] - x1s[i]) * _t
            sy = y1s[i] + (y2s[i] - y1s[i]) * _t
            best_custom[i] = _custom_pad_min_dist(
                custom, int(nids_arr[i]), list(zip(sx.tolist(), sy.tolist())),
                base_clearance, window=window)
    if cx.size == 0:
        return best_custom
    R = window
    minx = np.minimum(x1s, x2s); maxx = np.maximum(x1s, x2s)
    miny = np.minimum(y1s, y2s); maxy = np.maximum(y1s, y2s)
    near = ((cx[None, :] + ex_[None, :] >= minx[:, None] - R) &
            (cx[None, :] - ex_[None, :] <= maxx[:, None] + R) &
            (cy[None, :] + ey_[None, :] >= miny[:, None] - R) &
            (cy[None, :] - ey_[None, :] <= maxy[:, None] + R) &
            (nids_pad[None, :] != nids_arr[:, None]))
    ddx1 = x1s[:, None] - cx[None, :]; ddy1 = y1s[:, None] - cy[None, :]
    ddx2 = x2s[:, None] - cx[None, :]; ddy2 = y2s[:, None] - cy[None, :]
    frc = rc_[None, :]; frs = rs_[None, :]
    lx1 = ddx1 * frc + ddy1 * frs; ly1 = -ddx1 * frs + ddy1 * frc
    lx2 = ddx2 * frc + ddy2 * frs; ly2 = -ddx2 * frs + ddy2 * frc
    ihx = hx[None, :] - crr_[None, :]; ihy = hy[None, :] - crr_[None, :]

    def _pt_seg(px_, py_):
        _dx = lx2 - lx1; _dy = ly2 - ly1
        _L2 = _dx * _dx + _dy * _dy
        _safe = np.where(_L2 > 0, _L2, 1.0)
        _t = np.clip(((px_ - lx1) * _dx + (py_ - ly1) * _dy) / _safe, 0.0, 1.0)
        return np.hypot(px_ - (lx1 + _t * _dx), py_ - (ly1 + _t * _dy))

    c1x = -ihx; c1y = -ihy; c2x = ihx; c2y = -ihy
    c3x = ihx; c3y = ihy; c4x = -ihx; c4y = ihy

    def _pe(px_, py_, exa_, eya_, exb_, eyb_):
        _dex = exb_ - exa_; _dey = eyb_ - eya_
        _L = _dex * _dex + _dey * _dey
        _safe = np.where(_L > 0, _L, 1.0)
        _t = np.clip(((px_ - exa_) * _dex + (py_ - eya_) * _dey) / _safe, 0.0, 1.0)
        return np.hypot(px_ - (exa_ + _t * _dex), py_ - (eya_ + _t * _dey))

    dc1 = _pt_seg(c1x, c1y); dc2 = _pt_seg(c2x, c2y)
    dc3 = _pt_seg(c3x, c3y); dc4 = _pt_seg(c4x, c4y)
    eA0 = _pe(lx1, ly1, c1x, c1y, c2x, c2y); eA1 = _pe(lx1, ly1, c2x, c2y, c3x, c3y)
    eA2 = _pe(lx1, ly1, c3x, c3y, c4x, c4y); eA3 = _pe(lx1, ly1, c4x, c4y, c1x, c1y)
    eB0 = _pe(lx2, ly2, c1x, c1y, c2x, c2y); eB1 = _pe(lx2, ly2, c2x, c2y, c3x, c3y)
    eB2 = _pe(lx2, ly2, c3x, c3y, c4x, c4y); eB3 = _pe(lx2, ly2, c4x, c4y, c1x, c1y)
    d_e1 = np.minimum(np.minimum(dc1, dc2), np.minimum(eA0, eB0))
    d_e2 = np.minimum(np.minimum(dc2, dc3), np.minimum(eA1, eB1))
    d_e3 = np.minimum(np.minimum(dc3, dc4), np.minimum(eA2, eB2))
    d_e4 = np.minimum(np.minimum(dc4, dc1), np.minimum(eA3, eB3))
    d_inner = np.minimum(np.minimum(d_e1, d_e2), np.minimum(d_e3, d_e4))

    sdx = lx2 - lx1; sdy = ly2 - ly1

    def _cross(exa_, eya_, exb_, eyb_):
        hdx_ = exb_ - exa_; hdy_ = eyb_ - eya_
        oo1 = sdx * (eya_ - ly1) - sdy * (exa_ - lx1)
        oo2 = sdx * (eyb_ - ly1) - sdy * (exb_ - lx1)
        oo3 = hdx_ * (ly1 - eya_) - hdy_ * (lx1 - exa_)
        oo4 = hdx_ * (ly2 - eya_) - hdy_ * (lx2 - exa_)
        return (oo1 * oo2 < 0) & (oo3 * oo4 < 0)

    crs = (_cross(c1x, c1y, c2x, c2y) | _cross(c2x, c2y, c3x, c3y) |
           _cross(c3x, c3y, c4x, c4y) | _cross(c4x, c4y, c1x, c1y))
    import os as _dbg2
    if _dbg2.environ.get('CB_DEBUG'):
        print('DBG shapes: lx1', lx1.shape, 'ihx', ihx.shape,
              'dc1', dc1.shape, 'eA0', eA0.shape,
              'd_e1', d_e1.shape, 'd_inner', d_inner.shape,
              'crs', crs.shape)
    d_inner = np.where(crs, 0.0, d_inner)
    in1 = (lx1 >= -ihx) & (lx1 <= ihx) & (ly1 >= -ihy) & (ly1 <= ihy)
    in2 = (lx2 >= -ihx) & (lx2 <= ihx) & (ly2 >= -ihy) & (ly2 <= ihy)
    d_inner = np.where(in1 | in2, 0.0, d_inner)
    d = d_inner - crr_[None, :]
    if base_clearance is not None:
        excess = np.maximum(plc_[None, :] - base_clearance, 0.0)
        if net_clearances:
            fcls = np.array([max(0.0,
                                 net_clearances.get(int(f), base_clearance) - base_clearance)
                             for f in nids_pad], dtype=float)
            excess = np.maximum(excess, fcls[None, :])
        d = d - excess
    d = np.where(near, d, np.inf)
    res = np.min(d, axis=1)
    return np.minimum(res, best_custom)


def _seg_foreign_seg_dist_batch(pcb_data, net_ids, x1s, y1s, x2s, y2s, layer,
                                net_clearances=None, base_clearance=0.0,
                                track_clearances=None,
                                window=_FOREIGN_PAD_WINDOW):
    """Batched _seg_foreign_seg_dist: per-segment min foreign seg/via edge distance."""
    nids_arr = np.asarray(net_ids)
    N = len(x1s)
    nid_fs_, fax_, fay_, fbx_, fby_, fhw_ = _foreign_seg_arrays(pcb_data, layer)
    if nid_fs_.size == 0:
        return np.full(N, 1e9)
    R = window
    fminx = np.minimum(fax_, fbx_) - fhw_; fmaxx = np.maximum(fax_, fbx_) + fhw_
    fminy = np.minimum(fay_, fby_) - fhw_; fmaxy = np.maximum(fay_, fby_) + fhw_
    minx = np.minimum(x1s, x2s); maxx = np.maximum(x1s, x2s)
    miny = np.minimum(y1s, y2s); maxy = np.maximum(y1s, y2s)
    near = ((fmaxx[None, :] >= minx[:, None] - R) &
            (fminx[None, :] <= maxx[:, None] + R) &
            (fmaxy[None, :] >= miny[:, None] - R) &
            (fminy[None, :] <= maxy[:, None] + R) &
            (nid_fs_[None, :] != nids_arr[:, None]))
    d = _capsule_batch_dist(x1s, y1s, x2s, y2s,
                            fax_, fay_, fbx_, fby_) - fhw_[None, :]
    if net_clearances or track_clearances:
        fnid_fs_ = nid_fs_
        _nc = net_clearances or {}
        _tc = track_clearances or {}
        excess_fs_ = np.array([max(0.0,
                                   max(_nc.get(int(f), base_clearance),
                                       _tc.get(int(f), 0.0)) - base_clearance)
                               for f in fnid_fs_], dtype=float)
        d = d - excess_fs_[None, :]
    d = np.where(near, d, np.inf)
    res = np.min(d, axis=1)
    return np.where(np.isinf(res), 1e9, res)


def _seg_foreign_via_dist_batch(pcb_data, net_ids, x1s, y1s, x2s, y2s, layer,
                                net_clearances=None, base_clearance=0.0,
                                window=_FOREIGN_PAD_WINDOW):
    """Batched _seg_foreign_via_dist: per-segment min foreign-via edge distance."""
    nids_arr = np.asarray(net_ids)
    N = len(x1s)
    nids_via_, cx_, cy_, rad_ = _foreign_via_arrays(pcb_data)
    if cx_.size == 0:
        return np.full(N, 1e9)
    R = window
    mx = (x1s + x2s) / 2.0; my = (y1s + y2s) / 2.0
    near = ((np.abs(cx_[None, :] - mx[:, None]) <= R + np.abs(x2s - x1s)[:, None] / 2.0 + rad_[None, :]) &
            (np.abs(cy_[None, :] - my[:, None]) <= R + np.abs(y2s - y1s)[:, None] / 2.0 + rad_[None, :]) &
            (nids_via_[None, :] != nids_arr[:, None]))
    dxv = x2s - x1s; dyv = y2s - y1s
    l2v = dxv * dxv + dyv * dyv
    l2safe = np.where(l2v > 0, l2v, 1.0)[:, None]
    tt = np.clip(((cx_[None, :] - x1s[:, None]) * dxv[:, None] +
                  (cy_[None, :] - y1s[:, None]) * dyv[:, None]) / l2safe,
                 0.0, 1.0)
    d = np.hypot(cx_[None, :] - (x1s[:, None] + tt * dxv[:, None]),
                 cy_[None, :] - (y1s[:, None] + tt * dyv[:, None])) - rad_[None, :]
    zero = (l2v <= 0)[:, None]
    d = np.where(zero,
                 np.hypot(cx_[None, :] - x1s[:, None], cy_[None, :] - y1s[:, None]) - rad_[None, :],
                 d)
    if net_clearances:
        fnid_via_ = nids_via_
        excess_via_ = np.array([max(0.0,
                                    net_clearances.get(int(f), base_clearance) - base_clearance)
                                for f in fnid_via_], dtype=float)
        d = d - excess_via_[None, :]
    d = np.where(near, d, np.inf)
    res = np.min(d, axis=1)
    return np.where(np.isinf(res), 1e9, res)


def _seg_foreign_hole_dist_batch(pcb_data, net_ids, x1s, y1s, x2s, y2s,
                                 window=_FOREIGN_PAD_WINDOW):
    """Batched _seg_foreign_hole_dist: per-segment min foreign NPTH-hole distance."""
    nids_arr = np.asarray(net_ids)
    N = len(x1s)
    nid_h_, hax_, hay_, hbx_, hby_, hr_ = _foreign_hole_capsules(pcb_data)
    if nid_h_.size == 0:
        return np.full(N, 1e9)
    R = window
    hminx = np.minimum(hax_, hbx_) - hr_; hmaxx = np.maximum(hax_, hbx_) + hr_
    hminy = np.minimum(hay_, hby_) - hr_; hmaxy = np.maximum(hay_, hby_) + hr_
    minx = np.minimum(x1s, x2s); maxx = np.maximum(x1s, x2s)
    miny = np.minimum(y1s, y2s); maxy = np.maximum(y1s, y2s)
    near = ((hmaxx[None, :] >= minx[:, None] - R) &
            (hminx[None, :] <= maxx[:, None] + R) &
            (hmaxy[None, :] >= miny[:, None] - R) &
            (hminy[None, :] <= maxy[:, None] + R) &
            (nid_h_[None, :] != nids_arr[:, None]))
    d = _capsule_batch_dist(x1s, y1s, x2s, y2s,
                            hax_, hay_, hbx_, hby_) - hr_[None, :]
    d = np.where(near, d, np.inf)
    res = np.min(d, axis=1)
    return np.where(np.isinf(res), 1e9, res)