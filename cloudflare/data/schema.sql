CREATE TABLE IF NOT EXISTS batches (
    id TEXT PRIMARY KEY,
    station TEXT NOT NULL,
    read_at TEXT NOT NULL,
    events TEXT NOT NULL,
    received_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS batches_station_time ON batches(station, read_at);
