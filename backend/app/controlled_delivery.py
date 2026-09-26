"""Future live delivery boundary. Rehearsal callers must never invoke this path."""
from datetime import timedelta
from sqlalchemy import select, update
from app.config import settings
from app.chatwoot import ChatwootError
from app.automation_models import TouchReservation
from app.automation_service import reserve_touch, dt, iso, BLOCK_LABELS
from app.models import OutboundMessage, ConversationState, HandoffTask, utcnow
from app.outbound_control import global_message_sending_enabled
from app.conversation_policy import has_ai_label
from app.reception_rollout import reception_conversation_allowed


def submit_once(db, client, conversation_id: int, business_key: str, content: str, source: str, expected_version: int):
    # The irreversible boundary is fail-closed even if a caller bypasses the worker.
    if (not settings.outbound_enabled or not global_message_sending_enabled(db)
            or settings.app_profile in ("evaluation", "live_reply")):
        raise ChatwootError("outbound_disabled", "Live delivery disabled", 403)
    state=db.get(ConversationState,conversation_id)
    existing=db.scalar(select(OutboundMessage).where(OutboundMessage.idempotency_key==business_key))
    if existing: return existing
    if state.version!=expected_version:raise ChatwootError("state_changed","Conversation changed",409)
    remote_id=state.chatwoot_conversation_id
    contact_key=f"live:{state.tenant_id}:{state.contact_id or 'conv:'+str(state.id)}"
    db.commit()
    remote=client.get_conversation(remote_id)
    messages=client.get_messages(remote_id)
    labels=client.get_conversation_labels(remote_id)
    remote=remote.get("payload",remote)
    messages=messages.get("payload",[])
    labels=labels.get("payload",[]) if isinstance(labels,dict) else labels
    if (
        not isinstance(labels, list)
        or not has_ai_label(labels)
        or not remote.get("can_reply")
        or set(labels) & BLOCK_LABELS
    ):
        raise ChatwootError("latest_state_blocked","Latest state blocked",409)
    if not reception_conversation_allowed(db, remote_id):
        raise ChatwootError("test_conversation_required", "Conversation not allowlisted", 403)
    incoming=[m for m in messages if m.get("message_type") in (0,"incoming") and not m.get("private") and not (m.get("content_attributes") or {}).get("external_echo")]
    if not incoming:raise ChatwootError("window_unknown","No trustworthy customer message",409)
    from app.history_sync import message_timestamp
    latest=max(dt(message_timestamp(m["created_at"])) for m in incoming)
    if dt(utcnow())>=latest+timedelta(hours=24,minutes=-5):raise ChatwootError("automatic_window_closed","Automatic window closed",409)
    db.expire_all()
    state=db.get(ConversationState,conversation_id)
    handoff=db.scalar(select(HandoffTask.id).where(HandoffTask.conversation_state_id==state.id,HandoffTask.status.in_(["pending","claimed"])))
    if (handoff or state.version!=expected_version or state.ai_mode!="enabled"
            or not state.ai_label_present):
        raise ChatwootError("state_changed","Conversation changed",409)
    if source in ("sop","wakeup"):
        from zoneinfo import ZoneInfo
        if not 9<=dt(utcnow()).astimezone(ZoneInfo("Asia/Shanghai")).hour<21:raise ChatwootError("outside_contact_hours","Contact hours closed",409)
        if not reserve_touch(db,contact_key,business_key,utcnow()):raise ChatwootError("contact_reserved","Contact already reserved",409)
    # Persist unknown BEFORE the request. A crash after submission is never retried blindly.
    row=OutboundMessage(conversation_state_id=state.id,idempotency_key=business_key,source_type=source,content=content,status="submission_unknown")
    db.add(row)
    reservation=db.scalar(select(TouchReservation).where(TouchReservation.owner_key==business_key))
    if reservation:reservation.status="submission_unknown"
    db.commit()
    try:
        result=client.create_text_message(remote_id,content)
        if not isinstance(result,dict) or not result.get("id"):return row
        row.chatwoot_message_id=int(result["id"])
        row.status,row.submitted_at="submitted",utcnow()
        if reservation:reservation.status="submitted"
    except Exception:
        row.error_code="submission_unknown_reconcile_required"
        reservation=db.scalar(select(TouchReservation).where(TouchReservation.owner_key==business_key))
        if reservation:reservation.status="submission_unknown"
    db.commit()
    return row


def reconcile_known(db, client, outbound_id: int):
    row=db.get(OutboundMessage,outbound_id)
    if not row or not row.chatwoot_message_id:return {"status":"manual_reconciliation_required"}
    conversation=db.get(ConversationState,row.conversation_state_id)
    db.commit()
    payload=client.get_messages(conversation.chatwoot_conversation_id)
    match=next((m for m in payload.get("payload",[]) if m.get("id")==row.chatwoot_message_id),None)
    if match and match.get("status") in ("delivered","read","failed"):
        row.status=match["status"]
        db.commit()
    return {"id":row.id,"status":row.status}
