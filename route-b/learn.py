#!/usr/bin/env python3
# Route B LEARNER — makes the CS bot more human + accurate over time.
# Reads REAL replies that were actually sent from the store mailbox (the human
# gold standard) over the last N days, distills reusable tone patterns, FAQ
# facts, and few-shot Q->A examples via the Anthropic API, and appends them to
# kb/learned-examples.md. The cloud_worker injects that file into its prompt, so
# every future answer imitates how a real human answered before.
#
# Runs headless in GitHub Actions (own cron). Commits the updated learned file
# back to the repo (durable memory across runs). Authorized owner automation on
# the store's own mailbox.

import imaplib, email, json, os, re, time, hashlib
import urllib.request
from email.header import decode_header, make_header
from datetime import datetime, timedelta

def env(k, d=""):
    return (os.environ.get(k, d) or "").strip()

STORE = env("STORE", "station").lower()
USER = env("STORE_GMAIL_USER")
APP_PW = env("STORE_GMAIL_APP_PASSWORD")
if STORE == "studio":
    STORE_NAME = "SneakerStudio"
    USER = env("STUDIO_GMAIL_USER", USER)
    APP_PW = env("STUDIO_GMAIL_APP_PASSWORD", APP_PW)
else:
    STORE_NAME = "SneakerStation"

ANTHROPIC_API_KEY = env("ANTHROPIC_API_KEY")
MODEL = env("ANTHROPIC_MODEL", "claude-sonnet-4-5")
LOOKBACK_DAYS = int(env("LEARN_LOOKBACK_DAYS", "14"))
MAX_PAIRS = int(env("LEARN_MAX_PAIRS", "40"))      # how many recent Q/A pairs to feed
MAX_EXAMPLES = int(env("LEARN_MAX_EXAMPLES", "50"))  # cap stored few-shot bank
TG_TOKEN = env("TELEGRAM_BOT_TOKEN")
TG_CHAT = env("TELEGRAM_CHAT_ID")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LEARNED_PATH = os.path.join(ROOT, "kb", f"learned-{STORE}.md")
SENT_BOX = "[Gmail]/Sent Mail"

def log(*a):
    print(time.strftime("%Y-%m-%dT%H:%M:%S"), "learn", STORE, *a, flush=True)

def tg(msg):
    if not TG_TOKEN or not TG_CHAT:
        return
    try:
        import urllib.parse
        data = urllib.parse.urlencode({"chat_id": TG_CHAT, "text": msg}).encode()
        urllib.request.urlopen(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data=data, timeout=15)
    except Exception:
        pass

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

QUOTE_MARK = re.compile(r"^\s*(On .+wrote:|בתאריך .+(כתב|מאת)|.*<[^>]+@[^>]+>.*כתב)")

def split_reply(body):
    """Top part = the human's answer; quoted part = the customer's question."""
    answer, quoted, in_quote = [], [], False
    for ln in body.splitlines():
        if not in_quote and QUOTE_MARK.match(ln):
            in_quote = True
            continue
        if in_quote:
            quoted.append(re.sub(r"^\s*>+\s?", "", ln))
        else:
            if ln.strip().startswith(">"):
                in_quote = True
                quoted.append(re.sub(r"^\s*>+\s?", "", ln))
            else:
                answer.append(ln)
    return "\n".join(answer).strip(), "\n".join(quoted).strip()

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

def collect_pairs():
    """Pull recent SENT replies; each gives (question_from_quote, human_answer)."""
    M = imap_connect()
    M.select(f'"{SENT_BOX}"', readonly=True)
    since = (datetime.utcnow() - timedelta(days=LOOKBACK_DAYS)).strftime("%d-%b-%Y")
    typ, data = M.search(None, f'(SINCE {since})')
    ids = data[0].split() if data and data[0] else []
    log(f"sent in last {LOOKBACK_DAYS}d = {len(ids)}")
    pairs = []
    for num in reversed(ids):
        if len(pairs) >= MAX_PAIRS:
            break
        typ, md = M.fetch(num, "(BODY.PEEK[])")
        if typ != "OK" or not md or not md[0]:
            continue
        msg = email.message_from_bytes(md[0][1])
        subject = dh(msg.get("Subject", ""))
        answer, question = split_reply(get_body(msg))
        # skip empties, pure auto-notifications, and tiny one-liners
        if not answer or len(answer) < 25:
            continue
        if not question:
            question = subject
        pairs.append({
            "subject": subject,
            "question": question[:900],
            "answer": answer[:900],
        })
    M.logout()
    return pairs

