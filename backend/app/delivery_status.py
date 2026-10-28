"""Read-only receipt reconciliation; never retries a customer send."""
from datetime import datetime, timedelta
import time

from sqlalchemy import select, or_

from app.chatwoot import ChatwootError, ReadOnlyChatwootClient
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


def channel_receipt(status, attributes=None):
    """Chatwoot can report an external error without a status field."""
    if status in ('delivered', 'read'):
        return status
    if isinstance(attributes, dict) and str(attributes.get('external_error') or '').strip():
        return 'failed'
    return status if status in RECEIPTS else None


def channel_delivery_blocked(db, conversation_id):
    """A failed automatic send stays blocked until a newer real customer input.

    The durable outbound record survives session restarts and AI label toggles.
    Receipt reconciliation may resolve a false failure, but never resends it.
    """
    failed = db.scalar(select(OutboundMessage).where(
        OutboundMessage.conversation_state_id == conversation_id,
        OutboundMessage.source_type.in_(['ai', 'sop']),
        OutboundMessage.idempotency_key.like('live%'),
        OutboundMessage.status == 'failed',
        OutboundMessage.chatwoot_message_id.is_not(None)
    ).order_by(OutboundMessage.chatwoot_message_id.desc()).limit(1))
    if not failed:
        return False
    newer = db.scalar(select(MessageEvent.id).where(
        MessageEvent.conversation_state_id == conversation_id,
        MessageEvent.direction == 'incoming', MessageEvent.private.is_(False),
        MessageEvent.content_attributes['external_echo'].as_boolean().is_not(True),
        MessageEvent.chatwoot_message_id > failed.chatwoot_message_id,
        MessageEvent.created_at > failed.created_at).limit(1))
    return newer is None


def apply_receipt(db, out, status, attributes=None):
    message = db.scalar(select(MessageEvent).where(
        MessageEvent.conversation_state_id == out.conversation_state_id,
        MessageEvent.chatwoot_message_id == out.chatwoot_message_id))
    status = receipt_status(channel_receipt(status, attributes),
        channel_receipt(message.status, message.content_attributes) if message else None)
    if status not in RECEIPTS:
        return False
    final = receipt_status(out.status, message.status if message else None, status)
    changed = out.status != final or (message is not None and message.status != final)
    out.status = final
    if message:
        message.status = final
        message.attribution = out.source_type
    external_error = (attributes or {}).get('external_error') or (
        (message.content_attributes or {}).get('external_error') if message else None)
    if final == 'failed' and external_error:
        out.content_attributes = {**(out.content_attributes or {}), 'external_error': str(external_error)[:1000]}
        out.error_code = 'channel_send_failed'
    elif final in ('delivered', 'read'):
        out.content_attributes = {k: v for k, v in (out.content_attributes or {}).items() if k != 'external_error'}
        if out.error_code == 'channel_send_failed':
            out.error_code = None
        if message:
            message.content_attributes = {k: v for k, v in (message.content_attributes or {}).items() if k != 'external_error'}
    if final == "failed" and out.source_type in {"ai", "sop"} and channel_delivery_blocked(db, out.conversation_state_id):
        pause_failed_delivery(db, out)
    return bool(changed)


def pause_failed_delivery(db, out):
    """Stop continuation without sending, retrying, or committing the caller's transaction."""
    from app.reception_v3.live import cancel
    from app.operations import ensure_handoff
    state=db.get(ConversationState,out.conversation_state_id)
    if state:
        cancel(db,state.id,'channel_send_failed')
        ensure_handoff(db,state,'channel_send_failed','渠道报告发送失败，请人工核对。系统不会自动重发。')


