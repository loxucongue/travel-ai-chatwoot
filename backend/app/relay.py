import json
import logging

import httpx
from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal
from app.models import ChatwootConnection
from app.webhook_ingest import ingest_payload

logger = logging.getLogger("relay")


class RelayError(RuntimeError):
    pass


class RelayClient:
    def __init__(self) -> None:
        self.client = httpx.Client(
            base_url=settings.relay_base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {settings.relay_api_token}"},
            timeout=httpx.Timeout(settings.relay_request_timeout_seconds, connect=min(5, settings.relay_request_timeout_seconds)),
            limits=httpx.Limits(max_connections=2, max_keepalive_connections=1, keepalive_expiry=60),
            trust_env=False,
        )

    def close(self) -> None:
        self.client.close()

    def lease(self) -> dict:
        response = self.client.post("/v1/relay/lease", json={"limit": settings.relay_poll_batch_size, "lease_seconds": settings.worker_lease_seconds})
        if response.status_code != 200:
            raise RelayError(f"relay_lease_{response.status_code}")
        return response.json()

    def ack(self, lease_token: str, ids: list[int]) -> None:
        response = self.client.post("/v1/relay/ack", json={"lease_token": lease_token, "ids": ids})
        if response.status_code != 200:
            raise RelayError(f"relay_ack_{response.status_code}")

    def retry(self, lease_token: str, ids: list[int], error: str) -> None:
        response = self.client.post("/v1/relay/retry", json={"lease_token": lease_token, "ids": ids, "error": error, "delay_seconds": 5})
        if response.status_code != 200:
            raise RelayError(f"relay_retry_{response.status_code}")

    def cleanup(self) -> int:
        response = self.client.post("/v1/relay/cleanup")
        if response.status_code != 200:
            raise RelayError(f"relay_cleanup_{response.status_code}")
        return int(response.json().get("deleted") or 0)


def cleanup_relay_once() -> int:
    if not settings.relay_enabled:
        return 0
    client = RelayClient()
    try:
        return client.cleanup()
    finally:
        client.close()


def poll_relay_once(client: RelayClient | None = None) -> int:
    if not settings.relay_enabled:
        return 0
    owns_client = client is None
    client = client or RelayClient()
    try:
        leased = client.lease()
        lease_token = str(leased.get("lease_token") or "")
        events = leased.get("events") if isinstance(leased.get("events"), list) else []
        ack_ids: list[int] = []
        retry_ids: list[int] = []
        last_error = "local_ingest_failed"
        for event in events:
            relay_id = int(event["id"])
            payload = event.get("payload")
            if not isinstance(payload, dict):
                retry_ids.append(relay_id)
                last_error = "invalid_relay_payload"
                continue
            try:
                with SessionLocal() as db:
                    account_id = int((payload.get("account") or {}).get("id") or 0)
                    connection = db.scalar(select(ChatwootConnection).where(ChatwootConnection.account_id == account_id))
                    if not connection:
                        raise RelayError(f"connection_not_found:{account_id}")
                    ingest_payload(db, connection, payload, json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
                ack_ids.append(relay_id)
            except Exception as exc:
                logger.warning("relay_ingest_failed relay_id=%s code=%s", relay_id, type(exc).__name__)
                retry_ids.append(relay_id)
                last_error = type(exc).__name__
        if ack_ids:
            client.ack(lease_token, ack_ids)
        if retry_ids:
            client.retry(lease_token, retry_ids, last_error)
        return len(ack_ids)
    finally:
        if owns_client:
            client.close()
