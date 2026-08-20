#!/usr/bin/env python3
# Route B CATCH-UP — closes the gap the answer-rate audit exposed: customers who
# wrote in and never got any reply at all, because station produced drafts
# nobody sent and the worker only ever looks at UNSEEN mail.
#
# Deliberately narrow. It sends ONE canned, factual holding reply, and only to
# people asking where their order is. Anything touching money or the order
# itself (refund, exchange, cancellation, damage, address change) is never
# answered here: it is collected and mailed to Itay to handle himself.
#
# The canned reply states no dates and no numbers. The knowledge base marks
# shipping times as "pull from Shopify, do not guess", so this promises nothing
# beyond the courier calling to arrange delivery.
#
# Owner-authorised automation on the stores' own mailboxes.

import imaplib, smtplib, email, json, os, re, time, socket
import urllib.request
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from datetime import datetime, timedelta

from diagnose import answer_rate, IGNORE_SENDER, dh, BOT_HEADER, SENT_BOX, THRID

socket.setdefaulttimeout(120)


def env(k, d=""):
    return (os.environ.get(k, d) or "").strip()


DAYS = int(env("CATCHUP_DAYS", "7"))
MAX_SEND = int(env("CATCHUP_MAX", "40"))
DRY_RUN = env("CATCHUP_DRY_RUN", "1") != "0"
DONE_LABEL = "cs-bot-seen"
OWNER_EMAIL = env("OWNER_EMAIL", "itayadler99@gmail.com")
ANTHROPIC_API_KEY = env("ANTHROPIC_API_KEY")
MODEL = env("ANTHROPIC_MODEL", "claude-sonnet-4-5")

# Hard block. If any of this appears we never auto-reply, whatever the model
# thinks. Money and order changes are Itay's call, not the bot's.
HANDS_OFF = re.compile(
    r"(החזר|זיכוי|לזכות|כסף בחזרה|ביטול|לבטל|מבטל|החלפה|להחליף|מחליף|"
    r"מידה אחרת|שינוי כתובת|לשנות כתובת|פגום|נזק|שבור|קרוע|מזויף|"
    r"תלונה|עורך דין|תביעה|משטרה|צרכנות|"
    r"refund|chargeback|cancel|exchange|return|damaged|broken|fake|lawyer|dispute)",
    re.I)

CLASSIFY_PROMPT = """אתה מסווג פניות של לקוחות בחנות סניקרס.
החזר JSON בלבד: {{"kind": "...", "reason": "..."}}

kind חייב להיות אחד מאלה:
- "shipping" — הלקוח שואל איפה ההזמנה, מתי תגיע, מה קורה עם המשלוח, מבקש מעקב, או מתלונן על עיכוב. בלי בקשה לכסף בחזרה ובלי בקשה לשנות את ההזמנה.
- "other" — כל דבר אחר. כולל החזר כספי, ביטול, החלפה, מידה, שינוי כתובת, מוצר פגום, שאלה לפני קנייה, שיתוף פעולה, ספאם, או כל דבר שאתה לא בטוח לגביו.

בספק תמיד "other".

נושא: {subject}

גוף הפנייה:
{body}
"""

REPLY_HE = """היי {name},

תודה על הפנייה.

ההזמנה שלך בדרך אליך.

ברגע שחברת השילוח תגיע לאזור שלך, נציג ייצור איתך קשר טלפוני לתיאום מסירה.

אם יש עוד משהו, אנחנו כאן.

{store}
"""

REPLY_EN = """Hi {name},

Thanks for reaching out.

Your order is on its way to you.

Once the courier reaches your area, they will call you to arrange delivery.

If there is anything else, we are here.

{store}
"""


def log(*a):
    print(time.strftime("%Y-%m-%dT%H:%M:%S"), "catchup", *a, flush=True)


def get_body(msg):
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and "attachment" not in str(part.get("Content-Disposition", "")):
                try:
                    return part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
                except Exception:
                    pass
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                try:
                    html = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
                    return re.sub(r"<[^>]+>", " ", html)
                except Exception:
                    pass
        return ""
    try:
        return msg.get_payload(decode=True).decode(msg.get_content_charset() or "utf-8", "replace")
    except Exception:
        return ""


