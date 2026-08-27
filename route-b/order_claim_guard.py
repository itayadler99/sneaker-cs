#!/usr/bin/env python3
"""Blocks two kinds of sentence the bot has no standing to write.

Why this file exists (2026-08-26 incident, two separate customers):

1. STATUS CLAIM WITHOUT AN ORDER.
   לאה שירזי wrote "הזמנה שלא הגיעה" twice. catchup.py fired its canned
   template at her: "ההזמנה שלך בדרך אליך". She has no order. Not under that
   email, not under that name, not in either store, not in customers/search and
   not in a GraphQL orders(query:) sweep. The template never asked Shopify
   anything - it just reassured her. She is now three weeks into chasing a
   parcel that was never ours to ship.

2. ACTION CLAIMED AS DONE.
   Order #2571 asked to update a phone number. The bot replied "עדכנתי את
   המספר". Nothing was updated: this bot has read-only Shopify credentials and
   no write path at all. Itay updated it by hand two days later.

promise_guard.py reads what the bot OFFERS (money, cancellations). This file
reads what the bot ASSERTS: that an order exists and is moving, and that the
bot itself already changed something. Both are checkable facts, so both are
checked here rather than left to the model's judgement.

Contract: every outbound path calls violation(text, orders_found) and refuses to
send on a hit, exactly as it already does for promise_guard.
"""
import re

# Sentences that only make sense if a real order was pulled from Shopify.
# Deliberately about the ORDER, not about the store: "אנחנו כאן לכל שאלה" and
# "תודה על הפנייה" must stay sendable to anyone.
ORDER_STATUS_CLAIM = re.compile(
    # The optional #1234 matters: the model habitually names the order it is
    # talking about, and a gap of "#1205 " was enough to walk straight past an
    # earlier version of this pattern.
    r"(ההזמנה\s*(שלך|שלכם)?\s*(#\s*\d{2,6}\s*)?(כבר\s*)?(בדרך|יצאה|נשלחה|בטיפול|בתהליך|מוכנה|הגיעה|תגיע)|"
    r"החבילה\s*(שלך)?\s*(#\s*\d{2,6}\s*)?(כבר\s*)?(בדרך|יצאה|נשלחה|הגיעה|תגיע)|"
    r"המשלוח\s*(שלך)?\s*(#\s*\d{2,6}\s*)?(כבר\s*)?(בדרך|יצא|נשלח|יגיע)|"
    r"הזמנתך\s*(בדרך|נשלחה|בטיפול)|"
    r"אנחנו\s*מכינים\s*את\s*ההזמנה|בהכנה\s*אצלנו|נארזת|"
    r"(חברת\s*)?השילוח\s*(תגיע|יצרה|ייצור|תיצור)|"
    r"נציג\s*(ייצור|יצור|יתקשר)\s*(איתך)?\s*קשר|"
    r"your\s+order\s+(is|has)\s+(on\s+its\s+way|been\s+shipped|shipped|ready)|"
    r"the\s+(parcel|package|courier)\s+is\s+on\s+its\s+way)",
    re.I)

# Sentences claiming the bot already changed something in Shopify or with the
# courier. There is no write path. These are false whenever they are sent, so
# orders_found does not rescue them.
ACTION_DONE_CLAIM = re.compile(
    r"(עדכנתי|עדכנו|עודכן\s*(במערכת|אצלנו|בהזמנה)|שינינו|שיניתי|החלפנו\s*את\s*ה(כתובת|מספר|מידה)|"
    r"הזנתי|רשמתי\s*אצלנו|הוספתי\s*להזמנה|"
    r"הוצאנו\s*(עבורך|לך)\s*הזמנה|פתחנו\s*(לך|עבורך)\s*הזמנה|יצרנו\s*(לך|עבורך)\s*הזמנה|"
    r"דיברנו\s*עם\s*(חברת\s*)?השילוח|יצרנו\s*קשר\s*עם\s*(חברת\s*)?השילוח|אישרנו\s*ל(הם|חברת)|"
    r"ביקשנו\s*מהשליח|הזזנו\s*את|קידמנו\s*את\s*ה(הזמנה|משלוח)|"
    r"(i|we)\s+(have\s+)?(updated|changed|amended|corrected)\s+(your|the)\s+"
    r"(order|address|phone|number|details)|"
    r"(we\s+)?(have\s+)?contacted\s+the\s+(courier|carrier|shipping\s+company))",
    re.I)


def violation(text, orders_found):
    """Return (kind, phrase) for a sentence the bot may not send, else None.

    kind is "action-done" or "status-no-order", so the caller can word the
    escalation mail correctly - they are different failures with different
    fixes, and lumping them together is how the phone-number case stayed
    invisible for two days.

    orders_found must be the truth from Shopify for THIS customer, not the
    model's own order_found field. The model reports whatever it decided to
    believe; only the API knows.
    """
    text = text or ""
    m = ACTION_DONE_CLAIM.search(text)
    if m:
        return ("action-done", m.group(0))
    if not orders_found:
        m = ORDER_STATUS_CLAIM.search(text)
        if m:
            return ("status-no-order", m.group(0))
    return None
