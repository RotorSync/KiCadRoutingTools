#!/usr/bin/env python3
"""Measure stamp-time signals for adaptive SI radius design.

For each board computes BOTH:
  1) final-segment-based victim-aggressor distance distribution
  2) PAD-based distribution (available at stamp time -- victim pads vs
     already-routed aggressor copper)

Per-net summaries too so we can see which nets drive exposure.
"""
import sys, os, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'py_router'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'rust_router'))
import numpy as np
from scipy.spatial import cKDTree
from kicad_parser import parse_kicad_pcb
import si_classes as si

SAMPLE = 0.2


def seg_points(s):
    dx = s.end_x - s.start_x; dy = s.end_y - s.start_y
    L = (dx*dx + dy*dy) ** 0.5
    if L < 1e-9:
        return np.array([[s.start_x, s.start_y]])
    n = max(1, int(L / SAMPLE))
    ts = np.linspace(0, 1, n + 1)
    return np.stack([s.start_x + dx*ts, s.start_y + dy*ts], axis=1)


def shield_nets_by_layer(pcb):
    out = {}
    for z in pcb.zones:
        if z.net_id == 0:
            continue
        zn = (z.net_name or '').lstrip('/').lower()
        if zn.startswith('gnd') or zn == 'ground':
            out.setdefault(z.layer, set()).add(z.net_name)
    return out


def layer_is_shielded(a, b, copper_layers, shields):
    if a == b:
        return False
    try:
        ia = copper_layers.index(a); ib = copper_layers.index(b)
    except ValueError:
        return False
    lo, hi = (ia, ib) if ia < ib else (ib, ia)
    for i in range(lo+1, hi):
        if shields.get(copper_layers[i]):
            return True
    return False


def measure(board_path):
    pcb = parse_kicad_pcb(board_path)
    classes = si.classify_board(pcb, board_path=board_path)
    seg_nets = set(s.net_id for s in pcb.segments)
    victims = {nid for nid, inf in classes.items()
               if nid != -1 and nid in seg_nets and inf['class'] == si.VICTIM}
    aggressors = {nid for nid, inf in classes.items()
                  if nid != -1 and nid in seg_nets and inf['class'] == si.AGGRESSOR}
    res = {'board': board_path,
           'n_victim_nets': len(victims),
           'n_aggressor_nets': len(aggressors)}
    if not victims or not aggressors:
        res['note'] = 'no pairs'
        return res

    copper_layers = list(pcb.board_info.copper_layers)
    shields = shield_nets_by_layer(pcb)

    aggr_pts = {}
    for s in pcb.segments:
        if s.net_id not in aggressors:
            continue
        aggr_pts.setdefault(s.layer, []).append(seg_points(s))
    trees = {l: cKDTree(np.concatenate(p)) for l, p in aggr_pts.items()}

    # ---- final-segment distribution ----
    bands = [0.5, 1.0, 1.5, 2.0]
    same_hist = np.zeros(len(bands)+1); broad_hist = np.zeros(len(bands)+1)
    same_len = broad_len = tot_len = 0
    per_net_seg = {}
    for s in pcb.segments:
        if s.net_id not in victims:
            continue
        pts = seg_points(s)
        tot_len += len(pts)
        t = trees.get(s.layer)
        if t is not None:
            d,_ = t.query(pts)
            same_len += len(pts)
            for i,bd in enumerate(bands):
                same_hist[i] += np.sum(d <= bd)
            same_hist[-1] += np.sum(d > bands[-1])
            pn = per_net_seg.setdefault(s.net_id,
                {'name': pcb.nets[s.net_id].name if s.net_id in pcb.nets else str(s.net_id),
                 'n': 0})
            pn['n'] += len(pts)
            pn['med'] = pn.get('med', []) 
        try:
            li = copper_layers.index(s.layer)
        except ValueError:
            li = -1
        if li >= 0:
            best_d = np.full(len(pts), np.inf)
            for adj in (li-1, li+1):
                if adj < 0 or adj >= len(copper_layers):
                    continue
                alayer = copper_layers[adj]
                if layer_is_shielded(s.layer, alayer, copper_layers, shields):
                    continue
                t2 = trees.get(alayer)
                if t2 is None:
                    continue
                d2,_ = t2.query(pts)
                best_d = np.minimum(best_d, d2)
            fin = np.isfinite(best_d)
            if fin.sum():
                broad_len += fin.sum()
                bd = best_d[fin]
                for i,bnd in enumerate(bands):
                    broad_hist[i] += np.sum(bd <= bnd)
                broad_hist[-1] += np.sum(bd > bands[-1])

    def frac(h):
        tot = h.sum()
        return [round(float(x)/tot*100,2) if tot else 0 for x in h]

    res['same_layer_frac_pct'] = frac(same_hist)
    res['broadside_frac_pct'] = frac(broad_hist)

    # ---- PAD-based distribution (stamp-time signal) ----
    # For each victim net: distance from each of its pads to nearest aggressor
    # copper point ON THE PAD'S OWN LAYER(S).
    pad_dists_all = []
    per_net_pad = {}
    for nid in victims:
        name = pcb.nets[nid].name if nid in pcb.nets else str(nid)
        dlist = []
        for p in pcb.pads_by_net.get(nid, []):
            # nearest aggressor on any layer this pad touches
            best = np.inf
            for pl in p.layers:
                t = trees.get(pl)
                if t is None:
                    continue
                d,_ = t.query([[p.global_x, p.global_y]])
                best = min(best, float(d[0]))
            if np.isfinite(best):
                dlist.append(best)
        if dlist:
            pad_dists_all.extend(dlist)
            per_net_pad[nid] = {'name': name,
                                'n_pads': len(dlist),
                                'min_mm': round(min(dlist),4),
                                'median_mm': round(float(np.median(dlist)),4),
                                'p25_mm': round(float(np.percentile(dlist,25)),4)}
    if pad_dists_all:
        arr = np.array(pad_dists_all)
        res['pad_dist'] = {
            'n_pads': len(arr),
            'median_mm': round(float(np.median(arr)),4),
            'p25_mm': round(float(np.percentile(arr,25)),4),
            'p10_mm': round(float(np.percentile(arr,10)),4),
            'frac_le_05': round(float(np.mean(arr<=0.5)*100),2),
            'frac_le_10': round(float(np.mean(arr<=1.0)*100),2),
            'frac_le_15': round(float(np.mean(arr<=1.5)*100),2),
            'frac_le_20': round(float(np.mean(arr<=2.0)*100),2),
        }
    # per-net median distances sorted by closeness
    res['per_net_pad'] = sorted(per_net_pad.values(),
                                key=lambda d: d['median_mm'])
    return res


if __name__ == '__main__':
    b = sys.argv[1]
    r = measure(b)
    print(json.dumps(r))
