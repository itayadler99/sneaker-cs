#!/usr/bin/env python3
# Route B SUPERVISOR — the 24/7 manager of the CS bots.
# Watches both store mailboxes + the cloud workflow + the brains, samples recent
# auto-sent replies and grades how human/accurate they were, then posts a Telegram
# digest. Critical problems (a store offline, brain down, workflow failing, backlog
# exploding) trigger an urgent alert. Read-only: it never sends customer mail.
#
# Runs headless in GitHub Actions on its own cron. Authorized owner automation.

import imaplib, smtplib, email, json, os, re, time
import urllib.request, urllib.parse
from email.header import decode_header, make_header
from email.message import EmailMessage
from datetime import datetime, timezone

from diagnose import answer_rate

def env(k, d=""):
    return (os.environ.get(k, d) or "").strip()

ANTHROPIC_API_KEY = env("ANTHROPIC_API_KEY")
MODEL = env("ANTHROPIC_MODEL", "claude-sonnet-4-5")
TG_TOKEN = env("TELEGRAM_BOT_TOKEN")
TG_CHAT = env("TELEGRAM_CHAT_ID")
GH_TOKEN = env("GITHUB_TOKEN")
GH_REPO = env("GITHUB_REPOSITORY", "itayadler99/sneaker-cs")
BACKLOG_ALERT = int(env("SUPERVISOR_BACKLOG_ALERT", "40"))   # unhandled unseen -> alert
STALE_HOURS = int(env("SUPERVISOR_STALE_HOURS", "2"))        # no successful run in N h -> alert
SAMPLE_REPLIES = int(env("SUPERVISOR_SAMPLE_REPLIES", "5"))  # recent sent to grade
ANSWER_RATE_DAYS = int(env("SUPERVISOR_ANSWER_DAYS", "14"))  # window for the answer-rate audit
UNANSWERED_ALERT = int(env("SUPERVISOR_UNANSWERED_ALERT", "3"))  # customers with no reply -> alert
OWNER_EMAIL = env("OWNER_EMAIL", "itayadler99@gmail.com")    # Itay reads mail, not Telegram

DONE_LABEL = "cs-bot-seen"
SENT_BOX = "[Gmail]/Sent Mail"

STORES = {
    "station": {
        "name": "SneakerStation",
        "user": env("STORE_GMAIL_USER"),
        "pw": env("STORE_GMAIL_APP_PASSWORD"),
        "domain": env("STATION_SHOP_DOMAIN"),
        "token": env("STATION_ADMIN_TOKEN"),
    },
    "studio": {
        "name": "SneakerStudio",
        "user": env("STUDIO_GMAIL_USER") or env("STORE_GMAIL_USER"),
        "pw": env("STUDIO_GMAIL_APP_PASSWORD") or env("STORE_GMAIL_APP_PASSWORD"),
        "domain": env("STUDIO_SHOP_DOMAIN"),
        "token": env("STUDIO_ADMIN_TOKEN"),
    },
}

def log(*a):
    print(time.strftime("%Y-%m-%dT%H:%M:%S"), "supervisor", *a, flush=True)

def tg(msg):
    if not TG_TOKEN or not TG_CHAT:
        return
    try:
        data = urllib.parse.urlencode({"chat_id": TG_CHAT, "text": msg}).encode()
        urllib.request.urlopen(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data=data, timeout=15)
    except Exception as e:
        log("tg warn", repr(e))

def mail_owner(subject, body):
    """Telegram digests go unread, so anything urgent also lands in Itay's inbox."""
    cfg = STORES["station"]
    user, pw = cfg["user"], cfg["pw"]
    if not user or not pw or not OWNER_EMAIL:
        return
    try:
        em = EmailMessage()
        em["From"] = f"CS Supervisor <{user}>"
        em["To"] = OWNER_EMAIL
        em["Subject"] = subject
        em["X-CS-Bot"] = "supervisor"   # keep it out of the learning bank
        em.set_content(body)
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
            s.login(user, pw)
            s.send_message(em)
        log("emailed owner")
    except Exception as e:
        log("owner-mail warn", repr(e))


def dh(v):
    if not v:
        return ""
    try:
        return str(make_header(decode_header(v)))
    except Exception:
        return v

# ---- mailbox health: IMAP login + backlog (unseen not yet handled) ----
def check_mailbox(cfg):
    out = {"imap": False, "unseen": 0, "backlog": 0, "err": ""}
    if not cfg["user"] or not cfg["pw"]:
        out["err"] = "no creds"
        return out
    try:
        M = imaplib.IMAP4_SSL("imap.gmail.com")
        M.login(cfg["user"], cfg["pw"])
        out["imap"] = True
        M.select("INBOX", readonly=True)
        typ, data = M.search(None, "UNSEEN")
        ids = data[0].split() if data and data[0] else []
        out["unseen"] = len(ids)
        # backlog = unseen NOT already labeled cs-bot-seen (i.e. waiting on the bot)
        typ, seen = M.search(None, f'(UNSEEN X-GM-RAW "label:{DONE_LABEL}")')
        handled = len(seen[0].split()) if seen and seen[0] else 0
        out["backlog"] = max(0, out["unseen"] - handled)
        M.logout()
    except Exception as e:
        out["err"] = repr(e)[:120]
    return out

