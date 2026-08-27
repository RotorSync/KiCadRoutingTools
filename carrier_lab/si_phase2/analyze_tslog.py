#!/usr/bin/env python3
"""Analyze a timestamped step6 log (tslog.py format) into per-phase wall times.

Usage: analyze_tslog.py <step6.log>
"""
import re, sys

def parse(path):
    lines = []
    with open(path) as f:
        for ln in f:
            m = re.match(r'^\[\s*([0-9.]+)s\] (.*)$', ln.rstrip('\n'))
            if m:
                lines.append((float(m.group(1)), m.group(2)))
            else:
                lines.append((None, ln.rstrip('\n')))
    return lines

# Phase boundary markers (regex -> phase name). First match wins per line.
MARKERS = [
    (r'Loading .* to expand net patterns', 'start'),
    (r'^Loading .*\.kicad_pcb\.\.\.$', 'parse_board'),
    (r'Building base obstacle map', 'base_obstacle_map'),
    (r'Pre-computing net obstacle cache', 'net_obstacle_cache'),
    (r'Routing [0-9]+ single-ended net\(s\)\.\.\.', 'single_ended_loop'),
    (r'Multi-point Phase 3: Routing [0-9]+ tap connections', 'phase3_taps'),
    (r'Per-net fine-parameter rescue', 'rescue'),
    (r'Octolinear smoothing', 'smoothing'),
    (r'Beautify:', 'beautify'),
    (r'JSON_SUMMARY:', 'summary'),
    (r'Writing output|Writing .*\.kicad_pcb|Saving', 'writeback'),
    (r'EXIT=', 'exit'),
]

def main():
    path = sys.argv[1]
    lines = parse(path)
    ts = [t for t, _ in lines if t is not None]
    if not ts:
        print("no timestamps found")
        return
    total = ts[-1] - ts[0]
    print(f"total wall (first->last ts): {total:.1f}s")

    phases = []
    for t, text in lines:
        if t is None:
            continue
        for pat, name in MARKERS:
            if re.search(pat, text):
                phases.append((t, name, text[:90]))
                break
    dedup = []
    for p in phases:
        if dedup and dedup[-1][1] == p[1]:
            continue
        dedup.append(p)
    print("\n=== phase boundaries (wall) ===")
    for i, (t, name, text) in enumerate(dedup):
        dur = dedup[i+1][0] - t if i+1 < len(dedup) else ts[-1] - t
        print(f"{t:9.1f}s  {name:20s} {dur:8.1f}s  {text}")

if __name__ == '__main__':
    main()
