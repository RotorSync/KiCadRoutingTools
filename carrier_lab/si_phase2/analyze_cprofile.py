#!/usr/bin/env python3
"""Analyze a cProfile dump of the bulk route step into attribution tables.

Usage: analyze_cprofile.py <bulk.prof> [top_n]

Prints:
  1. Top-N functions by cumulative time
  2. Top-N functions by self (tottime) time
  3. Time inside Rust FFI (grid_router methods) vs python orchestration
  4. Phase-level attribution via the route.py call tree
"""
import pstats, sys

def main():
    path = sys.argv[1]
    top_n = int(sys.argv[2]) if len(sys.argv) > 2 else 30
    p = pstats.Stats(path)
    total = p.total_tt
    print(f"total_tt (profiled CPU): {total:.1f}s")

    print(f"\n=== TOP {top_n} BY CUMULATIVE TIME ===")
    p.sort_stats('cumulative').print_stats(top_n)

    print(f"\n=== TOP {top_n} BY SELF TIME ===")
    p.sort_stats('tottime').print_stats(top_n)

    # FFI boundary: grid_router methods
    print("\n=== RUST FFI (grid_router.*) ===")
    stats = p.stats
    ffi_tot = ffi_cum = 0
    for (mod, line, fn), (cc, nc, tt, ct, callers) in stats.items():
        if 'grid_router' in mod or 'GridRouter' in fn:
            print(f"  {fn:45s} tottime={tt:8.1f}s cumtime={ct:8.1f}s ncalls={nc}")
            ffi_tot += tt
            ffi_cum += ct
    print(f"  TOTAL FFI tottime: {ffi_tot:.1f}s ({100*ffi_tot/total:.1f}% of total)")
    print(f"  TOTAL FFI cumtime: {ffi_cum:.1f}s ({100*ffi_cum/total:.1f}% of total)")

if __name__ == '__main__':
    main()
