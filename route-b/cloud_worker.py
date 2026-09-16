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
import subprocess
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
CLI_MODEL = env("CLI_MODEL", "sonnet")   # brain fallback: the `claude` CLI (Max sub)
GATEWAY_KEY = env("AI_GATEWAY_API_KEY")  # brain fallback #3: Vercel AI Gateway
GATEWAY_MODEL = env("AI_GATEWAY_MODEL", "anthropic/claude-sonnet-4.5")
OPENAI_API_KEY = env("OPENAI_API_KEY")   # brain fallback #4, for the cloud (no CLI there)
OPENAI_MODEL = env("OPENAI_MODEL", "gpt-4.1")
API_VER = "2024-10"
MAX_PER_RUN = int(env("MAX_PER_RUN", "6"))
MIN_CONF = float(env("MIN_CONF", "0.80"))

# Whether a store actually sends is decided here, in the repo, not only in the
# workflow yaml. The yaml pins ENABLE_SEND per store and changing it needs a
# token scope we do not have, which is how station sat in draft-only mode for
# months while everyone assumed customers were being answered. This file is the
# switch we can reach; the env var stays as the fallback.
def _send_policy(store):
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "send_policy.json")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f).get(store)
    except Exception:
        return None

_policy = _send_policy(STORE)
ENABLE_SEND = (bool(_policy) if _policy is not None
               else env("ENABLE_SEND", "1") not in ("0", "false", "no", ""))
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
    # Authenticity (2026-09-10, owner instruction): the bot never answers this, in either
    # direction. A customer who asks gets escalated to Itay, never an automatic reply.
    r"מקורי|מקוריים|מקורית|מקוריות|אורגינל|זיוף|מזויף|מזויפות|חיקוי|רפליק|"
    r"authentic|genuine|replica|counterfeit|"
    r"refund|cancel|broken|damaged|lawyer|legal|chargeback|fraud|scam|complaint)",
    re.I,
)

# ---- OUTGOING guard (added 2026-08-24 after a live incident) ----
# SENSITIVE above only reads the CUSTOMER's mail. A plain stock question carries
# no sensitive keyword, so it passes — and on 2026-08-24 the model volunteered a
# full refund + cancellation on its own and the mail shipped. So we also read
# what the bot is ABOUT TO SAY. Rules live in promise_guard.py, shared with
# catchup.py, so there is exactly one list to maintain.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from promise_guard import violation as outgoing_violation
# Second outgoing gate (2026-08-26). promise_guard catches what the bot OFFERS;
# this one catches what it ASSERTS — that the order exists and is moving, or
# that the bot already changed something. Both were sent to real customers.
from order_claim_guard import violation as order_claim_violation


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

OWNER_EMAIL = env("OWNER_EMAIL", "itayadler99@gmail.com")

def mail_owner(subject, body):
    """Itay does not read Telegram. Anything he must actually see goes to his
    inbox as well (feedback_itay_never_reads_telegram)."""
    if not USER or not APP_PW or not OWNER_EMAIL:
        return
    try:
        em = EmailMessage()
        em["From"] = f"{STORE_NAME} CS Bot <{USER}>"
        em["To"] = OWNER_EMAIL
        em["Subject"] = subject
        em[BOT_HEADER] = "cloud-worker-alert"   # keep it out of the learning bank
        em.set_content(body)
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
            s.login(USER, APP_PW)
            s.send_message(em)
        log("emailed owner")
    except Exception as e:
        log("owner-mail warn", repr(e))

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

# Where a quoted thread begins. The old version matched only two English/Hebrew
# forms anchored at line start, and Gmail in Hebrew prefixes the line with an RTL
# mark (U+200F) so the anchor never matched: the whole thread leaked into the
# prompt and the model echoed it back at the customer.
BIDI = re.compile(r"[\u200e\u200f\u202a-\u202e\u2066-\u2069]")
QUOTE_START = re.compile(
    r"^\s*(On .+wrote:|בתאריך .+(מאת|כתב)|-{2,}\s*(Original Message|הודעה מקורית)|"
    r"(From|Sent|מאת|נשלח):\s|_{5,}|Forwarded message)", re.I)

