"""Persist Chatwoot events and conversation controls independently of worker entrypoints."""
from datetime import datetime, timedelta, timezone
from sqlalchemy import select, update
from app.conversation_policy import compute_state, observe_ai_label
from app.models import (ChatwootConnection, Contact, ConversationState, InboxBinding,
    HandoffTask, LabelEvent, MessageEvent, OutboundMessage, SopDefinition, SopEnrollment, SopJob, Tenant, WebhookEvent, utcnow)
from app.operations import audit, create_notification, ensure_handoff, setting_value
from app.reception_rollout import default_engine_assignment


def schedule_sop_enrollment(db, sop: SopDefinition, conversation: ConversationState) -> bool:
    # Grouped SOPs use the versioned rehearsal scheduler, never this legacy sender.
    if any(node.get("messages") or node.get("schedule_type") == "calendar_day" for node in sop.nodes):
        return False
    existing = db.scalar(select(SopEnrollment).where(SopEnrollment.sop_id == sop.id, SopEnrollment.conversation_state_id == conversation.id))
    if existing:
        return False
    enrollment = SopEnrollment(sop_id=sop.id, conversation_state_id=conversation.id)
    db.add(enrollment)
    db.flush()
    base = datetime.now(timezone.utc)
    previous = base
    for node in sop.nodes:
        if node.get("schedule_type") == "fixed":
            scheduled = datetime.fromisoformat(node["fixed_at"].replace("Z", "+00:00"))
        else:
            basis = previous if node.get("basis") == "previous_node" else base
            scheduled = basis + timedelta(
                minutes=int(node.get("delay_minutes") or 0),
                seconds=int(node.get("delay_seconds") or 0),
            )
        db.add(SopJob(enrollment_id=enrollment.id, node_key=node["key"], scheduled_at=scheduled.astimezone(timezone.utc).isoformat()))
        previous = scheduled
    return True


def enroll_matching_sops(db, conversation: ConversationState, added_labels: set[str]) -> None:
    if not added_labels:
        return
    rows = db.scalars(select(SopDefinition).where(SopDefinition.status == "running", SopDefinition.trigger_type.in_(["label", "stage"]))).all()
    for sop in rows:
        if sop.inbox_ids and conversation.inbox.chatwoot_inbox_id not in sop.inbox_ids:
            continue
        if set(sop.trigger_labels or []) & added_labels:
            schedule_sop_enrollment(db, sop, conversation)


def unwrap(value):
    if isinstance(value, dict) and isinstance(value.get("payload"), dict):
        return value["payload"]
    return value if isinstance(value, dict) else {}


def contact_labels(payload: dict) -> list[str]:
    direct = payload.get("labels")
    if isinstance(direct, list):
        return [str(x) for x in direct]
    for change in payload.get("changed_attributes") or []:
        if isinstance(change, dict) and "label_list" in change:
            value = change["label_list"].get("current_value")
            if isinstance(value, list):
                return [str(x) for x in value]
    return []


def extract_context(payload: dict) -> dict:
    event = payload.get("event")
    conversation = payload.get("conversation") or (payload if str(event).startswith("conversation_") else {})
    payload_sender = payload.get("sender") if isinstance(payload.get("sender"), dict) else {}
    conversation_sender = (conversation.get("meta") or {}).get("sender") or {}
    if str(event).startswith("contact_"):
        customer = payload
    elif isinstance(conversation_sender, dict) and conversation_sender.get("id"):
        customer = conversation_sender
    elif payload.get("message_type") == "incoming" or str(payload_sender.get("type", "")).lower() == "contact":
        customer = payload_sender
    else:
        customer = {}
    contact_inbox = conversation.get("contact_inbox") or {}
    inbox = payload.get("inbox") or {}
    return {
        "event": event,
        "conversation": conversation,
        "conversation_id": conversation.get("id") or (payload.get("id") if str(event).startswith("conversation_") else None),
        "inbox_id": inbox.get("id") or conversation.get("inbox_id"),
        "contact_id": contact_inbox.get("contact_id") or customer.get("id") or (payload.get("id") if str(event).startswith("contact_") else None),
        "contact_name": customer.get("name") or "未知客户",
        "message_id": payload.get("id") if str(event).startswith("message_") else None,
        "message_type": payload.get("message_type"),
        "private": bool(payload.get("private", False)),
        "content_type": payload.get("content_type") or "text",
        "content": payload.get("content") or "",
        "message_status": payload.get("status"),
    }


