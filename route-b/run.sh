#!/bin/bash
# Route B runner — launchd entry, every 5 min while the Mac is awake.
# Runs the GOOD order-aware auto-responder (cloud_worker.py) for both stores.
# cloud_worker dedups via the Gmail label `cs-bot-seen`, the SAME label the
# GitHub Actions cloud bot uses — so local (fast, Mac-on) and cloud (backup,
# Mac-off) cooperate with zero double-sends. cloud_worker sends its own
# Telegram alerts (full reply text), so no extra notifier is needed here.
export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
cd "$(dirname "$0")/.." || exit 1

# Load .env into the environment (cloud_worker reads os.environ only).
set -a
# shellcheck disable=SC1091
. ./.env
set +a

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

# Station mailbox
STORE=station MAX_PER_RUN=6 /usr/bin/python3 route-b/cloud_worker.py >> route-b/run.log 2>&1
# Studio mailbox (only if its app password is configured)
if [ -n "$STUDIO_GMAIL_APP_PASSWORD" ]; then
  STORE=studio MAX_PER_RUN=6 /usr/bin/python3 route-b/cloud_worker.py >> route-b/run.log 2>&1
fi
