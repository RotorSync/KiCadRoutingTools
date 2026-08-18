#!/usr/bin/env python3
"""#589 per-net forensics: did each detailed route FOLLOW its plan, and
did it succeed?

For every probed net: plan corridor (dump) vs final copper (routed board)
-> xy adherence (fraction of final copper within TOL buckets of the plan,
any layer), layer adherence (fraction on the plan's layers at those spots),
plan/actual layer histograms, via counts, and the log outcome (SUCCESS /
FAILED / ripped etc.).

Usage:
  python3 route_vs_plan.py DUMP.json BOARD.kicad_pcb LOG [--detail NET ...]
"""
import argparse
import json
import math
import re
import sys
from collections import defaultdict

WORKTREE = "/Users/andy/Documents/KiCadRoutingTools/.claude/worktrees/test-589"
sys.path.insert(0, WORKTREE)
sys.path.insert(0, WORKTREE + "/py_router")

TOL = 2  # buckets (~2 track pitches) counts as "on plan"


def parse_log(path):
    """Per-net outcome from a route.py log: name -> dict(status, iters,
    line). Later blocks (retries/rips) overwrite earlier ones, so the
    final state wins."""
    out = {}
    cur = None
    for line in open(path, errors="replace"):
        m = re.match(r"\[\d+/\d+[^\]]*\] Routing (.+) \(id=(\d+)\)", line)
        if m:
            cur = m.group(1)
            out.setdefault(cur, {"id": int(m.group(2)), "status": "?",
                                 "iters": 0, "rips": 0})
            continue
        if cur is None:
            continue
        if "SUCCESS:" in line:
            out[cur]["status"] = "ok"
            mi = re.search(r"(\d+) iterations", line)
            if mi:
                out[cur]["iters"] += int(mi.group(1))
        elif "FAILED:" in line:
            out[cur]["status"] = "FAIL"
        elif "Ripping up" in line:
            out[cur]["rips"] += 1
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dump"); ap.add_argument("board"); ap.add_argument("log")
    ap.add_argument("--detail", nargs="*", default=[])
    args = ap.parse_args()

    from kicad_parser import parse_kicad_pcb
    from bresenham_utils import walk_line

    doc = json.load(open(args.dump))
    cfg = doc["config"]
    gs = cfg["grid_step"]
    layers = cfg["layers"]
    li = {n: i for i, n in enumerate(layers)}
    bucket = max(2, int(round((cfg["track_width"] + cfg["clearance"]) / gs)))
    rough = {int(k): [tuple(p) for p in v]
             for k, v in doc["rough_paths"].items()}
    name_of = {i: n for n, i in [(n, int(i)) for n, i in doc["nets_input"]]}
    id_of = {n: i for i, n in name_of.items()}

    # plan buckets per net: set of (bx,by) and layer histogram
    plan_xy = {}; plan_lh = {}
    for nid, path in rough.items():
        xy = set(); lh = defaultdict(int)
        for (x1, y1, l1), (x2, y2, l2) in zip(path, path[1:]):
            if l1 != l2:
                continue
            for gx, gy in walk_line(x1, y1, x2, y2):
                xy.add((gx // bucket, gy // bucket))
            lh[l1] += max(abs(x2 - x1), abs(y2 - y1))
        plan_xy[nid] = xy
        plan_lh[nid] = dict(lh)
    plan_layer_at = defaultdict(set)   # (bx,by) -> layers planned there
    for nid, path in rough.items():
        for (x1, y1, l1), (x2, y2, l2) in zip(path, path[1:]):
            if l1 != l2:
                continue
            for gx, gy in walk_line(x1, y1, x2, y2):
                plan_layer_at[(nid, gx // bucket, gy // bucket)].add(l1)

    pcb = parse_kicad_pcb(args.board)
    log = parse_log(args.log)

    # final copper per net (only probed nets)
    rows = []
    for nid in rough:
        segs = [s for s in pcb.segments if s.net_id == nid]
        vias = [v for v in pcb.vias if v.net_id == nid]
        if not segs:
            st = log.get(name_of.get(nid, ""), {})
            rows.append((name_of.get(nid, str(nid)), st.get("status", "?"),
                         0.0, 0.0, 0.0, len(vias),
                         sum(plan_lh[nid].values()) * gs, "", st))
            continue
        on_xy = on_layer = tot = 0.0
        act_lh = defaultdict(float)
        for s in segs:
            L = math.hypot(s.end_x - s.start_x, s.end_y - s.start_y)
            if L <= 0 or s.layer not in li:
                continue
            l = li[s.layer]
            act_lh[l] += L
            n = max(1, int(L / (bucket * gs)))
            for k in range(n + 1):
                t = k / n
                bx = int(round((s.start_x + t * (s.end_x - s.start_x)) / gs)) // bucket
                by = int(round((s.start_y + t * (s.end_y - s.start_y)) / gs)) // bucket
                w = L / (n + 1)
                tot += w
                near = any((bx + dx, by + dy) in plan_xy[nid]
                           for dx in range(-TOL, TOL + 1)
                           for dy in range(-TOL, TOL + 1))
                if near:
                    on_xy += w
                    if any(l in plan_layer_at.get((nid, bx + dx, by + dy),
                                                  ())
                           for dx in range(-TOL, TOL + 1)
                           for dy in range(-TOL, TOL + 1)):
                        on_layer += w
        st = log.get(name_of.get(nid, ""), {})
        adh_xy = on_xy / tot if tot else 0.0
        adh_l = on_layer / tot if tot else 0.0
        act = "/".join(f"{layers[l]}:{v:.0f}" for l, v in
                       sorted(act_lh.items(), key=lambda t: -t[1]))
        rows.append((name_of.get(nid, str(nid)), st.get("status", "?"),
                     adh_xy, adh_l, tot, len(vias),
                     sum(plan_lh[nid].values()) * gs, act, st))

    rows.sort(key=lambda r: (r[1] != "FAIL", r[2]))
    print(f"{'net':<28}{'stat':<6}{'adhXY':>6}{'adhLay':>7}{'mm':>7}"
          f"{'vias':>5}{'planmm':>7}  actual-layers")
    for name, stat, axy, al, mm, nv, pmm, act, st in rows:
        print(f"{name:<28}{stat:<6}{axy:>6.0%}{al:>7.0%}{mm:>7.1f}"
              f"{nv:>5}{pmm:>7.1f}  {act}")
    n_ok = sum(1 for r in rows if r[1] == "ok")
    fails = [r[0] for r in rows if r[1] == "FAIL"]
    with_c = [r for r in rows if r[4] > 1.0]
    mean_axy = sum(r[2] for r in with_c) / max(1, len(with_c))
    mean_al = sum(r[3] for r in with_c) / max(1, len(with_c))
    print(f"\nsummary: {n_ok} ok, {len(fails)} FAIL ({', '.join(fails)})")
    print(f"mean adherence over {len(with_c)} nets with >1mm copper: "
          f"xy {mean_axy:.0%}, layer {mean_al:.0%}")

    for name in args.detail:
        nid = id_of.get(name)
        if nid is None:
            print(f"\n[detail] {name}: not a probed net"); continue
        print(f"\n[detail] {name} (id={nid})")
        print(f"  plan layers: " + ", ".join(
            f"{layers[l]}={v * gs:.1f}mm"
            for l, v in sorted(plan_lh[nid].items(), key=lambda t: -t[1])))
        p = rough[nid]
        print(f"  plan: {len(p)} waypoints, "
              f"{sum(1 for a, b in zip(p, p[1:]) if a[2] != b[2])} via(s), "
              f"start ({p[0][0]*gs:.1f},{p[0][1]*gs:.1f} {layers[p[0][2]]})"
              f" end ({p[-1][0]*gs:.1f},{p[-1][1]*gs:.1f} "
              f"{layers[p[-1][2]]})")
        st = log.get(name, {})
        print(f"  log: {st}")


if __name__ == "__main__":
    main()
