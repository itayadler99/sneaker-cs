#!/usr/bin/env python3
"""Experiment: does the legacy Apps Script bot only answer UNREAD mail?

Phase A (control): send a customer-like test mail from the Studio address to the
Station inbox and LEAVE IT UNREAD. If the old bot is alive it should auto-reply
within ~5-10 min (its known cadence). This proves the old bot is up and answering.

Phase B (test): send a second test mail and immediately mark it \\Seen via IMAP.
If the old bot does NOT reply to this one, it filters on unread => marking mail
read instantly starves it out completely.

Usage: python3 starve_test.py send_unread | send_seen | check
State (message-ids) kept in starve_test_state.json next to this file.
"""
import imaplib, smtplib, ssl, email, json, os, sys, time
from email.message import EmailMessage
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "starve_test_state.json"

# load .env
for line in (ROOT.parent / ".env").read_text().splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k, v.strip().strip('"').strip("'"))

STATION_USER = os.environ["STORE_GMAIL_USER"]
STUDIO_USER = os.environ["STUDIO_GMAIL_USER"]
STUDIO_PW = os.environ["STUDIO_GMAIL_APP_PASSWORD"]
STATION_PW = os.environ["STORE_GMAIL_APP_PASSWORD"]


def load_state():
    return json.loads(STATE.read_text()) if STATE.exists() else {}


def save_state(s):
    STATE.write_text(json.dumps(s, indent=1))


def send_from_studio(subject, body):
    em = EmailMessage()
    em["From"] = STUDIO_USER
    em["To"] = STATION_USER
    em["Subject"] = subject
    em.set_content(body)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context()) as s:
        s.login(STUDIO_USER, STUDIO_PW)
        s.send_message(em)
    return em["Subject"]


def station_imap():
    M = imaplib.IMAP4_SSL("imap.gmail.com")
    M.login(STATION_USER, STATION_PW)
    return M


def mark_seen_by_subject(subject, tries=12):
    """Poll station INBOX until the test mail arrives, then flag \\Seen + our label."""
    M = station_imap()
    M.select("INBOX")
    for _ in range(tries):
        typ, d = M.search(None, f'(SUBJECT "{subject}")')
        ids = d[0].split() if d and d[0] else []
        if ids:
            for num in ids:
                M.store(num, "+FLAGS", "\\Seen")
                M.store(num, "+X-GM-LABELS", "cs-bot-seen")
            M.logout()
            return True
        time.sleep(5)
    M.logout()
    return False


def check_reply(subject):
    """Did anything reply to subject? Look in station Sent for Re: subject."""
    M = station_imap()
    M.select('"[Gmail]/Sent Mail"', readonly=True)
    typ, d = M.search(None, f'(SUBJECT "{subject}")')
    ids = d[0].split() if d and d[0] else []
    out = []
    for num in ids:
        typ, md = M.fetch(num, "(BODY.PEEK[HEADER.FIELDS (SUBJECT DATE TO)])")
        out.append(md[0][1].decode(errors="replace"))
    M.logout()
    return out


BODY = ("Hi, quick question - do you ship to Eilat and how long does delivery "
        "usually take? Thanks!")

if __name__ == "__main__":
    cmd = sys.argv[1]
    st = load_state()
    if cmd == "send_unread":
        subj = f"Shipping question {int(time.time())}"
        send_from_studio(subj, BODY)
        st["unread_subject"] = subj
        st["unread_ts"] = time.time()
        save_state(st)
        print("sent (left unread):", subj)
    elif cmd == "send_seen":
        subj = f"Delivery time question {int(time.time())}"
        send_from_studio(subj, BODY)
        ok = mark_seen_by_subject(subj)
        st["seen_subject"] = subj
        st["seen_ts"] = time.time()
        st["seen_marked"] = ok
        save_state(st)
        print("sent + marked seen:", subj, "| marked:", ok)
    elif cmd == "check":
        for key in ("unread_subject", "seen_subject"):
            subj = st.get(key)
            if not subj:
                continue
            replies = check_reply(subj)
            age = int(time.time() - st.get(key.replace("subject", "ts"), 0))
            print(f"[{key}] {subj} | age={age}s | sent-replies={len(replies)}")
            for r in replies:
                print("   ", r.replace("\r\n", " | ").strip())
    else:
        print("unknown cmd")
