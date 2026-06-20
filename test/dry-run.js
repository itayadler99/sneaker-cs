// Offline proof: engine routing + order formatting, no API keys needed.
const { generateReply } = require("../src/reply-engine");

const mockOrder = [{
  name: "#1042", email: "dana@example.com",
  displayFinancialStatus: "PAID", displayFulfillmentStatus: "FULFILLED", createdAt: "2026-06-10",
  fulfillments: [{ status: "SUCCESS", trackingInfo: [{ company: "Israel Post", number: "RR123456789IL", url: "https://track..." }] }],
  lineItems: { edges: [{ node: { title: "Nike Air Force 1", variantTitle: "42", quantity: 1 } }] },
}];

(async () => {
  if (!process.env.ANTHROPIC_API_KEY) {
    console.log("NO ANTHROPIC_API_KEY — skipping live model call. Modules load OK:",
      typeof generateReply === "function");
    return;
  }
  const r = await generateReply({
    storeName: "Sneaker Station",
    message: "נושא: איפה ההזמנה שלי?\n\nהיי, הזמנתי לפני שבוע (#1042) ועוד לא קיבלתי. מתי זה מגיע?",
    orders: mockOrder,
  });
  console.log(JSON.stringify(r, null, 2));
})();
