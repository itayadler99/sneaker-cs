"""Itay's customer-facing voice, shared by cloud_worker.py and catchup.py.

Owner instruction 2026-09-26: a customer asking where their order is gets
reassured in Itay's own words - no identification needed, nothing invented.
The template is his own sent reply from that day."""
from datetime import datetime

try:
    from zoneinfo import ZoneInfo
    _IL = ZoneInfo("Asia/Jerusalem")
except Exception:
    _IL = None


def greeting(now=None):
    """The opener Itay uses, picked by Israel time: שבת שלום / שבוע טוב /
    בוקר טוב / צהריים טובים / ערב טוב."""
    now = now or datetime.now(_IL)
    wd, h = now.weekday(), now.hour          # Mon=0 ... Fri=4, Sat=5, Sun=6
    if (wd == 4 and h >= 12) or (wd == 5 and h < 18):
        return "היי, שבת שלום"
    if wd == 6 and h < 12:
        return "היי, שבוע טוב"
    if 5 <= h < 12:
        return "היי, בוקר טוב"
    if 12 <= h < 17:
        return "היי, צהריים טובים"
    return "היי, ערב טוב"


SHIPPING_TEMPLATE = (
    "{greeting} 😊\n"
    "כפי שמצוין באתר, זמני המשלוח נעים בין 7-18 ימי עסקים, בתוספת של עד 4 ימי "
    "עסקים עבור עיבוד והכנת ההזמנה.\n\n"
    "הכול מתנהל בהתאם לטווחי הזמנים, וכרגע נותר רק להמתין לעדכון מחברת השילוח "
    "לצורך תיאום המסירה. ברגע שהמשלוח יתקרב, חברת השילוח תיצור קשר ישירות.\n\n"
    "{closing}\n{store}"
)


def closing(now=None):
    now = now or datetime.now(_IL)
    wd, h = now.weekday(), now.hour
    if wd == 4:
        return "סוף שבוע נעים!"
    if wd == 5 and h < 18:
        return "המשך שבת נעימה!"
    if wd == 5:
        return "שיהיה שבוע טוב!"
    if wd == 3:
        return "סוף שבוע נעים!"
    return "יום נעים!"


def shipping_template(store, now=None):
    return SHIPPING_TEMPLATE.format(greeting=greeting(now), closing=closing(now),
                                    store=store)


