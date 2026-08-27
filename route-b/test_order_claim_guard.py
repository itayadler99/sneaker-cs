import os
import sys; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from order_claim_guard import violation as v

REAL_LEAH = """היי לאה,

תודה על הפנייה.

ההזמנה שלך בדרך אליך.

ברגע שחברת השילוח תגיע לאזור שלך, נציג ייצור איתך קשר טלפוני לתיאום מסירה.

אם יש עוד משהו, אנחנו כאן.

SneakerStudio
"""
REAL_2571 = """היי,

עדכנתי את המספר. לגבי התאריך, קשה לי להבטיח כי ההזמנה עדיין בשלב העיבוד ולא יצאה עדיין לדרך.

Sneaker Station
"""
REAL_YAFA = "הוצאנו עבורך הזמנה חדשה אל דאגה"
REAL_NORA = "אישרנו להם"

OK_GENERIC = """היי דנה,

תודה על הפנייה ומצטערים על ההמתנה.

זמני המשלוח אצלנו נעים בין 7 ל-18 ימי עסקים, בנוסף לעד 4 ימי עסקים לעיבוד ההכנה.

אנחנו כאן לכל שאלה.

SneakerStudio
"""
OK_WITH_ORDER = """היי מיכל,

ההזמנה שלך #1205 כבר בתהליך הכנה.

נעדכן ברגע שהיא תישלח.

SneakerStudio
"""
NOT_A_CUSTOMER = """היי לאה,

בדקנו לפי השם והמייל שלך ולא מצאנו אצלנו שום הזמנה.

כדאי לפנות לחנות שממנה רכשת עם מספר ההזמנה שבאישור.

SneakerStudio
"""

cases = [
    ("catchup template -> non-customer", REAL_LEAH, False, "status-no-order"),
    ("catchup template -> real customer", REAL_LEAH, True,  None),
    ("#2571 'עדכנתי את המספר'",           REAL_2571, True,  "action-done"),
    ("יפה 'הוצאנו עבורך הזמנה חדשה'",     REAL_YAFA, True,  "action-done"),
    ("נורה 'אישרנו להם'",                  REAL_NORA, True,  "action-done"),
    ("generic shipping-times reply",       OK_GENERIC, False, None),
    ("status quoted WITH an order",        OK_WITH_ORDER, True, None),
    ("status quoted WITHOUT an order",     OK_WITH_ORDER, False, "status-no-order"),
    ("'לא מצאנו הזמנה' reply",             NOT_A_CUSTOMER, False, None),
]
bad = 0
for name, text, found, want in cases:
    got = v(text, found)
    kind = got[0] if got else None
    ok = kind == want
    bad += not ok
    print(("PASS " if ok else "FAIL ") + f"{name:38} orders={str(found):5} -> {kind!r}"
          + (f"  [{got[1]}]" if got else "") + ("" if ok else f"   WANTED {want!r}"))
print("\nfailures:", bad)
sys.exit(1 if bad else 0)
