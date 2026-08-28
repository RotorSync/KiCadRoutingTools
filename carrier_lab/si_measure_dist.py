#!/usr/bin/env python3
"""Measure victim-aggressor distance distributions on a routed board.

For every victim segment sample points along its length and compute the
distance to the NEAREST aggressor copper on the same layer and on adjacent
unshielded layers (mirroring metric_si_coupling's exposure model). Reports
the fraction of victim length within distance bands.

Usage: si_measure_dist.py <board.kicad_pcb> [--json out.json]
"""
import sys, os, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'py_router'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'rust_router'))
import numpy as np
from scipy.spatial import cKDTree
from kicad_parser import parse_kicad_pcb
import si_classes as si

SAMPLE = 0.2   # mm between sample points along segments


def seg_points(s):
    dx = s.end_x - s.start_x
    dy = s.end_y - s.start_y
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
    if not victims or not aggressors:
        return {'board': board_path, 'n_victims': len(victims),
                'n_aggressors': len(aggressors), 'note': 'no pairs'}

    copper_layers = list(pcb.board_info.copper_layers)
    shields = shield_nets_by_layer(pcb)

    # Per-layer aggressor point clouds
    aggr_pts = {}
    aggr_len = {}
    for s in pcb.segments:
        if s.net_id not in aggressors:
            continue
        pts = seg_points(s)
        aggr_pts.setdefault(s.layer, []).append(pts)
        aggr_len[s.layer] = aggr_len.get(s.layer, 0) + len(pts)
    trees = {}
    for layer, plist in aggr_pts.items():
        trees[layer] = cKDTree(np.concatenate(plist))

    # Victim length per distance band per layer-pair kind
    bands = [0.5, 1.0, 1.5, 2.0]
    same_layer_hist = np.zeros(len(bands)+1)
    broadside_hist = np.zeros(len(bands)+1)
    total_victim_len = 0.0
    same_layer_len = 0.0
    broadside_len = 0.0
    n_victim_segs = 0

    for s in pcb.segments:
        if s.net_id not in victims:
            continue
        pts = seg_points(s)
        n_victim_segs += len(pts)
        total_victim_len += len(pts)

        # same-layer nearest aggressor
        t = trees.get(s.layer)
        if t is not None:
            d, _ = t.query(pts)
            same_layer_len += len(pts)
            for i in range(len(bands)):
                same_layer_hist[i] += np.sum(d <= bands[i])
            same_layer_hist[-1] += np.sum(d > bands[-1])

        # broadside: adjacent unshielded layers
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
                d2, _ = t2.query(pts)
                best_d = np.minimum(best_d, d2)
            finite = np.isfinite(best_d)
            if finite.sum():
                broadside_len += finite.sum()
                bd = best_d[finite]
                for i in range(len(bands)):
                    broadside_hist[i] += np.sum(bd <= bands[i])
                broadside_hist[-1] += np.sum(bd > bands[-1])

    def frac(h):
        tot = h.sum()
        if tot == 0:
            return [0.0]*len(h)
        return [round(float(x)/tot*100, 2) for x in h]

    res = {
        'board': board_path,
        'n_victim_nets': len(victims),
        'n_aggressor_nets': len(aggressors),
        'n_victim_samples': int(total_victim_len),
        'same_layer_samples': int(same_layer_len),
        'broadside_samples': int(broadside_len),
        'same_layer_frac_pct': frac(same_layer_hist),
        'broadside_frac_pct': frac(broadside_hist),
        'bands_mm': bands,
    }
    # combined exposure-weighted profile: min of same/broadside per sample is
    # not directly available; report both separately.
    return res


if __name__ == '__main__':
    b = sys.argv[1]
    r = measure(b)
    print(json.dumps(r, indent=2))
    if '--json' in sys.argv:
        outp = sys.argv[sys.argv.index('--json')+1]
        json.dump(r, open(outp, 'w'), indent=2)
