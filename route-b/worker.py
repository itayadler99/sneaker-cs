#!/usr/bin/env python3
# Route B — IMAP customer-service drafter for the Sneaker Station store mailbox.
# Authorized business automation: owner-supplied app password, OWN mailbox, DRAFT-ONLY.
# Reads full email body (not just subject), asks the claude CLI (owner's subscription)
# to classify + draft order-aware replies using the live shopify-sneakers MCP.
# NEVER sends. Sensitive (refund/exchange/complaint/legal) -> left for Itay.

import imaplib, email, json, os, re, subprocess, sys, time, hashlib
import urllib.request, urllib.parse
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import parsedate_to_datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV = {}
for line in open(os.path.join(ROOT, ".env")):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        ENV[k] = v

USER = ENV["STORE_GMAIL_USER"]
APP_PW = ENV["STORE_GMAIL_APP_PASSWORD"]
STORE_NAME = "SneakerStation"
SHOP_MCP = "shopify-sneakers"   # MCP fallback (last 50 orders only, no email search)
ADMIN_TOKEN = ENV.get("STATION_ADMIN_TOKEN", "").strip()
SHOP_DOMAIN = ENV.get("STATION_SHOP_DOMAIN", "").strip()
API_VER = "2024-10"
DRAFTS_BOX = "[Gmail]/Drafts"
KB = open(os.path.join(ROOT, "kb", "knowledge-base.md"), encoding="utf-8").read()

STATE_PATH = os.path.join(ROOT, "route-b", "processed.json")
MAX_PER_RUN = int(os.environ.get("MAX_PER_RUN", "8"))

IGNORE_SENDER = re.compile(
    r"@(t\.shopifyemail\.com|shopify\.com|email\.shopify\.com|notifications\.tiktok\.com|jotform\.com|loox\.io|klaviyo|facebookmail\.com|support\.facebook\.com|facebook\.com|metamail\.com|fb\.com|accounts\.google\.com|google\.com|paypal\.com|mailchimp)",
    re.I,
)

def log(*a):
    print(time.strftime("%Y-%m-%dT%H:%M:%S"), *a, flush=True)

def load_state():
    try:
        return set(json.load(open(STATE_PATH)))
    except Exception:
        return set()

def save_state(s):
    json.dump(sorted(s), open(STATE_PATH, "w"))

def dh(v):
    if not v:
        return ""
    try:
        return str(make_header(decode_header(v)))
    except Exception:
        return v

def get_body(msg):
    if msg.is_multipart():
        # prefer text/plain
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
    # keep only the latest message, drop quoted history
    lines = []
    for ln in body.splitlines():
        if re.match(r"^\s*(On .+wrote:|בתאריך .+מאת)", ln):
            break
        if ln.strip().startswith(">"):
            continue
        lines.append(ln)
    return "\n".join(lines).strip() or body.strip()

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
- בלי מקפים ארוכים (—). בלי סופרלטיבים. בלי ניסוחים רובוטיים או תבניתיים. בלי "אני כאן כדי לעזור" וקלישאות בוט.
- חתמי בצורה טבעית בשם החנות.

החזירי אך ורק JSON שורה אחת, בלי טקסט נוסף:
{{"action":"draft"|"escalate","reply":"<תשובה מלאה בעברית עם ירידות שורה כ-\\n>","confidence":0.0-1.0,"reason":"<קצר>","order_found":true|false,"order_number":"<מספר או ריק>"}}

=== הזמנות הלקוח (נשלף חי מ-Shopify, מקור אמת) ===
{orders}

=== ידע ===
{kb}

=== פרטי הפנייה ===
שולח: {sender_name} <{sender_email}>
נושא: {subject}

=== תוכן הפנייה ===
{message}
"""

ORDER_GQL = """{ orders(first: 5, query: %s) { edges { node {
  name email createdAt displayFinancialStatus displayFulfillmentStatus
  lineItems(first: 15) { edges { node { title quantity variantTitle } } }
  fulfillments { trackingInfo { number url company } estimatedDeliveryAt }
} } } }"""

def shopify_gql(query):
    """POST a read-only GraphQL query to the store's Shopify Admin API. Returns data or None."""
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
    """Pull the customer's real orders from Shopify by email, then by order number in body."""
    orders = _orders_from(shopify_gql(ORDER_GQL % json.dumps(f"email:{sender_email}")))
    if not orders:
        for n in list(dict.fromkeys(re.findall(r"#?\s*(\d{3,6})", body or "")))[:3]:
            orders.extend(_orders_from(shopify_gql(ORDER_GQL % json.dumps(f"name:{n}"))))
    return orders

def format_orders(orders):
    """Compact, accurate, hallucination-proof order summary for the prompt."""
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

