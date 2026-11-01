# China2Go AI / V3

V3 is the only active reception engine. Live Chatwoot and playground sessions share the Agent, Skills, configuration and durable delivery state. V1/V2 execution code has been removed; historical database records and migrations remain readable.

See [configuration mapping and release evidence](docs/development/v3-consolidation-20260928.md).

For the current end-to-end behavior, effective configuration, output transformations and remaining gaps, see [接待全流程审计（2026-11-01）](docs/development/reception-full-flow-audit-20261101.md). This audit supersedes older descriptions where behavior differs; it distinguishes deployed fixes from proposed work.

## Local development

Python 3.11+, Node.js 22.12+. Install `backend[dev]` and frontend dependencies. From backend, run `alembic upgrade head`.

Run separate terminals in backend:

```text
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
python -m app.playground_worker
```

From frontend: `npm run dev -- --host 127.0.0.1 --port 4175`.
Local rehearsal settings: `APP_PROFILE=evaluation`, `OUTBOUND_MODE=disabled`, `CHATWOOT_WRITE_ENABLED=false`.

Live entry: `python -m app.live_reply_worker`. `LIVE_REPLY_CONCURRENCY` sets model concurrency (default 4, range 1-8). Delivery uses a separate pool. Increasing concurrency reduces queueing, not individual model latency.

## Verification and deployment

Backend: `python -m pytest tests -q`. Frontend: `npm test` and `npm run build`.
Release: `python scripts/deploy_v3.py stage|deploy|check|rollback`.
Preflight migrates an isolated database copy; deployment replaces code directories while retaining live data/configuration. Rollback restores code without restoring old customer data.

Real-model connected conversations: `backend/scripts/verify_reception_v3.py`, with an isolated database, cost ledger and channel writes blocked. `--natural` waits real three/five-minute intervals in the copied configuration.

Historical documents describe previous releases, not the current runtime.
