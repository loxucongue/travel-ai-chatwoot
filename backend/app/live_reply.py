"""Opt-in reactive replies. No SOP, wakeup or notification dispatch lives here."""
from dataclasses import asdict
from copy import deepcopy
from datetime import timedelta
from hashlib import sha256
import json
from pathlib import Path
import time

from sqlalchemy import select, update

from app.chatwoot_service import client_for, payload_dict, string_payload
from app.automation_service import blocking_labels, dt, iso, reply_policy
from app.chatwoot import ChatwootError, normalize_collection
from app.config import settings
from app.conversation_policy import compute_state, has_ai_label, observe_ai_label
from app.db import SessionLocal
from app.decision_service import _explicit_stop_request, generate_decision
from app.deepseek_evaluation import EvaluationCallError
from app.delivery_status import apply_receipt, receipt_status, pause_failed_delivery
from app.delivery_plan import delivery_mode_for, ordered_delivery_parts
from app.reply_context import fetch_customer_context
from app.history_sync import message_direction, message_timestamp, unpack_messages
from app.live_reply_models import LiveReplyJob
from app.lead_capture import (
    apply_model_policy,
    mark_requested,
    observe_history,
    record_capture,
)
from app.lead_capture_models import LeadCaptureState
from app.material_library import (candidate_materials, catalog_assets, material_info,
                                  resolve_materials, same_content_group, by_media, material_live_approved)
from app.models import (AiRun, ChatwootConnection, ConversationState, HandoffTask, MessageEvent,
                        OutboundMessage, StoredMedia, Tenant, WebhookEvent, utcnow)
from app.operations import create_notification, ensure_handoff, setting_value
from app.outbound_control import global_message_sending_enabled
from app.route_reply import (deferred_initial_follow_up, deferred_follow_up_group, journey_context,
                             journey_for, mark_group_delivered, playbook_prompt, record_group_delivery,
                             prepare_route_reply, route_snapshot_from_values)
from app.route_packages import DEFAULT_INITIAL_DELIVERY_INTERVAL_SECONDS, ROUTES
from app.reception_config import effective_reception_policy
from app.web_knowledge import enrich_context_with_web_knowledge
from app.reception_rollout import reception_conversation_allowed
from app.conversation_mirror import upsert_mirrors


class ReplyBlocked(Exception):
    pass


def _initial_delivery_mode(decision, route_spec=None) -> str:
    """Resolve the reviewed content group's delivery order for a reply with assets."""
    return delivery_mode_for(
        str(decision.route_variant or ""),
        list(decision.covered_content_groups or []),
        list(decision.material_keys or []),
        route_spec=route_spec,
    )


def input_payload(messages: list[dict]) -> tuple[str, list[dict]]:
    """Return customer text and non-sensitive attachment metadata for the model."""
    text = "\n".join(str(message.get("content") or "") for message in messages).strip()
    attachments = []
    for message in messages:
        for attachment in message.get("attachments") or []:
            attachments.append({
                "file_type": str(attachment.get("file_type") or "file"),
                "extension": str(attachment.get("extension") or ""),
            })
    return text, attachments


def live_policy(db):
    return setting_value(db, "live_reply", {})


def assert_worker_ready(db):
    policy = live_policy(db)
    if settings.app_profile != "live_reply" or not settings.outbound_enabled or not policy.get("armed_at"):
        raise ReplyBlocked("live_reply_not_armed")
    return policy


def assert_armed(db):
    policy = assert_worker_ready(db)
    if not global_message_sending_enabled(db):
        raise ReplyBlocked("global_message_sending_disabled")
    return policy


def fresh(at, now=None):
    try:
        return timedelta(seconds=-30) <= dt(now or utcnow()) - dt(message_timestamp(at)) < timedelta(minutes=5)
    except (ValueError, TypeError):
        return False


def handoff_stale_message(db, state, message, *, armed_at, labels=None):
    """Escalate an unanswered post-activation message; never replay old history."""
    if not armed_at or not message or not message.created_at:
        return False
    try:
        if dt(message.created_at) < dt(armed_at) or fresh(message.created_at):
            return False
    except (ValueError, TypeError):
        return False
    connection = db.scalar(select(ChatwootConnection).where(ChatwootConnection.tenant_id == state.tenant_id))
    observed_labels = labels if labels is not None else (state.labels or [])
    tenant = db.get(Tenant, state.tenant_id)
    contact_labels = (state.contact.labels or []) if state.contact else []
    if (not connection or connection.account_id != settings.live_reply_account_id
            or state.inbox.chatwoot_inbox_id != settings.live_reply_inbox_id
            or not tenant.ai_enabled or not state.inbox.ai_enabled
            or not reception_conversation_allowed(db, state.chatwoot_conversation_id)
            or not has_ai_label(observed_labels)
            or state.ai_mode_source == "customer_stop"
            or (state.ai_mode == "disabled" and state.ai_mode_source != "system")
            or set([*observed_labels, *contact_labels]) & blocking_labels(db)):
        return False
    answered = db.scalar(select(MessageEvent.id).where(
        MessageEvent.conversation_state_id == state.id, MessageEvent.direction == "outgoing",
        MessageEvent.private.is_(False), MessageEvent.chatwoot_message_id > message.chatwoot_message_id))
    newer = db.scalar(select(MessageEvent.id).where(
        MessageEvent.conversation_state_id == state.id, MessageEvent.direction == "incoming",
        MessageEvent.private.is_(False), MessageEvent.chatwoot_message_id > message.chatwoot_message_id))
    if answered or newer:
        return False
    ensure_handoff(db, state, "reply_queue_overdue", "客户消息超过自动回复时限，已停止自动补发，请顾问检查并跟进。", "P1")
    return True


def complete(db, job, status, reason=None):
    job.status, job.error_code, job.completed_at = status, reason, utcnow()
    run = db.scalar(select(AiRun).where(AiRun.trigger_message_id == job.trigger_message_id))
    if run:
        run.status, run.error_code, run.completed_at = status, reason, utcnow()
    state = db.get(ConversationState, job.conversation_state_id)
    if db.scalar(select(HandoffTask.id).where(HandoffTask.conversation_state_id == state.id,
                                             HandoffTask.status.in_(["pending", "claimed"]))):
        state.effective_ai_state, state.effective_state_reason = "HUMAN_HANDOFF", "handoff:active_task"
    db.commit()


def enroll_after_submitted_reply(
    db,
    state,
    job,
    route_variant: str,
    *,
    safety_flags: list[str] | None = None,
    deferred_follow_up: dict | None = None,
) -> dict:
    """Attach every successfully submitted reply to the same live journey.

    Route-selection quick replies use the unclassified journey.  Keeping this
    after delivery ensures a scheduling failure can never cause the customer
    message itself to be sent twice.
    """
    if ("skip_silence_enrollment" in set(safety_flags or [])
            or "stop_automation" in set(safety_flags or [])
            or state.ai_mode_source == "customer_stop"):
        result = {
            "status": "skipped",
            "reason": "decision_skips_silence_enrollment",
            "route_variant": route_variant,
        }
        refreshed = db.get(LiveReplyJob, job.id)
        refreshed.trace = {**(refreshed.trace or {}), "sop_enrollment": result}
        db.commit()
        return result
    try:
        from app.live_sop import enroll_model_route_sop

        result = enroll_model_route_sop(
            db,
            state,
            route_variant,
            request_key=f"passive-route:{job.id}:{route_variant or 'unclassified'}",
            deferred_follow_up=deferred_follow_up,
        )
    except Exception as exc:
        db.rollback()
        result = {
            "status": "error",
            "reason": type(exc).__name__,
            "route_variant": route_variant,
        }
    refreshed = db.get(LiveReplyJob, job.id)
    refreshed.trace = {**(refreshed.trace or {}), "sop_enrollment": result}
    db.commit()
    return result


def stop_customer_automation(db, state):
    """Persist an explicit opt-out before any model or outbound work."""
    from app.live_sop import cancel_live_sop_on_control_change

    state.ai_mode = "disabled"
    state.ai_mode_source = "customer_stop"
    state.ai_mode_updated_at = utcnow()
    state.effective_ai_state = "AI_PAUSED_CONVERSATION"
    state.effective_state_reason = "customer_requested_stop"
    db.execute(update(LiveReplyJob).where(
        LiveReplyJob.conversation_state_id == state.id,
        LiveReplyJob.status.in_(["queued", "processing"]),
    ).values(status="cancelled", error_code="customer_requested_stop", completed_at=utcnow()))
    cancel_live_sop_on_control_change(db, state, "customer_requested_stop")
    db.commit()


