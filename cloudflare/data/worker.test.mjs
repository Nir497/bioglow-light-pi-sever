import { test } from "node:test";
import assert from "node:assert/strict";
import { webcrypto } from "node:crypto";
import worker from "./worker.mjs";
globalThis.crypto ||= webcrypto;

const event = { id: "01234567-0123-4123-8123-012345678901", station: "bioglow-pi", source: "light",
  read_at: "2026-11-06T00:00:00Z", http_status: 503, result: { status: "unavailable" } };
const request = (body, token = "test-secret") => new Request("https://example.com/v1/events", {
  method: "POST", headers: { Authorization: `Bearer ${token}` }, body: JSON.stringify(body),
});
function environment() {
  const rows = new Map();
  return { rows, INGEST_TOKEN: "test-secret", DB: {
    prepare(sql) { assert.match(sql, /ON CONFLICT\(id\) DO NOTHING/); return { bind: (...args) => ({
      async run() { if (!rows.has(args[0])) rows.set(args[0], args); },
    }) }; },
  } };
}
test("uploads are private; misconfiguration fails closed", async () => {
  assert.equal((await worker.fetch(request({ events: [event] }), {})).status, 503);
  const env = environment();
  assert.equal((await worker.fetch(request({ events: [event] }, "wrong"), env)).status, 401);
  assert.equal(env.rows.size, 0);
  assert.equal((await worker.fetch(new Request("https://example.com/v1/events"), env)).status, 404);
});
test("duplicate deliveries acknowledge the same IDs and preserve raw scores", async () => {
  const env = environment();
  const animal = { ...event, source: "microphone", result: { animals: [{ score: -3.2 }] } };
  for (let i = 0; i < 2; i++) {
    const response = await worker.fetch(request({ events: [animal] }), env);
    assert.equal(response.status, 200);
    assert.deepEqual(await response.json(), { accepted: [event.id] });
  }
  assert.equal(env.rows.size, 1);
  assert.equal(JSON.parse(env.rows.get(event.id)[3])[0].result.animals[0].score, -3.2);
});
test("invalid events never reach D1", async () => {
  const env = environment();
  for (const body of [{ events: [] }, { events: [null] }, { events: [{ ...event, source: "audio" }] },
    { events: [{ ...event, http_status: 0 }] }, { events: Array(51).fill(event) }]) {
    assert.equal((await worker.fetch(request(body), env)).status, 400);
  }
  assert.equal(env.rows.size, 0);
});
test("storage failure is retryable and oversized bodies are rejected", async () => {
  const env = environment();
  env.DB.prepare = () => { throw Error("offline"); };
  assert.equal((await worker.fetch(request({ events: [event] }), env)).status, 503);
  assert.equal((await worker.fetch(request({ events: [event], extra: "x".repeat(1_000_000) }), env)).status, 413);
});
