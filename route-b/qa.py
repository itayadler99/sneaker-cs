#!/usr/bin/env python3
# QA manager for the Sneaker Station CS bot — the "team lead".
# Authorized owner business automation: OWN mailbox, read-only audit + flag (no send).
# After each drafter run it re-checks every draft sitting in [Gmail]/Drafts against LIVE
# Shopify data and the original customer email, catching mistakes BEFORE Itay could send:
#   - draft cites an order number that doesn't exist for that customer (hallucination)
#   - draft claims "shipped / tracking" but the real order has no fulfillment (invented)
#   - draft promises a hard delivery date (forbidden)
#   - the original email was sensitive (refund/exchange/legal) and should have escalated
# Any failing draft is labeled "QA-FAIL" in Gmail so it is never sent unreviewed.
# Health + verdicts are written to qa.log / health.json. On failure (or a stale bot) an
# alert draft is written to Itay's own inbox — never to a customer.

import imaplib, email, json, os, re, time, importlib.util
from email.utils import parseaddr

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# reuse the drafter's helpers (env, shopify lookup, body parsing) without running it
_spec = importlib.util.spec_from_file_location("worker", os.path.join(ROOT, "route-b", "worker.py"))
w = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(w)

USER = w.USER
APP_PW = w.APP_PW
DRAFTS_BOX = w.DRAFTS_BOX
QA_LABEL = "QA-FAIL"
ALERT_TO = w.ENV.get("OWNER_ALERT_EMAIL", "itayadler99@gmail.com")
QA_LOG = os.path.join(ROOT, "route-b", "qa.log")
HEALTH = os.path.join(ROOT, "route-b", "health.json")
RUN_LOG = os.path.join(ROOT, "route-b", "run.log")

SENSITIVE = re.compile(
    r"(החזר|כסף בחזרה|זיכוי|החלפ|ביטול|לבטל|פגום|שבור|נשבר|תלונה|מאוכזב|לקוי|"
    r"תביע|עורך דין|משפט|ערכאות|refund|return|exchange|cancel|broken|damaged|complaint|lawyer|legal)",
    re.I,
)
HARD_DATE = re.compile(
    r"(ביום\s+\w+|בתאריך\s+\d|\d{1,2}[./]\d{1,2}(?:[./]\d{2,4})?|"
    r"מחר|מחרתיים|תוך\s+\d+\s+(?:שעות|ימי עסקים|ימים)\s+בדיוק)")
TRACK_CLAIM = re.compile(r"(נשלח|יצאה?\s+לדרך|מספר מעקב|מעקב:|tracking)", re.I)