def strip_thread(body):
    """Only the part the customer actually typed now."""
    lines = []
    for ln in body.splitlines():
        clean = BIDI.sub("", ln)
        if QUOTE_START.match(clean):
            break
        if clean.strip().startswith(">"):
            continue
        lines.append(clean)
    return "\n".join(lines).strip() or BIDI.sub("", body).strip()

quote_top = strip_thread   # old name, still used elsewhere

# Typos the model has actually produced and shipped to customers. A rule in the
# prompt did not stop them; a substitution does.
TYPOS = {"משלןח": "משלוח", "הזמנח": "הזמנה", "תודח": "תודה", "שלןם": "שלום"}

def clean_reply(text):
    text = BIDI.sub("", text or "")
    out = []
    for ln in text.splitlines():
        if QUOTE_START.match(ln) or ln.strip().startswith(">"):
            break          # never quote the thread back at the customer
        out.append(ln)
    text = "\n".join(out)
    for bad, good in TYPOS.items():
        text = text.replace(bad, good)
    return re.sub(r"\n{3,}", "\n\n", text).strip()

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

שלב 3 — אסור בהחלט להציע (גם אם הלקוח לא ביקש):
- אסור להציע, לרמוז או להזכיר כאפשרות: החזר כספי, ביטול הזמנה, זיכוי, החלפה, פיצוי, קופון, הנחה, או כל ויתור כספי אחר. גם לא בנימוס, גם לא כ"אם תעדיפי". ההחלטות האלה שייכות לבעל החנות בלבד.
- אם נראה לך שהלקוח צריך אחת מהאפשרויות האלה, זו בדיוק הסיבה להחזיר action=escalate ולא לכתוב תשובה.

שלב 3ב — מקוריות המוצר, איסור מוחלט:
- אסור לך לכתוב את המילים "מקורי", "מקוריים", "מקוריות", "אורגינל", "authentic", "זיוף", "חיקוי" או כל נוסח שנוגע בשאלה אם המוצר מקורי או לא. לא לחיוב, לא לשלילה, לא ברמז.
- לקוח ששואל "הנעליים מקוריות?" או כל וריאציה = **action=escalate**, בלי לכתוב תשובה. זה עובר לבעל החנות.
- גם אם יש בדוגמאות הנלמדות למטה תשובה שאומרת שהמוצרים מקוריים — **אל תחקי אותה.** ההוראה הזו גוברת על כל דוגמה.

שלב 4 — מתי לא לענות (escalate):
- תלונה, נזק, מוצר פגום, החזר כספי, החלפה, ביטול, איום משפטי, שאלה על מקוריות, או כל מקרה שאת לא בטוחה בו = אל תכתבי תשובה, החזרי action=escalate.

טון ושפה — שיישמע אנושי:
- עברית תקנית וטבעית, כמו אדם אמיתי שכותב ללקוח. בדקי איות לפני שאת מחזירה.
- חם, אישי, קצר וענייני. פני ללקוח בשמו הפרטי.
- בלי מקפים ארוכים. בלי סופרלטיבים. בלי ניסוחים רובוטיים או תבניתיים. בלי "אני כאן כדי לעזור" וקלישאות בוט.
- חתמי בצורה טבעית בשם החנות.

מבנה התשובה — חובה, שלושת החלקים:
1. פנייה בשם הלקוח ומשפט קצר שמראה שקראת מה הוא כתב.
2. התשובה עצמה לפי נתוני ההזמנה.
3. משפט סיום שמזמין אותו לחזור אלינו אם צריך.
תשובה של שורה אחת יבשה נחשבת כישלון גם אם היא נכונה עובדתית.

⛔ אסור להעתיק לתוך התשובה את המייל של הלקוח, שורות שמתחילות ב-">", או היסטוריית התכתבות.
כתבי רק את הטקסט החדש שלך.

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

