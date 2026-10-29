"""Reconcile callbacks that arrive before our send response is committed.

AI and advisors use the same Chatwoot user. Only a saved message ID proves
ownership; neither a matching user nor matching text proves an AI echo.
"""
from datetime import timedelta

from sqlalchemy import select

from app.models import MessageEvent, OutboundMessage, utcnow
from app.runtime_settings import dt


PENDING = 'outgoing_origin_pending'
RECONCILE_TIMEOUT = timedelta(seconds=30)


def pending(db, conversation_id):
    return db.scalar(select(MessageEvent.id).where(
        MessageEvent.conversation_state_id == conversation_id,
        MessageEvent.attribution == PENDING).limit(1)) is not None


def defer_unlinked(db, event, conversation, message):
    """Persist a short wait, or fail closed if a send never becomes identifiable.

The existing webhook worker retries this event without another customer send.
While pending, delivery pauses without cancelling the introduction. A real
advisor message is handled normally once the simultaneous AI send is linked.
"""
    unresolved = db.scalar(select(OutboundMessage.id).where(
        OutboundMessage.conversation_state_id == conversation.id,
        OutboundMessage.chatwoot_message_id.is_(None),
        OutboundMessage.source_type.in_(['ai', 'sop']),
        OutboundMessage.status.in_(['submission_unknown', 'unknown'])).limit(1))
    if unresolved is None:
        return False
    if dt(utcnow()) - dt(event.received_at) < RECONCILE_TIMEOUT:
        message.attribution = PENDING
        event.status, event.error_code, event.processed_at = 'retry', PENDING, None
    else:
        # A timed-out send may have succeeded remotely. Do not call its echo a
        # human reply, start the advisor timer, or retry the customer message.
        from app.operations import ensure_handoff
        from app.reception_v3.live import cancel
        message.attribution = 'unknown'
        cancel(db, conversation.id, 'outgoing_origin_unknown')
        ensure_handoff(db, conversation, 'delivery_unknown',
                       '外发消息来源及发送结果尚未确认，请顾问核对；系统不会自动重发。')
        event.status, event.error_code, event.processed_at = 'live_observed', 'outgoing_origin_unknown', utcnow()
    db.commit()
    return True
