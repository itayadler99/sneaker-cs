#!/usr/bin/env python3
# Route B CLOUD worker — order-aware customer-service AUTO-RESPONDER.
# Runs headless in the cloud (GitHub Actions cron) — no Mac required.
# Brain = Anthropic Messages API (the local worker uses the `claude` CLI / Max sub,
# which only exists on Itay's machine; in the cloud we call the API directly).
# Order facts are pulled live from Shopify and injected into the prompt, so the
# model never invents tracking numbers or dates.
#
# Authorized business automation: owner-supplied app password, OWN store mailbox.
# SENDS replies only when the model returns action=draft AND confidence>=MIN_CONF
# AND the message is not sensitive. Anything else (refund/complaint/legal/unsure)
# is escalated to the owner via Telegram and left UNREAD for manual handling.

import imaplib, smtplib, email, json, os, re, sys, time, hashlib
import socket
import urllib.request, urllib.parse

# Hard global socket timeout: without it a single stalled IMAP fetch hangs the
# process forever (July 20 2026: worker hung 21 days, blocking launchd + watchdog).
socket.setdefaulttimeout(60)
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import parsedate_to_datetime, formatdate, make_msgid

# ---- config (env-driven so the same file serves both stores in the cloud) ----
def env(k, d=""):
    return (os.environ.get(k, d) or "").strip()

STORE = env("STORE", "station").lower()                 # station | studio
USER = env("STORE_GMAIL_USER")
APP_PW = env("STORE_GMAIL_APP_PASSWORD")
if STORE == "studio":
    STORE_NAME = "SneakerStudio"
    ADMIN_TOKEN = env("STUDIO_ADMIN_TOKEN")
    SHOP_DOMAIN = env("STUDIO_SHOP_DOMAIN")
    # studio may use its own mailbox; fall back to the shared one if unset
    USER = env("STUDIO_GMAIL_USER", USER)
    APP_PW = env("STUDIO_GMAIL_APP_PASSWORD", APP_PW)
else:
    STORE_NAME = "SneakerStation"
    ADMIN_TOKEN = env("STATION_ADMIN_TOKEN")
    SHOP_DOMAIN = env("STATION_SHOP_DOMAIN")

ANTHROPIC_API_KEY = env("ANTHROPIC_API_KEY")
MODEL = env("ANTHROPIC_MODEL", "claude-sonnet-4-5")
API_VER = "2024-10"
MAX_PER_RUN = int(env("MAX_PER_RUN", "6"))
MIN_CONF = float(env("MIN_CONF", "0.80"))
ENABLE_SEND = env("ENABLE_SEND", "1") not in ("0", "false", "no", "")
TG_TOKEN = env("TELEGRAM_BOT_TOKEN")
TG_CHAT = env("TELEGRAM_CHAT_ID")

DRAFTS_BOX = "[Gmail]/Drafts"
SENT_BOX = "[Gmail]/Sent Mail"
DONE_LABEL = "cs-bot-seen"   # Gmail label = durable state (the cloud disk is ephemeral)
BOT_HEADER = "X-CS-Bot"      # stamped on machine-written mail so learn.py can exclude it
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KB = open(os.path.join(ROOT, "kb", "knowledge-base.md"), encoding="utf-8").read()
# few-shot bank distilled from REAL human-sent replies (learn.py). Makes the bot
# imitate how a real human answered before. Optional — empty until learn.py runs.
_LEARNED_PATH = os.path.join(ROOT, "kb", f"learned-{STORE}.md")
try:
    LEARNED = open(_LEARNED_PATH, encoding="utf-8").read().strip()
except Exception:
    LEARNED = ""

IGNORE_SENDER = re.compile(
    r"@(t\.shopifyemail\.com|shopify\.com|email\.shopify\.com|notifications\.tiktok\.com|"
    r"jotform\.com|loox\.io|klaviyo|facebookmail\.com|support\.facebook\.com|facebook\.com|"
    r"metamail\.com|fb\.com|accounts\.google\.com|google\.com|paypal\.com|mailchimp)",
    re.I,
)
# Hard safety net: never auto-send if any of these appear, regardless of model output.
SENSITIVE = re.compile(
    r"(החזר|זיכוי|ביטול|פגום|שבור|נזק|תלונה|מתבייש|עורך דין|תביעה|משטרה|הונאה|רמאות|"
    r"refund|cancel|broken|damaged|lawyer|legal|chargeback|fraud|scam|complaint)",
    re.I,
)

def log(*a):
    print(time.strftime("%Y-%m-%dT%H:%M:%S"), STORE, *a, flush=True)

