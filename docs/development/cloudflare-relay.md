# Cloudflare Worker + D1 Webhook Relay

## Purpose

The relay gives Chatwoot a stable public HTTPS webhook while FastAPI and the AI
worker remain on the local machine.

```text
Chatwoot Cloud
  -> POST https://relay.luoxuecong.asia/v1/webhooks/chatwoot/{connection_key}
  -> Cloudflare Worker
  -> D1 relay_events
  <- local Python worker leases events over HTTPS
  -> local SQLite webhook_events
  -> existing AI processing and Chatwoot Create Message API
```

The local machine only creates outbound HTTPS connections. Cloudflare Tunnel,
LocalTunnel and Tailscale Funnel are not required for webhook delivery.

## Security Boundary

- `CHATWOOT_CONNECTION_KEY` protects the public webhook path.
- `RELAY_API_TOKEN` protects lease, ACK, retry and statistics endpoints.
- Both values are Cloudflare Worker secrets and must never be placed in
  `wrangler.jsonc`, frontend code, logs or documentation.
- The local copy of `RELAY_API_TOKEN` is stored only in `backend/.env`.
- D1 stores the original Chatwoot payload for a short delivery window. ACKed
  events are deleted after 7 days and dead-letter events after 30 days. The
  local worker invokes authenticated cleanup every 6 hours.
- A D1 event is ACKed only after the local SQLite transaction commits.

## Worker Endpoints

| Method | Path | Authentication | Purpose |
|---|---|---|---|
| GET | `/v1/health` | none | Edge and D1 readiness |
| POST | `/v1/webhooks/chatwoot/{connection_key}` | secret path | Validate, deduplicate and persist webhook |
| POST | `/v1/relay/lease` | Bearer token | Lease pending events for the local worker |
| POST | `/v1/relay/ack` | Bearer token | Confirm durable local ingestion |
| POST | `/v1/relay/retry` | Bearer token | Release failed events with delay |
| POST | `/v1/relay/cleanup` | Bearer token | Apply the 7/30 day retention policy |
| GET | `/v1/relay/stats` | Bearer token | Pending, leased, ACKed and dead counts |

## Local Validation

From `cloudflare-relay/`:

```powershell
npm install
npm run check
npm run db:migrate:local
npm run dev -- --port 8790
```

Create an ignored `.dev.vars` containing:

```text
CHATWOOT_CONNECTION_KEY=<existing connection key>
RELAY_API_TOKEN=<random token>
```

Set the backend values while testing locally:

```text
RELAY_BASE_URL=http://127.0.0.1:8790
RELAY_API_TOKEN=<same random token>
```

## Production Deployment

From `cloudflare-relay/`:

```powershell
npx wrangler login --device
npx wrangler d1 create china2go-chatwoot-relay
npx wrangler d1 migrations apply china2go-chatwoot-relay --remote
npx wrangler secret put CHATWOOT_CONNECTION_KEY
npx wrangler secret put RELAY_API_TOKEN
npm run deploy
```

Replace the placeholder `database_id` in `wrangler.jsonc` with the ID returned
by `wrangler d1 create` before migrations or deployment. The custom domain is
`relay.luoxuecong.asia` and must be in the Cloudflare zone of the authenticated
account.

After deployment:

1. Set `RELAY_BASE_URL=https://relay.luoxuecong.asia` and the matching token in
   `backend/.env`.
2. Restart the local Python worker.
3. Change the Chatwoot Account Webhook URL to the relay webhook URL.
4. Keep the same required event subscriptions.
5. Send a new Facebook test message and verify D1 ACK count, local event, AI run
   and Chatwoot outgoing message.

## Failure Behavior

- Local machine offline: events remain in D1 and are delivered after restart.
- Local ingestion fails: event returns to pending after a short delay.
- Lease holder crashes: the lease expires and another poll can reclaim it.
- Ten failed leases: event becomes `dead` for manual inspection.
- Duplicate Chatwoot delivery: D1 and local SQLite both enforce idempotency.
- Chatwoot outgoing or private messages: existing AI worker rules skip them.
