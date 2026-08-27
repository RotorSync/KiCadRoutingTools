#!/usr/bin/env python3
"""Aggregate py-spy raw samples into a self-time table.

Usage: aggregate_pyspy.py <raw.txt> [top_n]
Each raw line is "frame1;frame2;...;frameN <count>". We count:
  - self time: the leaf frame of each sample
  - cumulative time: every frame in the stack
Frames are normalized to "func@file:line" (basename of file).
"""
import re, sys, collections

def norm(frame):
    # frame format: "name (file:line)" or "name (file)" or "name"
    m = re.match(r'^(.*?)\s*\((.*?)\)$', frame)
    if m:
        name, loc = m.group(1), m.group(2)
        # loc may be "file:line" or "file"
        if ':' in loc:
            file, line = loc.rsplit(':', 1)
        else:
            file, line = loc, ''
        base = file.split('/')[-1]
        return f"{name}@{base}:{line}"
    return frame

def main():
    path = sys.argv[1]
    top_n = int(sys.argv[2]) if len(sys.argv) > 2 else 40
    self_time = collections.Counter()
    cum_time = collections.Counter()
    total = 0
    with open(path) as f:
        for ln in f:
            ln = ln.rstrip('\n')
            if not ln.strip():
                continue
            # last token is count
            parts = ln.rsplit(' ', 1)
            if len(parts) != 2:
                continue
            stack, cnt = parts[0], int(parts[1])
            frames = [norm(x) for x in stack.split(';')]
            total += cnt
            if frames:
                self_time[frames[-1]] += cnt
            for fr in frames:
                cum_time[fr] += cnt
    print(f"total samples: {total}")
    print("\n=== SELF time (leaf frames) ===")
    for fr, c in self_time.most_common(top_n):
        print(f"{c:8d} {100.0*c/total:6.2f}%  {fr}")
    print("\n=== CUMULATIVE time (all frames) ===")
    for fr, c in cum_time.most_common(top_n):
        print(f"{c:8d} {100.0*c/total:6.2f}%  {fr}")

if __name__ == '__main__':
    main()
