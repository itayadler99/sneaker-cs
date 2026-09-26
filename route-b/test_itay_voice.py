"""Owner instruction 2026-09-26: shipping questions get Itay's template, the bot
never asks the customer to identify themselves and never invents a reason."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("STORE", "station")
import cloud_worker as W
from order_claim_guard import violation

ASK = "היי, לא הצלחתי לאתר את ההזמנה שלך, אפשר לשלוח לי את מספר ההזמנה?"
INV = "היי, ההזמנה התעכבה כי צריך לאתר דגם מול הספקים, נחזור אליך."
GOOD = "היי, ההזמנה #1252 נשלחה, מעקב FedEx 123."

def run(reply, orders, body="איפה ההזמנה שלי?", action="draft"):
    return W.apply_itay_voice({"action": action, "reply": reply, "confidence": .9},
                              "", body, orders, False)

fresh = [{"displayFulfillmentStatus": "UNFULFILLED", "createdAt": W.datetime.utcnow().strftime("%Y-%m-%d")}]
stale = [{"displayFulfillmentStatus": "UNFULFILLED", "createdAt": "2026-01-01"}]

assert run(ASK, [])[1], "no order + asks for id -> template"
assert run("anything", [], action="escalate")[1], "no order escalation -> template"
assert run(INV, fresh)[1], "invented reason -> template"
assert not run(GOOD, fresh)[1], "clean order-aware reply is kept"
assert not run(ASK, stale)[1], "month-old unfulfilled order stays with Itay"
assert not run(ASK, [], body="איזו מידה לקחת?")[1], "not a shipping question"
assert not W.apply_itay_voice({"action": "draft", "reply": ASK}, "", "איפה ההזמנה", [], True)[1], "sensitive stays out"
t = run(ASK, [])[0]["reply"]
assert not W.ASKS_FOR_ID.search(t) and not W.INVENTED.search(t)
assert W.ASKS_FOR_ID.search(ASK) and W.INVENTED.search(INV)
print("ok")
