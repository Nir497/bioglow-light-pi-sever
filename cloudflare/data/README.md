# BioGlow data receiver

Target: `https://bioglow-data.orengolov.workers.dev`. This Worker receives outbound
HTTPS uploads from the existing Python service. No second tunnel, DNS change,
Cloudflare API token, router change, or additional Pi service is required.

## Set up in the Cloudflare dashboard

1. Open **Storage & databases → D1 SQL Database → Create database**. Name it
   `bioglow-data`. Open its **Console** and run the contents of `schema.sql`.
2. Open **Workers & Pages → bioglow-data → Bindings → Add binding → D1 database**.
   Use variable name **DB** (uppercase), and select the database from step 1.
3. On the Pi, stage an upload secret:

   ```bash
   cd /home/nir/bioglow-birdcast
   .venv/bin/python scripts/prepare_telemetry.py
   cat .runtime/telemetry-token.pending
   ```

   The last command displays the key in your terminal so you can copy it into
   Cloudflare. It is generated locally, not a tunnel token or Cloudflare API key.
   Do not paste it into chat, Git, or a command argument.
4. In the Worker, open **Settings → Variables and Secrets → Add**. Choose type
   **Secret**, name **INGEST_TOKEN**, and paste the staged key as its value.
   Save/deploy the change.
5. Open the Worker's **Edit code** editor, replace the starter code with
   `worker.mjs`, and **Deploy**. Preserve the DB binding and INGEST_TOKEN secret.
   `/health` should now return `{"status":"ok"}`. This endpoint checks the code
   is deployed; it does not test database configuration.
6. Tell the configuring agent **Worker configured**. The agent can verify the
   authenticated receiver with the staged key before activating the sender.
   Activation moves the staged file to `.runtime/telemetry-token`, then restarts
   the existing `bioglow.service` and verifies local service and cloud delivery.
   A dashboard restart resets station mode to Normal/Auto and stops automatic
   microphone scheduling, as usual.

No upload starts until the active token file exists and the service starts.
To disable uploads and new queue entries, move the active token back to the
pending filename. Restart to stop both background threads immediately.

## Data and delivery

New logged radar, luminance, manual BirdCast, and recording/identification events
are queued at their existing cadence. Animal names and raw scores are preserved;
predictions remain unverified candidates, not percentages. No WAV or audio bytes
are included. Existing historical files are not backfilled.

Lamp state is sampled once a second with station mode, configured direction in
degrees, spacing/offset/beam width, color, acknowledged and target brightness,
on/off, and controller status/errors. These are reported software/controller
values, not an independent measurement of physical light output or direction.
Changes shorter than one second may not appear in lamp samples.

The durable SQLite queue is `.runtime/telemetry.sqlite3`. An exact upload batch
of up to 50 events is persisted before transmission. Acknowledged events are
removed locally; failed uploads retry after 5–60 seconds, including after process
restart. Lost responses safely resend the same batch; D1 deduplicates by its
first event ID. Network I/O runs outside collection and HTTP request threads.
Queue write errors are logged; local history continues, but those failed queue
writes are not automatically backfilled. The queue has no expiry or size cap;
prolonged outages consume Pi disk space.

The Worker accepts authenticated `POST /v1/events` only; it exposes no data read
API or Pi controls. Cloud records persist independently: deleting a local
recording does not delete its already uploaded identification events.

D1 stores one JSON batch per upload, normally roughly every five seconds, rather
than one row per reading. This retains every event while reducing indexed writes.
Normal uploads use about 17,280 batches per day; retries/catch-up and other
account activity can change usage. Monitor database Metrics and storage growth.
Cloudflare's current free D1 allowance is 100,000 row writes/day, with indexes
also counting as writes; see https://developers.cloudflare.com/d1/platform/pricing/.
No paid plan or automatic retention policy is enabled by this code.

Inspect recent cloud arrivals from the D1 Console:

```sql
SELECT id, read_at, received_at, json_array_length(events) AS event_count
FROM batches ORDER BY received_at DESC LIMIT 10;
```

Inspect individual readings from the latest ten batches using the time index:

```sql
SELECT json_extract(e.value, '$.source') AS source,
       json_extract(e.value, '$.read_at') AS read_at,
       json_extract(e.value, '$.http_status') AS http_status,
       json_extract(e.value, '$.result') AS result
FROM (SELECT events FROM batches WHERE station = 'bioglow-pi'
      ORDER BY read_at DESC LIMIT 10) AS b,
     json_each(b.events) AS e;
```

## Local validation

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest test_telemetry -v
node --test cloudflare/data/worker.test.mjs
```

These checks use temporary queues and mocked network/D1 operations. End-to-end
delivery requires configuring and deploying the real Worker and database.