def mirror_event(db, event):
    policy = live_policy(db)
    payload = event.payload
    out = db.scalar(select(OutboundMessage).join(ConversationState).join(
        ChatwootConnection, ChatwootConnection.tenant_id == ConversationState.tenant_id).where(
        ChatwootConnection.id == event.connection_id,
        OutboundMessage.chatwoot_message_id == payload.get("id"))) if event.event in ("message_created", "message_updated") else None
    existing_message = db.scalar(select(MessageEvent).where(
        MessageEvent.conversation_state_id == out.conversation_state_id,
        MessageEvent.chatwoot_message_id == out.chatwoot_message_id)) if out else None
    previous_media = list(existing_message.attachments or []) if existing_message else []
    previous_time = existing_message.created_at if existing_message else None
    incoming = (event.event == "message_created" and message_direction(payload.get("message_type")) == "incoming"
                and not payload.get("private") and not (payload.get("content_attributes") or {}).get("external_echo"))
    current = bool(policy.get("armed_at") and event.received_at >= policy["armed_at"] and fresh(event.received_at))
    current = current and (not incoming or (payload.get("created_at") and fresh(payload["created_at"])))
    state, message, ctx, _ = upsert_mirrors(db, event, side_effects=False, update_controls=bool(current))
    if state and current and not incoming:
        human_reply = (event.event == "message_created" and message_direction(payload.get("message_type")) == "outgoing"
                       and not payload.get("private") and not db.scalar(select(OutboundMessage.id).where(
                           OutboundMessage.chatwoot_message_id == payload.get("id"))))
        label_removed = not has_ai_label(state.labels)
        if human_reply or label_removed or set(state.labels) & blocking_labels(db):
            for job in db.scalars(select(LiveReplyJob).where(LiveReplyJob.conversation_state_id == state.id,
                                                           LiveReplyJob.status.in_(["queued", "processing"]))).all():
                job.status, job.error_code, job.completed_at = "cancelled", "human_or_control_changed", utcnow()
            if label_removed:
                from app.live_sop import cancel_live_sop_on_control_change
                cancel_live_sop_on_control_change(db, state, "ai_label_removed")
    if state and message and incoming and _explicit_stop_request(message.content or ""):
        stop_customer_automation(db, state)
    if state and message and incoming and not current and policy.get("armed_at"):
        labels = (payload.get("conversation") or {}).get("labels", state.labels or [])
        handoff_stale_message(db, state, message, armed_at=policy["armed_at"], labels=labels)
    if state and message and incoming and current:
        from app.live_sop import cancel_live_sop_on_customer_message
        cancel_live_sop_on_customer_message(db, state, message)
        if db.scalar(select(HandoffTask.id).where(HandoffTask.conversation_state_id == state.id,
                                                 HandoffTask.status.in_(["pending", "claimed"]))):
            state.effective_ai_state, state.effective_state_reason = "HUMAN_HANDOFF", "handoff:active_task"
        if (global_message_sending_enabled(db)
                and event.account_id == settings.live_reply_account_id and state.inbox.chatwoot_inbox_id == settings.live_reply_inbox_id
                and state.effective_ai_state == "AI_ACTIVE"
                and has_ai_label(state.labels or [])
                and reception_conversation_allowed(db, state.chatwoot_conversation_id)):
            exists = db.scalar(select(LiveReplyJob.id).where(LiveReplyJob.trigger_message_id == message.id))
            if not exists:
                # One durable job per trigger. New arrivals supersede the queued decision.
                older = db.scalars(select(LiveReplyJob).where(LiveReplyJob.conversation_state_id == state.id,
                                                              LiveReplyJob.status == "queued")).all()
                ids, started = [], utcnow()
                for old in older:
                    ids.extend(old.input_ids)
                    started = min(started, old.created_at)
                    old.status, old.error_code, old.completed_at = "cancelled", "merged_into_newer_turn", utcnow()
                config, _ = reply_policy(db, state.inbox_binding_id)
                wait = max(0, min(5, float(config.get("merge_wait_seconds", 2))))
                maximum = max(wait, min(5, float(config.get("merge_max_seconds", 5))))
                due = min(dt(utcnow()) + timedelta(seconds=wait), dt(started) + timedelta(seconds=maximum))
                db.add(LiveReplyJob(conversation_state_id=state.id, trigger_message_id=message.id,
                    engine_version=state.ai_engine_version, engine_release_id=state.ai_engine_release_id,
                    input_ids=list(dict.fromkeys([*ids, message.chatwoot_message_id])), due_at=iso(due), created_at=started))
    if event.event in ("message_created", "message_updated"):
        if out:
            if message and existing_message:
                if "attachments" not in payload:
                    message.attachments = previous_media
                if not payload.get("created_at"):
                    message.created_at = previous_time
            apply_receipt(db, out, payload.get("status") or out.status)
    from app.automation_shadow import observe_webhook
    observe_webhook(db, event=event, mirrored=(state, ctx))
    event.status, event.processed_at = "live_observed", utcnow()
    db.commit()


def snapshot(client, remote_id):
    remote = payload_dict(client.get_conversation(remote_id))
    def verified_labels(response):
        value = response.get("payload") if isinstance(response, dict) else response
        if not isinstance(value, list) or any(not isinstance(x, str) for x in value):
            raise ReplyBlocked("remote_labels_unknown")
        return value
    labels = verified_labels(client.get_conversation_labels(remote_id))
    messages, _ = unpack_messages(client.get_messages(remote_id))
    sender = (remote.get("meta") or {}).get("sender") or {}
    contact_id = sender.get("id") or (remote.get("contact_inbox") or {}).get("contact_id")
    if not contact_id:
        raise ReplyBlocked("remote_contact_unknown")
    contact_labels = verified_labels(client.get_contact_labels(int(contact_id)))
    messages = sorted(messages, key=lambda m: int(m.get("id") or 0))
    return {"remote": remote, "labels": labels, "messages": messages, "contact_labels": contact_labels}


