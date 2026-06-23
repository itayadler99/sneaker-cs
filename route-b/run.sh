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

# Telegram notifier (visible autonomy: Itay must hear about every new draft).
TG_TOKEN=$(grep -E '^TELEGRAM_BOT_TOKEN=' .env | cut -d= -f2-)
TG_CHAT=$(grep -E '^TELEGRAM_CHAT_ID=' .env | cut -d= -f2-)
tg() {
  [ -n "$TG_TOKEN" ] && [ -n "$TG_CHAT" ] || return 0
  curl -s -m 15 "https://api.telegram.org/bot${TG_TOKEN}/sendMessage" \
    --data-urlencode "chat_id=${TG_CHAT}" --data-urlencode "text=$1" >/dev/null 2>&1
}

WORKER_OUT=$(MAX_PER_RUN=6 /usr/bin/python3 route-b/worker.py 2>&1)
echo "$WORKER_OUT" >> route-b/run.log
# QA manager: audit every fresh draft against live Shopify, flag mistakes, alert owner.
QA_OUT=$(/usr/bin/python3 route-b/qa.py 2>&1)
echo "$QA_OUT" >> route-b/run.log

# Notify Itay when there's something for him to act on.
DRAFTED=$(echo "$WORKER_OUT" | grep -oE 'drafted=[0-9]+' | tail -1 | cut -d= -f2)
ESCALATED=$(echo "$WORKER_OUT" | grep -oE 'escalated=[0-9]+' | tail -1 | cut -d= -f2)
QA_FAILS=$(echo "$QA_OUT" | grep -oE 'fail=[0-9]+' | tail -1 | cut -d= -f2)
if [ "${DRAFTED:-0}" -gt 0 ] || [ "${ESCALATED:-0}" -gt 0 ]; then
  tg "🤖 בוט שירות SneakerStation: ${DRAFTED:-0} טיוטות חדשות, ${ESCALATED:-0} להסלמה. בדוק ב-Gmail של החנות."
fi
if [ "${QA_FAILS:-0}" -gt 0 ]; then
  tg "⚠️ QA תפס ${QA_FAILS} טיוטות בעייתיות (סומנו QA-FAIL, לא יישלחו). בדוק."
fi
