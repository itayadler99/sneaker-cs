# -*- coding: utf-8 -*-
"""The bot never answers whether the goods are original — in either direction.

Owner instruction, 2026-09-10, verbatim:
    "אסור להגיד 'מוצרים מקוריים' בלבד. לא לנקוב ולא להתקרב לנושא הזה בכלל.
     אם אנשים שואלים, אתה מעביר לי את זה, אבל אתה לא אומר בחיים 'מוצרים מקוריים', בחיים."

The real sentence below was sitting 26 times inside kb/learned-studio.md as a
few-shot example the prompt told the model to imitate, and kb/learned-station.md
line 418 is a customer threatening to sue over having been told exactly that.
"""
import os, re, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from promise_guard import violation, authenticity_violation

REAL_STUDIO_REPLY = """היי,

כן, כל הנעליים אצלנו במלאי הן מקוריות 100%.

אנחנו כאן לכל שאלה 😊

Sneaker Studio
"""

MUST_BLOCK = [
    REAL_STUDIO_REPLY,
    "הנעליים אצלנו מקוריות",
    "המוצרים אינם מקוריים של מותגים",
    "זה לא זיוף",
    "אלה לא חיקויים",
    "All our shoes are 100% original",
    "these are authentic",
]

MUST_PASS = [
    "היי דנה,\n\nההזמנה יצאה ותגיע תוך 7-21 ימי עסקים.\n\nSneakerStation\n",
    "היי,\n\nהמידה שהזמנת היא 42. נעדכן ברגע שיש מספר מעקב.\n\nSneakerStation\n",
]

def main():
    bad = 0
    for t in MUST_BLOCK:
        hit = violation(t)
        if not hit:
            print("FAIL not blocked:", t[:60].replace("\n", " ")); bad += 1
    for t in MUST_PASS:
        hit = violation(t)
        if hit:
            print("FAIL wrongly blocked:", repr(hit), "|", t[:60].replace("\n", " ")); bad += 1
    # the learned banks must never carry the answer again
    kb = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "kb")
    for name in ("learned-studio.md", "learned-station.md"):
        p = os.path.join(kb, name)
        if not os.path.exists(p):
            continue
        body = open(p, encoding="utf-8").read()
        body = re.sub(r"^> ⛔.*$", "", body, flags=re.M)   # our own removal marker
        body = re.sub(r"^## דוגמאות לחיקוי\s*$", "", body, flags=re.M)  # section header
        for m in re.finditer(r"(מקוריות\s*100%|הן מקוריות|100%\s*original)", body, re.I):
            print("FAIL learned bank still teaches it:", name, m.group(0)); bad += 1
    print("FAILURES:", bad)
    sys.exit(1 if bad else 0)

if __name__ == "__main__":
    main()
