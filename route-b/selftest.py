#!/usr/bin/env python3
# Route B SELF-TEST — the end-to-end proof, run daily.
#
# Every component check we own was green through a two-week outage: launchd saw
# fresh log lines, GitHub Actions marked each run "success", IMAP was up,
# Shopify was up. None of them asked the only question that matters: if a
# customer writes in right now, does an answer come back?
#
# So this writes in as a customer. It mails the Station mailbox from the Studio
# mailbox with a unique token, waits for the bot's reply to land, and shouts on
# WhatsApp if it never does. Both the probe and the reply are moved to Trash
# afterwards, so the mailboxes stay clean.

import email, imaplib, json, os, smtplib, sys, time, urllib.parse, urllib.request, uuid
from email.message import EmailMessage
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
DIR = os.path.dirname(os.path.abspath(__file__))
WA_TO = os.environ.get("WA_ALERT_TO", "972542383620")
WAIT_MIN = int(os.environ.get("SELFTEST_WAIT_MIN", "25"))


def env(k, d=""):
    return (os.environ.get(k, d) or "").strip()


def wa(msg):
    """WhatsApp through the local bridge; Telegram when there is no bridge.

    The same file runs on the Mac and in GitHub Actions. In the cloud there is
    no bridge to talk to, and an alarm that can only fire on a machine that may
    be switched off is not an alarm."""
    body = json.dumps({"recipient": WA_TO, "message": msg}).encode("utf-8")
    req = urllib.request.Request("http://localhost:8080/api/send", data=body,
                                 method="POST", headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            if r.status == 200:
                return True
    except Exception as e:
        print("wa bridge unavailable", repr(e))
    tok, chat = env("TELEGRAM_BOT_TOKEN"), env("TELEGRAM_CHAT_ID")
    if not tok or not chat:
        return False
    try:
        urllib.request.urlopen(urllib.request.Request(
            f"https://api.telegram.org/bot{tok}/sendMessage",
            data=urllib.parse.urlencode({"chat_id": chat, "text": msg}).encode(),
            method="POST"), timeout=20)
        return True
    except Exception as e:
        print("telegram failed", repr(e)); return False


def trash(user, pw, token):
    """Take the probe (and its answer) out of the way."""
    try:
        M = imaplib.IMAP4_SSL("imap.gmail.com"); M.login(user, pw)
        for box in ("INBOX", '"[Gmail]/Sent Mail"'):
            M.select(box)
            typ, d = M.search(None, "TEXT", f'"{token}"')
            ids = d[0].split() if typ == "OK" and d and d[0] else []
            if ids:
                M.store(b",".join(ids).decode(), "+X-GM-LABELS", "\\Trash")
        M.logout()
    except Exception as e:
        print("cleanup warn", repr(e))


def main():
    st_user, st_pw = env("STORE_GMAIL_USER"), env("STORE_GMAIL_APP_PASSWORD")
    sd_user, sd_pw = env("STUDIO_GMAIL_USER"), env("STUDIO_GMAIL_APP_PASSWORD")
    if not all((st_user, st_pw, sd_user, sd_pw)):
        print("missing creds, skipping"); return

    token = f"SELFTEST-{uuid.uuid4().hex[:8]}"
    # Reference a real recent order. A stranger with no order in Shopify is
    # escalated by design, so a probe without one could never come back
    # answered - it would report a broken bot every single day.
    order_ref = ""
    try:
        os.environ.setdefault("STORE", "station")
        import cloud_worker as cw
        data = cw.shopify_gql(cw.ORDER_GQL % '"financial_status:paid"')
        orders = cw._orders_from(data)
        if orders:
            order_ref = (orders[0].get("name") or "").lstrip("#")
    except Exception as e:
        print("probe order lookup failed", repr(e))
    m = EmailMessage()
    m["From"], m["To"] = sd_user, st_user
    m["Subject"] = f"מתי מגיעה ההזמנה שלי? ({token[-6:]})"   # unique: Gmail threads by subject
    m.set_content(f"היי, מה קורה עם ההזמנה שלי מספר {order_ref}? עדיין לא קיבלתי עדכון.\n\n[{token}]"
                  if order_ref else
                  f"היי, הזמנתי נעליים לפני כמה ימים ועדיין לא קיבלתי עדכון. מה הסטטוס?\n\n[{token}]")
    s = smtplib.SMTP_SSL("smtp.gmail.com", 465); s.login(sd_user, sd_pw)
    s.send_message(m); s.quit()
    sent_at = time.time()
    print(f"{datetime.now():%Y-%m-%dT%H:%M:%S} probe sent {token}")

    # The bot answers the Studio address, so the reply lands in Studio's inbox.
    answered = False
    while time.time() - sent_at < WAIT_MIN * 60 and not answered:
        time.sleep(60)
        try:
            M = imaplib.IMAP4_SSL("imap.gmail.com"); M.login(sd_user, sd_pw)
            M.select("INBOX")
            typ, d = M.search(None, "FROM", st_user, "SINCE",
                              time.strftime("%d-%b-%Y", time.localtime(sent_at)))
            for num in (d[0].split() if typ == "OK" and d and d[0] else []):
                t, md = M.fetch(num, "(BODY.PEEK[])")
                if t != "OK" or not md or not isinstance(md[0], tuple):
                    continue
                msg = email.message_from_bytes(md[0][1])
                if msg.get("X-CS-Bot") or "הזמנה" in (msg.get("Subject") or ""):
                    answered = True
                    break
            M.logout()
        except Exception as e:
            print("poll warn", repr(e))

    mins = int((time.time() - sent_at) / 60)
    if answered:
        print(f"OK: bot answered the probe in ~{mins} min")
    else:
        print(f"FAIL: no answer after {mins} min")
        wa(f"🔴 בדיקת קצה-לקצה של בוט שירות הלקוחות נכשלה: שלחתי פנייה כמו לקוח "
            f"ואף תשובה לא חזרה תוך {mins} דקות. הבוט לא עונה ללקוחות עכשיו.")
    if answered:
        trash(sd_user, sd_pw, token)
        trash(st_user, st_pw, token)
    else:
        # Leave the probe where it is. On 2026-09-17 the cleanup ran on failure
        # too and threw away the one message that could explain why no answer
        # came back.
        print(f"probe {token} left in place as evidence")


if __name__ == "__main__":
    main()
