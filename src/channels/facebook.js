// Facebook/Instagram Page DM adapter. Reads Page conversations, stages replies.
// Auth: Page access token with pages_messaging + pages_read_engagement.
// v1 = stage reply for Itay approval; v2 = auto-send safe categories.
const GRAPH = "https://graph.facebook.com/v21.0";

function tok() {
  const t = process.env.FB_PAGE_TOKEN;
  if (!t) throw new Error("FB_PAGE_TOKEN not set");
  return t;
}

async function listConversations(limit = 20) {
  const r = await fetch(`${GRAPH}/me/conversations?fields=participants,updated_time,snippet&limit=${limit}&access_token=${tok()}`);
  const j = await r.json();
  return j.data || [];
}

async function getMessages(conversationId) {
  const r = await fetch(`${GRAPH}/${conversationId}?fields=messages{message,from,created_time}&access_token=${tok()}`);
  return r.json();
}

async function sendMessage(recipientId, text) {
  const r = await fetch(`${GRAPH}/me/messages?access_token=${tok()}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ recipient: { id: recipientId }, message: { text }, messaging_type: "RESPONSE" }),
  });
  return r.json();
}

module.exports = { listConversations, getMessages, sendMessage };
