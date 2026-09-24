#!/usr/bin/env python3
# THE SECOND OPINION ON OUR OWN SKIP RULES.
#
# Three outages in three weeks, three different causes, one shape:
#
#   2026-09-02  the API ran out of credit, every mail escalated, all marked handled
#   2026-09-17  a legacy bot marked mail read, our UNSEEN search saw an empty inbox
#   2026-09-24  Shopify wraps the store's contact form, IGNORE_SENDER ate all of it
#
# Every monitor we own was green through all three, because every monitor asks
# about the machine: did the run finish, is IMAP up, did a probe come back. The
# bot's own log said `todo=0` while sixteen people waited for an answer.
#
# The hole is the same every time: a message reaches a terminal state that is
# neither "answered" nor "handed to Itay", and nothing counts it. This file is
# the only check that argues with the skip rules instead of trusting them. It
# deliberately does NOT import IGNORE_SENDER or is_customer_inquiry - a filter
# cannot audit itself. It hands the raw mail to the brain and asks one question:
# is a human waiting for an answer in here?
#
# When the answer is yes it does two things: releases the message back to the
# bot (once - `cs-audit-released` stops a loop) so the machine repairs itself
# without waiting for anyone, and tells Itay on WhatsApp who is waiting.

import email, imaplib, json, os, re, sys, time
from datetime import datetime, timedelta
from email.header import decode_header, make_header

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# The outcome ledger starts here. Mail that arrived before it carries no
# outcome label because the code that writes them did not exist yet, so
# "handled with no decision" only means something after this date.
LEDGER_START = os.environ.get("LEDGER_START", "25-Sep-2026")
WINDOW_DAYS = int(os.environ.get("AUDIT_WINDOW_DAYS", "3"))
BATCH = int(os.environ.get("AUDIT_BATCH", "20"))
RELEASED = "cs-audit-released"
DRY = os.environ.get("AUDIT_DRY", "") not in ("", "0", "false")
WA_TO = os.environ.get("WA_ALERT_TO", "972542383620")

STORES = {
    "station": ("STORE_GMAIL_USER", "STORE_GMAIL_APP_PASSWORD"),
    "studio": ("STUDIO_GMAIL_USER", "STUDIO_GMAIL_APP_PASSWORD"),
}

VERDICT_PROMPT = """להלן הודעות שהגיעו לתיבת שירות הלקוחות של חנות נעליים אונליין.
הבוט שלנו החליט על כל אחת מהן שאף אחד לא מחכה לתשובה, והשליך אותה. אתה הביקורת על ההחלטה הזו.

עבור כל הודעה ענה על שאלה אחת בלבד: האם יש בתוכה בן אדם אמיתי שמחכה שיחזרו אליו?

כן = אדם כתב **אלינו** וממתין שנחזור אליו. גם אם הוא כותב דרך טופס באתר, גם אם המערכת ששלחה את המייל היא שופיפיי או כל פלטפורמה אחרת, גם אם השאלה קצרה או לא ברורה, גם אם זה "תחזרו אליי" בלי פרטים.
לא = דיוור שיווקי, חשבונית, התראת מערכת, אישור הזמנה אוטומטי, ניוזלטר, ספאם, פישינג, או הודעה שהחנות עצמה שלחה.
לא = התראה של פלטפורמה על פעולה שקרתה באתר (ביקורת שנכתבה, דירוג שהתקבל, מוצר שאזל). יש שם טקסט של לקוח, אבל הוא לא פנה אלינו ולא מחכה לתשובה במייל הזה.

בספק - ענה כן. המחיר של "כן" מוטעה הוא מייל מיותר לבעל החנות. המחיר של "לא" מוטעה הוא לקוח שלא קיבל מענה אף פעם.

ההודעות:
%s

החזר אך ורק JSON שורה אחת, בלי טקסט נוסף:
{"human": [רשימת המספרים של ההודעות שיש בהן אדם שמחכה לתשובה]}"""


def log(*a):
    print(f"{datetime.now():%Y-%m-%dT%H:%M:%S}", *a, flush=True)


def dh(v):
    if not v:
        return ""
    try:
        return str(make_header(decode_header(v)))
    except Exception:
        return v


