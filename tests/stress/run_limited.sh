#!/bin/bash
# Run a command with a ~12 GB RSS watchdog (process + direct children).
# Usage: run_limited.sh <cmd> [args...]   (override with LIMIT_KB=<kb>)
# Exits 137 with MEMORY_LIMIT_EXCEEDED on stderr if the limit is breached.
#
# Command recording for the redo harness (issue #132) is done by the tools
# themselves (redo_record.record_invocation, called from each board-mutating
# CLI's main()), gated on the REDO_MANIFEST env var. That captures the run
# reliably even when a command is NOT routed through this wrapper, which the LLM
# agent does inconsistently. This wrapper therefore only enforces the memory cap.
#
# #422: raised 4 GB -> 12 GB. The #422 static-keep-out-bitmap fix cut the
# obstacle-map footprint on large sparse boards (crkbd 0.025 signal: ~9 GB -> ~5
# GB peak), but the fine grid over a big split-keyboard outline can still spike a
# few GB in the reconciliation/write phase. 12 GB keeps those legitimate runs
# from being killed on 16 GB+ CI while still catching a true runaway. The deeper
# fix -- a coarser grid far from obstacles -- is tracked as a follow-up issue.
LIMIT_KB=${LIMIT_KB:-12582912}

# ORPHAN GUARD (RUNBOOK rule 12: never end your turn while a routing command is
# still running). Agents background a long route step anyway --
# `nohup ... run_limited.sh ... & disown` -- and then return. The step outlives
# its launcher, nobody ever grades its output, and it burns a core for hours: the
# 2026-08-06 sets-11-25 wave lost ottercast_audio, abn6502 and pcie_test_edge to
# exactly this (each a NORESULT + a relaunch), and left 3 orphaned route.py
# processes to be killed by hand. It also destroys the replay record, because a
# killed process never runs redo_record's atexit timings hook.
#
# The `&` is in the PARENT shell, so it is invisible here. What IS observable is
# the consequence: when the launching shell exits, we are reparented to init.
# That is unambiguous -- a foreground command's parent stays alive for its whole
# duration. Grace period first, so a transient re-parent can't false-positive.
# Escape hatch: STRESS_ALLOW_ORPHAN=1 (warn, don't kill).
ORPHAN_GRACE=${ORPHAN_GRACE:-60}
PPID0=$PPID
START=$(date +%s)
orphaned() {   # 0 = we have been orphaned
  [ "$(( $(date +%s) - START ))" -ge "$ORPHAN_GRACE" ] || return 1
  local now; now=$(ps -o ppid= -p $$ 2>/dev/null | tr -d ' ')
  [ -n "$now" ] || return 1
  [ "$now" = "1" ] || { [ "$now" != "$PPID0" ] && [ "$PPID0" != "1" ]; }
}

"$@" &
PID=$!
trap 'kill -9 $PID 2>/dev/null' INT TERM
while kill -0 $PID 2>/dev/null; do
  if orphaned; then
    MARK="${REDO_RUNDIR:-$PWD}/ORPHANED_STEP.txt"
    {
      echo "$(date '+%Y-%m-%d %H:%M:%S') ORPHANED (launcher exited; was PPID=$PPID0)"
      echo "  cmd: $*"
    } >> "$MARK" 2>/dev/null
    echo "BACKGROUNDED_STEP_ORPHANED: this command outlived the shell that started it." >&2
    echo "  RUNBOOK rule 12: run routing commands in the FOREGROUND and block until they return." >&2
    echo "  To wait past the 10-min foreground cap, poll instead:" >&2
    echo "    until ! pgrep -f '<unique-cmd-fragment>' >/dev/null; do sleep 10; done" >&2
    echo "  Recorded in $MARK" >&2
    if [ -n "${STRESS_ALLOW_ORPHAN:-}" ]; then
      echo "  STRESS_ALLOW_ORPHAN set -- continuing anyway." >&2
      ORPHAN_GRACE=99999999   # warn once, then stop checking
    else
      echo "  Killing it: its output would never be graded (and the replay record is already lost)." >&2
      pgrep -P $PID 2>/dev/null | xargs -I{} kill -9 {} 2>/dev/null
      kill -9 $PID 2>/dev/null
      exit 138
    fi
  fi
  RSS=$(ps -o rss= -p $PID 2>/dev/null | awk '{print $1+0}')
  CH=$(pgrep -P $PID 2>/dev/null | while read c; do ps -o rss= -p "$c" 2>/dev/null; done | awk '{s+=$1} END{print s+0}')
  TOT=$(( ${RSS:-0} + ${CH:-0} ))
  if [ "$TOT" -gt "$LIMIT_KB" ]; then
    echo "MEMORY_LIMIT_EXCEEDED: ${TOT}KB rss > ${LIMIT_KB}KB cap - killing job" >&2
    pgrep -P $PID 2>/dev/null | xargs -I{} kill -9 {} 2>/dev/null
    kill -9 $PID 2>/dev/null
    exit 137
  fi
  sleep 2
done
wait $PID