def log(*a):
    line = " ".join(str(x) for x in (time.strftime("%Y-%m-%dT%H:%M:%S"), *a))
    print(line, flush=True)
    with open(QA_LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")

def fetch_body(M, num):
    typ, md = M.fetch(num, "(BODY.PEEK[])")
    if typ != "OK" or not md or not md[0]:
        return None, None
    msg = email.message_from_bytes(md[0][1])
    return msg, w.get_body(msg)

def original_customer_email(M, to_addr):
    """Latest inbox message from this customer, for the sensitive re-check."""
    M.select("INBOX")
    try:
        typ, data = M.search(None, "FROM", '"%s"' % to_addr)
    except Exception:
        return ""
    ids = data[0].split() if data and data[0] else []
    if not ids:
        return ""
    _, body = fetch_body(M, ids[-1])  # most recent match
    return body or ""

def audit_draft(M, to_addr, body):
    """Return list of failure reasons for one draft (empty = pass)."""
    fails = []
    cited = set(re.findall(r"#(\d{3,6})", body or ""))
    orders = w.find_orders(to_addr, "")
    real_names = {re.sub(r"\D", "", (o.get("name") or "")) for o in orders}
    has_tracking = any(
        t.get("number")
        for o in orders for f in (o.get("fulfillments") or [])
        for t in (f.get("trackingInfo") or [])
    )
    for c in cited:
        if c not in real_names:
            fails.append(f"מצוטטת הזמנה #{c} שלא קיימת ללקוח (המצאה)")
    if TRACK_CLAIM.search(body or "") and not has_tracking:
        # only a problem if it asserts it already shipped / gives a number
        if re.search(r"(מספר מעקב|מעקב:|כבר נשלח|יצאה לדרך|tracking)", body or "", re.I):
            fails.append("הטיוטה מרמזת על משלוח/מעקב שלא קיים בהזמנה האמיתית")
    if HARD_DATE.search(body or ""):
        fails.append("הטיוטה מבטיחה תאריך/מועד מדויק (אסור)")
    M.select("INBOX")
    orig = original_customer_email(M, to_addr)
    if orig and SENSITIVE.search(orig):
        fails.append("הפנייה המקורית רגישה (החזר/החלפה/משפטי) והייתה צריכה לעבור אליך, לא טיוטה")
    return fails

def label_fail(M, uid):
    try:
        M.uid("STORE", uid, "+X-GM-LABELS", f'("{QA_LABEL}")')
    except Exception as e:
        log("label failed", repr(e))

def write_alert_draft(M, lines):
    from email.message import EmailMessage
    em = EmailMessage()
    em["From"] = f"CS QA <{USER}>"
    em["To"] = ALERT_TO
    em["Subject"] = f"[CS-QA] {time.strftime('%Y-%m-%d %H:%M')} — נמצאו {len(lines)} בעיות לבדיקה"
    em.set_content("מנהל ה-QA של הבוט מצא בעיות בטיוטות / בבריאות המערכת:\n\n" + "\n".join(lines))
    M.append(DRAFTS_BOX, "(\\Draft)", imaplib.Time2Internaldate(time.time()), em.as_bytes())

def bot_health():
    """Read run.log: when did the drafter last finish, and was it ok."""
    try:
        last_done, last_ts = None, None
        for ln in open(RUN_LOG, encoding="utf-8"):
            if "DONE" in ln:
                last_done = ln.strip()
                last_ts = ln[:19]
        if not last_ts:
            return False, "no DONE line in run.log"
        t = time.mktime(time.strptime(last_ts, "%Y-%m-%dT%H:%M:%S"))
        age_min = (time.time() - t) / 60
        if age_min > 90:
            return False, f"drafter stale: last run {int(age_min)} min ago"
        return True, last_done
    except Exception as e:
        return False, f"health read error: {e}"

def imap_connect():
    last = None
    for attempt in range(5):
        try:
            M = imaplib.IMAP4_SSL("imap.gmail.com")
            M.login(USER, APP_PW)
            return M
        except (OSError, imaplib.IMAP4.error) as e:
            last = e
            log(f"IMAP connect attempt {attempt+1} failed: {e}")
            time.sleep(5 * (attempt + 1))
    raise last

def main():
    open(QA_LOG, "a").close()
    M = imap_connect()
    alerts = []

    ok, detail = bot_health()
    log("HEALTH", "OK" if ok else "FAIL", detail)
    if not ok:
        alerts.append(f"בריאות הבוט: {detail}")

    M.select('"%s"' % DRAFTS_BOX)
    typ, data = M.uid("search", None, "ALL")
    uids = data[0].split() if data and data[0] else []
    checked = passed = failed = 0
    for uid in uids:
        typ, md = M.uid("fetch", uid, "(BODY.PEEK[])")
        if typ != "OK" or not md or not md[0]:
            continue
        msg = email.message_from_bytes(md[0][1])
        to_addr = parseaddr(msg.get("To", ""))[1].lower()
        if not to_addr or to_addr == ALERT_TO.lower():
            continue  # skip our own alert drafts
        body = w.get_body(msg)
        fails = audit_draft(M, to_addr, body)
        checked += 1
        if fails:
            failed += 1
            label_fail(M, uid)
            log("FAIL", to_addr, "|", "; ".join(fails))
            alerts.append(f"טיוטה ל-{to_addr}: " + "; ".join(fails))
        else:
            passed += 1
            log("PASS", to_addr)

    if alerts:
        M.select('"%s"' % DRAFTS_BOX)
        write_alert_draft(M, alerts)

    json.dump({
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "bot_ok": ok, "bot_detail": detail,
        "drafts_checked": checked, "passed": passed, "failed": failed,
    }, open(HEALTH, "w"), ensure_ascii=False, indent=2)
    log("DONE", f"checked={checked} pass={passed} fail={failed} alerts={len(alerts)}")
    M.logout()

if __name__ == "__main__":
    main()