def tg(msg):
    if not TG_TOKEN or not TG_CHAT:
        return
    try:
        data = urllib.parse.urlencode({"chat_id": TG_CHAT, "text": msg}).encode()
        urllib.request.urlopen(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data=data, timeout=15)
    except Exception as e:
        # never fail the run over Telegram, but never fail silently either
        log("TELEGRAM-FAIL", repr(e))

def msg_labels(imap, num):
    """Gmail labels on a message (used as durable processed-state in the cloud)."""
    try:
        typ, d = imap.fetch(num, "(X-GM-LABELS)")
        return (d[0].decode("utf-8", "replace") if d and d[0] else "")
    except Exception:
        return ""

def mark_done(imap, num):
    try:
        imap.store(num, "+X-GM-LABELS", DONE_LABEL)
    except Exception as e:
        log("label warn", repr(e))

def dh(v):
    if not v:
        return ""
    try:
        return str(make_header(decode_header(v)))
    except Exception:
        return v

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

def quote_top(body):
    lines = []
    for ln in body.splitlines():
        if re.match(r"^\s*(On .+wrote:|בתאריך .+מאת)", ln):
            break
        if ln.strip().startswith(">"):
            continue
        lines.append(ln)
    return "\n".join(lines).strip() or body.strip()

# ---- Shopify order lookup (live source of truth, injected into the prompt) ----
ORDER_GQL = """{ orders(first: 5, query: %s) { edges { node {
  name email createdAt displayFinancialStatus displayFulfillmentStatus
  lineItems(first: 15) { edges { node { title quantity variantTitle } } }
  fulfillments { trackingInfo { number url company } estimatedDeliveryAt }
} } } }"""

