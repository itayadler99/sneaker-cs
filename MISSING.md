# מה חוסר כדי לעלות לאוויר — הדברים היחידים שרק איתי יכול לתת (פעם אחת)

המנוע בנוי ועובד (dry-run עובר). כל מה שלמטה זה חיבורים/מפתחות שלא קיימים במחשב ודורשים פעולה חד-פעמית. כל השאר עליי.

## 1. מפתח LLM — ANTHROPIC_API_KEY
console.anthropic.com → API keys → צור מפתח + הוסף billing. (זה מה שמייצר את התשובות.)

## 2. גישת Shopify Admin לכל חנות (לשליפת הזמנות/מעקב חי)
לכל חנות: Admin → Settings → Apps → Develop apps → Create app → Admin API scopes: read_orders, read_fulfillments, read_customers → Install → העתק `shpat_...` + הדומיין `xxx.myshopify.com`.
- STATION_SHOP_DOMAIN + STATION_ADMIN_TOKEN
- STUDIO_SHOP_DOMAIN + STUDIO_ADMIN_TOKEN

## 3. תיבת Gmail של החנות (נפרדת מהאישית של איתי)
OAuth חד-פעמי לחשבון sneakerstationisrael@ (ולסטודיו):
GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET, STORE_GMAIL_REFRESH_TOKEN.
חלופה אוטונומית (route A): הרצה דרך Chrome המחובר של איתי, בלי OAuth — נבחר אם זה עדיף.

## 4. עמוד פייסבוק/אינסטגרם — FB_PAGE_TOKEN
Page access token עם pages_messaging + pages_read_engagement (Graph API / Business).

---
ברגע שיש את אלה ב-.env → `npm start` (לכל חנות) מתחיל לייצר טיוטות תשובה מדויקות. שלב הבא: cron 24/7 + auto-send לקטגוריות בטוחות.
