from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.chatwoot import ReadOnlyChatwootClient
from app.config import settings
from app.history_sync import sync_conversation_history
from app.models import ChatwootConnection, Contact, ConversationState, InboxBinding, MessageEvent, SyncJob, Tenant, utcnow
from app.security import decrypt_secret
from app.automation_models import SyncConversationCursor
from app.reception_rollout import default_engine_assignment


def _collection(value: object) -> tuple[list[dict], int]:
    if not isinstance(value, dict):
        return (value if isinstance(value, list) else []), 0
    data = value.get("data") if isinstance(value.get("data"), dict) else value
    payload = data.get("payload") if isinstance(data, dict) else []
    meta = data.get("meta") if isinstance(data, dict) else {}
    return (payload if isinstance(payload, list) else []), int((meta or {}).get("all_count") or 0)


def _backup_database(job: SyncJob) -> None:
    prefix = "sqlite:///./"
    if not settings.database_url.startswith(prefix):
        return
    source = Path(settings.database_url.removeprefix(prefix)).resolve()
    if not source.exists():
        return
    target_dir = source.parent / "backups"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"before_evaluation_sync_{job.id}.db"
    if not target.exists():
        with sqlite3.connect(source) as source_db, sqlite3.connect(target) as target_db:
            source_db.backup(target_db)


def _upsert_conversation(db: Session, tenant: Tenant, inbox: InboxBinding, item: dict) -> ConversationState | None:
    conversation_id = item.get("id")
    if not conversation_id:
        return None
    sender = ((item.get("meta") or {}).get("sender") or {})
    contact = None
    if sender.get("id"):
        contact = db.scalar(select(Contact).where(Contact.tenant_id == tenant.id, Contact.chatwoot_contact_id == int(sender["id"])))
        if not contact:
            contact = Contact(tenant_id=tenant.id, chatwoot_contact_id=int(sender["id"]), name=sender.get("name") or "Unknown")
            db.add(contact)
            db.flush()
        contact.name = sender.get("name") or contact.name
        contact.email = sender.get("email") or contact.email
        contact.phone_number = sender.get("phone_number") or contact.phone_number
        contact.custom_attributes = sender.get("custom_attributes") or contact.custom_attributes
        contact.updated_at = utcnow()
    state = db.scalar(select(ConversationState).where(ConversationState.tenant_id == tenant.id, ConversationState.chatwoot_conversation_id == int(conversation_id)))
    if not state:
        state = ConversationState(
            tenant_id=tenant.id,
            inbox_binding_id=inbox.id,
            contact_id=contact.id if contact else None,
            chatwoot_conversation_id=int(conversation_id),
            **default_engine_assignment(),
        )
        db.add(state)
        db.flush()
    state.inbox_binding_id = inbox.id
    state.contact_id = contact.id if contact else state.contact_id
    state.status = str(item.get("status") or state.status)
    state.can_reply = bool(item.get("can_reply", state.can_reply))
    state.labels = [str(value) for value in item.get("labels", [])]
    meta = item.get("meta") or {}
    assignee, team = meta.get("assignee") or {}, meta.get("team") or {}
    state.assignee_id = int(assignee["id"]) if assignee.get("id") else None
    state.assignee_name = assignee.get("name") or assignee.get("available_name")
    state.team_id = int(team["id"]) if team.get("id") else None
    state.team_name = team.get("name")
    state.updated_at = utcnow()
    return state


def _remote_is_newer(db: Session, state: ConversationState, item: dict) -> bool:
    value = item.get("last_activity_at") or item.get("timestamp")
    if not value:
        return False
    if isinstance(value, (int, float)):
        remote = datetime.fromtimestamp(value, timezone.utc).isoformat()
    else:
        remote = str(value)
    latest = db.scalar(select(MessageEvent.created_at).where(MessageEvent.conversation_state_id == state.id).order_by(MessageEvent.created_at.desc()).limit(1))
    return not latest or remote > latest


def process_evaluation_sync(db: Session) -> bool:
    job = db.scalar(select(SyncJob).where(SyncJob.kind == "evaluation_history", SyncJob.status.in_(["pending", "running"])).order_by(SyncJob.id).limit(1))
    if not job:
        return False
    job.status, job.updated_at = "running", utcnow()
    if job.phase == "backup":
        _backup_database(job)
        job.phase, job.current_page = "conversations", 1
        db.commit()
        return True
    tenant = db.get(Tenant, job.tenant_id)
    inbox = db.scalar(select(InboxBinding).where(InboxBinding.tenant_id == tenant.id, InboxBinding.chatwoot_inbox_id == settings.evaluation_inbox_id))
    connection = db.scalar(select(ChatwootConnection).where(ChatwootConnection.tenant_id == tenant.id))
    if not inbox or not connection or not connection.encrypted_api_token:
        job.status, job.error_code, job.updated_at = "failed", "evaluation_sync_not_configured", utcnow()
        db.commit()
        return True
    cursor = db.scalar(select(SyncConversationCursor).where(SyncConversationCursor.job_id == job.id, SyncConversationCursor.status == "pending").order_by(SyncConversationCursor.id))
    if cursor:
        cursor_id, state_id = cursor.id, cursor.conversation_state_id
        db.commit()
        try:
            sync_conversation_history(state_id)
            cursor = db.get(SyncConversationCursor, cursor_id)
            cursor.status, cursor.error_code = "completed", None
        except Exception as exc:
            db.rollback()
            cursor = db.get(SyncConversationCursor, cursor_id)
            cursor.attempts += 1
            cursor.error_code = type(exc).__name__
            if cursor.attempts >= 3:
                cursor.status = "failed"
        job.completed_items = db.scalar(select(func.count()).select_from(SyncConversationCursor).where(SyncConversationCursor.job_id == job.id, SyncConversationCursor.status == "completed")) or 0
        job.failed_items = db.scalar(select(func.count()).select_from(SyncConversationCursor).where(SyncConversationCursor.job_id == job.id, SyncConversationCursor.status == "failed")) or 0
        db.commit()
        return True
    client = ReadOnlyChatwootClient(connection.base_url, connection.account_id, decrypt_secret(connection.encrypted_api_token), timeout=settings.chatwoot_request_timeout_seconds)
    try:
        items, total = _collection(client.list_conversations(page=job.current_page, inbox_id=settings.evaluation_inbox_id))
    except Exception as exc:
        job.error_code = type(exc).__name__
        db.commit()
        raise
    finally:
        client.close()
    if not items:
        if job.stable_passes == 0:
            job.stable_passes = 1
            job.current_page = 1
            job.phase = "verification_pass"
        else:
            job.status = "failed" if job.failed_items else "completed"
            job.phase, job.completed_at = "completed", utcnow()
        job.updated_at = utcnow()
        db.commit()
        return True
    state_ids: list[int] = []
    for item in items:
        item_inbox = item.get("inbox_id") or ((item.get("inbox") or {}).get("id"))
        if item_inbox and int(item_inbox) != settings.evaluation_inbox_id:
            continue
        state = _upsert_conversation(db, tenant, inbox, item)
        if state:
            existing = db.scalar(select(SyncConversationCursor).where(SyncConversationCursor.job_id == job.id, SyncConversationCursor.conversation_state_id == state.id))
            if not existing:
                db.add(SyncConversationCursor(job_id=job.id, conversation_state_id=state.id))
                if job.phase == "verification_pass":
                    job.stable_passes = 0
            elif job.phase == "verification_pass" and _remote_is_newer(db, state, item):
                existing.status = "pending"
    job.total_items = max(job.total_items, total)
    job.current_page += 1
    job.updated_at = utcnow()
    db.commit()
    return True