def guard(db, job, snap, expected=None):
    policy = assert_armed(db)
    db.expire_all()
    state = db.get(ConversationState, job.conversation_state_id)
    db.refresh(job)
    if job.status != "processing":
        raise ReplyBlocked("reply_job_not_active")
    if (job.engine_version != state.ai_engine_version
            or job.engine_release_id != state.ai_engine_release_id):
        raise ReplyBlocked("engine_version_changed")
    if job.engine_version == "v2":
        from app.reception_v2.runtime import ENGINE_RELEASE_ID
        if job.engine_release_id != ENGINE_RELEASE_ID:
            raise ReplyBlocked("engine_release_unavailable")
    remote, labels, messages = snap["remote"], snap["labels"], snap["messages"]
    connection = db.scalar(select(ChatwootConnection).where(ChatwootConnection.tenant_id == state.tenant_id))
    if (connection.account_id != settings.live_reply_account_id or state.inbox.chatwoot_inbox_id != settings.live_reply_inbox_id
            or remote.get("inbox_id") != settings.live_reply_inbox_id
            or remote.get("id") != state.chatwoot_conversation_id):
        raise ReplyBlocked("live_scope_mismatch")
    if not reception_conversation_allowed(db, state.chatwoot_conversation_id):
        raise ReplyBlocked("test_conversation_required")
    observe_ai_label(state, labels, "chatwoot_live")
    state.labels, state.can_reply = labels, remote.get("can_reply") is True
    if state.contact:
        state.contact.labels = snap["contact_labels"]
    if not has_ai_label(labels):
        state.effective_ai_state = "AI_PAUSED_CONVERSATION"
        state.effective_state_reason = "ai_opt_in_required"
        db.commit()
        raise ReplyBlocked("ai_opt_in_required")
    state.effective_ai_state, state.effective_state_reason = compute_state(
        db.get(Tenant, state.tenant_id), state.inbox, labels, snap["contact_labels"], state.can_reply,
        state.ai_mode, state.ai_sync_status, state.ai_label_present)
    db.commit()
    if state.effective_ai_state != "AI_ACTIVE":
        raise ReplyBlocked(state.effective_state_reason)
    if set(labels + snap["contact_labels"]) & blocking_labels(db):
        raise ReplyBlocked("human_or_blocking_label")
    if db.scalar(select(HandoffTask.id).where(HandoffTask.conversation_state_id == state.id,
                                            HandoffTask.status.in_(["pending", "claimed"]))):
        raise ReplyBlocked("human_handoff_active")
    config, version = reply_policy(db, state.inbox_binding_id)
    if not config.get("enabled") or not setting_value(db, "ai_adapter", {}).get("enabled", True):
        raise ReplyBlocked("reply_policy_disabled")
    incoming = [m for m in messages if message_direction(m.get("message_type")) == "incoming" and not m.get("private")
                and not (m.get("content_attributes") or {}).get("external_echo")]
    if not incoming:
        raise ReplyBlocked("automatic_window_unknown")
    latest = incoming[-1]
    if not latest.get("created_at"):
        raise ReplyBlocked("automatic_window_unknown")
    at = message_timestamp(latest.get("created_at"))
    trigger = db.get(MessageEvent, job.trigger_message_id)
    if latest["id"] != trigger.chatwoot_message_id:
        raise ReplyBlocked("new_customer_message")
    own_ids = set(db.scalars(select(OutboundMessage.chatwoot_message_id).where(
        OutboundMessage.conversation_state_id == state.id, OutboundMessage.chatwoot_message_id.is_not(None))).all())
    if any(message_direction(m.get("message_type")) == "outgoing" and not m.get("private")
           and m.get("id") not in own_ids and int(m.get("id") or 0) > latest["id"] for m in messages):
        raise ReplyBlocked("human_already_replied")
    if dt(utcnow()) >= dt(at) + timedelta(hours=23, minutes=55):
        raise ReplyBlocked("automatic_window_closed")
    if not fresh(at) or dt(at) < dt(policy["armed_at"]):
        raise ReplyBlocked("historical_or_stale_trigger")
    if db.scalar(select(OutboundMessage.id).where(OutboundMessage.conversation_state_id == state.id,
                                                OutboundMessage.status == "submission_unknown")):
        raise ReplyBlocked("submission_unknown_reconcile_required")
    fingerprint = (tuple(sorted(labels)), tuple(sorted(snap["contact_labels"])), state.version, version,
                   (remote.get("meta") or {}).get("assignee", {}).get("id") if (remote.get("meta") or {}).get("assignee") else None,
                   policy["armed_at"])
    if expected is not None and fingerprint != expected:
        raise ReplyBlocked("control_changed_during_generation")
    db.commit()
    return state, fingerprint


def pending_capture_guard(db, job, snap):
    """Verify a contact reply after AI has already handed the conversation to a person."""
    policy = assert_armed(db)
    db.expire_all()
    state = db.get(ConversationState, job.conversation_state_id)
    remote, messages = snap["remote"], snap["messages"]
    connection = db.scalar(select(ChatwootConnection).where(ChatwootConnection.tenant_id == state.tenant_id))
    if (connection.account_id != settings.live_reply_account_id or state.inbox.chatwoot_inbox_id != settings.live_reply_inbox_id
            or remote.get("inbox_id") != settings.live_reply_inbox_id or remote.get("id") != state.chatwoot_conversation_id):
        raise ReplyBlocked("live_scope_mismatch")
    if not reception_conversation_allowed(db, state.chatwoot_conversation_id):
        raise ReplyBlocked("test_conversation_required")
    incoming = [m for m in messages if message_direction(m.get("message_type")) == "incoming" and not m.get("private")
                and not (m.get("content_attributes") or {}).get("external_echo")]
    trigger = db.get(MessageEvent, job.trigger_message_id)
    if not incoming or incoming[-1].get("id") != trigger.chatwoot_message_id:
        raise ReplyBlocked("new_customer_message")
    at = incoming[-1].get("created_at")
    if not at or not fresh(at) or dt(message_timestamp(at)) < dt(policy["armed_at"]):
        raise ReplyBlocked("historical_or_stale_trigger")
    capture = db.scalar(select(LeadCaptureState).where(
        LeadCaptureState.conversation_state_id == state.id
    ))
    if not capture or capture.status != "asked":
        raise ReplyBlocked("lead_capture_not_pending")
    return state, capture


def safe_decision_json(decision):
    """Do not duplicate customer contact values in operational traces."""
    value = asdict(decision)
    value["contact_values"] = {
        kind: "[captured]" for kind in (value.get("contact_values") or {})
    }
    return value


def live_materials(db, keys, route, tenant_id, *, snapshot=None):
    allowed = {a.asset_key for a in catalog_assets(db, tenant_id) if a.metadata_json.get("live_approved") is True}
    if set(keys) - allowed:
        raise ReplyBlocked("material_not_approved_for_live")
    infos = resolve_materials(db, keys, route, tenant_id, snapshot=snapshot)
    for info in infos:
        media = db.get(StoredMedia, info["media_id"])
        asset = by_media(db, media, asset_key=info["asset_key"])
        if not material_live_approved(db, asset, media, expected_hash=info["media_hash"]):
            raise ReplyBlocked("material_not_approved_for_live")
    return infos


def previously_sent(db, state, info):
    query = select(OutboundMessage).join(ConversationState).where(
        ConversationState.tenant_id == state.tenant_id, OutboundMessage.media_id.is_not(None),
        OutboundMessage.status.in_(["submission_unknown", "submitted", "sent", "delivered", "read"]))
    query = query.where(ConversationState.contact_id == state.contact_id) if state.contact_id else query.where(ConversationState.id == state.id)
    assets = {a.metadata_json.get("stored_media_id"): a for a in catalog_assets(db, state.tenant_id)}
    for out in db.scalars(query).all():
        asset = assets.get(out.media_id)
        recorded_hash = ((out.content_attributes or {}).get("_delivery_item") or {}).get("asset_hash")
        if recorded_hash and recorded_hash == info["media_hash"]:
            return True
    return False


def terminal_handoff_guard(db, client, job, state):
    """Authorize only the current terminal handoff message at the send boundary."""
    assert_armed(db)
    snap = snapshot(client, state.chatwoot_conversation_id)
    db.expire_all()
    job = db.get(LiveReplyJob, job.id)
    state = db.get(ConversationState, state.id)
    remote, labels, messages = snap["remote"], snap["labels"], snap["messages"]
    _, handoff_label = _capture_labels(db)
    if not job or job.status != "processing":
        raise ReplyBlocked("terminal_handoff_job_not_active")
    if (
        remote.get("id") != state.chatwoot_conversation_id
        or remote.get("inbox_id") != settings.live_reply_inbox_id
        or state.inbox.chatwoot_inbox_id != settings.live_reply_inbox_id
    ):
        raise ReplyBlocked("live_scope_mismatch")
    if not reception_conversation_allowed(db, state.chatwoot_conversation_id):
        raise ReplyBlocked("test_conversation_required")
    if remote.get("can_reply") is not True:
        raise ReplyBlocked("can_reply_false")
    if not has_ai_label(labels):
        raise ReplyBlocked("ai_opt_in_required")
    if handoff_label not in labels:
        raise ReplyBlocked("terminal_handoff_label_missing")
    forbidden = {"AI关闭", "拒绝联系", "黑名单"}
    if forbidden & set(labels + snap["contact_labels"]):
        raise ReplyBlocked("human_or_contact_block")
    if not db.scalar(select(HandoffTask.id).where(
        HandoffTask.conversation_state_id == state.id,
        HandoffTask.status.in_(["pending", "claimed"]),
    )):
        raise ReplyBlocked("terminal_handoff_task_missing")
    incoming = [
        item for item in messages
        if message_direction(item.get("message_type")) == "incoming"
        and not item.get("private")
        and not (item.get("content_attributes") or {}).get("external_echo")
    ]
    trigger = db.get(MessageEvent, job.trigger_message_id)
    if not incoming or not trigger or incoming[-1].get("id") != trigger.chatwoot_message_id:
        raise ReplyBlocked("new_customer_message")
    at = incoming[-1].get("created_at")
    if not at or dt(utcnow()) >= dt(message_timestamp(at)) + timedelta(hours=23, minutes=55):
        raise ReplyBlocked("automatic_window_closed")
    own_ids = set(db.scalars(select(OutboundMessage.chatwoot_message_id).where(
        OutboundMessage.conversation_state_id == state.id,
        OutboundMessage.chatwoot_message_id.is_not(None),
    )).all())
    if any(
        message_direction(item.get("message_type")) == "outgoing"
        and not item.get("private")
        and item.get("id") not in own_ids
        and int(item.get("id") or 0) > int(incoming[-1].get("id") or 0)
        for item in messages
    ):
        raise ReplyBlocked("human_already_replied")
    state.labels = labels
    state.can_reply = True
    state.ai_label_present = True
    state.updated_at = utcnow()
    db.commit()
    return state


