#!/usr/bin/env python3
"""Per-net time distribution from a step6 log's 'Net total time' lines.

Usage: analyze_nettimes.py <step6.log>

Prints: count, sum, top-N nets by time with their iteration counts, and the
top-10 vs long-tail split. Net names come from the 'NAME (net N):' header
line that precedes each net's Phase-3 block (streaming current-net tracker).
"""
import re, sys

def main():
    path = sys.argv[1]
    net_times = []  # (name, time, iters)
    cur_net = None
    for ln in open(path):
        # strip optional tslog timestamp prefix
        ln = re.sub(r'^\[\s*[0-9.]+s\] ', '', ln.rstrip('\n'))
        nm = re.match(r'^([^()]+) \(net \d+\):$', ln)
        if nm:
            cur_net = nm.group(1).strip()
            continue
        m = re.search(r'Net total time: ([0-9.]+)s, ([0-9]+) iterations', ln)
        if m:
            net_times.append((cur_net, float(m.group(1)), int(m.group(2))))
            cur_net = None

    if not net_times:
        print("no Net total time lines found")
        return
    times = [t for _, t, _ in net_times]
    iters = [i for _, _, i in net_times]
    print(f"count: {len(net_times)}")
    print(f"sum time: {sum(times):.1f}s")
    print(f"sum iters: {sum(iters):,}")
    print(f"max time: {max(times):.1f}s")
    print(f"mean time: {sum(times)/len(times):.2f}s")
    print(f"median time: {sorted(times)[len(times)//2]:.2f}s")

    net_times.sort(key=lambda x: -x[1])
    print("\n=== TOP 20 nets by Net total time ===")
    for name, t, it in net_times[:20]:
        print(f"{t:8.1f}s {it:10,} iters  {name}")

    top10 = sum(t for _, t, _ in net_times[:10])
    rest = sum(t for _, t, _ in net_times[10:])
    print(f"\ntop-10 sum: {top10:.1f}s ({100*top10/sum(times):.1f}% of net-time sum)")
    print(f"rest sum:   {rest:.1f}s ({100*rest/sum(times):.1f}% of net-time sum)")

if __name__ == '__main__':
    main()