def body_of(msg):
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                try:
                    return part.get_payload(decode=True).decode(
                        part.get_content_charset() or "utf-8", "replace")
                except Exception:
                    pass
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                try:
                    html = part.get_payload(decode=True).decode(
                        part.get_content_charset() or "utf-8", "replace")
                    return re.sub(r"<[^>]+>", " ", html)
                except Exception:
                    pass
        return ""
    try:
        return msg.get_payload(decode=True).decode(msg.get_content_charset() or "utf-8", "replace")
    except Exception:
        return ""


def search(M, *criteria):
    typ, d = M.search(None, *criteria)
    return d[0].split() if typ == "OK" and d and d[0] else []


def replied_to(M, since):
    """address -> when we last wrote to it. The contact form has no threading,
    so "this thread has a reply" cannot see the answer we sent: the reply goes
    to the customer's own address in a brand new thread. Match on who we wrote
    to instead, or the audit re-releases people who were already answered and
    they get told the same thing twice."""
    out = {}
    try:
        M.select('"[Gmail]/Sent Mail"', readonly=True)
        for n in search(M, f"(SINCE {since})"):
            typ, md = M.fetch(n, "(INTERNALDATE BODY.PEEK[HEADER.FIELDS (TO)])")
            raw = b"".join(x[0] if isinstance(x, tuple) else x for x in (md or []) if x)
            hdr = b"".join(x[1] for x in (md or []) if isinstance(x, tuple) and x[1])
            try:
                when = time.mktime(imaplib.Internaldate2tuple(raw))
            except Exception:
                when = 0
            for a in re.findall(rb"[\w.\-+]+@[\w.\-]+", hdr):
                k = a.decode().lower()
                out[k] = max(out.get(k, 0), when)
    except Exception as e:
        log("replied-to warn", repr(e))
    return out


def collect(M, since):
    """What to put in front of the brain, and what is outright broken.

    Two sources feed the audit. `cs-skipped` is the bot saying "nobody is
    waiting here" - the claim this file exists to challenge. `cs-bot-seen` with
    no outcome label at all is worse: a path that marked the mail handled
    without deciding anything. Before the ledger existed every message looks
    like that, so only the ones that arrived after LEDGER_START are reported as
    a defect. Older ones still get audited - they are exactly where the
    contact-form mail was buried."""
    answered = replied_to(M, since)
    M.select("INBOX")
    released = set(search(M, f"(SINCE {since})", "X-GM-LABELS", f'"{RELEASED}"'))
    skipped = set(search(M, f"(SINCE {since})", "X-GM-LABELS", '"cs-skipped"'))
    handled = set(search(M, f"(SINCE {since})", "X-GM-LABELS", '"cs-bot-seen"'))
    decided = set()
    for lab in ("cs-answered", "cs-to-itay", "cs-skipped", "cs-duplicate"):
        decided |= set(search(M, f"(SINCE {since})", "X-GM-LABELS", f'"{lab}"'))
    no_outcome = handled - decided
    try:
        floor = time.mktime(time.strptime(LEDGER_START, "%d-%b-%Y"))
    except Exception:
        floor = time.time()
    broken = []
    for n in sorted(no_outcome):
        typ, md = M.fetch(n, "(INTERNALDATE)")
        raw = b"".join(x[0] if isinstance(x, tuple) else x for x in (md or []) if x)
        try:
            if time.mktime(imaplib.Internaldate2tuple(raw)) >= floor:
                broken.append(n)
        except Exception:
            pass
    candidates = sorted(skipped - released)
    return [n for n in candidates if not _already_answered(M, n, answered)], broken


def _already_answered(M, num, answered):
    """True when we wrote to this sender after their mail arrived."""
    typ, md = M.fetch(num, "(INTERNALDATE BODY.PEEK[HEADER.FIELDS (FROM REPLY-TO)])")
    raw = b"".join(x[0] if isinstance(x, tuple) else x for x in (md or []) if x)
    hdr = b"".join(x[1] for x in (md or []) if isinstance(x, tuple) and x[1])
    try:
        arrived = time.mktime(imaplib.Internaldate2tuple(raw))
    except Exception:
        arrived = 0
    for a in re.findall(rb"[\w.\-+]+@[\w.\-]+", hdr):
        if answered.get(a.decode().lower(), 0) > arrived:
            return True
    return False