def submit_part(db, client, job, state, run, key, content="", info=None, quick_replies=None,
                *, terminal_handoff=False, delivery_groups=None, delivery_item_id="", opening_delivery=None):
    if terminal_handoff:
        state = terminal_handoff_guard(db, client, job, state)
    else:
        state, _ = guard(db, job, snapshot(client, state.chatwoot_conversation_id))
    existing = db.scalar(select(OutboundMessage).where(OutboundMessage.idempotency_key == key))
    if existing:
        raise ReplyBlocked("submission_already_attempted")
    media = db.get(StoredMedia, info["media_id"]) if info else None
    if terminal_handoff and delivery_groups is None:
        terminal_journey = journey_for(db, state)
        spec = route_snapshot_from_values(terminal_journey.route_variant, terminal_journey.slots)
        delivery_groups = list((job.decision or {}).get("covered_content_groups") or [])
        if info and spec is not None:
            delivery_groups = [name for name, group in spec.get("groups", {}).items()
                               if info.get("asset_key") in (group.get("assets") or [])]
        delivery_item_id = delivery_item_id or key
    attributes = {
        "items": [{"title": title, "value": title} for title in quick_replies]
    } if quick_replies else {}
    if opening_delivery is not None:
        attributes["_opening_delivery"] = deepcopy(opening_delivery)
    out = OutboundMessage(conversation_state_id=state.id, ai_run_id=run.id, idempotency_key=key,
        source_type="ai", source_id=job.id, content=content, status="submission_unknown",
        content_type=media.media_type if media else "input_select" if quick_replies else "text",
        content_attributes=attributes, media_id=media.id if media else None)
    if delivery_groups is not None:
        from app.delivery_tracking import attach_delivery_item, find_existing_delivery
        delivery_journey = journey_for(db, state)
        try:
            attach_delivery_item(
                db, out, delivery_journey, delivery_groups,
                asset_key=str((info or {}).get("asset_key") or ""), item_id=delivery_item_id or key,
            )
        except ValueError as exc:
            raise ReplyBlocked(str(exc)) from exc
        # Terminal acknowledgements keep their guarded, non-retryable send path.
        duplicate = None if terminal_handoff or opening_delivery is not None else find_existing_delivery(db, out, delivery_journey)
        if duplicate is not None:
            return duplicate
    db.add(out)
    db.commit()  # Never hold a write lock across HTTP; persist uncertainty before sending.
    try:
        if media:
            result = payload_dict(client.create_attachment_message(
                state.chatwoot_conversation_id, content, media.storage_path, media.mime_type
            ))
        elif quick_replies:
            result = payload_dict(client.create_input_select_message(
                state.chatwoot_conversation_id, content, quick_replies
            ))
        else:
            result = payload_dict(client.create_text_message(state.chatwoot_conversation_id, content))
        if not isinstance(result.get("id"), int):
            raise ReplyBlocked("submission_unknown_reconcile_required")
        out.chatwoot_message_id, out.status, out.submitted_at = result["id"], receipt_status(result.get("status"), "submitted"), utcnow()
        message = db.scalar(select(MessageEvent).where(MessageEvent.conversation_state_id == state.id,
                                                      MessageEvent.chatwoot_message_id == result["id"]))
        if not message:
            message = MessageEvent(conversation_state_id=state.id, chatwoot_message_id=result["id"], direction="outgoing")
            db.add(message)
        message.content, message.status, message.attribution = content, receipt_status(message.status, out.status), "ai"
        out.status = message.status
        from app.delivery_tracking import record_delivery_progress
        record_delivery_progress(db, out)
        message.attachments = result.get("attachments") or []
        message.content_type = media.media_type if media else "input_select" if quick_replies else "text"
        message.content_attributes = {key: value for key, value in out.content_attributes.items() if not key.startswith("_")}
        state.last_message = content or f"[{media.media_type}]" if media else content
        state.updated_at = utcnow()
        db.commit()
    except Exception:
        out.error_code = "submission_unknown_reconcile_required"
        db.commit()
        raise ReplyBlocked("submission_unknown_reconcile_required")
    if out.status == "failed":
        pause_failed_delivery(db, out)
        db.commit()
        raise ReplyBlocked("channel_send_failed")
    return out


def _capture_labels(db):
    mappings = setting_value(db, "label_mappings", {})
    lead = next(iter(mappings.get("lead_labels") or ["已留资"]), "已留资")
    handoff = next(iter(mappings.get("handoff_labels") or ["人工接管"]), "人工接管")
    return lead, handoff


def _handoff_summary(journey, reason: str, customer_text: str = "") -> str:
    slots = dict(getattr(journey, "slots", {}) or {}) if journey else {}
    route = str(getattr(journey, "route_variant", "") or "未選線路") if journey else "未選線路"
    details = [f"原因：{reason}", f"線路：{route}"]
    if slots.get("party_size") not in (None, ""):
        details.append(f"人數：{slots['party_size']}")
    if slots.get("departure_window") not in (None, ""):
        details.append(f"出發時間：{slots['departure_window']}")
    preferences = slots.get('_v2_state') or {}
    if preferences.get('contact_at'):
        details.append(f"約定聯繫時間（此前請勿主動聯繫）：{preferences['contact_at']}")
    if preferences.get('refused_channels'):
        details.append('拒絕渠道：' + '、'.join(preferences['refused_channels']))
    if preferences.get('proactive_opt_out'):
        details.append('客戶已拒絕主動聯繫')
    customer_text = " ".join(str(customer_text or "").split())[:240]
    if customer_text:
        details.append(f"客戶本輪原話：{customer_text}")
    sent = list(getattr(journey, "sent_groups", []) or []) if journey else []
    if sent:
        details.append(f"已提供內容：{'、'.join(sent[-8:])}")
    return "｜".join(details)


def prepare_captured_handoff(db, state, capture, journey=None, customer_text: str = "", pending_materials=None):
    """Persist the stop condition before any acknowledgement is submitted."""
    if pending_materials is None and state.ai_engine_version == 'v2':
        # Recovery/history paths do not send an unsolicited attachment. Preserve
        # the outstanding obligation in the actual consultant task instead.
        from app.route_packages import ROUTES
        group = ROUTES.get(getattr(journey, 'route_variant', ''), {}).get('policies', {}).get('post_capture_material_group')
        pending_materials = [group] if group else []
    ensure_handoff(
        db, state, "lead_captured",
        _handoff_summary(journey, "客戶已主動提供聯絡方式", customer_text)
        + ('｜待補資料：' + '、'.join(pending_materials) if pending_materials else ''),
    )
    state.ai_mode = "disabled"
    state.ai_mode_source = "lead_capture"
    state.ai_mode_updated_at = utcnow()
    capture.label_sync_status = "pending"
    capture.updated_at = utcnow()
    db.commit()


