"""Analyze corridor_data.json -> tables for corridor_quality_findings.md.

Search-space ratio (speedup ceiling): per net, restricted search area =
corridor tube area on the layers it uses; unrestricted = full board area x all
copper layers (what a detailed router explores today). Ratio = restricted/full.
"""
import json, math

WIDTHS = [0.25, 0.5, 1.0, 2.0, 3.0, 4.0]

def q(vals, p):
    if not vals:
        return float("nan")
    s = sorted(vals)
    k = (len(s) - 1) * p
    f = math.floor(k); c = math.ceil(k)
    if f == c:
        return s[int(k)]
    return s[f] * (c - k) + s[c] * (k - f)

def fmt(x):
    if x != x:
        return "   nan "
    return f"{x*100:6.1f}%"

def main():
    with open("/home/austin/krt_work/planning_lab/corridor_data.json") as f:
        data = json.load(f)
    for board in data:
        name = board["board"].split("/")[-1]
        nets = [n for n in board["nets"] if n.get("planned")]
        full_per_net = board["board_area"] * board["n_layers"]
        print(f"\n===== {name} =====")
        print(f"routed={board['nets_routed']} planned={board['nets_planned']} "
              f"planned&routed={len(nets)} area={board['board_area']:.0f}mm2 x{board['n_layers']} "
              f"full-per-net={full_per_net:.0f}mm2")
        print("\n-- 2D containment (geometry only, layer ignored) --")
        print(f"{'W':>5} {'p25':>8} {'median':>8} {'p75':>8} {'mean':>8} {'frac>=80%':>10} {'frac>=90%':>10}")
        for W in WIDTHS:
            vals = [n["c2d"][str(W)] for n in nets if str(W) in n["c2d"] and n["c2d"][str(W)] == n["c2d"][str(W)]]
            ge80 = sum(1 for v in vals if v >= 0.8) / len(vals)
            ge90 = sum(1 for v in vals if v >= 0.9) / len(vals)
            print(f"{W:>5} {fmt(q(vals,0.25)):>8} {fmt(q(vals,0.5)):>8} {fmt(q(vals,0.75)):>8} "
                  f"{fmt(sum(vals)/len(vals)):>8} {ge80*100:>9.1f}% {ge90*100:>9.1f}%")
        print("\n-- layer-matched containment (corridor sub-polyline on same layer) --")
        print(f"{'W':>5} {'p25':>8} {'median':>8} {'p75':>8} {'mean':>8}")
        for W in WIDTHS:
            vals = [n["clayer"][str(W)] for n in nets if str(W) in n["clayer"] and n["clayer"][str(W)] == n["clayer"][str(W)]]
            print(f"{W:>5} {fmt(q(vals,0.25)):>8} {fmt(q(vals,0.5)):>8} {fmt(q(vals,0.75)):>8} "
                  f"{fmt(sum(vals)/len(vals)):>8}")
        # search-space ratio: per-net tube / full-per-net
        print("\n-- search-space ratio (per-net tube / full-per-net search area) --")
        print(f"{'W':>5} {'median':>8} {'mean':>8} {'speedup ceil(median)':>20}")
        for W in WIDTHS:
            ratios = []
            for n in nets:
                L = n["plan_len_2d"]
                tube = L * 2 * W
                ratios.append(tube / full_per_net)
            med = q(ratios, 0.5)
            mean = sum(ratios) / len(ratios)
            print(f"{W:>5} {med:>8.4f} {mean:>8.4f} {1/med if med>0 else float('inf'):>18.1f}x")
        # failure modes at W=2 and W=4
        for W in [2.0, 4.0]:
            bad = [n for n in nets if n["c2d"][str(W)] < 0.5]
            print(f"\n-- failure modes at W={W} (2D): nets with containment <50%: "
                  f"{len(bad)}/{len(nets)} ({len(bad)/max(1,len(nets))*100:.0f}%)")
            worst = sorted(nets, key=lambda n: n["c2d"][str(W)])[:8]
            for n in worst:
                print(f"  {n['name'][:26]:<26} act={n['act_len']:7.1f} plan={n['plan_len_2d']:7.1f} "
                      f"c2d={n['c2d'][str(W)]*100:5.1f}% clayer={n['clayer'][str(W)]*100:5.1f}%")

if __name__ == "__main__":
    main()
