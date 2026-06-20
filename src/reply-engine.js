// Order-aware reply engine. Draft-only by default. Never invents facts.
const fs = require("fs");
const path = require("path");
const Anthropic = require("@anthropic-ai/sdk");

const KB = fs.readFileSync(path.join(__dirname, "..", "kb", "knowledge-base.md"), "utf8");
const client = new Anthropic({ apiKey: process.env.ANTHROPIC_API_KEY });

const ESCALATE = "ESCALATE";

const SYSTEM = `אתה נציג שירות של חנות סניקרס ישראלית. עברית בלבד, חם, קצר, ענייני, בלי סופרלטיבים.

חוקי ברזל:
1. ענה רק על בסיס (א) הידע שמצורף, (ב) נתוני ההזמנה החיים שמצורפים. אם אין לך עובדה ודאית — אל תמציא.
2. אם הפנייה היא תלונה/נזק/החזר כספי, או שאתה לא בטוח, או שאין נתוני הזמנה ונדרשים — החזר בדיוק את המילה ${ESCALATE} ותו לא.
3. אל תבטיח תאריכים/מחירים/מדיניות שלא מופיעים בנתונים.
4. החזר JSON: {"action":"draft"|"escalate","reply":"...","confidence":0-1,"reason":"..."}`;

function formatOrder(orders) {
  if (!orders?.length) return "לא נמצאה הזמנה תואמת.";
  return orders
    .map((o) => {
      const items = (o.lineItems?.edges || [])
        .map((e) => `${e.node.title}${e.node.variantTitle ? ` (${e.node.variantTitle})` : ""} x${e.node.quantity}`)
        .join(", ");
      const track = (o.fulfillments || [])
        .flatMap((f) => f.trackingInfo || [])
        .map((t) => `${t.company || ""} ${t.number || ""} ${t.url || ""}`.trim())
        .join(" | ") || "אין מספר מעקב עדיין";
      return `הזמנה ${o.name} | תשלום:${o.displayFinancialStatus} | שילוח:${o.displayFulfillmentStatus} | פריטים:${items} | מעקב:${track} | תאריך:${o.createdAt}`;
    })
    .join("\n");
}

// message: customer text. orders: array from shopify lookup (may be []).
async function generateReply({ storeName, message, orders }) {
  const user = `חנות: ${storeName}

=== ידע ===
${KB}

=== נתוני הזמנה חיים ===
${formatOrder(orders)}

=== פניית הלקוח ===
${message}`;

  const resp = await client.messages.create({
    model: "claude-sonnet-4-6",
    max_tokens: 800,
    system: SYSTEM,
    messages: [{ role: "user", content: user }],
  });
  const text = resp.content[0]?.text?.trim() || "";
  try {
    const json = JSON.parse(text);
    if (json.action === "escalate" || !json.reply) return { action: "escalate", reason: json.reason || "low confidence" };
    return json;
  } catch {
    return { action: "escalate", reason: "unparseable model output" };
  }
}

module.exports = { generateReply };
