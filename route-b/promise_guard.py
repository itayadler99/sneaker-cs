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


def violation(text):
    """Return the banned phrase the outgoing text promises, or None.

    Money and order-lifecycle decisions belong to the owner, not the model.
    There is no confidence score high enough to bypass this.
    """
    m = OUTGOING_BAN.search(text or "")
    return m.group(0) if m else None
