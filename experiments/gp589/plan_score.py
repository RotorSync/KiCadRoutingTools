#!/usr/bin/env python3
"""#589 plan-quality scorer: judge an ordering / layer assignment OFFLINE,
from a KICAD_GLOBAL_PLAN_DUMP json, optionally anchored by the human-routed
original board.

Usage:
  python3 plan_score.py DUMP.json [--human HUMAN.kicad_pcb] [--worktree DIR]

Outputs:
  - order table: sequential-commit simulation stats per candidate order
    (baseline/share/share_rev/planar/contended [+human_share if --human])
  - layer table: predicted-demand overflow under probe layers, plan
    layer_pref, and human majority layers
  - human anchor: layer utilization comparison, human packing density
"""
import argparse
import json
import sys
from collections import defaultdict

WORKTREE = "/Users/andy/Documents/KiCadRoutingTools/.claude/worktrees/test-589"


def load_worktree(worktree):
    sys.path.insert(0, worktree)
    sys.path.insert(0, worktree + "/py_router")


def path_buckets(path, bucket):
    """Bucket set + per-bucket-layer spans for one simplified rough path.
    Returns set of (layer, bx, by). Via transitions add the bucket on BOTH
    endpoint layers (the barrel blocks more, but the plan only knows its
    endpoints' layers)."""
    from bresenham_utils import walk_line
    cells = set()
    for (x1, y1, l1), (x2, y2, l2) in zip(path, path[1:]):
        if l1 != l2:
            cells.add((l1, x1 // bucket, y1 // bucket))
            cells.add((l2, x1 // bucket, y1 // bucket))
            continue
        for gx, gy in walk_line(x1, y1, x2, y2):
            cells.add((l1, gx // bucket, gy // bucket))
    return cells


def layer_hist(path):
    """Length-weighted layer histogram of a rough path (grid units)."""
    h = defaultdict(int)
    for (x1, y1, l1), (x2, y2, l2) in zip(path, path[1:]):
        if l1 == l2:
            h[l1] += max(abs(x2 - x1), abs(y2 - y1))
    return dict(h)


SPILL_RADIUS = 4  # buckets; ~1 pitch each = how far a detour may reach


def simulate_order(order_ids, buckets_of, allowed_layers, n_layers):
    """Sequential-commit SPILL simulation. Nets commit their bucket sets
    in order. A claim on an occupied bucket relocates ("spills") to the
    nearest free bucket -- same-layer rings first (a lateral detour), then
    other allowed layers at growing rings (a via detour, costed heavier)
    -- and CONSUMES it, so late arrivals in a full corridor genuinely pay
    more: this is what makes the score order-sensitive (raw conflict
    totals are order-invariant). No free bucket within SPILL_RADIUS =
    a predicted failure."""
    occ = set()
    detour = 0.0
    fails = 0
    per_net_detour = defaultdict(float)
    fail_nets = set()
    for i, nid in enumerate(order_ids):
        cells = buckets_of.get(nid)
        if not cells:
            continue
        for (l, bx, by) in cells:
            if (l, bx, by) not in occ:
                occ.add((l, bx, by))
                continue
            placed = False
            for r in range(1, SPILL_RADIUS + 1):
                ring = []
                for dx in range(-r, r + 1):
                    for dy in range(-r, r + 1):
                        if max(abs(dx), abs(dy)) != r:
                            continue
                        if (l, bx + dx, by + dy) not in occ:
                            ring.append((l, bx + dx, by + dy, r))
                if not ring:
                    for l2 in allowed_layers:
                        if l2 == l:
                            continue
                        for dx in range(-(r - 1), r):
                            for dy in range(-(r - 1), r):
                                if (l2, bx + dx, by + dy) not in occ:
                                    ring.append((l2, bx + dx, by + dy,
                                                 r + 1))
                if ring:
                    c = min(ring)
                    occ.add(c[:3])
                    detour += c[3]
                    per_net_detour[nid] += c[3]
                    placed = True
                    break
            if not placed:
                fails += 1
                fail_nets.add(nid)
        # position index i unused in the score itself; late pain emerges
        # naturally from occupancy growth
    worst = max(per_net_detour.values(), default=0.0)
    return {"fails": fails, "fail_nets": len(fail_nets),
            "detour": round(detour, 0),
            "nets_detouring": len(per_net_detour),
            "worst_net_detour": round(worst, 0)}


def overflow_stats(buckets_of, ids):
    """Static (order-free) overflow of a demand map: buckets claimed by
    >1 net, and total excess claims."""
    occ = defaultdict(int)
    for nid in ids:
        for c in buckets_of.get(nid, ()):
            occ[c] += 1
    over = sum(1 for v in occ.values() if v > 1)
    excess = sum(v - 1 for v in occ.values() if v > 1)
    return {"buckets": len(occ), "over_buckets": over, "excess": excess}


def relabel_layers(path, target_layer):
    """Crude single-layer reassignment: every lateral span moves to
    target_layer (endpoints/vias untouched conceptually; this measures
    corridor demand, not routability)."""
    return [(x, y, target_layer) for (x, y, _l) in path]


def build_orders(doc):
    """Reconstruct every candidate order with the REAL _order_block +
    front partition, from the dump."""
    from global_plan import GlobalPlan, _order_block
    plan = GlobalPlan()
    plan.share_w = {int(a): {int(b): w for b, w in ws.items()}
                    for a, ws in doc["share_w"].items()}
    plan.conflict_w = {int(a): {int(b): w for b, w in ws.items()}
                       for a, ws in doc["conflict_w"].items()}
    nets_in = [(n, int(i)) for n, i in doc["nets_input"]]
    front = set(doc["front_ids"])
    orders = {"baseline": [i for _, i in nets_in]}
    for mode in ("share", "share_rev", "planar", "contended"):
        fr = [t for t in nets_in if t[1] in front]
        rest = [t for t in nets_in if t[1] not in front]
        ordered = (_order_block(fr, plan, mode)
                   + _order_block(rest, plan, mode))
        orders[mode] = [i for _, i in ordered]
    # sanity: dumped order_used should equal reconstructed knob order
    used = [int(i) for _, i in doc["order_used"]]
    knob_mode = doc["knobs"].get("order") or "baseline"
    if knob_mode in orders and orders[knob_mode] != used:
        print(f"  WARNING: reconstructed '{knob_mode}' != order_used "
              f"from the run")
    return nets_in, orders


def human_paths(board_path, name_to_id, layers, grid_step, bucket):
    """Per-net bucket sets + layer histograms from the HUMAN board's
    copper, restricted to the probed nets (matched by name) and the
    config's layers."""
    from kicad_parser import parse_kicad_pcb
    from bresenham_utils import walk_line
    pcb = parse_kicad_pcb(board_path)
    li = {name: i for i, name in enumerate(layers)}
    # human board's net ids by name
    hid_to_nid = {}
    for hid, net in pcb.nets.items():
        if net.name in name_to_id:
            hid_to_nid[hid] = name_to_id[net.name]
    buckets_of = defaultdict(set)
    lhist = defaultdict(lambda: defaultdict(float))
    via_count = defaultdict(int)
    for seg in pcb.segments:
        nid = hid_to_nid.get(seg.net_id)
        if nid is None or seg.layer not in li:
            continue
        l = li[seg.layer]
        x1, y1 = (int(round(seg.start_x / grid_step)),
                  int(round(seg.start_y / grid_step)))
        x2, y2 = (int(round(seg.end_x / grid_step)),
                  int(round(seg.end_y / grid_step)))
        for gx, gy in walk_line(x1, y1, x2, y2):
            buckets_of[nid].add((l, gx // bucket, gy // bucket))
        import math
        lhist[nid][l] += math.hypot(seg.end_x - seg.start_x,
                                    seg.end_y - seg.start_y)
    for via in pcb.vias:
        nid = hid_to_nid.get(via.net_id)
        if nid is None:
            continue
        via_count[nid] += 1
        bx, by = (int(round(via.x / grid_step)) // bucket,
                  int(round(via.y / grid_step)) // bucket)
        for l in range(len(layers)):
            buckets_of[nid].add((l, bx, by))
    matched = set(buckets_of) | set(lhist)
    return dict(buckets_of), {k: dict(v) for k, v in lhist.items()}, \
        dict(via_count), matched


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dump")
    ap.add_argument("--human")
    ap.add_argument("--worktree", default=WORKTREE)
    args = ap.parse_args()
    load_worktree(args.worktree)

    doc = json.load(open(args.dump))
    cfg = doc["config"]
    grid_step = cfg["grid_step"]
    layers = cfg["layers"]
    n_layers = len(layers)
    layer_costs = (cfg["layer_costs"] or [1.0] * n_layers)
    while len(layer_costs) < n_layers:
        layer_costs.append(1.0)
    allowed = [i for i in range(n_layers) if layer_costs[i] >= 0]
    bucket = max(2, int(round((cfg["track_width"] + cfg["clearance"])
                              / grid_step)))
    rough = {int(nid): [tuple(p) for p in path]
             for nid, path in doc["rough_paths"].items()}
    print(f"== {args.dump}: {len(rough)} rough paths, grid {grid_step}, "
          f"bucket {bucket} cells (~{bucket*grid_step:.2f}mm pitch), "
          f"layers {layers} (allowed {allowed})")

    buckets_of = {nid: path_buckets(p, bucket) for nid, p in rough.items()}
    nets_in, orders = build_orders(doc)
    name_of = {i: n for n, i in nets_in}

    # ---- human anchor -------------------------------------------------
    hbuckets = hlh = hvias = None
    if args.human:
        name_to_id = {n: i for n, i in nets_in}
        hbuckets, hlh, hvias, matched = human_paths(
            args.human, name_to_id, layers, grid_step, bucket)
        print(f"\n-- human board: matched {len(matched)}/{len(nets_in)} "
              f"probed nets by name")
        # human share graph -> orders derived from HUMAN corridors
        share = defaultdict(lambda: defaultdict(int))
        occupancy = defaultdict(set)
        for nid, cells in hbuckets.items():
            for c in cells:
                occupancy[c].add(nid)
        for netset in occupancy.values():
            ns = sorted(netset)
            for i, a in enumerate(ns):
                for b in ns[i + 1:]:
                    share[a][b] += 1
                    share[b][a] += 1
        from global_plan import GlobalPlan, _order_block
        hplan = GlobalPlan()
        hplan.share_w = {a: dict(ws) for a, ws in share.items()}
        hplan.conflict_w = hplan.share_w
        front = set(doc["front_ids"])
        fr = [t for t in nets_in if t[1] in front]
        rest = [t for t in nets_in if t[1] not in front]
        for mode, key in (("share", "human_share"),
                          ("share_rev", "human_share_rev")):
            orders[key] = [i for _, i in
                           (_order_block(fr, hplan, mode)
                            + _order_block(rest, hplan, mode))]

    # ---- order table (probe-path demand model) ------------------------
    print("\n-- ORDER scores (sequential spill sim on probe paths; "
          "lower = more room kept)")
    print(f"{'order':<18}{'fails':>6}{'fnets':>6}{'detour':>8}"
          f"{'nets-det':>9}{'worst':>7}")
    for name, ids in orders.items():
        st = simulate_order(ids, buckets_of, allowed, n_layers)
        print(f"{name:<18}{st['fails']:>6}{st['fail_nets']:>6}"
              f"{st['detour']:>8}{st['nets_detouring']:>9}"
              f"{st['worst_net_detour']:>7}")

    # ---- layer table --------------------------------------------------
    ids = list(rough)
    base = overflow_stats(buckets_of, ids)
    print(f"\n-- LAYER demand overflow (static, order-free)")
    print(f"probe layers      : {base}")
    lp = {int(k): v for k, v in doc["layer_pref"].items()}
    if lp:
        b2 = dict(buckets_of)
        for nid, l in lp.items():
            if nid in rough:
                b2[nid] = path_buckets(relabel_layers(rough[nid], l),
                                       bucket)
        print(f"plan layer_pref   : {overflow_stats(b2, ids)} "
              f"({len(lp)} nets reassigned)")
    if hlh:
        b3 = dict(buckets_of)
        moved = 0
        for nid in ids:
            h = hlh.get(nid)
            if h:
                maj = max(h.items(), key=lambda t: t[1])[0]
                b3[nid] = path_buckets(relabel_layers(rough[nid], maj),
                                       bucket)
                moved += 1
        print(f"human majority    : {overflow_stats(b3, ids)} "
              f"({moved} nets relabeled)")

    # ---- layer utilization: probes vs human ---------------------------
    if hlh:
        pu = defaultdict(float)
        for nid, p in rough.items():
            for l, c in layer_hist(p).items():
                pu[l] += c * grid_step
        hu = defaultdict(float)
        for nid, h in hlh.items():
            for l, c in h.items():
                hu[l] += c
        pt, ht = sum(pu.values()) or 1, sum(hu.values()) or 1
        print(f"\n-- LAYER utilization, probed nets only "
              f"(probe mm | human mm)")
        for i, lname in enumerate(layers):
            print(f"  {lname:<8} {pu[i]:>8.0f}mm {100*pu[i]/pt:>5.1f}%  | "
                  f"{hu[i]:>8.0f}mm {100*hu[i]/ht:>5.1f}%")
        # human packing density: overflow of the human demand map itself
        print(f"\nhuman demand map  : "
              f"{overflow_stats(hbuckets, list(hbuckets))}")
        # per-net layer agreement
        agree = tot = 0
        for nid, p in rough.items():
            ph, hh = layer_hist(p), hlh.get(nid)
            if not ph or not hh:
                continue
            tot += 1
            if (max(ph.items(), key=lambda t: t[1])[0]
                    == max(hh.items(), key=lambda t: t[1])[0]):
                agree += 1
        print(f"probe-vs-human majority-layer agreement: {agree}/{tot}")
        pv = sum(1 for nid in rough
                 for a, b in zip(rough[nid], rough[nid][1:]) if a[2] != b[2])
        hv = sum(hvias.get(nid, 0) for nid in rough)
        print(f"via count (probed nets): probes {pv} | human {hv}")


if __name__ == "__main__":
    main()
