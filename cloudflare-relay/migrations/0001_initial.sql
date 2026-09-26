CREATE TABLE relay_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  idempotency_key TEXT NOT NULL UNIQUE,
  account_id INTEGER NOT NULL,
  event_name TEXT NOT NULL,
  resource_id TEXT NOT NULL,
  payload TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'leased', 'acked', 'dead')),
  attempts INTEGER NOT NULL DEFAULT 0,
  available_at INTEGER NOT NULL,
  lease_token TEXT,
  lease_expires_at INTEGER,
  last_error TEXT,
  received_at INTEGER NOT NULL,
  acked_at INTEGER
);

CREATE INDEX idx_relay_events_claim
  ON relay_events(status, available_at, lease_expires_at, id);

CREATE INDEX idx_relay_events_retention
  ON relay_events(status, acked_at, received_at);