def shopify_gql(query):
    if not ADMIN_TOKEN or not SHOP_DOMAIN:
        return None
    body = json.dumps({"query": query}).encode("utf-8")
    req = urllib.request.Request(
        f"https://{SHOP_DOMAIN}/admin/api/{API_VER}/graphql.json",
        data=body, method="POST",
        headers={"X-Shopify-Access-Token": ADMIN_TOKEN, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r).get("data")
    except Exception as e:
        log("shopify gql failed", repr(e))
        return None

def _orders_from(data):
    if not data:
        return []
    return [e["node"] for e in data.get("orders", {}).get("edges", [])]

def find_orders(sender_email, body):
    orders = _orders_from(shopify_gql(ORDER_GQL % json.dumps(f"email:{sender_email}")))
    if not orders:
        for n in list(dict.fromkeys(re.findall(r"#?\s*(\d{3,6})", body or "")))[:3]:
            orders.extend(_orders_from(shopify_gql(ORDER_GQL % json.dumps(f"name:{n}"))))
    return orders

def format_orders(orders):
    if not orders:
        return "(לא נמצאה הזמנה תואמת ב-Shopify לפי האימייל/המספר.)"
    out = []
    for o in orders[:5]:
        lines = [
            f"הזמנה {o.get('name','')} | נוצרה {(o.get('createdAt') or '')[:10]}",
            f"תשלום: {o.get('displayFinancialStatus','')} | מילוי: {o.get('displayFulfillmentStatus','')}",
        ]
        items = [e["node"] for e in (o.get("lineItems") or {}).get("edges", [])]
        if items:
            lines.append("פריטים: " + "; ".join(
                f"{li.get('title','')} {li.get('variantTitle') or ''} x{li.get('quantity',1)}".strip()
                for li in items))
        for f in (o.get("fulfillments") or []):
            for t in (f.get("trackingInfo") or []):
                tn, tu, co = t.get("number"), t.get("url"), t.get("company")
                if tn or tu:
                    lines.append(f"מעקב: {co or ''} {tn or ''} {tu or ''}".strip())
        out.append("\n".join(lines))
    return "\n\n".join(out)

# ---- brain: Anthropic Messages API ----
PROMPT_TMPL = """את נציגת שירות לקוחות אמיתית בחנות סניקרס ישראלית ({store}). את עונה כמו בן אדם, לא כמו בוט.

שלב 1 — נתוני ההזמנה האמיתיים:
- מתחת מצורף בלוק "הזמנות הלקוח" שנשלף ישירות מ-Shopify לפי האימייל/המספר. זהו מקור האמת היחיד להזמנה.
- בססי את כל התשובה על הנתונים שבבלוק — מספר ההזמנה, הפריטים שהוזמנו, סטטוס המשלוח, מספר מעקב אם יש, ותאריכים. ציטטי אותם בתשובה.
- אם הבלוק ריק או לא תואם את מה שהלקוח שואל, ואינך יכולה לוודא את התשובה — אל תנחשי.

שלב 2 — דיוק:
- עני אך ורק על בסיס נתוני ההזמנה החיים + הידע המצורף. אסור להמציא תאריך, מספר מעקב, מחיר, או מדיניות.
- אם לא מצאת הזמנה תואמת, וזו שאלה כללית של "איפה ההזמנה/מתי יגיע" — תני הרגעה כללית לפי הקשר העיכובים בידע, בלי להמציא מספר מעקב או תאריך.
- אם הלקוח שואל על משהו שאי אפשר לאמת מהנתונים — escalate.

שלב 3 — מתי לא לענות (escalate):
- תלונה, נזק, מוצר פגום, החזר כספי, החלפה, ביטול, איום משפטי, או כל מקרה שאת לא בטוחה בו = אל תכתבי תשובה, החזרי action=escalate.

טון ושפה — שיישמע אנושי:
- עברית תקנית וטבעית, כמו אדם אמיתי שכותב ללקוח.
- חם, אישי, קצר וענייני. פני ללקוח בשמו הפרטי.
- בלי מקפים ארוכים. בלי סופרלטיבים. בלי ניסוחים רובוטיים או תבניתיים. בלי "אני כאן כדי לעזור" וקלישאות בוט.
- חתמי בצורה טבעית בשם החנות.

החזירי אך ורק JSON שורה אחת, בלי טקסט נוסף:
{{"action":"draft"|"escalate","reply":"<תשובה מלאה בעברית עם ירידות שורה כ-\\n>","confidence":0.0-1.0,"reason":"<קצר>","order_found":true|false,"order_number":"<מספר או ריק>"}}

=== הזמנות הלקוח (נשלף חי מ-Shopify, מקור אמת) ===
{orders}

=== ידע ===
{kb}

=== דוגמאות מתשובות אמיתיות שנשלחו על ידי נציג אנושי (חקי את הטון, הסגנון והעובדות האלה) ===
{learned}

=== פרטי הפנייה ===
שולח: {sender_name} <{sender_email}>
נושא: {subject}

=== תוכן הפנייה ===
{message}
"""

def ask_brain(sender_name, sender_email, subject, message):
    orders_block = format_orders(find_orders(sender_email, message))
    prompt = PROMPT_TMPL.format(
        store=STORE_NAME, kb=KB, orders=orders_block,
        learned=LEARNED or "(עדיין אין דוגמאות נלמדות.)",
        sender_name=sender_name, sender_email=sender_email,
        subject=subject, message=message,
    )
    payload = json.dumps({
        "model": MODEL,
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=payload, method="POST",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            resp = json.load(r)
        raw = "".join(b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text").strip()
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:200]
        log("anthropic HTTPError", e.code, detail)
        return {"action": "escalate", "reason": f"api {e.code}"}
    except Exception as e:
        return {"action": "escalate", "reason": f"api error {e!r}"}
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return {"action": "escalate", "reason": "no json from brain"}
    try:
        j = json.loads(m.group(0))
    except Exception:
        return {"action": "escalate", "reason": "unparseable brain json"}
    if j.get("action") != "draft" or not j.get("reply"):
        return {"action": "escalate", "reason": j.get("reason", "low confidence")}
    return j

# ---- send (SMTP) + archive a copy to Sent ----
def send_reply(to_addr, to_name, subject, body, in_reply_to, references):
    em = EmailMessage()
    em["From"] = f"{STORE_NAME} <{USER}>"
    em["To"] = f"{to_name} <{to_addr}>" if to_name else to_addr
    em["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    em["Date"] = formatdate(localtime=True)
    em["Message-ID"] = make_msgid(domain=USER.split("@")[-1])
    # Machine-readable provenance. learn.py treats Sent Mail as the human gold
    # standard; without this stamp it would distil the bot's own replies back
    # into the few-shot bank and slowly drift away from how Itay actually writes.
    em[BOT_HEADER] = "cloud-worker"
    if in_reply_to:
        em["In-Reply-To"] = in_reply_to
        em["References"] = (references + " " + in_reply_to).strip() if references else in_reply_to
    em.set_content(body)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
        s.login(USER, APP_PW)
        s.send_message(em)
    return em

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

def append_draft(imap, em):
    imap.append(DRAFTS_BOX, "(\\Draft)", imaplib.Time2Internaldate(time.time()), em.as_bytes())

def main():
    if not ANTHROPIC_API_KEY:
        log("FATAL no ANTHROPIC_API_KEY"); print("DONE sent=0 escalated=0"); return
    M = imap_connect()
    M.select("INBOX")
    # Server-side dedup. Asking Gmail for "unseen and not already labelled" costs
    # one round trip; checking the label per message used to cost one fetch per
    # unseen mail (~280/run on studio), which is what tripped the 60s socket
    # timeout and failed the run.
    ids = []
    try:
        typ, data = M.search(None, "UNSEEN", "NOT", "X-GM-LABELS", f'"{DONE_LABEL}"')
        if typ == "OK":
            ids = data[0].split() if data and data[0] else []
        else:
            raise imaplib.IMAP4.error(f"search typ={typ}")
    except imaplib.IMAP4.error as e:
        log("label-search unsupported, falling back:", repr(e))
        typ, data = M.search(None, "UNSEEN")
        ids = data[0].split() if data and data[0] else []
        ids = [n for n in ids if DONE_LABEL not in msg_labels(M, n)]
    log(f"todo={len(ids)} cap={MAX_PER_RUN} send={ENABLE_SEND}")
    sent = escalated = skipped = 0
    for num in reversed(ids):
        if sent + escalated >= MAX_PER_RUN:
            break
        # Belt and braces: the search above should already exclude handled mail,
        # but re-answering a customer is expensive, so confirm the label on the
        # few messages we are actually about to touch.
        if DONE_LABEL in msg_labels(M, num):
            continue
        typ, md = M.fetch(num, "(BODY.PEEK[])")
        if typ != "OK" or not md or not md[0]:
            continue
        msg = email.message_from_bytes(md[0][1])
        msgid = (msg.get("Message-ID") or "").strip()
        frm = email.utils.parseaddr(msg.get("From", ""))
        sender_name, sender_email = dh(frm[0]), frm[1].lower()
        subject = dh(msg.get("Subject", ""))
        if not sender_email or IGNORE_SENDER.search(sender_email):
            mark_done(M, num); skipped += 1; continue
        if sender_email == USER.lower() or re.search(r"order\s+#?\d+\s+placed|\[Sneaker", subject, re.I):
            mark_done(M, num); skipped += 1; continue
        body = quote_top(get_body(msg))
        if not body:
            mark_done(M, num); skipped += 1; continue

        res = ask_brain(sender_name, sender_email, subject, body)
        conf = float(res.get("confidence") or 0)
        sensitive = bool(SENSITIVE.search(subject + " " + body))
        can_send = (res.get("action") == "draft" and conf >= MIN_CONF
                    and not sensitive and ENABLE_SEND)

        if can_send:
            try:
                em = send_reply(sender_email, sender_name, subject,
                                res["reply"], msgid, dh(msg.get("References", "")))
                # archive a copy into Sent so it threads in Gmail
                try:
                    M.append(f'"{SENT_BOX}"', "(\\Seen)",
                             imaplib.Time2Internaldate(time.time()), em.as_bytes())
                except Exception as e:
                    log("sent-append warn", repr(e))
                # mark the customer mail as read (handled)
                M.store(num, "+FLAGS", "\\Seen")
                sent += 1
                log(f"SENT  {sender_email} | {subject[:40]} | conf={conf}")
                tg(f"🤖 {STORE_NAME} — נשלחה תשובה אוטומטית ללקוח\n"
                   f"אל: {sender_name} <{sender_email}>\n"
                   f"נושא: {subject}\n\n{res['reply']}")
            except Exception as e:
                escalated += 1
                log(f"SEND-FAIL {sender_email} | {e!r}")
                tg(f"⚠️ {STORE_NAME}: כשל שליחה ל-{sender_email}. צריך בדיקה ידנית.")
        else:
            # leave a draft for Itay + ping, keep unread
            try:
                em = EmailMessage()
                em["From"] = f"{STORE_NAME} <{USER}>"
                em["To"] = sender_email
                em["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
                em[BOT_HEADER] = "cloud-worker-draft"
                if res.get("reply"):
                    em.set_content(res["reply"])
                else:
                    em.set_content("(להשלמה ידנית)")
                append_draft(M, em)
            except Exception:
                pass
            escalated += 1
            why = "רגיש" if sensitive else res.get("reason", f"conf={conf}")
            log(f"ESCAL {sender_email} | {subject[:40]} | {why}")
            tg(f"📥 {STORE_NAME} — פנייה הושארה לך (לא נשלח, טיוטה ב-Gmail)\n"
               f"מ: {sender_name} <{sender_email}>\n"
               f"נושא: {subject}\n"
               f"סיבה: {why}"
               + (f"\n\nטיוטה מוצעת:\n{res['reply']}" if res.get("reply") else ""))
        mark_done(M, num)   # durable: never re-bill the brain on this message again
    M.logout()
    log(f"DONE sent={sent} escalated={escalated} skipped={skipped}")

if __name__ == "__main__":
    try:
        main()
    except (OSError, imaplib.IMAP4.error) as e:
        # A transient Gmail/IMAP hiccup is not a bug worth a failed-run email on
        # every cron tick. Log it loudly and exit clean; the supervisor already
        # alerts when runs go stale or the backlog grows, so a real outage is
        # still caught.
        log(f"TRANSIENT imap/net error, exiting clean: {e!r}")
        print("DONE sent=0 escalated=0 transient=1")