DISTILL_PROMPT = """אתה מאמן את צוות שירות הלקוחות של חנות הסניקרס {store}.
לפניך זוגות אמיתיים של פנייה מלקוח -> תשובה שנשלחה בפועל על ידי נציג אנושי.
המטרה: לזקק מתוכם הנחיות שיגרמו לבוט שירות הלקוחות לענות בדיוק כמו האנשים האלה — אנושי, חם, מדויק, שירותי.

נתח את הזוגות והפק שלושה דברים:

1) דפוסי סגנון וטון (3-7 כללים קצרים): איך הנציג פותח, איך מנסח, אורך, מילים אופייניות, איך חותם, מה הוא נמנע ממנו. כללים פרקטיים שאפשר לחקות.

2) עובדות / מדיניות / FAQ חדשים שעולים מהתשובות (זמני משלוח, החלפות, מידות, תשלום, וכו') — רק עובדות שנאמרו בפועל בתשובות. אם אין חדש, כתוב "אין".

3) 3-6 דוגמאות few-shot מנצחות: בחר את הזוגות הכי טובים (שאלה -> תשובה), נקה פרטים אישיים מזהים (שמות מלאים, אימיילים, מספרי הזמנה ספציפיים -> החלף ב[שם]/[מספר]), והבא אותם כתבנית לחיקוי.

החזר Markdown נקי בלבד במבנה הזה בדיוק:

## דפוסי טון
- ...

## עובדות ל-KB
- ...

## דוגמאות לחיקוי
### דוגמה
פנייה: ...
תשובה: ...

זוגות אמיתיים:
{pairs}
"""

def distill(pairs):
    blob = "\n\n".join(
        f"[{i+1}] נושא: {p['subject']}\nפנייה: {p['question']}\nתשובה: {p['answer']}"
        for i, p in enumerate(pairs))
    prompt = DISTILL_PROMPT.format(store=STORE_NAME, pairs=blob)
    payload = json.dumps({
        "model": MODEL, "max_tokens": 2000,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=payload, method="POST",
        headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as r:
        resp = json.load(r)
    return "".join(b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text").strip()

def trim_examples(text):
    """Keep the most recent MAX_EXAMPLES '### דוגמה' blocks to bound file size."""
    blocks = text.split("### דוגמה")
    head = blocks[0]
    examples = blocks[1:]
    if len(examples) > MAX_EXAMPLES:
        examples = examples[-MAX_EXAMPLES:]
    return head + "".join("### דוגמה" + b for b in examples)

def main():
    if not ANTHROPIC_API_KEY:
        log("FATAL no ANTHROPIC_API_KEY"); print("DONE learned=0"); return
    if not APP_PW:
        log("no mailbox creds for this store"); print("DONE learned=0"); return
    pairs = collect_pairs()
    if len(pairs) < 3:
        log(f"only {len(pairs)} pairs — too few to learn"); print("DONE learned=0"); return
    insight = distill(pairs)
    if not insight:
        log("empty distill"); print("DONE learned=0"); return

    stamp = datetime.utcnow().strftime("%Y-%m-%d")
    header = (f"# דוגמאות נלמדות — {STORE_NAME}\n\n"
              f"> נוצר אוטומטית מ-{len(pairs)} תשובות אמיתיות אחרונות. הבוט מחקה את הסגנון והעובדות כאן.\n")
    prev = ""
    if os.path.exists(LEARNED_PATH):
        prev = open(LEARNED_PATH, encoding="utf-8").read()
        prev = re.sub(r"^# דוגמאות נלמדות.*?\n", "", prev, flags=re.S | re.M) if prev.startswith("#") else prev
    body = f"{header}\n## עדכון {stamp} ({len(pairs)} תשובות)\n\n{insight}\n\n{prev}".strip()
    body = trim_examples(body)
    with open(LEARNED_PATH, "w", encoding="utf-8") as f:
        f.write(body + "\n")
    log(f"wrote {LEARNED_PATH} ({len(body)} chars)")
    tg(f"🧠 {STORE_NAME}: למדתי מ-{len(pairs)} תשובות אמיתיות אחרונות. בנק הדוגמאות עודכן — הבוט יענה יותר אנושי.")
    print(f"DONE learned={len(pairs)}")

if __name__ == "__main__":
    main()
