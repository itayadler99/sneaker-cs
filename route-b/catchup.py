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
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from promise_guard import violation
from order_claim_guard import violation as order_claim_violation
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
DRY_RUN = env("CATCHUP_DRY_RUN", "0") != "0"
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


API_VER = "2024-10"

# The template below tells the customer their order is on its way. Until
# 2026-08-26 nothing checked that an order existed: לאה שירזי, who has never
# bought from either store, was told her parcel was in transit and spent three
# weeks chasing it. This is the check that was missing.
ORDER_EXISTS_GQL = """{ orders(first: 5, query: %s, sortKey: CREATED_AT, reverse: true) {
  edges { node { name createdAt displayFulfillmentStatus } } } }"""

# An order that already reached the customer is not "on its way". The first dry
# run would have told two people with FULFILLED orders that their parcel was in
# transit: #2352 delivered on 10 July and #2501 on 9 August.
DELIVERED = {"FULFILLED", "DELIVERED", "RESTOCKED"}


def open_order(shop_domain, token, email_addr):
    """True if this address has an order that has NOT been delivered yet.

    Three-valued on purpose:
      True  - there is an order still in flight, the holding reply is true
      False - no order, or every order already arrived; both mean do not send
      None  - we could not ask (no credentials, API down)

    None is not False. The caller must treat "unknown" as "do not send": an
    outage must never be the reason a customer gets told their parcel is coming.
    """
    if not shop_domain or not token or not email_addr:
        return None
    q = json.dumps(f"email:{email_addr}")
    req = urllib.request.Request(
        f"https://{shop_domain}/admin/api/{API_VER}/graphql.json",
        data=json.dumps({"query": ORDER_EXISTS_GQL % q}).encode("utf-8"),
        method="POST",
        headers={"X-Shopify-Access-Token": token, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.load(r).get("data") or {}
        nodes = [e["node"] for e in data.get("orders", {}).get("edges", [])]
    except Exception as e:
        log("shopify lookup failed", email_addr, repr(e))
        return None
    return any((n.get("displayFulfillmentStatus") or "").upper() not in DELIVERED
               for n in nodes)


# Replies to the "we have one pair left in your size" blast. The classifier
# reads them as shipping questions - they mention an order, a size and wanting
# it - and the first dry run was about to answer three of them with "your order
# is on its way". They are people trying to BUY. A holding reply loses the sale
# and confuses someone whose previous order arrived weeks ago.
CAMPAIGN_SUBJECT = re.compile(
    r"(נשאר\s*לנו\s*זוג|נשארו\s*לנו|מה\s*(אתה|את)\s*חושב\s*על|"
    r"what\s+did\s+you\s+think\s+about|confirm\s+you\s+want\s+to\s+receive)", re.I)


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

    # Pass 1: headers only, batched. A mailbox with a few hundred messages in the
    # window cannot afford one full-body fetch each - that is what timed out.
    HDRS = "From Subject Message-ID References List-Unsubscribe Precedence"
    candidates = []
    for i in range(0, len(inbox_ids), 100):
        spec = b",".join(inbox_ids[i:i + 100]).decode()
        typ, md = M.fetch(spec, f"(X-GM-THRID BODY.PEEK[HEADER.FIELDS ({HDRS})])")
        if typ != "OK":
            continue
        for item in md:
            if not isinstance(item, tuple) or len(item) < 2:
                continue
            m = THRID.search(item[0])
            num = re.match(rb"\s*(\d+)", item[0])
            if not m or not num:
                continue
            thrid = m.group(1).decode()
            if thrid in answered:
                continue
            hdr = email.message_from_bytes(item[1])
            name, addr = email.utils.parseaddr(hdr.get("From", ""))
            addr = addr.lower()
            if not addr or addr == me or IGNORE_SENDER.search(addr):
                continue
            # Bulk mail always carries an unsubscribe header. Real customers writing
            # from their own mailbox never do, so this is a cleaner filter than
            # chasing sender domains, and it keeps newsletters out of Itay's list.
            if hdr.get("List-Unsubscribe") or hdr.get("Precedence") in ("bulk", "list"):
                continue
            subject = dh(hdr.get("Subject", ""))
            if re.search(r"order\s+#?\d+\s+placed|\[Sneaker", subject, re.I):
                continue
            candidates.append({
                "num": num.group(1), "thrid": thrid, "email": addr,
                "name": dh(name), "subject": subject,
                "msgid": (hdr.get("Message-ID") or "").strip(),
                "refs": dh(hdr.get("References", "")),
            })
            answered.add(thrid)   # one reply per thread, not per message

    # Pass 2: bodies, only for what survived. Small enough to fetch one by one.
    todo = []
    for c in candidates:
        typ, md = M.fetch(c["num"], "(BODY.PEEK[])")
        if typ != "OK" or not md or not isinstance(md[0], tuple):
            continue
        body = strip_quote(get_body(email.message_from_bytes(md[0][1])))
        if not body:
            continue
        c["body"] = body
        todo.append(c)
    return M, todo


# Gmail folds long headers across lines. A thread with a dozen messages has a
# References header containing real CRLFs, and EmailMessage refuses those:
# "Header values may not contain linefeed or carriage return characters". The
# live run on 2026-08-27 failed exactly this way on the ONE customer it wanted
# to answer - and the failure mode is cruel, because the longest References
# header belongs to the customer who has written the most times.
def hdr(value):
    """Flatten any header value to a single line."""
    return re.sub(r"\s+", " ", (value or "")).strip()


def send_reply(M, user, pw, store_name, item):
    # The canned templates promise nothing today, but a future edit must not be
    # able to slip a refund/cancellation offer past review. Same guard the live
    # worker uses (2026-08-24 incident).
    body_is_hebrew = bool(re.search(r"[\u0590-\u05FF]", item["body"]))
    tpl = REPLY_HE if body_is_hebrew else REPLY_EN
    text = tpl.format(name=first_name(item["name"], item["email"]), store=store_name)
    promised = violation(text)
    if promised:
        log(f"BLOCKED-PROMISE catchup template promises \"{promised}\" — not sending")
        raise RuntimeError(f"catchup template promises '{promised}'")
    # The template asserts the order is in transit, so it may only go to someone
    # Shopify confirms has an order. run_store already filtered on this; this is
    # the belt to that braces, and it is what makes the template safe to edit.
    claimed = order_claim_violation(text, bool(item.get("has_order")))
    if claimed:
        kind, phrase = claimed
        log(f"BLOCKED-CLAIM catchup would tell {item['email']} \"{phrase}\" ({kind}) with no order — not sending")
        raise RuntimeError(f"catchup claims '{phrase}' ({kind}) but Shopify has no order for {item['email']}")

    em = EmailMessage()
    em["From"] = f"{store_name} <{user}>"
    em["To"] = item["email"]
    subj = hdr(item["subject"])
    em["Subject"] = subj if subj.lower().startswith("re:") else f"Re: {subj}"
    em["Date"] = formatdate(localtime=True)
    em["Message-ID"] = make_msgid(domain=user.split("@")[-1])
    em[BOT_HEADER] = "catchup"
    if item["msgid"]:
        em["In-Reply-To"] = hdr(item["msgid"])
        em["References"] = hdr(item["refs"] + " " + item["msgid"])
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


def run_store(store_name, user, pw, shop_domain="", admin_token=""):
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
    classified = []
    for item in todo:
        kind, reason = classify(item["subject"], item["body"])
        classified.append((kind, reason, item))

    # If someone has any thread that needs Itay, do not send them a cheerful
    # "your order is on its way" on a different thread. The dry run caught a
    # customer whose parcel had actually been cancelled at the pickup point.
    in_trouble = {c[2]["email"] for c in classified if c[0] != "shipping"}

    for kind, reason, item in classified:
        if len(result["sent"]) >= MAX_SEND:
            break
        if kind != "shipping" or item["email"] in in_trouble:
            why = reason if kind != "shipping" else "has another open issue"
            result["handoff"].append({"email": item["email"], "name": item["name"],
                                      "subject": item["subject"], "why": why,
                                      "body": item["body"][:400]})
            continue
        # A reply to a marketing blast is a customer trying to buy, not asking
        # where a parcel is. Answering it with a holding template loses the sale.
        if CAMPAIGN_SUBJECT.search(item["subject"] or ""):
            why = "תגובה לקמפיין מלאי/ביקורת — לקוח שרוצה לקנות, לא שאלת משלוח"
            log(f"CAMPAIGN skip -> {item['email']} | {item['subject'][:50]}")
            result["handoff"].append({"email": item["email"], "name": item["name"],
                                      "subject": item["subject"], "why": why,
                                      "body": item["body"][:400]})
            continue
        # "Where is my order" is only answerable for someone whose order is
        # still in flight.
        found = open_order(shop_domain, admin_token, item["email"])
        item["has_order"] = bool(found)
        if found is not True:
            why = ("אין ב-Shopify הזמנה פתוחה למייל הזה — או שלא רכש אצלנו, או "
                   "שכל ההזמנות שלו כבר סופקו, או שהזמין תחת מייל אחר"
                   if found is False else
                   "לא הצלחתי לבדוק מול Shopify (אין טוקן או שהקריאה נכשלה)")
            log(f"NO-OPEN-ORDER skip -> {item['email']} | {why}")
            result["handoff"].append({"email": item["email"], "name": item["name"],
                                      "subject": item["subject"], "why": why,
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
        ("SneakerStation", station_user, station_pw,
         env("STATION_SHOP_DOMAIN"), env("STATION_ADMIN_TOKEN")),
        ("SneakerStudio", env("STUDIO_GMAIL_USER") or station_user,
         env("STUDIO_GMAIL_APP_PASSWORD") or station_pw,
         env("STUDIO_SHOP_DOMAIN"), env("STUDIO_ADMIN_TOKEN")),
    ]
    results = [run_store(n, u, p, d, t) for n, u, p, d, t in stores]
    owner_report(results, station_user, station_pw)
    total_sent = sum(len(r["sent"]) for r in results)
    total_hand = sum(len(r["handoff"]) for r in results)
    print(f"DONE catchup dry={DRY_RUN} sent={total_sent} handoff={total_hand}")


if __name__ == "__main__":
    main()
