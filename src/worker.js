// Orchestrator: pull unread support email -> resolve order -> draft accurate reply.
// Draft-only. Escalations are left unread for Itay. Run per store.
const gmail = require("./channels/gmail");
const { lookupByEmail, lookupByOrderName } = require("./shopify");
const { generateReply } = require("./reply-engine");

const STORE = process.env.STORE || "station"; // station | studio
const STORE_NAME = STORE === "studio" ? "Sneaker Studio" : "Sneaker Station";

function headers(msg) {
  const h = {};
  for (const x of msg.payload?.headers || []) h[x.name.toLowerCase()] = x.value;
  return h;
}
function decodeBody(payload) {
  const part = (payload.parts || []).find((p) => p.mimeType === "text/plain") || payload;
  const data = part.body?.data;
  return data ? Buffer.from(data, "base64").toString("utf8") : payload.snippet || "";
}
function extractOrderNo(text) {
  const m = text.match(/#?(\d{3,})/);
  return m ? m[1] : null;
}

async function run() {
  const ids = await gmail.listUnreadSupport(20);
  let drafted = 0, escalated = 0;
  for (const { id } of ids) {
    const msg = await gmail.getMessage(id);
    const h = headers(msg);
    const from = (h.from || "").match(/<(.+?)>/)?.[1] || h.from;
    const body = decodeBody(msg.payload);

    const orderNo = extractOrderNo(h.subject + " " + body);
    let orders = orderNo ? await lookupByOrderName(STORE, orderNo) : [];
    if (!orders.length && from) orders = await lookupByEmail(STORE, from);

    const result = await generateReply({ storeName: STORE_NAME, message: `נושא: ${h.subject}\n\n${body}`, orders });

    if (result.action === "draft") {
      await gmail.createDraftReply({
        threadId: msg.threadId,
        to: from,
        subject: h.subject?.startsWith("Re:") ? h.subject : `Re: ${h.subject}`,
        body: result.reply,
      });
      drafted++;
    } else {
      escalated++; // left unread for Itay
    }
  }
  console.log(JSON.stringify({ store: STORE, processed: ids.length, drafted, escalated }));
}

if (require.main === module) run().catch((e) => { console.error(e); process.exit(1); });
module.exports = { run };
