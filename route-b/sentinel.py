#!/usr/bin/env python3
# Route B SENTINEL — the alarm that measures the customer, not the machine.
#
# 2026-09-02 to 2026-09-16: the Anthropic credit balance hit zero, the bot
# answered nobody for two weeks, and every existing signal stayed green - the
# launchd watchdog saw fresh log lines, GitHub Actions marked each run
# "success", and the supervisor's own alert went out by Telegram and email,
# every four hours, ~84 times, into channels nobody was reading. Itay found the
# outage himself.
#
# So this checks three things only, all of them outcomes:
#   1. is ANY brain reachable (API, the claude CLI, OpenAI)?
#   2. did mail arrive in the last 24h with zero answers sent?
#   3. are there customers with no reply at all?
# It alerts on WhatsApp - the one channel Itay actually reads - at most once
# per 24h per incident, and says nothing at all while things work.

import json, os, re, subprocess, sys, time, urllib.request
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
DIR = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(DIR, "sentinel_state.json")
RUNLOG = os.path.join(DIR, "run.log")
WA_TO = os.environ.get("WA_ALERT_TO", "972542383620")
QUIET_H = int(os.environ.get("SENTINEL_QUIET_HOURS", "24"))


def env(k, d=""):
    return (os.environ.get(k, d) or "").strip()


def wa(msg):
    """Silent send through the local whatsapp bridge. No GUI, no stolen focus."""
    body = json.dumps({"recipient": WA_TO, "message": msg}).encode("utf-8")
    req = urllib.request.Request("http://localhost:8080/api/send", data=body,
                                 method="POST", headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status == 200
    except Exception as e:
        print("wa send failed", repr(e))
        return False


def brains():
    """Which brains can actually answer right now."""
    out = {}
    key = env("ANTHROPIC_API_KEY")
    if key:
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=json.dumps({"model": env("ANTHROPIC_MODEL", "claude-sonnet-4-5"),
                             "max_tokens": 5,
                             "messages": [{"role": "user", "content": "ping"}]}).encode(),
            method="POST",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                out["anthropic-api"] = r.status == 200
        except Exception:
            out["anthropic-api"] = False
    else:
        out["anthropic-api"] = False

    child = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    try:
        p = subprocess.run(["claude", "-p", "--output-format", "text", "--model",
                            env("CLI_MODEL", "sonnet"), "--dangerously-skip-permissions"],
                           input="Reply with the single word: ok", capture_output=True,
                           text=True, timeout=180, env=child, cwd="/tmp")
        out["claude-cli"] = "ok" in (p.stdout or "").lower()
    except Exception:
        out["claude-cli"] = False

    ok = env("OPENAI_API_KEY")
    if ok:
        req = urllib.request.Request(
            "https://api.openai.com/v1/chat/completions",
            data=json.dumps({"model": env("OPENAI_MODEL", "gpt-4.1"), "max_tokens": 5,
                             "messages": [{"role": "user", "content": "ok"}]}).encode(),
            method="POST", headers={"Authorization": f"Bearer {ok}",
                                    "content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                out["openai"] = r.status == 200
        except Exception:
            out["openai"] = False
    else:
        out["openai"] = False
    return out


LINE = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d) (station|studio) "
                  r"(?:todo=(\d+)|DONE sent=(\d+) escalated=(\d+) skipped=(\d+)(?: brain_down=(\d+))?)")


def traffic(hours=24):
    """What the last 24h of runs actually did for customers."""
    cut = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S")
    agg = {"arrived": 0, "sent": 0, "escalated": 0, "brain_down": 0}
    try:
        with open(RUNLOG, encoding="utf-8", errors="replace") as f:
            for ln in f:
                m = LINE.match(ln)
                if not m or m.group(1) < cut:
                    continue
                if m.group(3) is not None:
                    agg["arrived"] += int(m.group(3))
                else:
                    agg["sent"] += int(m.group(4) or 0)
                    agg["escalated"] += int(m.group(5) or 0)
                    agg["brain_down"] += int(m.group(7) or 0)
    except FileNotFoundError:
        pass
    return agg


def unanswered_counts(days=3):
    from diagnose import answer_rate
    res = {}
    for name, u, p in (("Station", env("STORE_GMAIL_USER"), env("STORE_GMAIL_APP_PASSWORD")),
                       ("Studio", env("STUDIO_GMAIL_USER") or env("STORE_GMAIL_USER"),
                        env("STUDIO_GMAIL_APP_PASSWORD") or env("STORE_GMAIL_APP_PASSWORD"))):
        if not u or not p:
            continue
        try:
            res[name] = answer_rate(u, p, days, skip_label="cs-bot-seen")["unanswered"]
        except Exception as e:
            print(name, "answer_rate failed", repr(e))
    return res


def main():
    try:
        state = json.load(open(STATE, encoding="utf-8"))
    except Exception:
        state = {}

    b = brains()
    t = traffic(24)
    un = unanswered_counts(3)
    incidents = []

    if not any(b.values()):
        incidents.append(("no-brain",
                          "🔴 בוט שירות הלקוחות בלי מוח: כל הספקים נפלו "
                          f"({', '.join(k for k in b)}). פניות לקוח לא מסומנות כטופלו "
                          "וייענו אוטומטית כשמוח יחזור."))
    if t["arrived"] > 0 and t["sent"] == 0:
        incidents.append(("silent-24h",
                          f"🔴 24 שעות בלי אף תשובה ללקוח. הגיעו {t['arrived']} פניות, "
                          f"נשלחו 0 תשובות ({t['escalated']} הועברו אליך)."))
    for name, n in un.items():
        if n >= 3:
            incidents.append((f"unanswered-{name}",
                              f"🔴 {name}: {n} לקוחות מ-3 הימים האחרונים לא קיבלו שום תשובה."))

    stamp = time.time()
    fired = []
    for key, msg in incidents:
        last = float(state.get(key, 0))
        if stamp - last >= QUIET_H * 3600:
            if wa(msg):
                state[key] = stamp
                fired.append(key)
    for key in list(state):
        if key not in [k for k, _ in incidents] and key != "last_run":
            state.pop(key, None)          # incident resolved: re-arm the alarm
    state["last_run"] = stamp
    json.dump(state, open(STATE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    print(f"{datetime.now():%Y-%m-%dT%H:%M:%S} brains={b} traffic={t} unanswered={un} "
          f"incidents={[k for k, _ in incidents]} alerted={fired}")


if __name__ == "__main__":
    main()