def sync_captured_labels(db, client, state, capture, remote_labels=None, *, finalize=False):
    """Prepare or finalize the lead handoff label sequence."""
    lead_label, handoff_label = _capture_labels(db)
    if remote_labels is None:
        remote_labels = string_payload(client.get_conversation_labels(state.chatwoot_conversation_id))
    if not finalize and not has_ai_label(remote_labels):
        capture.label_sync_status = "failed"
        db.commit()
        return "failed"
    desired = [
        label for label in remote_labels
        if not finalize or str(label).strip().casefold() != "ai"
    ]
    desired = list(dict.fromkeys([*desired, lead_label, handoff_label]))
    status = "synced"
    try:
        catalog = normalize_collection(client.list_labels())
        existing = {str(item.get("title") or "").strip().casefold() for item in catalog}
        definitions = (
            (lead_label, "客户已提供可联系信息", "#22C55E"),
            (handoff_label, "需要真人客服接续处理", "#DC2626"),
        )
        for title, description, color in definitions:
            if title.casefold() not in existing:
                client.create_label(title, description, color, True)
                existing.add(title.casefold())
        client.set_conversation_labels(state.chatwoot_conversation_id, desired)
        verified = string_payload(client.get_conversation_labels(state.chatwoot_conversation_id))
        if (
            not {lead_label, handoff_label}.issubset(set(verified))
            or (finalize and has_ai_label(verified))
            or (not finalize and not has_ai_label(verified))
        ):
            raise ReplyBlocked("lead_capture_label_verification_failed")
        state.labels = verified
        if finalize:
            observe_ai_label(state, verified, "lead_capture")
            capture.label_sync_status = "synced"
        else:
            state.ai_label_present = True
            state.ai_sync_status = "synced"
    except (ChatwootError, ReplyBlocked):
        state.ai_sync_status = "conflict"
        capture.label_sync_status = "failed"
        status = "failed"
        if finalize:
            create_notification(
                db,
                "ai.handoff_label_finalize_failed",
                "人工接管标签需要检查",
                f"会话 #{state.chatwoot_conversation_id} 已由人工任务阻断，但 ai 标签移除失败，请人工检查。",
                state.chatwoot_conversation_id,
            )
    state.updated_at = capture.updated_at = utcnow()
    db.commit()
    return status


def sync_handoff_label(db, client, state, remote_labels=None, *, finalize=False):
    _, handoff_label = _capture_labels(db)
    if remote_labels is None:
        remote_labels = string_payload(client.get_conversation_labels(state.chatwoot_conversation_id))
    if not finalize and not has_ai_label(remote_labels):
        return "failed"
    desired = [
        label for label in remote_labels
        if not finalize or str(label).strip().casefold() != "ai"
    ]
    desired = list(dict.fromkeys([*desired, handoff_label]))
    status = "synced"
    try:
        catalog = normalize_collection(client.list_labels())
        existing = {str(item.get("title") or "").strip().casefold() for item in catalog}
        if handoff_label.casefold() not in existing:
            client.create_label(handoff_label, "需要真人客服接续处理", "#DC2626", True)
        client.set_conversation_labels(state.chatwoot_conversation_id, desired)
        verified = string_payload(client.get_conversation_labels(state.chatwoot_conversation_id))
        if (
            handoff_label not in verified
            or (finalize and has_ai_label(verified))
            or (not finalize and not has_ai_label(verified))
        ):
            raise ReplyBlocked("handoff_label_verification_failed")
        state.labels = verified
        if finalize:
            observe_ai_label(state, verified, "handoff")
        else:
            state.ai_label_present = True
            state.ai_sync_status = "synced"
    except (ChatwootError, ReplyBlocked):
        state.ai_sync_status = "conflict"
        status = "failed"
        if finalize:
            create_notification(
                db,
                "ai.handoff_label_finalize_failed",
                "人工接管标签需要检查",
                f"会话 #{state.chatwoot_conversation_id} 已由人工任务阻断，但 ai 标签移除失败，请人工检查。",
                state.chatwoot_conversation_id,
            )
    state.updated_at = utcnow()
    db.commit()
    return status


OPENING_PLAN_FORMAT = "opening-v1"


