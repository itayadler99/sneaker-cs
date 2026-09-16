#!/usr/bin/env python3
# Route B BACKLOG — answers the customers who wrote in while the brain was down.
#
# Between 2026-09-02 and 2026-09-16 the Anthropic API returned "credit balance
# too low" on every call, ask_brain turned that into action=escalate, and ~22
# real customers got a Telegram ping to Itay instead of an answer. Those mails
# are already labelled cs-bot-seen, so the normal worker will never look at them
# again. This closes that hole once.
#
# Each reply is built on the customer's REAL order, pulled live from Shopify:
#   - order found + inside the site's stated window  -> say where it stands, and
#     do NOT apologise for a delay that has not happened.
#   - order found + past the stated window           -> apologise for the delay.
#   - anything sensitive (refund/cancel/complaint/wrong item/size swap) or no
#     order at all -> never auto-answered. It goes to Itay with the facts.
# The late REPLY is a separate fact from a late DELIVERY and is acknowledged in
# one short clause, only where the mail really has been sitting for days.

import email, imaplib, json, os, re, sys, time
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cloud_worker as cw
from diagnose import IGNORE_SENDER, SENT_BOX, THRID, dh

DRY_RUN = (os.environ.get("BACKLOG_DRY_RUN", "1") or "1") != "0"
DAYS = int(os.environ.get("BACKLOG_DAYS", "17"))
MAX_SEND = int(os.environ.get("BACKLOG_MAX", "40"))
SLOW_REPLY_DAYS = 3

# Site-stated windows, in BUSINESS days, from kb/learned-{store}.md.
WINDOW = {"station": (4, 21), "studio": (4, 18)}[cw.STORE]

# Marketing/platform senders that survive diagnose's IGNORE_SENDER.
NOISE = re.compile(
    r"(myprotein|hellorep|zipify|winninghunter|kaching|judge\.?me|loox|"
    r"testflight|dondymarketing|@email\.apple\.com|reply@|hello@|team@|"
    r"success@|support@judge|brevo|welcome@|@t\\.|billing@|invoice)", re.I)


def biz_days(start, end):
    d, n = start.date(), 0
    while d < end.date():
        d += timedelta(days=1)
        if d.weekday() not in (4, 5):   # Israeli weekend: Fri, Sat
            n += 1
    return n


def order_facts(orders):
    """Deterministic truth about the newest order. The model never computes this."""
    if not orders:
        return None
    o = orders[0]
    raw = (o.get("createdAt") or "").replace("Z", "+00:00")
    try:
        created = datetime.fromisoformat(raw)
    except ValueError:
        return None
    elapsed = biz_days(created, datetime.now(timezone.utc))
    proc, ship = WINDOW
    status = (o.get("displayFulfillmentStatus") or "").upper()
    shipped = status in ("FULFILLED", "PARTIALLY_FULFILLED", "IN_TRANSIT", "OUT_FOR_DELIVERY")
    delivered = status == "DELIVERED"
    tracking = ""
    for f in (o.get("fulfillments") or []):
        for t in (f.get("trackingInfo") or []):
            tracking = tracking or (t.get("number") or "")
    return {
        "name": o.get("name", ""),
        "created": created.strftime("%d/%m/%Y"),
        "elapsed": elapsed,
        "max_window": proc + ship,
        "status": status,
        "shipped": shipped,
        "delivered": delivered,
        "tracking": tracking,
        "late": elapsed > proc + ship,
        "stuck_unshipped": (not shipped) and (not delivered) and elapsed > proc + 3,
    }


RULES = """כללי הסבב הזה (גוברים על הכל):
1. עובדות ההזמנה למטה נשלפו חי מ-Shopify והן מקור האמת היחיד. אסור להמציא תאריך, מספר מעקב או סטטוס.
2. אם ההזמנה בתוך חלון הזמנים של האתר — להסביר בדיוק איפה היא עומדת ומה קורה הלאה. אסור להתנצל על עיכוב שלא קרה.
3. אם ההזמנה חרגה מחלון הזמנים — להתנצל על העיכוב במפורש, לומר שאנחנו בודקים מול חברת השילוח ומקדמים, בלי להבטיח תאריך שאין לנו.
4. אם עברו יותר מ-3 ימים מאז שהלקוח כתב — משפט קצר אחד של התנצלות על העיכוב בחזרה אליו. זה נפרד מהעיכוב במשלוח.
5. עברית, קצר, אנושי, בלי סופרלטיבים. אסור מקף ארוך, רק מקף רגיל.
6. כשמזכירים זמנים לצטט כמו באתר: עד 4 ימי עסקים עיבוד ואז 7-21 ימי עסקים משלוח. אסור לנקוב בסכום שלהם כמספר אחד.
7. אם אי אפשר לענות עובדתית - action=escalate.

--- מצב אמת של ההזמנה ---
{facts}
"""


