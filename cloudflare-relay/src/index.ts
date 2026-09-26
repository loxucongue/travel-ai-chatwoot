import { eventSuffix } from './event-key';

interface Env {
  DB: D1Database;
  CHATWOOT_CONNECTION_KEY: string;
  RELAY_API_TOKEN: string;
}

interface RelayEventRow {
  id: number;
  payload: string;
  attempts: number;
  received_at: number;
}

const MAX_BODY_BYTES = 2 * 1024 * 1024;
const MAX_LEASE_BATCH = 20;
const MAX_ATTEMPTS = 10;

function boundedInteger(value: unknown, fallback: number, minimum: number, maximum: number): number {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return fallback;
  return Math.max(minimum, Math.min(maximum, Math.trunc(parsed)));
}

function json(data: unknown, status = 200): Response {
  return Response.json(data, {
    status,
    headers: {
      'cache-control': 'no-store',
      'x-content-type-options': 'nosniff',
    },
  });
}

async function equalSecret(actual: string, expected: string): Promise<boolean> {
  const encoder = new TextEncoder();
  const [left, right] = await Promise.all([
    crypto.subtle.digest('SHA-256', encoder.encode(actual)),
    crypto.subtle.digest('SHA-256', encoder.encode(expected)),
  ]);
  const leftBytes = new Uint8Array(left);
  const rightBytes = new Uint8Array(right);
  let mismatch = 0;
  for (let index = 0; index < leftBytes.length; index += 1) mismatch |= leftBytes[index] ^ rightBytes[index];
  return mismatch === 0;
}

function hasSecret(value: string | undefined): value is string {
  return typeof value === 'string' && value.length >= 24;
}

async function requireRelayAuth(request: Request, env: Env): Promise<Response | null> {
  if (!hasSecret(env.RELAY_API_TOKEN)) return json({ error: { code: 'relay_not_configured' } }, 503);
  const authorization = request.headers.get('authorization') ?? '';
  const token = authorization.startsWith('Bearer ') ? authorization.slice(7) : '';
  return await equalSecret(token, env.RELAY_API_TOKEN) ? null : json({ error: { code: 'unauthorized' } }, 401);
}

function eventParts(payload: Record<string, unknown>): { accountId: number; eventName: string; resourceId: string } {
  const account = (payload.account ?? {}) as Record<string, unknown>;
  const conversation = (payload.conversation ?? {}) as Record<string, unknown>;
  const sender = (payload.sender ?? {}) as Record<string, unknown>;
  return {
    accountId: Number(account.id ?? 0),
    eventName: String(payload.event ?? 'unknown'),
    resourceId: String(payload.id ?? conversation.id ?? sender.id ?? 'unknown'),
  };
}

async function acceptWebhook(request: Request, env: Env, connectionKey: string): Promise<Response> {
  if (!hasSecret(env.CHATWOOT_CONNECTION_KEY)) return json({ error: { code: 'relay_not_configured' } }, 503);
  if (!(await equalSecret(connectionKey, env.CHATWOOT_CONNECTION_KEY))) {
    return json({ error: { code: 'webhook_not_found' } }, 404);
  }
  const contentLength = Number(request.headers.get('content-length') ?? 0);
  if (contentLength > MAX_BODY_BYTES) return json({ error: { code: 'payload_too_large' } }, 413);
  const bodyBuffer = await request.arrayBuffer();
  const body = new Uint8Array(bodyBuffer);
  if (body.byteLength > MAX_BODY_BYTES) return json({ error: { code: 'payload_too_large' } }, 413);

  let payload: Record<string, unknown>;
  try {
    const decoded = JSON.parse(new TextDecoder().decode(body)) as unknown;
    if (!decoded || typeof decoded !== 'object' || Array.isArray(decoded)) {
      return json({ error: { code: 'invalid_payload' } }, 422);
    }
    payload = decoded as Record<string, unknown>;
  } catch {
    return json({ error: { code: 'invalid_json' } }, 400);
  }
  const { accountId, eventName, resourceId } = eventParts(payload);
  if (!accountId) return json({ error: { code: 'account_missing' } }, 422);
  const suffix = await eventSuffix(eventName, bodyBuffer);
  const idempotencyKey = `${accountId}:${eventName}:${resourceId}${suffix}`;
  const now = Date.now();
  const result = await env.DB.prepare(
    `INSERT OR IGNORE INTO relay_events
      (idempotency_key, account_id, event_name, resource_id, payload, status, available_at, received_at)
     VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)`,
  ).bind(idempotencyKey, accountId, eventName, resourceId, JSON.stringify(payload), now, now).run();
  return json({ accepted: true, duplicate: result.meta.changes === 0 }, 202);
}

async function leaseEvents(request: Request, env: Env): Promise<Response> {
  const denied = await requireRelayAuth(request, env);
  if (denied) return denied;
  const input = await request.json().catch(() => ({})) as { limit?: number; lease_seconds?: number };
  const limit = boundedInteger(input.limit, 10, 1, MAX_LEASE_BATCH);
  const leaseSeconds = boundedInteger(input.lease_seconds, 60, 15, 120);
  const now = Date.now();
  const leaseToken = crypto.randomUUID();
  const candidates = await env.DB.prepare(
    `SELECT id FROM relay_events
     WHERE available_at <= ?
       AND (status = 'pending' OR (status = 'leased' AND lease_expires_at < ?))
     ORDER BY id LIMIT ?`,
  ).bind(now, now, limit).all<{ id: number }>();

  if (candidates.results.length) {
    await env.DB.batch(candidates.results.map(({ id }) => env.DB.prepare(
      `UPDATE relay_events
       SET status = 'leased', lease_token = ?, lease_expires_at = ?, attempts = attempts + 1
       WHERE id = ? AND available_at <= ?
         AND (status = 'pending' OR (status = 'leased' AND lease_expires_at < ?))`,
    ).bind(leaseToken, now + leaseSeconds * 1000, id, now, now)));
  }
  const leased = await env.DB.prepare(
    `SELECT id, payload, attempts, received_at FROM relay_events
     WHERE lease_token = ? AND status = 'leased' ORDER BY id`,
  ).bind(leaseToken).all<RelayEventRow>();
  return json({
    lease_token: leaseToken,
    events: leased.results.map((event) => ({
      id: event.id,
      payload: JSON.parse(event.payload),
      attempts: event.attempts,
      received_at: event.received_at,
    })),
  });
}

