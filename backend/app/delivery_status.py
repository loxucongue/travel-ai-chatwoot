"""Read-only receipt reconciliation; never retries a customer send."""
from datetime import datetime, timedelta
import time

from sqlalchemy import select, update

from app.chatwoot import ReadOnlyChatwootClient
from app.config import settings
from app.db import SessionLocal
from app.history_sync import unpack_messages
from app.models import ChatwootConnection, ConversationState, InboxBinding, MessageEvent, OutboundMessage, utcnow
from app.security import decrypt_secret


RECEIPTS = {"sent", "delivered", "read", "failed"}
_RANK = {"submission_unknown": 0, "submitted": 1, "sent": 2, "failed": 3, "delivered": 4, "read": 5}


def receipt_status(*values):
    # A late creation event must not overwrite a delivery/read receipt.
    known = [value for value in values if value in _RANK]
    return max(known, key=_RANK.get) if known else None


def apply_receipt(db, out, status):
    if status not in RECEIPTS:
        return False
    message = db.scalar(select(MessageEvent).where(
        MessageEvent.conversation_state_id == out.conversation_state_id,
        MessageEvent.chatwoot_message_id == out.chatwoot_message_id))
    final = receipt_status(out.status, message.status if message else None, status)
    changed = out.status != final or (message is not None and message.status != final)
    out.status = final
    if message:
        message.status = final
        message.attribution = out.source_type
    from app.delivery_tracking import record_delivery_progress
    record_delivery_progress(db, out)
    if changed and final == "failed" and out.source_type in {"ai", "sop"}:
        pause_failed_delivery(db, out)
    return bool(changed)


def pause_failed_delivery(db, out):
    """Stop continuation without sending, retrying, or committing the caller's transaction."""
    from app.automation_models import LiveSopEnrollment, LiveSopJob
    from app.live_reply_models import LiveReplyJob
    from app.operations import ensure_handoff

    state = db.get(ConversationState, out.conversation_state_id)
    if state is None:
        return
    now = utcnow()
    reason = "channel_send_failed"
    out.error_code = reason
    db.execute(update(LiveReplyJob).where(
        LiveReplyJob.conversation_state_id == state.id,
        LiveReplyJob.status.in_(["queued", "processing"]),
    ).values(status="blocked", error_code=reason, completed_at=now))
    enrollments = db.scalars(select(LiveSopEnrollment).where(
        LiveSopEnrollment.conversation_state_id == state.id,
        LiveSopEnrollment.status == "active",
    )).all()
    for enrollment in enrollments:
        enrollment.status = "attention_required"
        enrollment.exit_reason = reason
        enrollment.completed_at = now
        db.execute(update(LiveSopJob).where(
            LiveSopJob.enrollment_id == enrollment.id,
            LiveSopJob.status.in_(["scheduled", "waiting_dependency", "processing"]),
        ).values(status="blocked", reason=reason, completed_at=now))
    ensure_handoff(
        db, state, reason,
        f"渠道报告消息发送失败（出站记录 #{out.id}）。自动接待已暂停，请核对实际送达情况后处理；系统不会自动重发。",
    )


def reconcile_live_delivery(checked=None):
    """Check one recent live conversation, with bounded pagination and no write lock over HTTP."""
    checked = checked if checked is not None else {}
    cutoff = (datetime.fromisoformat(utcnow()) - timedelta(hours=48)).isoformat()
    with SessionLocal() as db:
        candidates = db.scalars(select(ConversationState).join(InboxBinding).join(
            ChatwootConnection, ChatwootConnection.tenant_id == ConversationState.tenant_id).join(
            OutboundMessage, OutboundMessage.conversation_state_id == ConversationState.id).where(
                ChatwootConnection.account_id == settings.live_reply_account_id,
                InboxBinding.chatwoot_inbox_id == settings.live_reply_inbox_id,
                OutboundMessage.idempotency_key.like("live%"),
                OutboundMessage.chatwoot_message_id.is_not(None),
                OutboundMessage.status.in_(["submitted", "sent", "delivered"]),
                OutboundMessage.created_at >= cutoff).distinct()).all()
        if not candidates:
            return 0
        state = min(candidates, key=lambda item: checked.get(item.id, 0))
        state_id, remote_id = state.id, state.chatwoot_conversation_id
        checked[state_id] = time.monotonic()
        connection = db.scalar(select(ChatwootConnection).where(ChatwootConnection.tenant_id == state.tenant_id))
        client_args = (connection.base_url, connection.account_id, decrypt_secret(connection.encrypted_api_token))
        targets = set(db.scalars(select(OutboundMessage.chatwoot_message_id).where(
            OutboundMessage.conversation_state_id == state_id,
            OutboundMessage.idempotency_key.like("live%"),
            OutboundMessage.chatwoot_message_id.is_not(None),
            OutboundMessage.created_at >= cutoff)).all())
    client = ReadOnlyChatwootClient(*client_args, timeout=5)
    receipts, before = {}, None
    try:
        for _ in range(3):
            messages, _ = unpack_messages(client.get_messages(remote_id, before=before))
            ids = [m.get("id") for m in messages if isinstance(m.get("id"), int)]
            for message in messages:
                if (message.get("id") in targets and message.get("status") in RECEIPTS
                        and message.get("message_type") in (1, "outgoing") and not message.get("private")):
                    receipts[message["id"]] = message["status"]
            if not ids or targets.issubset(receipts) or (before is not None and min(ids) >= before):
                break
            before = min(ids)
    finally:
        client.close()
    changed = 0
    with SessionLocal() as db:
        for out in db.scalars(select(OutboundMessage).where(
                OutboundMessage.conversation_state_id == state_id,
                OutboundMessage.idempotency_key.like("live%"),
                OutboundMessage.chatwoot_message_id.in_(receipts))).all():
            changed += apply_receipt(db, out, receipts[out.chatwoot_message_id])
        db.commit()
    return changed
