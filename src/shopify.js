// Shopify order lookup — the missing piece that makes replies accurate.
// Resolves a customer email or order number to live order + fulfillment/tracking.

const STORES = {
  station: {
    domain: process.env.STATION_SHOP_DOMAIN, // e.g. xxx.myshopify.com
    token: process.env.STATION_ADMIN_TOKEN,  // shpat_...
    name: "Sneaker Station",
  },
  studio: {
    domain: process.env.STUDIO_SHOP_DOMAIN,
    token: process.env.STUDIO_ADMIN_TOKEN,
    name: "Sneaker Studio",
  },
};

const API_VERSION = "2025-07";

async function adminGraphQL(store, query, variables) {
  const s = STORES[store];
  if (!s?.domain || !s?.token) throw new Error(`store ${store} not configured`);
  const res = await fetch(`https://${s.domain}/admin/api/${API_VERSION}/graphql.json`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Shopify-Access-Token": s.token },
    body: JSON.stringify({ query, variables }),
  });
  if (!res.ok) throw new Error(`shopify ${store} ${res.status}`);
  return res.json();
}

const ORDER_FIELDS = `
  name
  email
  displayFulfillmentStatus
  displayFinancialStatus
  createdAt
  shippingAddress { address1 city zip country }
  fulfillments {
    status
    trackingInfo { number url company }
    estimatedDeliveryAt
  }
  lineItems(first: 20) { edges { node { title quantity variantTitle } } }
`;

// Find the most relevant order for a customer email (latest), with live tracking.
async function lookupByEmail(store, email) {
  const q = `query($q:String!){ orders(first:3, query:$q, sortKey:CREATED_AT, reverse:true){ edges { node { ${ORDER_FIELDS} } } } }`;
  const data = await adminGraphQL(store, q, { q: `email:${email}` });
  return (data?.data?.orders?.edges || []).map((e) => e.node);
}

async function lookupByOrderName(store, name) {
  const clean = String(name).replace(/^#/, "");
  const q = `query($q:String!){ orders(first:1, query:$q){ edges { node { ${ORDER_FIELDS} } } } }`;
  const data = await adminGraphQL(store, q, { q: `name:${clean}` });
  return (data?.data?.orders?.edges || []).map((e) => e.node);
}

module.exports = { STORES, lookupByEmail, lookupByOrderName };
