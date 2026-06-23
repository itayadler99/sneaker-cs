#!/bin/bash
# Watchdog for the Sneaker CS drafter. Independent of the main timer.
# Detects a wedged/stale bot (no healthy round recently), self-heals, and pings Itay.
# Safe: only ever clears a stale lock and kicks run.sh — never sends customer mail.
export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
cd "$(dirname "$0")/.." || exit 1

DIR="route-b"
LOG="$DIR/watchdog.log"
RUNLOG="$DIR/run.log"
STAMP="$DIR/.watchdog_last_alert"
now=$(date +%s)
log() { echo "$(date +%FT%T) $*" >> "$LOG"; }

TG_TOKEN=$(grep -E '^TELEGRAM_BOT_TOKEN=' .env | cut -d= -f2-)
TG_CHAT=$(grep -E '^TELEGRAM_CHAT_ID=' .env | cut -d= -f2-)
tg() {
  [ -n "$TG_TOKEN" ] && [ -n "$TG_CHAT" ] || return 0
  curl -s -m 15 "https://api.telegram.org/bot${TG_TOKEN}/sendMessage" \
    --data-urlencode "chat_id=${TG_CHAT}" --data-urlencode "text=$1" >/dev/null 2>&1
}

# Age of the newest run.log line (proxy for last bot activity).
if [ -f "$RUNLOG" ]; then
  age=$(( now - $(stat -f %m "$RUNLOG") ))
else
  age=999999
fi

# Healthy cadence is 5 min. If nothing happened in 20 min the bot is wedged or
# the Mac just woke from sleep — clear any stale lock and force a fresh round.
if [ "$age" -gt 1200 ]; then
  log "stale: last activity ${age}s ago — self-healing"
  LOCKDIR="/tmp/sneakercs.run.lock"
  if [ -d "$LOCKDIR" ]; then
    lage=$(( now - $(stat -f %m "$LOCKDIR" 2>/dev/null || echo "$now") ))
    [ "$lage" -gt 900 ] && { rm -rf "$LOCKDIR"; log "cleared stale lock (age ${lage}s)"; }
  fi
  bash "$DIR/run.sh"
  sleep 3
  newage=$(( $(date +%s) - $(stat -f %m "$RUNLOG" 2>/dev/null || echo 0) ))
  if [ "$newage" -gt 120 ]; then
    # run.sh did not refresh the log -> genuinely broken. Rate-limit alerts to 1/hr.
    last=$( [ -f "$STAMP" ] && cat "$STAMP" || echo 0 )
    if [ $(( now - last )) -gt 3600 ]; then
      tg "🚨 בוט שירות SneakerStation לא רץ ${age}s והניסיון לרענן נכשל. צריך בדיקה ידנית במק."
      echo "$now" > "$STAMP"
    fi
    log "self-heal FAILED (newage=${newage}s) alerted"
  else
    log "self-heal OK (round ran)"
  fi
else
  log "ok (age ${age}s)"
fi