def describe(M, nums):
    out = []
    for n in nums:
        typ, md = M.fetch(n, "(BODY.PEEK[])")
        if typ != "OK" or not md or not isinstance(md[0], tuple):
            continue
        msg = email.message_from_bytes(md[0][1])
        out.append({
            "num": n,
            "from": dh(msg.get("From", "")),
            "reply_to": dh(msg.get("Reply-To", "")),
            "subject": dh(msg.get("Subject", "")),
            "body": re.sub(r"\s+", " ", body_of(msg))[:400],
        })
    return out


def ask(items):
    """Which of these hold a human? Returns a set of indexes."""
    import cloud_worker as cw
    block = "\n\n".join(
        f"[{i}] מאת: {it['from']}" + (f" (Reply-To: {it['reply_to']})" if it["reply_to"] else "")
        + f"\nנושא: {it['subject']}\nתוכן: {it['body']}"
        for i, it in enumerate(items))
    raw, why = cw.brain_raw(VERDICT_PROMPT % block)
    # No verdict is not "all clear". Leave the batch labelled as it was and say
    # so in the log: silence here is the failure mode this file exists to catch.
    if why or not raw:
        log("audit has no brain:", why or "empty reply")
        return None
    m = re.search(r"\{.*\}", raw, re.S)
    try:
        res = json.loads(m.group(0)) if m else {}
    except Exception:
        res = {}
    if not isinstance(res.get("human"), list):
        log("audit inconclusive", raw[:200])
        return None
    return {i for i in res["human"] if isinstance(i, int) and 0 <= i < len(items)}


def wa(msg):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import selftest
    return selftest.wa(msg)


def main():
    since = (datetime.utcnow() - timedelta(days=WINDOW_DAYS)).strftime("%d-%b-%Y")
    waiting, broken = [], []
    for store, (uk, pk) in STORES.items():
        user, pw = os.environ.get(uk, ""), os.environ.get(pk, "")
        if not user or not pw:
            continue
        os.environ["STORE"] = store
        M = imaplib.IMAP4_SSL("imap.gmail.com")
        M.login(user, pw)
        to_audit, fell_through = collect(M, since)
        log(f"{store} to-audit={len(to_audit)} no-outcome-after-ledger={len(fell_through)} window>={since}")
        for it in describe(M, fell_through):
            broken.append((store, it))
        items = describe(M, to_audit)
        for i in range(0, len(items), BATCH):
            chunk = items[i:i + BATCH]
            hits = ask(chunk)
            if hits is None:
                continue
            for idx in sorted(hits):
                it = chunk[idx]
                # Release it once so the bot answers it on its own. The label is
                # what stops an auditor/worker ping-pong over the same message.
                if not DRY:
                    M.store(it["num"], "-X-GM-LABELS", "cs-bot-seen cs-skipped")
                    M.store(it["num"], "+X-GM-LABELS", RELEASED)
                    M.store(it["num"], "-FLAGS", "\\Seen")
                log(f"{store} {'WOULD-RELEASE' if DRY else 'RELEASED'} {it['from'][:40]} | {it['subject'][:50]}")
                waiting.append((store, it))
        M.logout()

    if waiting and not DRY:
        lines = "\n".join(f"- {s}: {it['subject'][:45] or it['from'][:45]}" for s, it in waiting[:6])
        more = f"\n(ועוד {len(waiting) - 6})" if len(waiting) > 6 else ""
        wa(f"🟠 הבוט השליך {len(waiting)} פניות של לקוחות אמיתיים בלי לענות.\n"
           f"החזרתי אותן לתור, הוא יענה או יעביר אליך בריצה הבאה.\n{lines}{more}")
    if broken and not DRY:
        lines = "\n".join(f"- {s}: {it['subject'][:45] or it['from'][:45]}" for s, it in broken[:6])
        wa(f"🔴 {len(broken)} הודעות סומנו כטופלו בלי שום החלטה. "
           f"זה מסלול בקוד שיוצא בלי להכריע - צריך בדיקה.\n{lines}")
    log(f"DONE waiting={len(waiting)} no-outcome={len(broken)}")


if __name__ == "__main__":
    main()
