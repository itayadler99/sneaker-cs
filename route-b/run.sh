#!/bin/bash
# Route B runner — cron/launchd entry. Draft-only customer-service drafter.
export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
cd "$(dirname "$0")/.." || exit 1

# Overlap guard: at a 5-min cadence a slow run must not stack on the previous one.
LOCKDIR="/tmp/sneakercs.run.lock"
if ! mkdir "$LOCKDIR" 2>/dev/null; then
  PID=$(cat "$LOCKDIR/pid" 2>/dev/null)
  # Lock age guard: a real run finishes in <2 min. Anything older than 900s is a
  # crashed/orphaned lock whose PID may have been recycled to an unrelated live
  # process (kill -0 false-positive) — retake it regardless.
  LOCK_AGE=$(( $(date +%s) - $(stat -f %m "$LOCKDIR" 2>/dev/null || echo 0) ))
  if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null && [ "$LOCK_AGE" -lt 900 ]; then
    echo "$(date +%FT%T) SKIP run: previous still active (pid $PID)" >> route-b/run.log
    exit 0
  fi
  echo "$(date +%FT%T) stale lock (pid $PID age ${LOCK_AGE}s) — retaking" >> route-b/run.log
  rm -rf "$LOCKDIR"; mkdir "$LOCKDIR" 2>/dev/null || exit 0  # stale lock, retake
fi
echo $$ > "$LOCKDIR/pid"
trap 'rm -rf "$LOCKDIR"' EXIT
MAX_PER_RUN=6 /usr/bin/python3 route-b/worker.py >> route-b/run.log 2>&1
# QA manager: audit every fresh draft against live Shopify, flag mistakes, alert owner.
/usr/bin/python3 route-b/qa.py >> route-b/run.log 2>&1
