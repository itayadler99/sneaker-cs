// Gmail adapter for the store support inbox (sneakerstationisrael@gmail.com).
// Reads unread support threads, writes DRAFT replies (never auto-sends in v1).
// Auth: OAuth refresh token for the STORE account (separate from itay's personal MCP gmail).
const { google } = require("googleapis");

function client() {
  const o = new google.auth.OAuth2(process.env.GMAIL_CLIENT_ID, process.env.GMAIL_CLIENT_SECRET);
  o.setCredentials({ refresh_token: process.env.STORE_GMAIL_REFRESH_TOKEN });
  return google.gmail({ version: "v1", auth: o });
}

async function listUnreadSupport(maxResults = 20) {
  const gmail = client();
  const r = await gmail.users.messages.list({ userId: "me", q: "is:unread -category:promotions -in:sent", maxResults });
  return r.data.messages || [];
}

async function getMessage(id) {
  const gmail = client();
  const r = await gmail.users.messages.get({ userId: "me", id, format: "full" });
  return r.data;
}

// Create a draft reply in the same thread. v1 = draft only; Itay clicks send.
async function createDraftReply({ threadId, to, subject, body }) {
  const gmail = client();
  const raw = Buffer.from(
    `To: ${to}\r\nSubject: ${subject}\r\nContent-Type: text/plain; charset=UTF-8\r\n\r\n${body}`
  ).toString("base64url");
  return gmail.users.drafts.create({ userId: "me", requestBody: { message: { threadId, raw } } });
}

module.exports = { listUnreadSupport, getMessage, createDraftReply };