async function ackEvents(request: Request, env: Env): Promise<Response> {
  const denied = await requireRelayAuth(request, env);
  if (denied) return denied;
  const input = await request.json() as { lease_token?: string; ids?: number[] };
  const ids = Array.isArray(input.ids)
    ? input.ids.slice(0, MAX_LEASE_BATCH).map(Number).filter((id) => Number.isSafeInteger(id) && id > 0)
    : [];
  if (!input.lease_token || !ids.length) return json({ error: { code: 'invalid_ack' } }, 422);
  const now = Date.now();
  const results = await env.DB.batch(ids.map((id) => env.DB.prepare(
    `UPDATE relay_events SET status = 'acked', acked_at = ?, lease_token = NULL, lease_expires_at = NULL
     WHERE id = ? AND status = 'leased' AND lease_token = ?`,
  ).bind(now, id, input.lease_token)));
  return json({ acked: results.reduce((total, result) => total + Number(result.meta.changes ?? 0), 0) });
}

async function retryEvents(request: Request, env: Env): Promise<Response> {
  const denied = await requireRelayAuth(request, env);
  if (denied) return denied;
  const input = await request.json() as { lease_token?: string; ids?: number[]; error?: string; delay_seconds?: number };
  const ids = Array.isArray(input.ids)
    ? input.ids.slice(0, MAX_LEASE_BATCH).map(Number).filter((id) => Number.isSafeInteger(id) && id > 0)
    : [];
  if (!input.lease_token || !ids.length) return json({ error: { code: 'invalid_retry' } }, 422);
  const delaySeconds = boundedInteger(input.delay_seconds, 5, 1, 900);
  const availableAt = Date.now() + delaySeconds * 1000;
  const error = String(input.error ?? 'local_ingest_failed').slice(0, 300);
  await env.DB.batch(ids.map((id) => env.DB.prepare(
    `UPDATE relay_events
     SET status = CASE WHEN attempts >= ? THEN 'dead' ELSE 'pending' END,
         available_at = ?, last_error = ?, lease_token = NULL, lease_expires_at = NULL
     WHERE id = ? AND status = 'leased' AND lease_token = ?`,
  ).bind(MAX_ATTEMPTS, availableAt, error, id, input.lease_token)));
  return json({ retried: ids.length });
}

async function stats(request: Request, env: Env): Promise<Response> {
  const denied = await requireRelayAuth(request, env);
  if (denied) return denied;
  const result = await env.DB.prepare('SELECT status, COUNT(*) AS count FROM relay_events GROUP BY status').all<{ status: string; count: number }>();
  return json({ statuses: Object.fromEntries(result.results.map((row) => [row.status, row.count])) });
}

async function cleanup(request: Request, env: Env): Promise<Response> {
  const denied = await requireRelayAuth(request, env);
  if (denied) return denied;
  const now = Date.now();
  const results = await env.DB.batch([
    env.DB.prepare("DELETE FROM relay_events WHERE status = 'acked' AND acked_at < ?").bind(now - 7 * 24 * 60 * 60 * 1000),
    env.DB.prepare("DELETE FROM relay_events WHERE status = 'dead' AND received_at < ?").bind(now - 30 * 24 * 60 * 60 * 1000),
  ]);
  return json({ deleted: results.reduce((total, result) => total + Number(result.meta.changes ?? 0), 0) });
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    try {
      const url = new URL(request.url);
      if (request.method === 'GET' && url.pathname === '/v1/health') return json({ status: 'ok', storage: 'd1' });
      const webhookMatch = url.pathname.match(/^\/v1\/webhooks\/chatwoot\/([^/]+)$/);
      if (request.method === 'POST' && webhookMatch) return acceptWebhook(request, env, decodeURIComponent(webhookMatch[1]));
      if (request.method === 'POST' && url.pathname === '/v1/relay/lease') return leaseEvents(request, env);
      if (request.method === 'POST' && url.pathname === '/v1/relay/ack') return ackEvents(request, env);
      if (request.method === 'POST' && url.pathname === '/v1/relay/retry') return retryEvents(request, env);
      if (request.method === 'POST' && url.pathname === '/v1/relay/cleanup') return cleanup(request, env);
      if (request.method === 'GET' && url.pathname === '/v1/relay/stats') return stats(request, env);
      return json({ error: { code: 'not_found' } }, 404);
    } catch (error) {
      console.error(JSON.stringify({ event: 'relay_request_failed', error: error instanceof Error ? error.name : 'UnknownError' }));
      return json({ error: { code: 'internal_error' } }, 500);
    }
  },

  async scheduled(_controller: ScheduledController, env: Env): Promise<void> {
    const now = Date.now();
    await env.DB.batch([
      env.DB.prepare("DELETE FROM relay_events WHERE status = 'acked' AND acked_at < ?").bind(now - 7 * 24 * 60 * 60 * 1000),
      env.DB.prepare("DELETE FROM relay_events WHERE status = 'dead' AND received_at < ?").bind(now - 30 * 24 * 60 * 60 * 1000),
    ]);
  },
} satisfies ExportedHandler<Env>;
