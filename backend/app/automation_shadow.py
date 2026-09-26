"""Observe verified Chatwoot events; SOP delivery stays in rehearsal tables."""
from datetime import timedelta
from sqlalchemy import select, func

from app.models import WebhookEvent, User, ConversationState, MessageEvent, Contact, ChatwootConnection, Tenant, HandoffTask, utcnow
from app.automation_models import AutomationSession, RehearsalEnrollment
from app.automation_service import add_customer_message, cancel_generation, apply_controls, trigger_sops, advance_sops, dt
from app.history_sync import message_direction, message_timestamp


def mirror_controls(state, tenant, db):
    return {"channel": state.inbox.channel_type, "can_reply": state.can_reply,
            "human": bool(db.scalar(select(HandoffTask.id).where(HandoffTask.conversation_state_id == state.id, HandoffTask.status.in_(["pending", "claimed"])))),
            "ai_enabled": state.ai_mode not in ("off", "disabled") and state.ai_sync_status == "synced", "inbox_enabled": state.inbox.ai_enabled,
            "account_enabled": tenant.ai_enabled, "labels": state.labels,
            "contact_labels": state.contact.labels if state.contact else [],
            "permission_source": "webhook_mirror"}


def observe_webhook(db, event=None, mirrored=None):
    event = event or db.scalar(select(WebhookEvent).where(WebhookEvent.status == "pending").order_by(WebhookEvent.id).limit(1))
    if not event:
        return False
    from app.conversation_mirror import upsert_mirrors
    payload = event.payload
    public_incoming = event.event == "message_created" and message_direction(payload.get("message_type")) == "incoming" and not payload.get("private") and not (payload.get("content_attributes") or {}).get("external_echo")
    at = message_timestamp(payload.get("created_at") or utcnow())
    fresh = dt(utcnow()) - dt(event.received_at) < timedelta(minutes=5)
    if public_incoming:
        fresh = fresh and bool(payload.get("created_at")) and timedelta(seconds=-30) <= dt(utcnow()) - dt(at) < timedelta(minutes=5)
    if mirrored is not None:
        state, ctx = mirrored
    else:
        state, _, ctx, _ = upsert_mirrors(db, event, side_effects=False, update_controls=not (public_incoming and not fresh))
    connection = db.get(ChatwootConnection, event.connection_id)
    tenant = db.get(Tenant, connection.tenant_id)
    if state:
        session = db.scalar(select(AutomationSession).where(AutomationSession.environment == "shadow", AutomationSession.conversation_state_id == state.id))
        if not session:
            owner = db.scalar(select(User).where(User.role.in_(["admin", "super_admin"]), User.active.is_(True)))
            if not owner:
                return False
            history = db.scalars(select(MessageEvent).where(MessageEvent.conversation_state_id == state.id, MessageEvent.private.is_(False), MessageEvent.direction.in_(["incoming", "outgoing"]), MessageEvent.created_at < at).order_by(MessageEvent.created_at.desc())).all()
            first = select(func.min(MessageEvent.created_at)).join(ConversationState).where(ConversationState.inbox_binding_id == state.inbox_binding_id, MessageEvent.direction == "incoming", MessageEvent.private.is_(False))
            first = first.where(ConversationState.contact_id == state.contact_id) if state.contact_id else first.where(ConversationState.id == state.id)
            added_at = db.scalar(first)
            controls = {**mirror_controls(state, tenant, db), "observed_labels": ctx.get("previous_labels", []),
                        "customer_added_at": None if public_incoming and fresh and added_at == at else added_at,
                        "customer_added_source": "first_public_customer_message_in_inbox", "shadow_reply_enabled": False}
            session = AutomationSession(owner_id=owner.id, inbox_binding_id=state.inbox_binding_id, conversation_state_id=state.id,
                environment="shadow", mode="sop", virtual_now=utcnow(), controls=controls,
                messages=[{"id": m.chatwoot_message_id, "direction": m.direction, "content": m.content,
                           "content_type": m.content_type, "status": m.status, "created_at": m.created_at} for m in reversed(history)])
            db.add(session)
            db.flush()
        session.virtual_now = utcnow()
        observed = set(session.controls.get("observed_labels", ctx.get("previous_labels", [])))
        apply_controls(db, session, mirror_controls(state, tenant, db))
        if public_incoming:
            session.virtual_now = at
            content_type = "image" if payload.get("attachments") else payload.get("content_type", "text")
            if fresh:
                add_customer_message(db, session, payload.get("content") or "", f"cw:{payload['id']}", content_type, queue_reply=False)
            elif not any(x.get("id") == payload['id'] or x.get("client_key") == f"cw:{payload['id']}" for x in session.messages):
                # Backlog is context only; it must not cancel a newly started round.
                session.messages = sorted([*session.messages, {"id": payload['id'], "direction": "incoming", "content": payload.get("content") or "", "content_type": content_type, "created_at": at}], key=lambda x: dt(x['created_at']))
            session.virtual_now = utcnow()
        if fresh and event.event in ("conversation_created", "conversation_updated"):
            trigger_sops(db, session, added_labels=set(state.labels) - observed)
        session.controls = {**session.controls, "observed_labels": list(state.labels)}
    event.status, event.processed_at = "shadow_observed", utcnow()
    db.commit()
    return True


def invalidate_shadow(db, connection, payload):
    """Cancel stale inference immediately, but stop SOPs only on relevant conditions."""
    event = payload.get("event", "")
    conversation = payload.get("conversation") or (payload if event.startswith("conversation_") else {})
    query = select(AutomationSession).join(ConversationState).where(AutomationSession.environment == "shadow", ConversationState.tenant_id == connection.tenant_id)
    if event == "contact_updated":
        query = query.join(Contact).where(Contact.chatwoot_contact_id == payload.get("id"))
    elif conversation.get("id"):
        query = query.where(ConversationState.chatwoot_conversation_id == conversation["id"])
    else:
        return
    incoming = event == "message_created" and message_direction(payload.get("message_type")) == "incoming" and not payload.get("private") and not (payload.get("content_attributes") or {}).get("external_echo")
    stale_incoming = False
    if incoming:
        incoming = bool(payload.get("created_at")) and timedelta(seconds=-30) <= dt(utcnow()) - dt(message_timestamp(payload["created_at"])) < timedelta(minutes=5)
        stale_incoming = not incoming
    for session in db.scalars(query).all():
        if incoming:
            cancel_generation(db, session, "customer_new_message")
            session.generation += 1
        changes = {} if stale_incoming else {key: conversation[key] for key in ("labels", "can_reply") if key in conversation}
        if event == "contact_updated":
            from app.conversation_mirror import contact_labels
            if "labels" in payload or any("label_list" in x for x in payload.get("changed_attributes", []) if isinstance(x, dict)):
                changes.update({"contact_labels": contact_labels(payload), "contact_state_unknown": False})
        apply_controls(db, session, changes)


def advance_shadow_sops(db):
    sessions = db.scalars(select(AutomationSession).join(RehearsalEnrollment).where(
        AutomationSession.environment == "shadow", RehearsalEnrollment.status == "active").distinct()).all()
    for session in sessions:
        state = db.get(ConversationState, session.conversation_state_id)
        apply_controls(db, session, mirror_controls(state, db.get(Tenant, state.tenant_id), db))
        session.virtual_now = utcnow()
        advance_sops(db, session)
    if sessions:
        db.commit()
