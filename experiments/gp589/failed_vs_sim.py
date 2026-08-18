"""Per-net validation: do the nets that FAILED in the real wave-9/10 arms
coincide with the spill sim's high-detour tail under that arm's order?"""
import json, re, sys
from collections import defaultdict
sys.path.insert(0, "/Users/andy/Documents/KiCadRoutingTools/.claude/worktrees/test-589")
sys.path.insert(0, "/Users/andy/Documents/KiCadRoutingTools/.claude/worktrees/test-589/py_router")
sys.path.insert(0, ".")
import plan_score as ps

W = "/private/tmp/claude-501/-Users-andy-Documents-KiCadRoutingTools/212e67de-ad81-437b-9a89-62f452fb4bc5/scratchpad/gp589"
EXCLUDE = {"GND", "P3.3V", "P1.1V", "P1.35V", "VTT"}

def failed_nets(conn_path):
    txt = open(conn_path).read()
    nets = set()
    m = re.search(r"Unrouted nets \(\d+\):\n((?:    \S.*\n)+)", txt)
    if m:
        for line in m.group(1).splitlines():
            nets.add(line.strip().split(" (")[0])
    for m in re.finditer(r"^  (\S+) \(net \d+\):$", txt, re.M):
        nets.add(m.group(1))
    return {n for n in nets if n not in EXCLUDE}

doc = json.load(open("orangecrab_plan.json"))
cfg = doc["config"]
bucket = max(2, int(round((cfg["track_width"] + cfg["clearance"]) / cfg["grid_step"])))
rough = {int(nid): [tuple(p) for p in path] for nid, path in doc["rough_paths"].items()}
buckets_of = {nid: ps.path_buckets(p, bucket) for nid, p in rough.items()}
nets_in, orders = ps.build_orders(doc)
name_of = {i: n for n, i in nets_in}
id_of = {n: i for n, i in nets_in}
allowed = list(range(len(cfg["layers"])))

# per-net detour under a given order (re-run sim, record per-net)
def per_net_detour(order_ids):
    occ = set(); det = defaultdict(float)
    for nid in order_ids:
        for (l, bx, by) in buckets_of.get(nid, ()):  # noqa
            if (l, bx, by) not in occ:
                occ.add((l, bx, by)); continue
            placed = False
            for r in range(1, ps.SPILL_RADIUS + 1):
                ring = [(l, bx+dx, by+dy, r) for dx in range(-r, r+1) for dy in range(-r, r+1)
                        if max(abs(dx), abs(dy)) == r and (l, bx+dx, by+dy) not in occ]
                if not ring:
                    ring = [(l2, bx+dx, by+dy, r+1) for l2 in allowed if l2 != l
                            for dx in range(-(r-1), r) for dy in range(-(r-1), r)
                            if (l2, bx+dx, by+dy) not in occ]
                if ring:
                    c = min(ring); occ.add(c[:3]); det[nid] += c[3]; placed = True; break
            if not placed:
                det[nid] += 50
    return det

ARMS = [("wave9/oc_base.conn", "baseline"), ("wave9/oc_plan.conn", "share"),
        ("wave10/oc_contended.conn", "contended"), ("wave10/oc_xplanar.conn", "planar"),
        ("wave10/oc_sharerev.conn", "share_rev"), ("wave10/oc_res_only.conn", "baseline")]
for conn, mode in ARMS:
    fails = failed_nets(f"{W}/{conn}")
    det = per_net_detour(orders[mode])
    ranked = sorted(det, key=lambda n: -det[n])
    topq = set(ranked[:len(ranked)//4])
    fail_ids = {id_of[n] for n in fails if n in id_of}
    probed_fails = fail_ids & set(rough)
    hit = probed_fails & topq
    print(f"{conn:<28} arm-fails(sig)={len(fails):>2} probed={len(probed_fails):>2} "
          f"in-top-quartile-detour={len(hit):>2}  fail-names: {sorted(fails)[:6]}")
