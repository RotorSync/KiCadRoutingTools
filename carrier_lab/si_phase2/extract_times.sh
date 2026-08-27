#!/bin/bash
# Extract per-step user/elapsed times from a tuned-arm run's logs.
# Usage: extract_times.sh <arm>
set +e
ARM="$1"
LOGDIR=/tmp/si_tune_${ARM}/logs
echo "=== ARM $ARM ==="
for i in 1 2 3 4 5; do
  echo -n "step$i: "
  grep -E 'User time|Elapsed' "$LOGDIR/$i.log" | tr '\n' ' ' | sed 's/\t//g'
  echo
done