def ask_brain(sender_name, sender_email, subject, message):
    orders_block = format_orders(find_orders(sender_email, message))
    prompt = PROMPT_TMPL.format(
        store=STORE_NAME, mcp=SHOP_MCP, kb=KB, orders=orders_block,
        sender_name=sender_name, sender_email=sender_email,
        subject=subject, message=message,
    )
    try:
        out = subprocess.run(
            ["claude", "-p", "--output-format", "text",
             "--dangerously-skip-permissions",
             "--allowedTools", f"mcp__{SHOP_MCP}__get_orders,mcp__{SHOP_MCP}__get_order"],
            input=prompt, capture_output=True, text=True, timeout=240,
        )
    except subprocess.TimeoutExpired:
        return {"action": "escalate", "reason": "brain timeout"}
    raw = (out.stdout or "").strip()
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

def append_draft(imap, to_addr, to_name, subject, body, in_reply_to, references):
    em = EmailMessage()
    em["From"] = f"{STORE_NAME} <{USER}>"
    em["To"] = f"{to_name} <{to_addr}>" if to_name else to_addr
    em["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    if in_reply_to:
        em["In-Reply-To"] = in_reply_to
        em["References"] = (references + " " + in_reply_to).strip() if references else in_reply_to
    em.set_content(body)
    imap.append(DRAFTS_BOX, "(\\Draft)", imaplib.Time2Internaldate(time.time()),
                em.as_bytes())

def existing_draft_recipients(imap):
    """Emails that already have a draft -> never double-draft."""
    recips = set()
    imap.select('"%s"' % DRAFTS_BOX)
    typ, data = imap.search(None, "ALL")
    ids = data[0].split() if data and data[0] else []
    for i in ids:
        typ, md = imap.fetch(i, "(BODY.PEEK[HEADER.FIELDS (TO)])")
        if not md or not md[0]:
            continue
        to = email.utils.parseaddr(md[0][1].decode("utf-8", "replace").split(":", 1)[-1])[1].lower()
        if to:
            recips.add(to)
    return recips

def imap_connect():
    # Transient DNS/network blips (e.g. right after wake-from-sleep) must not crash
    # the whole round; retry a few times with backoff before giving up.
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
    seen = load_state()
    M = imap_connect()
    already = existing_draft_recipients(M)
    log(f"existing drafts cover {len(already)} recipients")
    M.select("INBOX")
    typ, data = M.search(None, "UNSEEN")
    ids = data[0].split() if data and data[0] else []
    log(f"unseen={len(ids)} cap={MAX_PER_RUN}")
    drafted = escalated = skipped = 0
    # newest first
    for num in reversed(ids):
        if drafted + escalated >= MAX_PER_RUN:
            break
        typ, md = M.fetch(num, "(BODY.PEEK[])")  # PEEK = do NOT mark as read
        if typ != "OK" or not md or not md[0]:
            continue
        msg = email.message_from_bytes(md[0][1])
        msgid = (msg.get("Message-ID") or "").strip()
        frm = email.utils.parseaddr(msg.get("From", ""))
        sender_name, sender_email = dh(frm[0]), frm[1].lower()
        subject = dh(msg.get("Subject", ""))
        key = msgid or hashlib.md5((sender_email + subject).encode()).hexdigest()
        if key in seen:
            continue
        if not sender_email or IGNORE_SENDER.search(sender_email):
            seen.add(key); skipped += 1; continue
        # store's own address / order notifications are not customer questions
        if sender_email == USER.lower() or re.search(r"order\s+#?\d+\s+placed|\[SneakerStation\]", subject, re.I):
            seen.add(key); skipped += 1
            log(f"SKIP   {sender_email} | not a customer question ({subject[:30]})")
            continue
        if sender_email in already:
            seen.add(key); skipped += 1
            log(f"SKIP   {sender_email} | already has a draft")
            continue
        body = quote_top(get_body(msg))
        if not body:
            seen.add(key); skipped += 1; continue
        res = ask_brain(sender_name, sender_email, subject, body)
        if res.get("action") == "draft":
            append_draft(M, sender_email, sender_name, subject,
                         res["reply"], msgid, dh(msg.get("References", "")))
            drafted += 1
            log(f"DRAFT  {sender_email} | {subject[:40]} | conf={res.get('confidence')}")
        else:
            escalated += 1
            log(f"ESCAL  {sender_email} | {subject[:40]} | {res.get('reason')}")
        seen.add(key)
        save_state(seen)
    M.logout()
    save_state(seen)
    log(f"DONE drafted={drafted} escalated={escalated} skipped={skipped}")

if __name__ == "__main__":
    main()
