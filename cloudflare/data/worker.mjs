const json = (body, status = 200) => Response.json(body, {
  status, headers: { "Cache-Control": "no-store" },
});
const sources = new Set(["radar", "light", "birdcast", "microphone", "lamp"]);

async function authorized(request, secret) {
  if (!secret) return false;
  const digest = async text => new Uint8Array(await crypto.subtle.digest(
    "SHA-256", new TextEncoder().encode(text)));
  const [a, b] = await Promise.all([
    digest(request.headers.get("Authorization") || ""), digest(`Bearer ${secret}`),
  ]);
  let difference = 0;
  for (let i = 0; i < a.length; i++) difference |= a[i] ^ b[i];
  return difference === 0;
}

export default {
  async fetch(request, env) {
    const path = new URL(request.url).pathname;
    if (path === "/health" && request.method === "GET") return json({ status: "ok" });
    if (path !== "/v1/events" || request.method !== "POST") return json({ error: "not_found" }, 404);
    if (!env.INGEST_TOKEN || !env.DB) return json({ error: "not_configured" }, 503);
    if (!await authorized(request, env.INGEST_TOKEN)) return json({ error: "unauthorized" }, 401);
    // Bound the actual body, including requests without Content-Length.
    let body;
    try {
      const reader = request.body?.getReader();
      if (!reader) return json({ error: "invalid_body" }, 400);
      const chunks = [];
      let length = 0;
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        length += value.length;
        if (length > 1_000_000) {
          await reader.cancel();
          return json({ error: "body_too_large" }, 413);
        }
        chunks.push(value);
      }
      const bytes = new Uint8Array(length);
      let offset = 0;
      for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.length; }
      body = JSON.parse(new TextDecoder().decode(bytes));
    } catch { return json({ error: "invalid_json" }, 400); }
    const events = body?.events;
    if (!Array.isArray(events) || events.length < 1 || events.length > 50 || events.some(e =>
      !e || typeof e.id !== "string" || !/^[a-f0-9-]{36}$/.test(e.id) ||
      e.station !== "bioglow-pi" || !sources.has(e.source) ||
      typeof e.read_at !== "string" || !Number.isFinite(Date.parse(e.read_at)) ||
      !Number.isInteger(e.http_status) || e.http_status < 100 || e.http_status > 599 ||
      !e.result || typeof e.result !== "object" || Array.isArray(e.result))) {
      return json({ error: "invalid_events" }, 400);
    }
    try {
      // The Pi persists each exact batch until acknowledged. One row per upload
      // avoids spending D1's free daily write allowance on one row per sample.
      await env.DB.prepare(
        "INSERT INTO batches(id, station, read_at, events) VALUES (?, ?, ?, ?) ON CONFLICT(id) DO NOTHING"
      ).bind(events[0].id, events[0].station, events[0].read_at, JSON.stringify(events)).run();
      return json({ accepted: events.map(e => e.id) });
    } catch { return json({ error: "storage_unavailable" }, 503); }
  },
};
