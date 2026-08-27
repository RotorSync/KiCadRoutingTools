#!/usr/bin/env python3
"""Timestamping tee: prefix each stdin line with elapsed wall-clock seconds,
write both to a log file and stdout."""
import sys, time

start = time.monotonic()
out_path = sys.argv[1] if len(sys.argv) > 1 else None
f = open(out_path, 'w') if out_path else None
for line in sys.stdin:
    t = time.monotonic() - start
    ts = f"[{t:8.2f}s]"
    if f:
        f.write(f"{ts} {line}")
        f.flush()
    sys.stdout.write(f"{ts} {line}")
    sys.stdout.flush()
if f:
    f.close()
