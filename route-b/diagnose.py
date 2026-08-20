#!/usr/bin/env python3
# Route B ANSWER-RATE AUDIT — the one metric that is about the customer.
#
# Every other health signal reports on the bot: did the run pass, is IMAP up, is
# the backlog small. All of those can be green while customers sit unanswered,
# which is exactly what happened on station, where sending was disabled and the
# bot produced drafts nobody ever sent.
#
# This walks the INBOX, keeps only mail from real people, and uses the Gmail
# thread id to ask whether anything was ever sent back on that thread. Replies
# are attributed to the bot (X-CS-Bot header) or to a human, so "the bot ran"
# and "the customer got an answer" stop being the same claim.
#
# Read-only: never sends, never labels, never deletes. Importable by supervisor.

import imaplib, email, os, re, time, socket
from email.header import decode_header, make_header
from datetime import datetime, timedelta

socket.setdefaulttimeout(120)

BOT_HEADER = "X-CS-Bot"
SENT_BOX = "[Gmail]/Sent Mail"

# Platform noise, not customers.
IGNORE_SENDER = re.compile(
    r"(no-?reply|do-?not-?reply|mailer-daemon|postmaster|notifications?@|"
    r"shopify|facebook|instagram|google|paypal|klaviyo|judge\.?me|mailchimp|"
    r"linkedin|twitter|tiktok|github|stripe|payplus|cardcom)", re.I)

THRID = re.compile(rb"X-GM-THRID\s+(\d+)")


def dh(v):
    if not v:
        return ""
    try:
        return str(make_header(decode_header(v)))
    except Exception:
        return v


def _chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _scan(M, box, since, fields):
    """[(thread_id, headers)] for every message in `box` since `since`."""
    typ, _ = M.select(f'"{box}"' if " " in box else box, readonly=True)
    if typ != "OK":
        raise imaplib.IMAP4.error(f"select {box} -> {typ}")
    typ, data = M.search(None, f"(SINCE {since})")
    ids = data[0].split() if data and data[0] else []
    out = []
    spec_fields = " ".join(fields)
    for batch in _chunks(ids, 100):
        spec = b",".join(batch).decode()
        typ, md = M.fetch(spec, f"(X-GM-THRID BODY.PEEK[HEADER.FIELDS ({spec_fields})])")
        if typ != "OK":
            continue
        for item in md:
            if not isinstance(item, tuple) or len(item) < 2:
                continue
            m = THRID.search(item[0])
            if m:
                out.append((m.group(1).decode(), email.message_from_bytes(item[1])))
    return out


def answer_rate(user, pw, days=30):
    """Facts about whether real customers were answered, and by whom."""
    since = (datetime.utcnow() - timedelta(days=days)).strftime("%d-%b-%Y")
    me = user.lower()
    M = imaplib.IMAP4_SSL("imap.gmail.com")
    M.login(user, pw)
    try:
        inbox = _scan(M, "INBOX", since, ["From", "Subject", "Date"])
        sent = _scan(M, SENT_BOX, since, ["Subject", "Date", BOT_HEADER])
    finally:
        try:
            M.logout()
        except Exception:
            pass

    customers = {}
    for thrid, hdr in inbox:
        frm = email.utils.parseaddr(hdr.get("From", ""))[1].lower()
        if not frm or frm == me or IGNORE_SENDER.search(frm):
            continue
        subj = dh(hdr.get("Subject", ""))
        if re.search(r"order\s+#?\d+\s+placed|\[Sneaker", subj, re.I):
            continue
        customers.setdefault(thrid, (frm, subj))

    by_bot, by_human = set(), set()
    for thrid, hdr in sent:
        (by_bot if hdr.get(BOT_HEADER) else by_human).add(thrid)

    unanswered = [t for t in customers if t not in by_bot and t not in by_human]
    total = len(customers)
    return {
        "days": days,
        "total": total,
        "by_bot": sum(1 for t in customers if t in by_bot),
        "by_human": sum(1 for t in customers if t in by_human and t not in by_bot),
        "unanswered": len(unanswered),
        "rate": (100 * (total - len(unanswered)) // total) if total else 100,
        "examples": [customers[t] for t in unanswered[:15]],
    }


def main():
    days = int((os.environ.get("DIAG_DAYS") or "30").strip())
    stores = {
        "SneakerStation": (os.environ.get("STORE_GMAIL_USER", ""),
                           os.environ.get("STORE_GMAIL_APP_PASSWORD", "")),
        "SneakerStudio": (os.environ.get("STUDIO_GMAIL_USER") or os.environ.get("STORE_GMAIL_USER", ""),
                          os.environ.get("STUDIO_GMAIL_APP_PASSWORD") or os.environ.get("STORE_GMAIL_APP_PASSWORD", "")),
    }
    for name, (user, pw) in stores.items():
        print(f"\n===== {name} =====")
        if not user or not pw:
            print("no creds, skipping")
            continue
        try:
            r = answer_rate(user.strip(), pw.strip(), days)
        except Exception as e:
            print(f"FAILED: {e!r}")
            continue
        print(f"customer threads last {r['days']}d : {r['total']}")
        print(f"  answered by BOT           : {r['by_bot']}")
        print(f"  answered by a HUMAN       : {r['by_human']}")
        print(f"  NEVER ANSWERED            : {r['unanswered']}")
        print(f"  answer rate               : {r['rate']}%")
        for frm, subj in r["examples"]:
            print(f"    UNANSWERED  {frm}  |  {subj[:70]}")
    print("\nDONE diagnose")


if __name__ == "__main__":
    main()