def strip_quote(body):
    out = []
    for ln in body.splitlines():
        if re.match(r"^\s*(On .+wrote:|בתאריך .+(כתב|מאת))", ln) or ln.strip().startswith(">"):
            break
        out.append(ln)
    return "\n".join(out).strip()


def classify(subject, body):
    """shipping = safe to auto-answer. Anything else goes to Itay."""
    if HANDS_OFF.search(subject + " " + body):
        return "other", "hands-off keyword"
    if not ANTHROPIC_API_KEY:
        return "other", "no api key"
    payload = json.dumps({
        "model": MODEL, "max_tokens": 200,
        "messages": [{"role": "user",
                      "content": CLASSIFY_PROMPT.format(subject=subject[:200], body=body[:2000])}],
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
        j = json.loads(m.group(0)) if m else {}
        kind = j.get("kind", "other")
        return ("shipping" if kind == "shipping" else "other"), j.get("reason", "")
    except Exception as e:
        log("classify warn", repr(e))
        return "other", "classify failed"


def first_name(sender_name, sender_email):
    n = (sender_name or "").strip()
    if n and "@" not in n:
        return n.split()[0]
    return (sender_email.split("@")[0] or "").split(".")[0].capitalize()


def find_unanswered(user, pw):
    """Customer threads from the last DAYS with no reply of any kind."""
    since = (datetime.utcnow() - timedelta(days=DAYS)).strftime("%d-%b-%Y")
    me = user.lower()
    M = imaplib.IMAP4_SSL("imap.gmail.com")
    M.login(user, pw)

    M.select("INBOX", readonly=True)
    typ, data = M.search(None, f"(SINCE {since})")
    inbox_ids = data[0].split() if data and data[0] else []

    M.select(f'"{SENT_BOX}"', readonly=True)
    typ, data = M.search(None, f"(SINCE {since})")
    sent_ids = data[0].split() if data and data[0] else []
    answered = set()
    for i in range(0, len(sent_ids), 100):
        spec = b",".join(sent_ids[i:i + 100]).decode()
        typ, md = M.fetch(spec, "(X-GM-THRID)")
        if typ != "OK":
            continue
        for item in md:
            raw = item[0] if isinstance(item, tuple) else item
            m = THRID.search(raw if isinstance(raw, bytes) else str(raw).encode())
            if m:
                answered.add(m.group(1).decode())

    M.select("INBOX", readonly=False)
    todo = []
    for num in inbox_ids:
        typ, md = M.fetch(num, "(X-GM-THRID BODY.PEEK[])")
        if typ != "OK" or not md or not isinstance(md[0], tuple):
            continue
        m = THRID.search(md[0][0])
        if not m:
            continue
        thrid = m.group(1).decode()
        if thrid in answered:
            continue
        msg = email.message_from_bytes(md[0][1])
        name, addr = email.utils.parseaddr(msg.get("From", ""))
        addr = addr.lower()
        if not addr or addr == me or IGNORE_SENDER.search(addr):
            continue
        subject = dh(msg.get("Subject", ""))
        if re.search(r"order\s+#?\d+\s+placed|\[Sneaker", subject, re.I):
            continue
        body = strip_quote(get_body(msg))
        if not body:
            continue
        todo.append({
            "num": num, "thrid": thrid, "email": addr, "name": dh(name),
            "subject": subject, "body": body,
            "msgid": (msg.get("Message-ID") or "").strip(),
            "refs": dh(msg.get("References", "")),
        })
        answered.add(thrid)   # one reply per thread, not per message
    return M, todo


def send_reply(M, user, pw, store_name, item):
    body_is_hebrew = bool(re.search(r"[\u0590-\u05FF]", item["body"]))
    tpl = REPLY_HE if body_is_hebrew else REPLY_EN
    text = tpl.format(name=first_name(item["name"], item["email"]), store=store_name)

    em = EmailMessage()
    em["From"] = f"{store_name} <{user}>"
    em["To"] = item["email"]
    em["Subject"] = item["subject"] if item["subject"].lower().startswith("re:") else f"Re: {item['subject']}"
    em["Date"] = formatdate(localtime=True)
    em["Message-ID"] = make_msgid(domain=user.split("@")[-1])
    em[BOT_HEADER] = "catchup"
    if item["msgid"]:
        em["In-Reply-To"] = item["msgid"]
        em["References"] = (item["refs"] + " " + item["msgid"]).strip()
    em.set_content(text)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
        s.login(user, pw)
        s.send_message(em)
    try:
        M.append(f'"{SENT_BOX}"', "(\\Seen)", imaplib.Time2Internaldate(time.time()), em.as_bytes())
        M.store(item["num"], "+FLAGS", "\\Seen")
        M.store(item["num"], "+X-GM-LABELS", DONE_LABEL)
    except Exception as e:
        log("post-send warn", repr(e))
    return text


def run_store(store_name, user, pw):
    result = {"store": store_name, "sent": [], "handoff": [], "error": ""}
    if not user or not pw:
        result["error"] = "no creds"
        return result
    try:
        M, todo = find_unanswered(user, pw)
    except Exception as e:
        result["error"] = repr(e)
        return result

    log(f"{store_name}: {len(todo)} unanswered in last {DAYS}d")
    for item in todo:
        if len(result["sent"]) >= MAX_SEND:
            break
        kind, reason = classify(item["subject"], item["body"])
        if kind != "shipping":
            result["handoff"].append({"email": item["email"], "name": item["name"],
                                      "subject": item["subject"], "why": reason,
                                      "body": item["body"][:400]})
            continue
        if DRY_RUN:
            log(f"DRY would send -> {item['email']} | {item['subject'][:50]}")
            result["sent"].append({"email": item["email"], "subject": item["subject"]})
            continue
        try:
            send_reply(M, user, pw, store_name, item)
            result["sent"].append({"email": item["email"], "subject": item["subject"]})
            log(f"SENT -> {item['email']} | {item['subject'][:50]}")
        except Exception as e:
            log(f"SEND-FAIL {item['email']} {e!r}")
            result["handoff"].append({"email": item["email"], "name": item["name"],
                                      "subject": item["subject"], "why": f"send failed: {e!r}",
                                      "body": item["body"][:400]})
    try:
        M.logout()
    except Exception:
        pass
    return result


def owner_report(results, user, pw):
    mode = "הרצת יבש, לא נשלח כלום" if DRY_RUN else "נשלח בפועל"
    lines = [f"סיכום מענה ללקוחות שלא נענו ({DAYS} ימים אחרונים). מצב: {mode}.", ""]
    for r in results:
        if r["error"]:
            lines.append(f"## {r['store']}: שגיאה {r['error']}")
            continue
        lines.append(f"## {r['store']}")
        lines.append(f"נשלחה תשובת מעקב משלוח ל-{len(r['sent'])} לקוחות.")
        for s in r["sent"]:
            lines.append(f"   - {s['email']} | {s['subject'][:70]}")
        lines.append("")
        lines.append(f"דורש טיפול שלך ({len(r['handoff'])}). לא נגעתי באלה:")
        for h in r["handoff"]:
            lines.append(f"   - {h['email']} | {h['subject'][:70]}")
            lines.append(f"     {h['body'][:200].strip()}")
        lines.append("")
    body = "\n".join(lines)
    print(body)
    if not user or not pw or not OWNER_EMAIL:
        return
    try:
        em = EmailMessage()
        em["From"] = f"CS Catchup <{user}>"
        em["To"] = OWNER_EMAIL
        em["Subject"] = f"[שירות לקוחות] מענה ללקוחות שנשכחו ({mode})"
        em[BOT_HEADER] = "catchup-report"
        em.set_content(body)
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
            s.login(user, pw)
            s.send_message(em)
        log("emailed owner report")
    except Exception as e:
        log("owner-mail warn", repr(e))


def main():
    station_user = env("STORE_GMAIL_USER")
    station_pw = env("STORE_GMAIL_APP_PASSWORD")
    stores = [
        ("SneakerStation", station_user, station_pw),
        ("SneakerStudio", env("STUDIO_GMAIL_USER") or station_user,
         env("STUDIO_GMAIL_APP_PASSWORD") or station_pw),
    ]
    results = [run_store(n, u, p) for n, u, p in stores]
    owner_report(results, station_user, station_pw)
    total_sent = sum(len(r["sent"]) for r in results)
    total_hand = sum(len(r["handoff"]) for r in results)
    print(f"DONE catchup dry={DRY_RUN} sent={total_sent} handoff={total_hand}")


if __name__ == "__main__":
    main()
