import hashlib
import json
from dataclasses import dataclass

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import ChatwootConnection, WebhookEvent, utcnow


class AccountMismatchError(ValueError):
    pass


@dataclass(frozen=True)
class IngestResult:
    duplicate: bool
    event_id: int | None


def event_parts(payload: dict) -> tuple[str, int, int | None, str]:
    event = str(payload.get("event", "unknown"))
    account_id = int((payload.get("account") or {}).get("id") or 0)
    conversation = payload.get("conversation") or {}
    inbox_id = (payload.get("inbox") or {}).get("id") or conversation.get("inbox_id")
    resource_id = str(payload.get("id") or conversation.get("id") or (payload.get("sender") or {}).get("id") or "unknown")
    return event, account_id, int(inbox_id) if inbox_id else None, resource_id


def canonical_payload(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def ingest_payload(db: Session, connection: ChatwootConnection, payload: dict, raw: bytes | None = None) -> IngestResult:
    raw = raw or canonical_payload(payload)
    event, account_id, inbox_id, resource_id = event_parts(payload)
    if account_id != connection.account_id:
        raise AccountMismatchError(f"expected account {connection.account_id}, got {account_id}")
    suffix = f":{hashlib.sha256(raw).hexdigest()[:16]}" if event in {"message_updated", "conversation_updated", "conversation_status_changed", "contact_updated"} else ""
    key = f"{account_id}:{event}:{resource_id}{suffix}"
    row = WebhookEvent(
        connection_id=connection.id,
        event=event,
        account_id=account_id,
        inbox_id=inbox_id,
        resource_id=resource_id,
        idempotency_key=key,
        payload=payload,
    )
    db.add(row)
    connection.last_webhook_at = utcnow()
    try:
        db.flush()
        from app.automation_shadow import invalidate_shadow
        invalidate_shadow(db, connection, payload)
        db.commit()
        return IngestResult(duplicate=False, event_id=row.id)
    except IntegrityError:
        db.rollback()
        connection.last_webhook_at = utcnow()
        db.commit()
        return IngestResult(duplicate=True, event_id=None)