def upsert_mirrors(db, event: WebhookEvent, *, side_effects: bool = True, update_controls: bool = True):
    payload = event.payload
    ctx = extract_context(payload)
    connection = db.get(ChatwootConnection, event.connection_id)
    tenant = db.get(Tenant, connection.tenant_id)
    contact = None
    if ctx["contact_id"]:
        contact = db.scalar(select(Contact).where(Contact.tenant_id == tenant.id, Contact.chatwoot_contact_id == int(ctx["contact_id"])))
        if not contact:
            contact = Contact(tenant_id=tenant.id, chatwoot_contact_id=int(ctx["contact_id"]), name=ctx["contact_name"])
            db.add(contact)
            db.flush()
        if ctx["contact_name"] != "未知客户":
            contact.name = ctx["contact_name"]
        contact_source = payload if str(ctx["event"]).startswith("contact_") else ((ctx["conversation"].get("meta") or {}).get("sender") or payload.get("sender") or {})
        contact.email = contact_source.get("email") or contact.email
        contact.phone_number = contact_source.get("phone_number") or contact.phone_number
        contact.custom_attributes = contact_source.get("custom_attributes") or contact.custom_attributes
        if ctx["event"] == "contact_updated":
            updated_labels = contact_labels(payload)
            if "labels" in payload or any("label_list" in x for x in payload.get("changed_attributes", []) if isinstance(x, dict)):
                contact.labels = updated_labels
        contact.updated_at = utcnow()

    if ctx["event"] in ("contact_created", "contact_updated"):
        if contact:
            for linked in db.scalars(select(ConversationState).where(ConversationState.contact_id == contact.id)).all():
                linked_inbox = db.get(InboxBinding, linked.inbox_binding_id)
                state, reason = compute_state(
                    tenant, linked_inbox, linked.labels or [], contact.labels or [], linked.can_reply,
                    linked.ai_mode, linked.ai_sync_status, linked.ai_label_present,
                )
                if db.scalar(select(HandoffTask.id).where(HandoffTask.conversation_state_id == linked.id,
                                                         HandoffTask.status.in_(["pending", "claimed"]))):
                    state, reason = "HUMAN_HANDOFF", "handoff:active_task"
                if state != linked.effective_ai_state or reason != linked.effective_state_reason:
                    linked.version += 1
                linked.effective_ai_state, linked.effective_state_reason, linked.updated_at = state, reason, utcnow()
        return None, None, ctx, False

    inbox = db.scalar(select(InboxBinding).where(InboxBinding.tenant_id == tenant.id, InboxBinding.chatwoot_inbox_id == ctx["inbox_id"]))
    if not inbox or not ctx["conversation_id"]:
        return None, None, ctx, False

    row = db.scalar(select(ConversationState).where(ConversationState.tenant_id == tenant.id, ConversationState.chatwoot_conversation_id == int(ctx["conversation_id"])))
    conversation_created = row is None
    if not row:
        row = ConversationState(
            tenant_id=tenant.id,
            inbox_binding_id=inbox.id,
            contact_id=contact.id if contact else None,
            chatwoot_conversation_id=int(ctx["conversation_id"]),
            **default_engine_assignment(),
        )
        db.add(row)
        db.flush()
    elif contact and not row.contact_id:
        row.contact_id = contact.id

    conversation = ctx["conversation"] if update_controls else {}
    previous_labels = list(row.labels or [])
    previous_ai_label_present = row.ai_label_present
    labels = conversation.get("labels")
    if isinstance(labels, list):
        row.labels = [str(x) for x in labels]
        if ctx["event"] in ("conversation_created", "conversation_updated", "conversation_status_changed", "message_created"):
            observe_ai_label(row, row.labels, "chatwoot")
    if "can_reply" in conversation:
        row.can_reply = bool(conversation["can_reply"])
    if conversation.get("status"):
        row.status = str(conversation["status"])
    assignment = (conversation.get("meta") or {}) if isinstance(conversation.get("meta"), dict) else {}
    assignee = assignment.get("assignee") if isinstance(assignment.get("assignee"), dict) else None
    team = assignment.get("team") if isinstance(assignment.get("team"), dict) else None
    if assignee:
        row.assignee_id = int(assignee["id"]) if assignee.get("id") else None
        row.assignee_name = assignee.get("name") or assignee.get("available_name")
    if team:
        row.team_id = int(team["id"]) if team.get("id") else None
        row.team_name = team.get("name")
    if ctx["event"] == "conversation_status_changed" and payload.get("status"):
        row.status = str(payload["status"])
    state, reason = compute_state(
        tenant, inbox, row.labels or [], contact.labels if contact else [], row.can_reply,
        row.ai_mode, row.ai_sync_status, row.ai_label_present,
    )
    if db.scalar(select(HandoffTask.id).where(HandoffTask.conversation_state_id == row.id,
                                             HandoffTask.status.in_(["pending", "claimed"]))):
        state, reason = "HUMAN_HANDOFF", "handoff:active_task"
    if state != row.effective_ai_state or reason != row.effective_state_reason:
        row.version += 1
    row.effective_ai_state, row.effective_state_reason, row.updated_at = state, reason, utcnow()
    ctx["previous_labels"] = previous_labels
    if side_effects and ctx["event"] in ("conversation_created", "conversation_updated") and previous_labels != (row.labels or []):
        added_labels = set(row.labels or []) - set(previous_labels)
        cutover = tenant.analytics_cutover_at or tenant.created_at
        if utcnow() >= cutover:
            for label in added_labels:
                db.add(LabelEvent(conversation_state_id=row.id, label=label, action="added"))
            for label in set(previous_labels) - set(row.labels or []):
                db.add(LabelEvent(conversation_state_id=row.id, label=label, action="removed"))
        mappings = setting_value(db, "label_mappings", {"handoff_labels": ["人工接管", "客诉"]})
        matched = next((label for label in row.labels if label in mappings.get("handoff_labels", [])), None)
        if matched:
            ensure_handoff(db, row, "label", f"Chatwoot 标签触发：{matched}", "P1" if matched == "客诉" else "P2")
        enroll_matching_sops(db, row, added_labels)
        if previous_ai_label_present != row.ai_label_present:
            audit(db, None, "conversation.ai_mode.synced_from_chatwoot", "conversation", row.chatwoot_conversation_id, {
                "enabled": row.ai_label_present,
                "event": ctx["event"],
            })
            if previous_ai_label_present and not row.ai_label_present:
                from app.live_reply_models import LiveReplyJob
                from app.live_sop import cancel_live_sop_on_control_change

                db.execute(update(LiveReplyJob).where(
                    LiveReplyJob.conversation_state_id == row.id,
                    LiveReplyJob.status.in_(["queued", "processing"]),
                ).values(
                    status="cancelled",
                    error_code="ai_label_removed",
                    completed_at=utcnow(),
                ))
                cancel_live_sop_on_control_change(db, row, "ai_label_removed")
                create_notification(
                    db,
                    "ai.label_removed",
                    "Chatwoot 已关闭 AI 接管",
                    f"会话 #{row.chatwoot_conversation_id} 的 ai 标签已被移除，自动发送已停止。",
                    row.chatwoot_conversation_id,
                )

    message = None
    if ctx["message_id"]:
        message = db.scalar(select(MessageEvent).where(MessageEvent.conversation_state_id == row.id, MessageEvent.chatwoot_message_id == int(ctx["message_id"])))
        if not message:
            message = MessageEvent(conversation_state_id=row.id, chatwoot_message_id=int(ctx["message_id"]), direction=ctx["message_type"] or "unknown")
            db.add(message)
            db.flush()
        if ctx["content"]:
            message.content = ctx["content"]
        message.direction = ctx["message_type"] or message.direction
        message.private = ctx["private"]
        message.content_type = ctx["content_type"]
        message.status = ctx["message_status"] or message.status
        message.sender_id = int(payload["sender"]["id"]) if isinstance(payload.get("sender"), dict) and payload["sender"].get("id") else None
        message.attachments = payload.get("attachments") if isinstance(payload.get("attachments"), list) else []
        message.content_attributes = payload.get("content_attributes") if isinstance(payload.get("content_attributes"), dict) else {}
        if message.direction == "incoming":
            message.attribution = "customer"
        elif message.direction == "outgoing":
            linked = db.scalar(select(OutboundMessage).where(OutboundMessage.chatwoot_message_id == int(ctx["message_id"])))
            message.attribution = linked.source_type if linked else "inferred_human"
        else:
            message.attribution = "system"
        if message.content and message.direction in ("incoming", "outgoing") and not message.private:
            row.last_message = message.content
        if message.direction == "incoming":
            row.last_customer_message_at = payload.get("created_at") or utcnow()
        if not side_effects:
            from app.history_sync import message_direction, message_timestamp
            message.direction = message_direction(ctx["message_type"])
            message.created_at = message_timestamp(payload.get("created_at") or utcnow())
        if side_effects and ctx["event"] == "message_updated":
            outbound = db.scalar(select(OutboundMessage).where(OutboundMessage.chatwoot_message_id == int(ctx["message_id"])))
            if outbound and ctx["message_status"] in ("sent", "delivered", "read", "failed"):
                outbound.status = ctx["message_status"]
    return row, message, ctx, conversation_created