# ---- Shopify reachability ----
def check_shopify(cfg):
    if not cfg["token"] or not cfg["domain"]:
        return False
    body = json.dumps({"query": "{ shop { name } }"}).encode("utf-8")
    req = urllib.request.Request(
        f"https://{cfg['domain']}/admin/api/2024-10/graphql.json",
        data=body, method="POST",
        headers={"X-Shopify-Access-Token": cfg["token"], "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return bool(json.load(r).get("data", {}).get("shop"))
    except Exception:
        return False

# ---- brain reachability ----
def check_brain():
    if not ANTHROPIC_API_KEY:
        return False
    payload = json.dumps({
        "model": MODEL, "max_tokens": 5,
        "messages": [{"role": "user", "content": "ping"}],
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=payload, method="POST",
        headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status == 200
    except Exception:
        return False

# ---- GitHub Actions: is the cs-bot workflow healthy / running? ----
def check_workflow():
    out = {"last": "", "status": "?", "age_h": None, "recent_fail": 0}
    if not GH_TOKEN:
        out["status"] = "no-token"
        return out
    url = f"https://api.github.com/repos/{GH_REPO}/actions/workflows/cs-bot.yml/runs?per_page=10"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {GH_TOKEN}",
        "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            runs = json.load(r).get("workflow_runs", [])
    except Exception as e:
        out["status"] = f"err {e!r}"[:60]
        return out
    if not runs:
        out["status"] = "no-runs"
        return out
    last = runs[0]
    out["status"] = last.get("conclusion") or last.get("status") or "?"
    out["last"] = (last.get("updated_at") or "")
    out["recent_fail"] = sum(1 for x in runs if x.get("conclusion") == "failure")
    try:
        t = datetime.strptime(out["last"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        out["age_h"] = round((datetime.now(timezone.utc) - t).total_seconds() / 3600, 1)
    except Exception:
        pass
    return out

# ---- sample recent auto-sent replies and grade them ----
def sample_sent(cfg, n):
    samples = []
    try:
        M = imaplib.IMAP4_SSL("imap.gmail.com")
        M.login(cfg["user"], cfg["pw"])
        M.select(f'"{SENT_BOX}"', readonly=True)
        typ, data = M.search(None, "ALL")
        ids = data[0].split() if data and data[0] else []
        for num in reversed(ids[-30:]):
            if len(samples) >= n:
                break
            typ, md = M.fetch(num, "(BODY.PEEK[])")
            if typ != "OK" or not md or not md[0]:
                continue
            msg = email.message_from_bytes(md[0][1])
            subj = dh(msg.get("Subject", ""))
            body = ""
            if msg.is_multipart():
                for p in msg.walk():
                    if p.get_content_type() == "text/plain":
                        try:
                            body = p.get_payload(decode=True).decode(
                                p.get_content_charset() or "utf-8", "replace"); break
                        except Exception:
                            pass
            else:
                try:
                    body = msg.get_payload(decode=True).decode(
                        msg.get_content_charset() or "utf-8", "replace")
                except Exception:
                    pass
            body = re.split(r"\n\s*(On .+wrote:|בתאריך)", body)[0].strip()
            if body and len(body) > 20:
                samples.append({"subject": subj, "reply": body[:700]})
        M.logout()
    except Exception as e:
        log("sample warn", cfg["name"], repr(e))
    return samples

GRADE_PROMPT = """אתה מנהל שירות לקוחות בכיר בחנות סניקרס ({store}).
לפניך תשובות אחרונות שנשלחו ללקוחות. דרג את האיכות הכוללת: כמה הן אנושיות, חמות,
מדויקות, שירותיות, ונקיות מניסוח רובוטי. החזר JSON שורה אחת בלבד:
{{"score":1-10,"verdict":"<משפט אחד בעברית>","fix":"<טיפ שיפור אחד קצר או ריק>"}}

תשובות:
{blob}
"""

def grade(store_name, samples):
    if not samples or not ANTHROPIC_API_KEY:
        return None
    blob = "\n\n".join(f"[{i+1}] נושא: {s['subject']}\n{s['reply']}"
                       for i, s in enumerate(samples))
    payload = json.dumps({
        "model": MODEL, "max_tokens": 300,
        "messages": [{"role": "user", "content": GRADE_PROMPT.format(store=store_name, blob=blob)}],
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=payload, method="POST",
        headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            resp = json.load(r)
        raw = "".join(b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text")
        m = re.search(r"\{.*\}", raw, re.S)
        return json.loads(m.group(0)) if m else None
    except Exception as e:
        log("grade warn", repr(e))
        return None

def main():
    wf = check_workflow()
    brain_ok = check_brain()
    lines = ["🛡️ מפקח שירות לקוחות — דוח מצב"]
    alerts = []

    # workflow line
    wf_icon = "✅" if wf["status"] == "success" else ("🟡" if wf["status"] in ("in_progress", "queued") else "🔴")
    age = f"{wf['age_h']}ש' " if wf["age_h"] is not None else ""
    lines.append(f"{wf_icon} Workflow: {wf['status']} ({age}אחרון, {wf['recent_fail']}/10 כשלים)")
    if wf["age_h"] is not None and wf["age_h"] > STALE_HOURS:
        alerts.append(f"⛔ ה-workflow לא רץ בהצלחה {wf['age_h']} שעות.")
    if wf["recent_fail"] >= 5:
        alerts.append(f"⛔ {wf['recent_fail']} מתוך 10 הריצות האחרונות נכשלו.")

    lines.append(f"{'✅' if brain_ok else '🔴'} מוח (Anthropic): {'תקין' if brain_ok else 'לא מגיב'}")
    if not brain_ok:
        alerts.append("⛔ ה-API של המוח לא מגיב. הבוט לא יכול לענות.")

    for key, cfg in STORES.items():
        if not cfg["user"] or not cfg["pw"]:
            lines.append(f"⚪ {cfg['name']}: לא מחובר (אין creds)")
            continue
        mb = check_mailbox(cfg)
        shop = check_shopify(cfg)
        mb_icon = "✅" if mb["imap"] else "🔴"
        shop_icon = "✅" if shop else "🔴"
        lines.append(
            f"{mb_icon} {cfg['name']}: תיבה {'תקינה' if mb['imap'] else 'נפלה'} "
            f"({mb['unseen']} לא נקראו, {mb['backlog']} ממתינות לבוט) | "
            f"Shopify {shop_icon}")
        if not mb["imap"]:
            alerts.append(f"⛔ {cfg['name']}: חיבור התיבה נפל ({mb['err']}).")
        if not shop:
            alerts.append(f"⚠️ {cfg['name']}: Shopify לא מגיב — הבוט לא ישלוף הזמנות.")
        if mb["backlog"] >= BACKLOG_ALERT:
            alerts.append(f"⚠️ {cfg['name']}: ערימה של {mb['backlog']} פניות ממתינות.")
        # The metric that is actually about the customer. Everything above can be
        # green while nobody gets a reply.
        try:
            ar = answer_rate(cfg["user"], cfg["pw"], ANSWER_RATE_DAYS)
            lines.append(
                f"   📮 נענו {ar['rate']}% מהפניות ב-{ar['days']} יום "
                f"({ar['total']} פניות: {ar['by_bot']} בוט, {ar['by_human']} אדם, "
                f"{ar['unanswered']} ללא מענה)")
            if ar["unanswered"] >= UNANSWERED_ALERT:
                names = ", ".join(f"{f}" for f, _ in ar["examples"][:5])
                alerts.append(
                    f"⛔ {cfg['name']}: {ar['unanswered']} לקוחות לא קיבלו תשובה בכלל. {names}")
        except Exception as e:
            log("answer-rate warn", repr(e))
            lines.append("   📮 לא הצלחתי למדוד אחוז מענה")

        # grade recent replies
        g = grade(cfg["name"], sample_sent(cfg, SAMPLE_REPLIES))
        if g:
            lines.append(f"   📊 איכות תשובות: {g.get('score','?')}/10 — {g.get('verdict','')}")
            if g.get("fix"):
                lines.append(f"   💡 שיפור: {g['fix']}")
            try:
                if float(g.get("score", 10)) <= 5:
                    alerts.append(f"⚠️ {cfg['name']}: איכות תשובות נמוכה ({g.get('score')}/10) — {g.get('verdict','')}")
            except Exception:
                pass

    digest = "\n".join(lines)
    log(digest)
    if alerts:
        body = "🚨 התראת מפקח:\n" + "\n".join(alerts) + "\n\n" + digest
        tg(body)
        mail_owner(f"[שירות לקוחות] {len(alerts)} בעיות דורשות טיפול", body)
    else:
        tg(digest)
    print("DONE supervisor alerts=%d" % len(alerts))

    # Sweep for customers who were never answered at all. Lives here because the
    # supervisor already holds every credential it needs and runs on a schedule.
    # Defaults to a dry run; see catchup.py for what it will and will not touch.
    try:
        import catchup
        catchup.main()
    except Exception as e:
        log("catchup warn", repr(e))

if __name__ == "__main__":
    main()