def build_prompt(sender_name, sender_email, subject, body, orders, facts, waited):
    f = "לא נמצאה הזמנה של הלקוח הזה." if not facts else (
        f"הזמנה {facts['name']} · בוצעה {facts['created']} · עברו {facts['elapsed']} ימי עסקים · "
        f"חלון האתר עד {facts['max_window']} ימי עסקים · "
        + ("נמסרה ללקוח" if facts["delivered"] else "יצאה למשלוח" if facts["shipped"] else "טרם יצאה למשלוח")
        + (f" · מעקב {facts['tracking']}" if facts["tracking"] else "")
        + " · " + ("חרגה מחלון הזמנים — להתנצל על העיכוב" if facts["late"]
                   else "תקועה בעיבוד יותר מדי זמן — להתנצל על העיכוב" if facts["stuck_unshipped"]
                   else "בתוך חלון הזמנים — לא להתנצל על עיכוב")
        + (f"\nהלקוח ממתין לתשובה שלנו {waited} ימים." if waited >= SLOW_REPLY_DAYS else ""))
    return cw.PROMPT_TMPL.format(
        store=cw.STORE_NAME, kb=cw.KB, orders=cw.format_orders(orders),
        learned=cw.LEARNED or "(אין)", sender_name=sender_name,
        sender_email=sender_email, subject=subject,
        message=body + "\n\n" + RULES.format(facts=f))


def label_escalated(M, sender_email, since):
    """Mark a customer we deliberately handed to Itay as processed.

    Without this they stay in the "nobody answered them" count for ever and the
    sentinel alarms every day about threads that are waiting for Itay on
    purpose - a red light that means nothing is the light we already ignored."""
    try:
        M.select("INBOX")
        typ, d = M.search(None, f"(SINCE {since})", "FROM", sender_email)
        ids = d[0].split() if typ == "OK" and d and d[0] else []
        if ids:
            M.store(b",".join(ids).decode(), "+X-GM-LABELS", "cs-bot-seen")
    except Exception as e:
        print("label warn", repr(e))


def digest(rows):
    """Close the loop honestly.

    A late order gets an answer saying we are checking with the courier - which
    is true only if somebody actually checks. So every late order we answered is
    listed here for Itay to push, next to the customers the bot refused to touch
    at all."""
    push = [r for r in rows if r["verdict"] == "SENT" and r["late"]]
    yours = [r for r in rows if r["verdict"].startswith("ESCALATE")]
    if not push and not yours:
        return
    lines = []
    if push:
        lines.append("נענו אוטומטית, אבל ההזמנה תקועה וצריכה דחיפה מולך:")
        lines += [f"  · {r['order'] or '?'} · {r['to']} · ממתין {r['waited']} ימים" for r in push]
        lines.append("")
    if yours:
        lines.append("לא נענו בכוונה - דורש החלטה שלך (החזר/החלפה/מוצר שגוי/אין הזמנה):")
        lines += [f"  · {r['order'] or '—'} · {r['to']} · {r['verdict'].split(':', 1)[-1][:70]}"
                  for r in yours]
    body = "\n".join(lines)
    cw.mail_owner(f"[{cw.STORE_NAME}] סבב השלמה: {len(push)} לדחוף, {len(yours)} אליך", body)
    try:
        import urllib.request
        msg = (f"📋 {cw.STORE_NAME}: סבב ההשלמה הסתיים. "
               f"{sum(1 for r in rows if r['verdict'] == 'SENT')} לקוחות קיבלו תשובה, "
               f"{len(push)} הזמנות תקועות צריכות דחיפה מולך, {len(yours)} פניות מחכות להחלטה שלך. "
               "הפירוט במייל.")
        urllib.request.urlopen(urllib.request.Request(
            "http://localhost:8080/api/send",
            data=json.dumps({"recipient": os.environ.get("WA_ALERT_TO", "972542383620"),
                             "message": msg}).encode("utf-8"),
            method="POST", headers={"content-type": "application/json"}), timeout=20)
    except Exception as e:
        print("wa digest failed", repr(e))


