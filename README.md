# sneaker-cs

מוח שירות אחד לשתי חנויות הסניקרס (Sneaker Station + Studio), אימייל + פייסבוק/אינסטגרם.
מתקן את הבעיה של הבוט הישן: **תשובות מדויקות כי הוא שולף הזמנה חיה מ-Shopify לפני שהוא עונה**, ולא ממציא.

## איך זה עובד
פנייה נכנסת → שולף מספר הזמנה/אימייל → מושך מ-Shopify סטטוס+מעקב חי → מנוע כותב תשובה בעברית על בסיס ידע + הזמנה בלבד → **טיוטה** (v1). לא בטוח / החזר / תלונה → מועבר לאיתי, לא נשלח.

## הרצה
```
cp .env.example .env   # מלא לפי MISSING.md
npm install
STORE=station npm start
STORE=studio  npm start
npm run dry            # בדיקת מנוע offline
```

## מבנה
- `kb/knowledge-base.md` — מקור אמת לתשובות (מדיניות נשלפת מ-Shopify, לא מנוחשת)
- `src/shopify.js` — שליפת הזמנה לפי אימייל/מספר + מעקב
- `src/reply-engine.js` — מנוע תשובה order-aware, draft-only, escalation gate
- `src/channels/{gmail,facebook}.js` — אדפטרים לערוצים
- `src/worker.js` — תזמורת: inbox → order → draft

ראה `MISSING.md` לחיבורים שצריך פעם אחת.
