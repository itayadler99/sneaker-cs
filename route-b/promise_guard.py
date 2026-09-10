#!/usr/bin/env python3
"""Single source of truth for what the bot may never promise a customer.

Why this file exists (2026-08-24 incident):
a customer asked a plain stock question — "do you still have a 42?" — which
contains no sensitive keyword, so the inbound SENSITIVE/HANDS_OFF filters let it
through. The model then volunteered, unprompted:

    "אם את מעדיפה לבטל את ההזמנה ולקבל החזר כספי מלא, אני לגמרי מבינה ואשמח לעזור בזה"

and it was sent to the customer. Itay never authorised the bot to offer money
back, cancellations, exchanges or compensation.

The inbound filters read the CUSTOMER. This one reads the BOT. Every outbound
path must run its text through violation() before sending. A hit is not a
warning: it blocks the send, leaves a draft, and mails Itay.
"""
import re

OUTGOING_BAN = re.compile(
    r"(החזר\s*כספי|החזר\s*מלא|להחזיר\s*לך\s*את\s*הכסף|נחזיר\s*לך|כסף\s*בחזרה|"
    r"זיכוי|לזכות\s*אותך|נזכה\s*אותך|"
    r"לבטל\s*את\s*ההזמנה|נבטל|מבטלים|ביטול\s*ההזמנה|ביטול\s*העסקה|נוכל\s*לבטל|אפשר\s*לבטל|"
    r"להחליף\s*(את\s*)?(ה)?(זוג|מוצר|מידה|הזמנה)|נחליף\s*לך|החלפה\s*ל|"
    r"פיצוי|לפצות|שובר|קופון|קוד\s*הנחה|הנחה\s*של|על\s*חשבוננו|"
    r"refund|chargeback|cancel\s*the\s*order|store\s*credit|compensat|voucher|coupon)",
    re.I)


# ---- AUTHENTICITY (added 2026-09-10, explicit owner instruction) ----
# Itay, verbatim: "אסור להגיד 'מוצרים מקוריים' בלבד. לא לנקוב ולא להתקרב לנושא הזה בכלל.
# אם אנשים שואלים, אתה מעביר לי את זה, אבל אתה לא אומר בחיים 'מוצרים מקוריים', בחיים."
#
# Why a hard gate and not a prompt line: kb/learned-studio.md carried 26 copies of the
# human reply "כן, כל הנעליים אצלנו במלאי הן מקוריות 100%", and the prompt tells the model
# to imitate the FACTS in those examples. kb/learned-station.md line 418 is a customer
# threatening to sue over exactly that sentence. A prompt instruction loses to 26 examples.
# This gate reads the BOT's outgoing text and blocks the send outright.
AUTHENTICITY_BAN = re.compile(
    r"(מקורי|מקוריים|מקורית|מקוריות|לא\s*מקורי|אורגינל|"
    r"זיוף|מזויף|מזויפות|חיקוי|העתק\s*(1:1|מדויק)|רפליק|"
    r"authentic|genuine|original\s*(product|shoes|pair)|100%\s*original|replica|counterfeit|fake)",
    re.I)


def authenticity_violation(text):
    """Return the authenticity wording the outgoing text uses, or None.

    The bot never answers whether the goods are original — in either direction.
    Any hit escalates to Itay instead of being sent.
    """
    m = AUTHENTICITY_BAN.search(text or "")
    return m.group(0) if m else None


def violation(text):
    """Return the banned phrase the outgoing text promises, or None.

    Money and order-lifecycle decisions belong to the owner, not the model.
    There is no confidence score high enough to bypass this.
    """
    m = OUTGOING_BAN.search(text or "")
    if m:
        return m.group(0)
    # Same gate, same consequence: block the send and hand it to Itay.
    return authenticity_violation(text)