def main():
    M = cw.imap_connect()
    since = (datetime.utcnow() - timedelta(days=DAYS)).strftime("%d-%b-%Y")
    me = cw.USER.lower()

    # Which threads did we already answer? Bot or human, either counts.
    M.select(f'"{SENT_BOX}"', readonly=True)
    typ, data = M.search(None, f"(SINCE {since})")
    answered = set()
    for num in (data[0].split() if data and data[0] else []):
        t, md = M.fetch(num, "(X-GM-THRID)")
        if t == "OK" and md and md[0]:
            m = THRID.search(md[0] if isinstance(md[0], bytes) else md[0][0])
            if m:
                answered.add(m.group(1).decode())

    M.select("INBOX")
    typ, data = M.search(None, f"(SINCE {since})")
    ids = data[0].split() if data and data[0] else []

    # Phase 1: gather, spending nothing. One customer can have three open
    # threads; they are one conversation, and a refund request in the oldest of
    # them decides how we treat the newest.
    seen_threads, by_sender = set(), {}
    for num in reversed(ids):                       # newest first
        t, md = M.fetch(num, "(X-GM-THRID BODY.PEEK[])")
        if t != "OK" or not md or not isinstance(md[0], tuple):
            continue
        tm = THRID.search(md[0][0])
        thrid = tm.group(1).decode() if tm else ""
        if not thrid or thrid in answered or thrid in seen_threads:
            continue
        msg = email.message_from_bytes(md[0][1])
        frm = email.utils.parseaddr(msg.get("From", ""))
        sender_name, sender_email = dh(frm[0]), frm[1].lower()
        subject = dh(msg.get("Subject", ""))
        if (not sender_email or sender_email == me
                or IGNORE_SENDER.search(sender_email) or NOISE.search(sender_email)
                or re.search(r"order\s+#?\d+\s+placed|\[Sneaker", subject, re.I)):
            continue
        body = cw.quote_top(cw.get_body(msg))
        if not body:
            continue
        try:
            waited = (datetime.now(timezone.utc) - parsedate_to_datetime(msg.get("Date"))).days
        except Exception:
            waited = 0
        if waited < 1:          # today's mail is the live worker's job
            continue
        seen_threads.add(thrid)
        by_sender.setdefault(sender_email, []).append({
            "name": sender_name, "subject": subject, "body": body,
            "waited": waited, "msgid": (msg.get("Message-ID") or "").strip(),
            "refs": dh(msg.get("References", "")),
            "sensitive": bool(cw.SENSITIVE.search(subject + " " + body)),
        })

    # Phase 2: one decision, and at most one reply, per customer.
    rows = []
    for sender_email, msgs in by_sender.items():
        newest = msgs[0]
        row = {"to": sender_email, "name": newest["name"], "subject": newest["subject"],
               "waited": newest["waited"], "threads": len(msgs), "order": "",
               "late": False, "verdict": "", "reply": "",
               "msgid": newest["msgid"], "refs": newest["refs"]}
        orders = cw.find_orders(sender_email, " ".join(m["body"] for m in msgs))
        facts = order_facts(orders)
        row["order"] = (facts or {}).get("name", "")
        row["late"] = bool(facts and (facts["late"] or facts["stuck_unshipped"]))

        if any(m["sensitive"] for m in msgs) or not facts:
            row["verdict"] = ("ESCALATE-sensitive" if any(m["sensitive"] for m in msgs)
                              else "ESCALATE-no-order")
            label_escalated(M, sender_email, since)
            rows.append(row); continue

        res = cw.ask_brain_prompt(build_prompt(newest["name"], sender_email,
                                               newest["subject"], newest["body"],
                                               orders, facts, newest["waited"]))
        reply = res.get("reply", "")
        if res.get("action") != "draft" or not reply:
            row["verdict"] = "ESCALATE-brain:" + str(res.get("reason", ""))[:70]
            label_escalated(M, sender_email, since)
            rows.append(row); continue
        if cw.outgoing_violation(reply) or cw.order_claim_violation(reply, bool(orders)):
            row["verdict"] = "ESCALATE-guard"; rows.append(row); continue

        row["reply"] = reply.replace("\u2014", "-").replace("\u2013", "-")
        if DRY_RUN:
            row["verdict"] = "WOULD-SEND"
        elif sum(1 for r in rows if r["verdict"] == "SENT") >= MAX_SEND:
            row["verdict"] = "SKIP-cap"
        else:
            try:
                em = cw.send_reply(sender_email, newest["name"], newest["subject"],
                                   row["reply"], newest["msgid"], newest["refs"])
                M.append(f'"{SENT_BOX}"', "(\\Seen)",
                         imaplib.Time2Internaldate(time.time()), em.as_bytes())
                row["verdict"] = "SENT"
                cw.log(f"BACKLOG-SENT {sender_email} | {row['order']}")
            except Exception as e:
                row["verdict"] = f"SEND-FAIL {e!r}"
        rows.append(row)

    M.logout()
    if not DRY_RUN:
        digest(rows)
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       f"backlog_{cw.STORE}{'_dry' if DRY_RUN else ''}.json")
    json.dump(rows, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"\n===== {cw.STORE_NAME} · {'DRY RUN' if DRY_RUN else 'LIVE'} · {len(rows)} threads -> {out}")
    for r in rows:
        print(f"{r['verdict']:<24} {r['to']:<32} {r['order']:<7} "
              f"{'LATE' if r['late'] else 'in-window':<10} waited={r['waited']}d "
              f"threads={r['threads']} | {r['subject'][:32]}")


if __name__ == "__main__":
    main()