def reconcile_live_delivery(checked=None):
    """Check unresolved receipts including older submissions, without resending."""
    checked = checked if checked is not None else {}
    cutoff = (datetime.fromisoformat(utcnow()) - timedelta(hours=48)).isoformat()
    with SessionLocal() as db:
        from app.reception_rollout import reception_inbox_ids
        candidates = db.scalars(select(ConversationState).join(InboxBinding).join(
            ChatwootConnection, ChatwootConnection.tenant_id == ConversationState.tenant_id).join(
            OutboundMessage, OutboundMessage.conversation_state_id == ConversationState.id).where(
                ChatwootConnection.account_id == settings.live_reply_account_id,
                InboxBinding.chatwoot_inbox_id.in_(reception_inbox_ids(db)),
                OutboundMessage.idempotency_key.like("live%"),
                OutboundMessage.chatwoot_message_id.is_not(None),
                or_(OutboundMessage.status.in_(["submitted", "sent"]),
                    (OutboundMessage.status == 'delivered') & (OutboundMessage.created_at >= cutoff))
                ).distinct()).all()
        candidates = [item for item in candidates
                      if checked.get(('unavailable_until', item.id), 0) <= time.monotonic()]
        if not candidates:
            return 0
        # Most passes prioritize unresolved sends. Reserve every fifth pass for
        # delivery/read updates so neither queue can starve the other.
        unresolved = set(db.scalars(select(OutboundMessage.conversation_state_id).where(
            OutboundMessage.status.in_(['submitted', 'sent']),
            OutboundMessage.idempotency_key.like('live%'),
            OutboundMessage.chatwoot_message_id.is_not(None))).all())
        checked['passes'] = checked.get('passes', 0) + 1
        urgent = [item for item in candidates if item.id in unresolved]
        pool = urgent if urgent and checked['passes'] % 5 else candidates
        state = min(pool, key=lambda item: checked.get(item.id, 0))
        state_id, remote_id = state.id, state.chatwoot_conversation_id
        checked[state_id] = time.monotonic()
        connection = db.scalar(select(ChatwootConnection).where(ChatwootConnection.tenant_id == state.tenant_id))
        client_args = (connection.base_url, connection.account_id, decrypt_secret(connection.encrypted_api_token))
        target_rows = db.execute(select(OutboundMessage.chatwoot_message_id, OutboundMessage.status).where(
            OutboundMessage.conversation_state_id == state_id,
            OutboundMessage.idempotency_key.like("live%"),
            OutboundMessage.chatwoot_message_id.is_not(None),
            or_(OutboundMessage.status.in_(['submitted', 'sent']),
                (OutboundMessage.status == 'delivered') & (OutboundMessage.created_at >= cutoff))
            ).order_by(OutboundMessage.chatwoot_message_id)).all()
        targets = [mid for mid, status in sorted(target_rows, key=lambda item: (
            (item[1] not in ('submitted', 'sent')) if checked['passes'] % 5 else False,
            checked.get((state_id, item[0]), 0), item[0]))[:20]]
    client = ReadOnlyChatwootClient(*client_args, timeout=5)
    receipts = {}
    try:
        for _ in range(3):
            missing = [mid for mid in targets if mid not in receipts]
            if not missing:
                break
            # A page ending at the newest selected target covers adjacent
            # messages in the same batch, rather than fetching one at a time.
            target = max(missing)
            before = target + 1
            checked[(state_id, target)] = time.monotonic()
            try:
                messages, _ = unpack_messages(client.get_messages(remote_id, before=before))
            except ChatwootError as exc:
                if exc.status_code != 404:
                    raise
                # Deleted/inaccessible historical conversations are not send
                # failures. Recheck later without blocking current receipts.
                checked[('unavailable_until', state_id)] = time.monotonic() + 15 * 60
                break
            for message in messages:
                status = channel_receipt(message.get('status'), message.get('content_attributes'))
                if (message.get("id") in targets and status in RECEIPTS
                        and message.get("message_type") in (1, "outgoing") and not message.get("private")):
                    receipts[message["id"]] = (status, message.get('content_attributes'))
                    checked[(state_id, message['id'])] = time.monotonic()
            # An absent/deleted remote message must not starve other targets.
            targets.remove(target)
    finally:
        client.close()
    changed = 0
    with SessionLocal() as db:
        for out in db.scalars(select(OutboundMessage).where(
                OutboundMessage.conversation_state_id == state_id,
                OutboundMessage.idempotency_key.like("live%"),
                OutboundMessage.chatwoot_message_id.in_(receipts))).all():
            status, attributes = receipts[out.chatwoot_message_id]
            changed += apply_receipt(db, out, status, attributes)
        db.commit()
    return changed