def _opening_plan_digest(plan):
    envelope = [{key: value for key, value in item.items()
                 if key not in {"item_id", "status", "outbound_id"}} for item in plan]
    return sha256(json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _freeze_opening_reply(db, job, state, decision):
    from app.opening_messages import OpeningItem, delivery_items, opening_media_info

    interval = decision.opening_interval_seconds
    if not isinstance(interval, int) or interval < 0:
        raise ReplyBlocked("opening_interval_invalid")
    configured = delivery_items(decision.opening_items, decision.opening_messages or [decision.reply])
    plan = []
    for index, raw in enumerate(configured):
        item = OpeningItem.model_validate(deepcopy(raw)).model_dump()
        info = opening_media_info(db, item, state.tenant_id) if item["content_type"] != "text" else None
        if info:
            item["media_hash"] = info["media_hash"]
        plan.append({
            "plan_version": OPENING_PLAN_FORMAT, "kind": "media" if info else "text",
            "content": item["content"], "material": info, "opening_item": item,
            "interval_seconds": interval if index else 0,
            "quick_replies": list(decision.reply_options) if index == len(configured) - 1 else [],
            "skip_previously_sent": bool(info and previously_sent(db, state, info)),
            "status": "pending",
        })
    if not plan or plan[-1]["kind"] != "text" or not plan[-1]["quick_replies"]:
        raise ReplyBlocked("opening_plan_invalid")
    digest = _opening_plan_digest(plan)
    for index, item in enumerate(plan):
        item["item_id"] = f"{digest}:{index}"
    job.trace = {**job.trace, "delivery_plan_format": OPENING_PLAN_FORMAT,
                 "delivery_plan_digest": digest, "delivery_plan": plan,
                 "quick_reply_count": len(decision.reply_options), "opening_message_count": len(plan)}


def _persisted_reply_key(job, item):
    info = item.get("material") if item.get("kind") == "media" else None
    if item.get("kind") not in {"text", "media"} or (item["kind"] == "media" and not info):
        raise ReplyBlocked("delivery_item_identity_mismatch")
    if job.trace.get("delivery_plan_format") == OPENING_PLAN_FORMAT:
        return f"live:{job.id}:opening-v1:{item['item_id']}"
    if (job.decision or {}).get('v2_delivery_sections'):
        return f"live:{job.id}:v2-section:{item['item_id']}"
    return (f"live:{job.id}:image:{info['media_hash']}" if info else
            f"live:{job.id}:follow-up" if item.get("is_follow_up") else f"live:{job.id}:text")


def _persisted_reply_attempts(db, job):
    """Fail closed on stale or foreign receipts before sending any remainder."""
    from hashlib import sha256

    plan = job.trace.get("delivery_plan") or []
    if job.trace.get("delivery_plan_format") not in {None, OPENING_PLAN_FORMAT}:
        raise ReplyBlocked("delivery_item_identity_mismatch")
    opening = job.trace.get("delivery_plan_format") == OPENING_PLAN_FORMAT
    if opening:
        digest = _opening_plan_digest(plan)
        if (not plan or digest != job.trace.get("delivery_plan_digest")
                or plan[-1].get("kind") != "text" or not plan[-1].get("quick_replies")
                or any(item.get("quick_replies") for item in plan[:-1])
                or any(item.get("item_id") != f"{digest}:{index}"
                       or item.get("plan_version") != OPENING_PLAN_FORMAT
                       for index, item in enumerate(plan))):
            raise ReplyBlocked("delivery_item_identity_mismatch")
    by_key = {_persisted_reply_key(job, item): item for item in plan}
    ids = [item.get("item_id") for item in plan]
    if len(by_key) != len(plan) or not all(ids) or len(set(ids)) != len(ids):
        raise ReplyBlocked("delivery_item_identity_mismatch")
    attempts = db.scalars(select(OutboundMessage).where(
        OutboundMessage.idempotency_key.like(f"live:{job.id}:%"),
    )).all()
    for out in attempts:
        item = by_key.get(out.idempotency_key)
        info = (item or {}).get("material") if (item or {}).get("kind") == "media" else None
        metadata = (out.content_attributes or {}).get("_delivery_item") or {}
        content = (item or {}).get("content", "")
        options = item.get("quick_replies") if opening and item else None
        opening_metadata = (out.content_attributes or {}).get("_opening_delivery") or {}
        if (not item or out.conversation_state_id != job.conversation_state_id
                or out.source_type != "ai" or out.source_id != job.id
                or out.content != content or out.media_id != (info or {}).get("media_id")
                or out.content_type != ((info or {}).get("content_type") if info else "input_select" if options else "text")
                or metadata.get("item_id") != item["item_id"]
                or metadata.get("content_hash") != sha256(content.encode()).hexdigest()
                or metadata.get("media_id") != out.media_id
                or metadata.get("route") != (job.decision or {}).get("route_variant", "")
                or set(metadata.get("group_keys") or []) != set(
                    [item.get('group_key')] if item and (job.decision or {}).get('v2_delivery_sections')
                    else (job.decision or {}).get("covered_content_groups") or [])
                or metadata.get("asset_key", "") != ((info or {}).get("asset_key") or "")
                or (info and not opening and metadata.get("asset_hash") != info.get("media_hash"))
                or (opening and (
                    opening_metadata != {"plan_digest": job.trace["delivery_plan_digest"],
                                         "item_id": item["item_id"],
                                         "media_hash": (info or {}).get("media_hash", "")}
                    or (out.content_attributes or {}).get("items", []) != [
                        {"title": title, "value": title} for title in options or []]
                ))):
            raise ReplyBlocked("delivery_item_identity_mismatch")
    return {out.idempotency_key: out for out in attempts}


def _execute_persisted_reply(db, client, job, state, run, fingerprint=None):
    """Execute the same immutable envelope for first submission and restart recovery."""
    plan = job.trace.get("delivery_plan") or []
    groups = (job.decision or {}).get("covered_content_groups") or []
    attempts = _persisted_reply_attempts(db, job)
    opening = job.trace.get("delivery_plan_format") == OPENING_PLAN_FORMAT
    for index, item in enumerate(plan):
        if opening and item.get("skip_previously_sent"):
            job.trace = {**job.trace, "delivery_plan": [
                {**part, "status": "previously_sent"} if part["item_id"] == item["item_id"] else part
                for part in job.trace["delivery_plan"]
            ]}
            db.commit()
            continue
        info = item.get("material") if item["kind"] == "media" else None
        key = _persisted_reply_key(job, item)
        existing = attempts.get(key)
        if existing:
            if existing.status not in {"submitted", "sent", "delivered", "read"}:
                raise ReplyBlocked("channel_send_failed" if existing.status == "failed" else "submission_unknown_reconcile_required")
            submitted = existing
        else:
            if index and item.get("interval_seconds"):
                time.sleep(float(item["interval_seconds"]))
            state, _ = guard(db, job, snapshot(client, state.chatwoot_conversation_id), fingerprint)
            if info and opening:
                from app.opening_messages import opening_media_info

                validated = opening_media_info(db, item["opening_item"], state.tenant_id)
                if validated != info:
                    raise ReplyBlocked("delivery_item_identity_mismatch")
            elif info:
                media = db.get(StoredMedia, info.get("media_id"))
                asset = by_media(db, media, asset_key=info.get("asset_key"))
                if (not media or media.tenant_id != state.tenant_id
                        or not material_live_approved(db, asset, media, expected_hash=info.get("media_hash"))):
                    raise ReplyBlocked("material_not_approved_for_live")
                material_info(db, info, (job.decision or {}).get("route_variant", ""), state.tenant_id)
            submitted = submit_part(
                db, client, job, state, run, key, item.get("content", ""), info=info,
                delivery_groups=[item['group_key']] if (job.decision or {}).get('v2_delivery_sections') else groups,
                delivery_item_id=item["item_id"],
                quick_replies=item.get("quick_replies") if opening else None,
                opening_delivery={"plan_digest": job.trace["delivery_plan_digest"],
                                  "item_id": item["item_id"],
                                  "media_hash": (info or {}).get("media_hash", "")} if opening else None,
            )
        job.trace = {**job.trace, "delivery_plan": [
            {**part, "status": submitted.status, "outbound_id": submitted.id}
            if part["item_id"] == item["item_id"] else part
            for part in job.trace["delivery_plan"]
        ]}
        contact_part = item.get("is_follow_up") or (
            item["kind"] == "text"
            and not any(part.get("is_follow_up") for part in plan)
        )
        if job.trace.get("contact_request_in_plan") and contact_part:
            capture = db.scalar(select(LeadCaptureState).where(LeadCaptureState.conversation_state_id == state.id))
            if capture and capture.status == "not_started":
                mark_requested(capture)
        db.commit()
    if opening:
        job.trace = {**job.trace, "outbound": True}


def process_job(job_id):
    client = None
    with SessionLocal() as db:
        job = db.get(LiveReplyJob, job_id)
        if not job or job.status != "queued":
            return
        claimed = db.execute(update(LiveReplyJob).where(LiveReplyJob.id == job_id, LiveReplyJob.status == "queued").values(status="processing"))
        db.commit()
        if not claimed.rowcount:
            return
        try:
            assert_armed(db)
            state = db.get(ConversationState, job.conversation_state_id)
            if (job.engine_version != state.ai_engine_version
                    or job.engine_release_id != state.ai_engine_release_id):
                raise ReplyBlocked("engine_version_changed")
            if job.engine_version == "v2":
                from app.reception_v2.runtime import ENGINE_RELEASE_ID
                if job.engine_release_id != ENGINE_RELEASE_ID:
                    raise ReplyBlocked("engine_release_unavailable")
            connection = db.scalar(select(ChatwootConnection).where(ChatwootConnection.tenant_id == state.tenant_id))
            db.commit()
            client = client_for(connection)
            snap = snapshot(client, state.chatwoot_conversation_id)
            if not has_ai_label(snap["labels"]):
                raise ReplyBlocked("ai_opt_in_required")
            trigger = db.get(MessageEvent, job.trigger_message_id)
            pending_capture = db.scalar(select(LeadCaptureState).where(
                LeadCaptureState.conversation_state_id == state.id,
                LeadCaptureState.status == "asked",
            ))
            if pending_capture and state.effective_ai_state != "AI_ACTIVE":
                state, capture = pending_capture_guard(db, job, snap)
                run = db.scalar(select(AiRun).where(AiRun.trigger_message_id == job.trigger_message_id))
                if not run:
                    run = AiRun(conversation_state_id=state.id, trigger_message_id=job.trigger_message_id)
                    db.add(run)
                    db.flush()
                inputs = [message for message in snap["messages"] if message.get("id") in job.input_ids]
                if not inputs:
                    complete(db, job, "no_action", "lead_capture_input_missing")
                    return
                text, current_attachments = input_payload(inputs)
                if _explicit_stop_request(text):
                    stop_customer_automation(db, state)
                    complete(db, job, "no_action", "customer_requested_stop")
                    return
                if not text:
                    complete(db, job, "no_action", "lead_capture_attachment_requires_human")
                    return
                history, history_trace = fetch_customer_context(client, snap["remote"], job.input_ids)
                context = {
                    "module": "lead_capture",
                    "engine_version": job.engine_version,
                    "source_message_id": job.trigger_message_id,
                    "source_message_ids": list(job.input_ids),
                    "customer_text": text,
                    "context_messages": history,
                    "context_complete": True,
                    "route_variant": "",
                    "memory": {},
                    "lead_capture": {
                        "status": capture.status,
                        "request_count": capture.request_count,
                        "captured_kinds": capture.captured_kinds or [],
                    },
                    "current_attachments": current_attachments,
                    "available_materials": [],
                    "reception_policy": effective_reception_policy(db),
                }
                job.trace = {**job.trace, **history_trace}
                db.commit()
                decision, calls, _, trace = generate_decision(context)
                decision, capture, _, pending_matches = apply_model_policy(
                    db, state, decision, history, text
                )
                prior_calls = job.trace.get("model_calls", [])
                job.decision = safe_decision_json(decision)
                job.trace = {
                    **job.trace,
                    **trace,
                    "model_cycles": int(job.trace.get("model_cycles", 0)) + 1,
                    "model_calls": prior_calls + calls,
                    "request_count": len(prior_calls) + len(calls),
                    "model_ms": trace.get("model_ms", 0) + sum(item.get("duration_ms", 0) for item in prior_calls),
                    "lead_capture": "captured" if pending_matches else capture.status,
                    "outbound": False,
                }
                run.action = "handoff" if pending_matches else "no_action"
                db.commit()
                if not pending_matches:
                    complete(db, job, "no_action", "contact_not_provided")
                    return
                capture = record_capture(db, state, pending_matches, job.trigger_message_id)
                prepare_captured_handoff(db, state, capture, journey_for(db, state), text)
                sync_captured_labels(db, client, state, capture, snap["labels"], finalize=True)
                complete(db, job, "handoff", "lead_captured")
                return
            state, fingerprint = guard(db, job, snap)
            run = db.scalar(select(AiRun).where(AiRun.trigger_message_id == job.trigger_message_id))
            if not run:
                run = AiRun(conversation_state_id=state.id, trigger_message_id=job.trigger_message_id)
                db.add(run)
                db.flush()
            if job.trace.get("resume_delivery_plan") and job.trace.get("delivery_plan"):
                _execute_persisted_reply(db, client, job, state, run, fingerprint)
                complete(db, job, "submitted")
                enroll_after_submitted_reply(
                    db, state, job, (job.decision or {}).get("route_variant", ""),
                    safety_flags=(job.decision or {}).get("safety_flags") or [],
                    deferred_follow_up=job.trace.get("deferred_follow_up"),
                )
                return
            messages = snap["messages"]
            inputs = [m for m in messages if m.get("id") in job.input_ids]
            if not inputs:
                complete(db, job, "skipped", "input_missing")
                return
            text, current_attachments = input_payload(inputs)
            if _explicit_stop_request(text):
                stop_customer_automation(db, state)
                complete(db, job, "no_action", "customer_requested_stop")
                return
            if not text and current_attachments:
                ensure_handoff(db, state, "attachment_requires_human", "客户附件需要顾问查看")
                complete(db, job, "handoff", "attachment_requires_human")
                return
            if not text:
                complete(db, job, "skipped", "empty_message")
                return
            db.commit()
            history, history_trace = fetch_customer_context(client, snap["remote"], job.input_ids)
            historical_capture = observe_history(db, state, history)
            if historical_capture.status == "captured":
                job.decision = {
                    "action": "handoff",
                    "intent": "contact",
                    "handoff_reason": "lead_already_captured",
                    "captured_kinds": historical_capture.captured_kinds,
                }
                job.trace = {**job.trace, **history_trace, "lead_capture": "captured", "outbound": False}
                run.action = "handoff"
                prepare_captured_handoff(db, state, historical_capture, journey_for(db, state), text)
                sync_captured_labels(
                    db, client, state, historical_capture, snap["labels"], finalize=True
                )
                complete(db, job, "handoff", "lead_already_captured")
                return
            journey = journey_for(db, state)
            if journey_context(journey).get("automatic_delivery_paused"):
                ensure_handoff(db, state, "delivery_history_requires_review",
                               "旧接待版本或发送记录无法确认，已暂停自动接待，请先核对历史进度。")
                db.commit()
                raise ReplyBlocked("delivery_history_requires_review")
            previous = db.scalar(select(LiveReplyJob).where(LiveReplyJob.conversation_state_id == state.id,
                LiveReplyJob.status == "submitted", LiveReplyJob.id < job.id).order_by(LiveReplyJob.id.desc()))
            route = journey.route_variant or (previous.decision.get("route_variant", "") if previous else "")
            previous_slots = previous.decision.get("slots", {}) if previous else {}
            previous_quotes = previous.decision.get("slot_evidence", {}) if previous else {}
            memory = {key: {"value": value, "quote": previous_quotes[key]} for key, value in previous_slots.items()
                      if previous_quotes.get(key) and any(previous_quotes[key] in item["content"] for item in history if item["direction"] == "incoming")}
            approved = {a.asset_key for a in catalog_assets(db, state.tenant_id) if a.metadata_json.get("live_approved") is True}
            context = {"module": "reply", "engine_version": job.engine_version, "customer_text": text, "context_messages": history, "context_complete": True, "route_variant": route, "memory": memory,
                       "source_message_id": job.trigger_message_id, "source_message_ids": list(job.input_ids),
                       "journey": journey_context(journey), "route_playbook": playbook_prompt(),
                       "now": utcnow(),
                       "last_customer_at": next((item.get('created_at') for item in reversed(history) if item.get('direction') == 'incoming'), None),
                       "reception_policy": effective_reception_policy(db),
                       "current_attachments": current_attachments,
                       "lead_capture": {"status": historical_capture.status, "request_count": historical_capture.request_count,
                                        "captured_kinds": historical_capture.captured_kinds or []},
                       "available_materials": [m for m in candidate_materials(db, state.tenant_id) if m["key"] in approved]}
            context = enrich_context_with_web_knowledge(
                db, state.tenant_id, context, environment="live"
            )
            db.commit()
            job.trace = {**job.trace, **history_trace}
            db.commit()
            decision, calls, _, trace = generate_decision(context)
            decision, journey = prepare_route_reply(db, state, decision, job.trigger_message_id)
            decision, capture, contact_requested, current_contacts = apply_model_policy(
                db, state, decision, history, text
            )
            prior_calls = job.trace.get("model_calls", [])
            job.decision, job.trace, run.action = safe_decision_json(decision), {**job.trace, **trace,
                "model_cycles": int(job.trace.get("model_cycles", 0)) + 1,
                "model_calls": prior_calls + calls, "request_count": len(prior_calls) + len(calls),
                "model_ms": trace.get("model_ms", 0) + sum(item.get("duration_ms", 0) for item in prior_calls),
                "lead_capture": "captured" if current_contacts else "planned" if contact_requested else capture.status}, decision.action
            db.commit()
            if current_contacts:
                snap = snapshot(client, state.chatwoot_conversation_id)
                state, _ = guard(db, job, snap, fingerprint)
                materials = live_materials(
                    db, decision.material_keys, decision.route_variant, state.tenant_id
                )
                skipped = [
                    item for item in materials
                    if not decision.allow_material_resend and previously_sent(db, state, item)
                ]
                materials = [item for item in materials if item not in skipped][:1]
                capture = record_capture(db, state, current_contacts, job.trigger_message_id)
                run.action = "handoff"
                prepare_captured_handoff(db, state, capture, journey, text,
                    [flag.removeprefix('pending_material:') for flag in decision.safety_flags if flag.startswith('pending_material:')])
                capture = db.get(LeadCaptureState, capture.id)
                prepare_status = sync_captured_labels(db, client, state, capture, snap["labels"])
                if prepare_status != "synced":
                    sync_captured_labels(db, client, state, capture, finalize=True)
                    raise ReplyBlocked("lead_capture_label_sync_failed")
                finalize_status = "pending"
                try:
                    if decision.reply:
                        submit_part(
                            db,
                            client,
                            job,
                            state,
                            run,
                            f"live:{job.id}:lead-captured",
                            decision.reply,
                            terminal_handoff=True,
                        )
                    for info in materials:
                        material_info(db, info, decision.route_variant, state.tenant_id)
                        submit_part(
                            db, client, job, state, run,
                            f"live:{job.id}:lead-captured:image:{info['media_hash']}",
                            info=info,
                            terminal_handoff=True,
                        )
                finally:
                    finalize_status = sync_captured_labels(
                        db, client, state, capture, finalize=True
                    )
                for group_key in decision.covered_content_groups:
                    mark_group_delivered(journey, group_key)
                job.trace = {
                    **job.trace,
                    "outbound": True,
                    "label_prepare_status": prepare_status,
                    "label_sync_status": finalize_status,
                    "image_count": len(materials),
                    "duplicate_images_skipped": len(skipped),
                }
                complete(db, job, "handoff", "lead_captured")
                return
            if "stop_automation" in (decision.safety_flags or []):
                stop_customer_automation(db, state)
                complete(db, job, "no_action", "customer_requested_stop")
                return
            if decision.action == "no_action":
                complete(db, job, "no_action")
                return
            snap = snapshot(client, state.chatwoot_conversation_id)
            state, _ = guard(db, job, snap, fingerprint)
            if decision.action == "handoff":
                materials = live_materials(
                    db, decision.material_keys, decision.route_variant, state.tenant_id
                )
                skipped = [
                    item for item in materials
                    if not decision.allow_material_resend and previously_sent(db, state, item)
                ]
                materials = [item for item in materials if item not in skipped][:1]
                handoff_task = ensure_handoff(
                    db, state, decision.handoff_reason or "ai_handoff",
                    _handoff_summary(
                        journey,
                        decision.handoff_reason or "需要專項顧問接續",
                        text,
                    ) + ("\n核對項目：" + "；".join(trace.get("confirmation_questions") or trace.get("unanswered_questions") or [])
                         + "\n核驗問題：" + "；".join(trace.get("unsupported_claims") or [])
                         if decision.handoff_reason in {"knowledge_verification_required", "knowledge_confirmation_required"} else ""),
                )
                job.trace = {**job.trace, "handoff_task_id": handoff_task.id}
                contact_at = ((journey.slots or {}).get('_v2_state') or {}).get('contact_at')
                if decision.handoff_reason == 'customer_contact_outside_window' and contact_at:
                    handoff_task.sla_due_at = contact_at
                state.ai_mode = "disabled"
                state.ai_mode_source = "handoff"
                state.ai_mode_updated_at = utcnow()
                db.commit()
                prepare_status = sync_handoff_label(db, client, state, snap["labels"])
                if prepare_status != "synced":
                    sync_handoff_label(db, client, state, finalize=True)
                    raise ReplyBlocked("handoff_label_sync_failed")
                finalize_status = "pending"
                try:
                    if decision.reply:
                        submit_part(
                            db, client, job, state, run,
                            f"live:{job.id}:handoff", decision.reply,
                            terminal_handoff=True,
                        )
                    for info in materials:
                        material_info(db, info, decision.route_variant, state.tenant_id)
                        submit_part(
                            db, client, job, state, run,
                            f"live:{job.id}:handoff:image:{info['media_hash']}",
                            info=info,
                            terminal_handoff=True,
                        )
                finally:
                    finalize_status = sync_handoff_label(
                        db, client, state, finalize=True
                    )
                for group_key in decision.covered_content_groups:
                    mark_group_delivered(journey, group_key)
                job.trace = {
                    **job.trace,
                    "outbound": True,
                    "handoff_label_prepare_status": prepare_status,
                    "handoff_label_sync_status": finalize_status,
                    "image_count": len(materials),
                    "duplicate_images_skipped": len(skipped),
                }
                complete(db, job, "handoff", decision.handoff_reason)
                return
            if decision.reply_options:
                _freeze_opening_reply(db, job, state, decision)
                db.commit()
                _execute_persisted_reply(db, client, job, state, run, fingerprint)
                complete(db, job, "submitted")
                enroll_after_submitted_reply(
                    db, state, job, decision.route_variant, safety_flags=decision.safety_flags
                )
                return
            bound_route = route_snapshot_from_values(decision.route_variant, journey.slots)
            if job.engine_version == 'v2' and decision.v2_delivery_sections:
                materials = [material for section in decision.v2_delivery_sections
                    for material in live_materials(db, section['asset_keys'], decision.route_variant,
                                                   state.tenant_id, snapshot=bound_route)]
            else:
                materials = live_materials(db, decision.material_keys, decision.route_variant, state.tenant_id, snapshot=bound_route)
            resend = decision.allow_material_resend
            skipped = [m for m in materials if not resend and previously_sent(db, state, m)]
            materials = [m for m in materials if m not in skipped]
            deferred = (
                deferred_initial_follow_up(decision, journey.sent_groups or [], slots=journey.slots)
                if job.engine_version == "v1" else None
            )
            reply = (
                str(decision.reply_body or "").strip()
                if deferred else str(decision.reply or "").strip()
            )
            follow_up_question = str(decision.follow_up_question or "").strip() if not deferred else ""
            if follow_up_question:
                reply = str(decision.reply_body or reply).strip()
                if reply.endswith(follow_up_question):
                    reply = reply[:-len(follow_up_question)].rstrip()
            delivery_mode = _initial_delivery_mode(decision, bound_route)
            interval_seconds = int(
                (bound_route or {}).get(
                    "initial_delivery_interval_seconds",
                    DEFAULT_INITIAL_DELIVERY_INTERVAL_SECONDS,
                )
            )
            parts = ordered_delivery_parts(
                reply, materials, delivery_mode,
                text_segments=decision.reply_segments,
                plan_id=f"live:{job.id}",
                content_group_key=decision.content_group_key or "",
                interval_seconds=interval_seconds,
                follow_up_question=follow_up_question,
                follow_up_type=decision.follow_up_type or "",
                follow_up_field=decision.follow_up_field or "",
            )
            if job.engine_version == 'v2' and decision.v2_delivery_sections:
                from app.reception_v2.material_delivery import introduction_parts
                parts = introduction_parts(decision.v2_delivery_sections, materials,
                    plan_id=f'live:{job.id}', interval_seconds=interval_seconds)
            job.trace = {**job.trace, "deferred_follow_up": deferred,
                         "contact_request_in_plan": bool(contact_requested and not deferred), "delivery_plan": [
                {"item_id": part.part_id, "plan_version": part.plan_version,
                 "group_key": part.content_group_key, "kind": part.kind,
                 "content": part.content, "material": part.material,
                 "interval_seconds": part.interval_seconds,
                 "is_follow_up": part.is_follow_up, "status": "pending"}
                for part in parts
            ]}
            db.commit()
            _execute_persisted_reply(db, client, job, state, run, fingerprint)
            job.trace = {**job.trace, "outbound": True, "image_count": len(materials), "duplicate_images_skipped": len(skipped)}
            complete(db, job, "submitted")
            enroll_after_submitted_reply(
                db, state, job, decision.route_variant,
                safety_flags=decision.safety_flags,
                deferred_follow_up=deferred,
            )
        except EvaluationCallError as exc:
            db.rollback()
            job = db.get(LiveReplyJob, job_id)
            cycles = int(job.trace.get("model_cycles", 0)) + 1
            calls = job.trace.get("model_calls", []) + exc.logs
            job.trace = {**job.trace, "outbound": False, "model_ms": sum(item.get("duration_ms", 0) for item in calls),
                         "model_cycles": cycles, "request_count": len(calls), "model_calls": calls, "request_hash": exc.digest}
            last_error = (exc.logs[-1].get("error_code") or "") if exc.logs else exc.code
            transient = "timeout" in last_error.lower() or last_error in (
                "ConnectError", "ReadError", "WriteError", "RemoteProtocolError", "http_429", "http_500", "http_502", "http_503", "http_504")
            trigger = db.get(MessageEvent, job.trigger_message_id)
            if transient and cycles < 2 and trigger and fresh(trigger.created_at):
                job.status, job.error_code = "queued", "model_retry_scheduled"
                job.due_at, job.completed_at = iso(dt(utcnow()) + timedelta(seconds=3)), None
                run = db.scalar(select(AiRun).where(AiRun.trigger_message_id == job.trigger_message_id))
                if run:
                    run.status, run.error_code = "retrying", exc.code
                db.commit()
                return
            if not transient or cycles >= 2 or not trigger or not fresh(trigger.created_at):
                state = db.get(ConversationState, job.conversation_state_id)
                factual_failure = "factual_verification_failed" in exc.code or exc.code == "reply_verification_failed"
                ensure_handoff(
                    db,
                    state,
                    "ai_factual_verification_failed" if factual_failure else "ai_service_unavailable",
                    (
                        "客户回复未通过事实或切题核验，已阻止发送，需要顾问检查并跟进"
                        if factual_failure
                        else "AI 服务连续调用失败，需要顾问跟进"
                    ),
                )
                job.trace = {
                    **job.trace,
                    "manual_followup_created": True,
                    "terminal_model_error": exc.code,
                }
            # Exhausted requests are not replayed after a restart.
            complete(db, job, "failed", exc.code)
        except ReplyBlocked as exc:
            db.rollback()
            job = db.get(LiveReplyJob, job_id)
            reason = str(exc)
            if reason in {"historical_or_stale_trigger", "automatic_window_closed"}:
                state = db.get(ConversationState, job.conversation_state_id)
                trigger = db.get(MessageEvent, job.trigger_message_id)
                if handoff_stale_message(db, state, trigger, armed_at=live_policy(db).get("armed_at")):
                    job.trace = {**job.trace, "manual_followup_created": True}
            complete(db, job, "submission_unknown" if "submission_unknown" in reason else "blocked", reason)
        except Exception as exc:
            db.rollback()
            job = db.get(LiveReplyJob, job_id)
            complete(db, job, "failed", getattr(exc, "code", type(exc).__name__))
        finally:
            if client:
                client.close()


def recover_jobs(db):
    for job in db.scalars(select(LiveReplyJob).where(LiveReplyJob.status == "processing")).all():
        attempts = db.scalars(select(OutboundMessage).where(OutboundMessage.idempotency_key.like(f"live:{job.id}:%"))).all()
        if job.trace.get("delivery_plan"):
            try:
                _persisted_reply_attempts(db, job)
            except ReplyBlocked as exc:
                job.status, job.error_code = "blocked", str(exc)
                continue
        if job.trace.get("delivery_plan") and all(out.status in {"submitted", "sent", "delivered", "read"} for out in attempts):
            job.status = "queued"
            job.trace = {**job.trace, "resume_delivery_plan": True}
        elif attempts:
            job.status, job.error_code = "submission_unknown", "interrupted_delivery_requires_review"
        else:
            job.status = "queued"
    db.commit()