def alert_brain_down(n):
    """One incident alert, not one per message and not one per five minutes."""
    # In GitHub Actions the filesystem is thrown away after every run, so the
    # rate limit below cannot hold and this would fire every five minutes - the
    # exact alert-flood that made the last outage invisible. The Mac's sentinel
    # owns alerting; the cloud just works quietly and leaves the mail for later.
    if env("GITHUB_ACTIONS"):
        return
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "brain_state.json")
    try:
        st = json.load(open(path, encoding="utf-8"))
    except Exception:
        st = {}
    last = (st.get(STORE) or {}).get("alerted_at", 0)
    if time.time() - float(last or 0) < 6 * 3600:
        return
    st.setdefault(STORE, {})["alerted_at"] = time.time()
    try:
        json.dump(st, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    except Exception:
        pass
    msg = (f"🔴 {STORE_NAME}: אף מוח לא זמין (API + CLI + OpenAI כולם נפלו). "
           f"{n} פניות לקוח ממתינות ולא סומנו כטופלו - הן ייענו אוטומטית ברגע שמוח יחזור. "
           f"אין צורך לענות ידנית.")
    tg(msg)
    mail_owner(f"🔴 {STORE_NAME}: הבוט בלי מוח", msg)


def note_brain_down(why):
    """Record that the whole brain chain refused. The sentinel reads this file:
    a single failing provider is not an incident, zero working providers is."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "brain_state.json")
    try:
        st = json.load(open(path, encoding="utf-8"))
    except Exception:
        st = {}
    st[STORE] = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "why": why}
    try:
        json.dump(st, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    except Exception:
        pass


def brain_via_cli(prompt):
    """Fallback brain: the `claude` CLI, billed to the Max subscription.

    ANTHROPIC_API_KEY is stripped from the child environment on purpose - with
    it set the CLI bills the same empty API account that just refused us."""
    child_env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    out = subprocess.run(
        ["claude", "-p", "--output-format", "text", "--model", CLI_MODEL,
         "--dangerously-skip-permissions"],
        input=prompt, capture_output=True, text=True, timeout=240,
        env=child_env, cwd="/tmp",
    )
    return (out.stdout or "").strip()


def brain_via_gateway(prompt):
    """Vercel AI Gateway - the cloud's own brain, billed to the Vercel account.

    GitHub Actions has no `claude` CLI and no subscription, so without this the
    cloud can only work while the Anthropic account has credit. The gateway
    speaks the OpenAI chat shape and reaches Claude through the same Vercel bill
    that already pays for the cron that triggers these runs. On the free tier it
    answers a handful of requests and then returns 429, so it only carries real
    traffic once credits are topped up - until then it fails over like any other
    dead provider."""
    if not GATEWAY_KEY:
        raise RuntimeError("no AI_GATEWAY_API_KEY")
    body = json.dumps({
        "model": GATEWAY_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 1024,
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://ai-gateway.vercel.sh/v1/chat/completions", data=body, method="POST",
        headers={"Authorization": f"Bearer {GATEWAY_KEY}", "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        d = json.load(r)
    return (d["choices"][0]["message"]["content"] or "").strip()


def brain_via_openai(prompt):
    """Last-resort brain. The CLI only exists on the Mac, so in GitHub Actions
    this is what keeps customers answered when the Anthropic API is down."""
    if not OPENAI_API_KEY:
        raise RuntimeError("no OPENAI_API_KEY")
    body = json.dumps({
        "model": OPENAI_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 1024,
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions", data=body, method="POST",
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}",
                 "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        d = json.load(r)
    return (d["choices"][0]["message"]["content"] or "").strip()


def ask_brain(sender_name, sender_email, subject, message, orders=None):
    # orders is passed in by main() so the same lookup result decides both what
    # the model sees and whether order_claim_guard lets the reply out. Looking
    # it up twice would let the two disagree.
    if orders is None:
        orders = find_orders(sender_email, message)
    orders_block = format_orders(orders)
    prompt = PROMPT_TMPL.format(
        store=STORE_NAME, kb=KB, orders=orders_block,
        learned=LEARNED or "(עדיין אין דוגמאות נלמדות.)",
        sender_name=sender_name, sender_email=sender_email,
        subject=subject, message=message,
    )
    return ask_brain_prompt(prompt)


def ask_brain_prompt(prompt):
    """Run one already-built prompt through the brain chain: API, then CLI.

    Split out of ask_brain so the backlog tool can hand over a prompt carrying
    its own extra rules without rebuilding the template by hand."""
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
    raw = ""
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            resp = json.load(r)
        raw = "".join(b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text").strip()
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:200]
        log("anthropic HTTPError", e.code, detail)
        why = f"api {e.code}"
    except Exception as e:
        why = f"api error {e!r}"
    else:
        why = ""
    if why:
        # The API is not the only brain we own. On 2026-09-02 the API credit
        # balance hit zero, every customer mail escalated for two weeks, and
        # nobody got an answer. The `claude` CLI on the Mac runs on the Max
        # subscription and costs nothing per call, so it takes over whenever the
        # API refuses. In GitHub Actions the CLI does not exist, the call raises,
        # and we escalate exactly as before.
        for name, fn in (("claude CLI", brain_via_cli), ("vercel gateway", brain_via_gateway),
                         ("openai", brain_via_openai)):
            try:
                raw = fn(prompt)
                if raw:
                    log(f"brain fallback -> {name} after", why)
                    break
            except Exception as e:
                log(f"brain fallback {name} failed", repr(e))
        else:
            note_brain_down(why)
            # brain_down is not "this message is hard", it is "we have no brain".
            # main() must leave the mail untouched so it is answered for real
            # once a provider comes back, instead of being marked handled.
            return {"action": "escalate", "reason": why, "brain_down": True}
    if not raw:
        return {"action": "escalate", "reason": why or "empty brain reply"}
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return {"action": "escalate", "reason": "no json from brain"}
    try:
        j = json.loads(m.group(0))
    except Exception:
        return {"action": "escalate", "reason": "unparseable brain json"}
    if j.get("action") != "draft" or not j.get("reply"):
        return {"action": "escalate", "reason": j.get("reason", "low confidence")}
    j["reply"] = clean_reply(j["reply"])
    # A two-word answer reads as a brush-off. Better a human writes it.
    if len(j["reply"]) < 60:
        return {"action": "escalate", "reason": "reply too curt"}
    return j

# ---- send (SMTP) + archive a copy to Sent ----
# Gmail folds long headers across lines. A thread with a dozen messages has a
# References header containing real CRLFs, and EmailMessage refuses those:
# "Header values may not contain linefeed or carriage return characters". The
# live run on 2026-08-27 failed exactly this way on the ONE customer it wanted
# to answer - and the failure mode is cruel, because the longest References
# header belongs to the customer who has written the most times.
def hdr(value):
    """Flatten any header value to a single line."""
    return re.sub(r"\s+", " ", (value or "")).strip()


def send_reply(to_addr, to_name, subject, body, in_reply_to, references):
    em = EmailMessage()
    em["From"] = f"{STORE_NAME} <{USER}>"
    em["To"] = f"{to_name} <{to_addr}>" if to_name else to_addr
    subject = hdr(subject)
    em["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    em["Date"] = formatdate(localtime=True)
    em["Message-ID"] = make_msgid(domain=USER.split("@")[-1])
    # Machine-readable provenance. learn.py treats Sent Mail as the human gold
    # standard; without this stamp it would distil the bot's own replies back
    # into the few-shot bank and slowly drift away from how Itay actually writes.
    em[BOT_HEADER] = "cloud-worker"
    if in_reply_to:
        em["In-Reply-To"] = hdr(in_reply_to)
        em["References"] = hdr(f"{references} {in_reply_to}") if references else hdr(in_reply_to)
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
    sent = escalated = skipped = brain_down = 0
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

        orders = find_orders(sender_email, body)
        res = ask_brain(sender_name, sender_email, subject, body, orders)
        if res.get("brain_down"):
            # No provider answered. Leave the mail UNREAD and UNLABELLED: the
            # next run with a working brain will pick it up and answer it. This
            # is the invariant that 2026-09-02 broke - back then every provider
            # failure still marked the customer as handled, so two weeks of mail
            # was burned and only Itay's Telegram knew about it.
            brain_down += 1
            log(f"BRAIN-DOWN leaving untouched: {sender_email} | {subject[:40]}")
            continue
        conf = float(res.get("confidence") or 0)
        sensitive = bool(SENSITIVE.search(subject + " " + body))
        # Read the reply we are about to send, not only the mail we received.
        promised = outgoing_violation(res.get("reply", ""))
        # bool(orders) is Shopify's answer, not res["order_found"] — the model
        # reports what it decided to believe, and on 2026-08-26 it believed a
        # non-customer had an order in transit.
        claimed = order_claim_violation(res.get("reply", ""), bool(orders))
        can_send = (res.get("action") == "draft" and conf >= MIN_CONF
                    and not sensitive and not promised and not claimed
                    and ENABLE_SEND)
        if claimed:
            kind, phrase = claimed
            why = ("הבוט טען שביצע פעולה שאין לו בכלל הרשאה לבצע (הטוקן קריאה בלבד)"
                   if kind == "action-done" else
                   "הבוט דיבר על מצב ההזמנה, אבל לא נמצאה שום הזמנה של הלקוח הזה ב-Shopify")
            log(f"BLOCKED-CLAIM {sender_email} | {kind} | \"{phrase}\" | orders={len(orders)}")
            mail_owner(
                f"🚩 {STORE_NAME}: הבוט ניסה לטעון \"{phrase}\" — נחסם",
                f"סוג החסימה: {kind}\n{why}\n\n"
                f"לקוח: {sender_name} <{sender_email}>\n"
                f"נושא: {subject}\n"
                f"הזמנות שנמצאו ב-Shopify: {len(orders)}\n\n"
                f"--- פניית הלקוח ---\n{body[:1200]}\n\n"
                f"--- מה שהבוט רצה לשלוח (לא נשלח, יושב כטיוטה) ---\n{res.get('reply','')}\n")
        if promised:
            log(f"BLOCKED-PROMISE {sender_email} | \"{promised}\" | conf={conf}")
            mail_owner(
                f"🚩 {STORE_NAME}: הבוט ניסה להציע ללקוח \"{promised}\" — נחסם",
                f"לקוח: {sender_name} <{sender_email}>\n"
                f"נושא: {subject}\n"
                f"הביטוי שנחסם: {promised}\n\n"
                f"--- פניית הלקוח ---\n{body[:1200]}\n\n"
                f"--- מה שהבוט רצה לשלוח (לא נשלח, יושב כטיוטה) ---\n{res.get('reply','')}\n")

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
                em["Subject"] = hdr(subject) if subject.lower().startswith("re:") else f"Re: {hdr(subject)}"
                em[BOT_HEADER] = "cloud-worker-draft"
                if res.get("reply"):
                    em.set_content(res["reply"])
                else:
                    em.set_content("(להשלמה ידנית)")
                append_draft(M, em)
            except Exception:
                pass
            escalated += 1
            why = (f"🚩 הבוט ניסה להבטיח \"{promised}\" — נחסם" if promised
                   else "רגיש" if sensitive else res.get("reason", f"conf={conf}"))
            log(f"ESCAL {sender_email} | {subject[:40]} | {why}")
            tg(f"📥 {STORE_NAME} — פנייה הושארה לך (לא נשלח, טיוטה ב-Gmail)\n"
               f"מ: {sender_name} <{sender_email}>\n"
               f"נושא: {subject}\n"
               f"סיבה: {why}"
               + (f"\n\nטיוטה מוצעת:\n{res['reply']}" if res.get("reply") else ""))
        mark_done(M, num)   # durable: never re-bill the brain on this message again
    M.logout()
    if brain_down:
        alert_brain_down(brain_down)
    log(f"DONE sent={sent} escalated={escalated} skipped={skipped} brain_down={brain_down}")

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
