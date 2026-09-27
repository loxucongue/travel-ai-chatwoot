"""Real silence-journey delivery with channel, handoff and deduplication guards."""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path
import time
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.chatwoot_service import client_for, payload_dict
from app.automation_models import LiveSopEnrollment, LiveSopJob, SopVersion, TouchReservation
from app.automation_service import blocking_labels, dt, iso, reserve_touch
from app.config import settings
from app.reception_config import live_silence_enabled
from app.conversation_policy import compute_state, has_ai_label, observe_ai_label
from app.db import SessionLocal
from app.delivery_plan import delivery_mode_for, ordered_delivery_parts, expand_static_delivery_nodes
from app.delivery_status import pause_failed_delivery, receipt_status
from app.delivery_tracking import (attach_delivery_item, find_existing_delivery,
                                   pin_route_assets, record_delivery_progress)
from app.history_sync import message_direction, message_timestamp
from app.live_reply import ReplyBlocked, safe_decision_json, snapshot
from app.live_reply_models import LiveReplyJob
from app.material_library import (by_media, candidate_materials, catalog_assets, material_live_approved,
                                  material_info, resolve_materials)
from app.models import (ChatwootConnection, ConversationState, ConversationJourney, HandoffTask, MessageEvent,
                        OutboundMessage, SopDefinition, StoredMedia, Tenant, AppSetting, utcnow)
from app.lead_capture import apply_model_policy, mark_requested
from app.lead_capture_models import LeadCaptureState
from app.decision_service import generate_decision
from app.deepseek_evaluation import EvaluationCallError
from app.operations import create_notification, ensure_handoff
from app.outbound_control import global_message_sending_enabled
from app.reply_context import fetch_complete_customer_history
from app.route_packages import ROUTES, UNCLASSIFIED_SOP_NAME, ensure_route_packages_current
from app.route_reply import (automatic_content_already_covered, append_deferred_initial_follow_up, deferred_follow_up_group, journey_context,
                             journey_for, playbook_prompt,
                             prepare_route_reply, route_snapshot_from_values, frozen_sop_nodes)
from app.reception_config import (SETTING_KEY, effective_reception_policy,
                                  configured_silence_nodes, configured_silence_ttl_hours,
                                  get_reception_configuration, silence_intervals)
from app.reception_rollout import ReceptionRollout, reception_rollout
from app.sop_schedule import content_items, relative_delay, schedule_at


LIVE_TERMINAL = {"submitted", "already_provided", "skipped", "verification_blocked", "skipped_model_failure", "cancelled", "blocked", "failed", "submission_unknown"}
RETRYABLE_BLOCKS = {"passive_reply_pending", "outside_contact_hours"}


def assert_live_sop_armed(db=None) -> ReceptionRollout:
    if (settings.app_profile != "live_reply" or not settings.outbound_enabled
            or not live_silence_enabled(db) or not global_message_sending_enabled(db)):
        raise ReplyBlocked("live_sop_not_armed")
    rollout = reception_rollout(db)
    if rollout.allowlist_enabled and not rollout.conversation_ids:
        raise ReplyBlocked("live_sop_not_armed")
    return rollout


def _subject(state: ConversationState) -> str:
    identity = f"contact:{state.tenant_id}:{state.contact_id}" if state.contact_id else f"conversation:{state.tenant_id}:{state.id}"
    return f"live:{identity}"


def _touch_subject(state: ConversationState) -> str:
    return f"live:{state.tenant_id}:{state.contact_id}" if state.contact_id else f"live:{state.tenant_id}:conv:{state.id}"


def _static_scope(db, state: ConversationState, version: SopVersion) -> None:
    rollout = assert_live_sop_armed(db)
    remote_id = state.chatwoot_conversation_id
    if rollout.allowlist_enabled and remote_id not in rollout.conversation_ids:
        raise ReplyBlocked("test_conversation_required")
    if not state.ai_label_present or state.ai_mode != "enabled":
        raise ReplyBlocked("ai_opt_in_required")
    if state.inbox.chatwoot_inbox_id != settings.live_reply_inbox_id:
        raise ReplyBlocked("live_scope_mismatch")
    if version.config.get("inbox_ids") and state.inbox.chatwoot_inbox_id not in version.config["inbox_ids"]:
        raise ReplyBlocked("sop_inbox_mismatch")


def _verified_remote(db, state: ConversationState, version: SopVersion, enrollment: LiveSopEnrollment | None, client):
    db.commit()
    db.expire_all()
    if enrollment:
        db.refresh(enrollment)
        db.refresh(state)
        if (enrollment.engine_version != state.ai_engine_version
                or enrollment.engine_release_id != state.ai_engine_release_id):
            raise ReplyBlocked("engine_version_changed")
        if enrollment.engine_version == "v2":
            from app.reception_v2.runtime import ENGINE_RELEASE_ID
            if enrollment.engine_release_id != ENGINE_RELEASE_ID:
                raise ReplyBlocked("engine_release_unavailable")
    _static_scope(db, state, version)
    connection = db.scalar(select(ChatwootConnection).where(ChatwootConnection.tenant_id == state.tenant_id))
    if not connection or connection.account_id != settings.live_reply_account_id:
        raise ReplyBlocked("live_scope_mismatch")
    snap = snapshot(client, state.chatwoot_conversation_id)
    remote, labels, messages = snap["remote"], snap["labels"], snap["messages"]
    if remote.get("id") != state.chatwoot_conversation_id or remote.get("inbox_id") != settings.live_reply_inbox_id:
        raise ReplyBlocked("live_scope_mismatch")
    observe_ai_label(state, labels, "chatwoot_live_sop")
    state.labels, state.can_reply = labels, remote.get("can_reply") is True
    if state.contact:
        state.contact.labels = snap["contact_labels"]
    state.effective_ai_state, state.effective_state_reason = compute_state(
        db.get(Tenant, state.tenant_id), state.inbox, labels, snap["contact_labels"], state.can_reply,
        state.ai_mode, state.ai_sync_status, state.ai_label_present)
    db.commit()
    if not has_ai_label(labels):
        raise ReplyBlocked("ai_opt_in_required")
    if state.effective_ai_state != "AI_ACTIVE":
        raise ReplyBlocked(state.effective_state_reason or "ai_or_handoff_blocked")
    if set(labels + snap["contact_labels"]) & blocking_labels(db):
        raise ReplyBlocked("human_or_contact_block")
    if db.scalar(select(HandoffTask.id).where(HandoffTask.conversation_state_id == state.id,
                                             HandoffTask.status.in_(["pending", "claimed"]))):
        raise ReplyBlocked("human_handoff_active")
    from app.customer_contact_policy import contact_constraint
    reason, _ = contact_constraint({'journey': journey_context(journey_for(db, state)), 'now': utcnow()})
    if reason:
        raise ReplyBlocked(reason)
    if remote.get("can_reply") is not True:
        raise ReplyBlocked("channel_cannot_reply")
    incoming = [m for m in messages if message_direction(m.get("message_type")) == "incoming"
                and not m.get("private") and not (m.get("content_attributes") or {}).get("external_echo")]
    if not incoming or not incoming[-1].get("created_at"):
        raise ReplyBlocked("automatic_window_unknown")
    latest_customer_at = dt(message_timestamp(incoming[-1]["created_at"]))
    if dt(utcnow()) >= latest_customer_at + timedelta(hours=23, minutes=55):
        raise ReplyBlocked("automatic_window_closed")
    reception = get_reception_configuration(db)
    now_local = dt(utcnow()).astimezone(ZoneInfo("Asia/Shanghai"))
    active_start = reception["silence"]["active_start"]
    active_end = reception["silence"]["active_end"]
    current_hm = now_local.hour * 60 + now_local.minute
    start_hm = int(active_start[:2]) * 60 + int(active_start[3:])
    end_hm = int(active_end[:2]) * 60 + int(active_end[3:])
    within_hours = start_hm <= current_hm < end_hm if start_hm < end_hm else (
        current_hm >= start_hm or current_hm < end_hm
    )
    if not within_hours:
        raise ReplyBlocked("outside_contact_hours")
    if enrollment and version.config.get("stop_on_incoming", True) and latest_customer_at > dt(enrollment.enrolled_at):
        raise ReplyBlocked("customer_new_message")
    if enrollment:
        own_ids = set(db.scalars(select(OutboundMessage.chatwoot_message_id).where(
            OutboundMessage.conversation_state_id == state.id,
            OutboundMessage.chatwoot_message_id.is_not(None))).all())
        for message in messages:
            if (message_direction(message.get("message_type")) == "outgoing" and not message.get("private")
                    and message.get("id") not in own_ids and message.get("created_at")
                    and dt(message_timestamp(message["created_at"])) > dt(enrollment.enrolled_at)):
                raise ReplyBlocked("human_replied")
    pending = db.scalar(select(LiveReplyJob.id).where(
        LiveReplyJob.conversation_state_id == state.id,
        LiveReplyJob.status.in_(["queued", "processing"])))
    if pending:
        raise ReplyBlocked("passive_reply_pending")
    unknown = db.scalar(select(OutboundMessage.id).where(
        OutboundMessage.conversation_state_id == state.id,
        OutboundMessage.status == "submission_unknown"))
    if unknown:
        raise ReplyBlocked("submission_unknown_reconcile_required")
    return snap


def validate_live_target(db, state: ConversationState, version: SopVersion) -> None:
    _static_scope(db, state, version)
    connection = db.scalar(select(ChatwootConnection).where(ChatwootConnection.tenant_id == state.tenant_id))
    db.commit()
    client = client_for(connection)
    try:
        _verified_remote(db, state, version, None, client)
    finally:
        client.close()


def enroll_live_sop(db, state: ConversationState, version: SopVersion, *, request_key: str,
                    reenroll: bool = False, allow_repeat_delivery: bool = False,
                    trigger_source: str = "manual_test",
                    deferred_follow_up: dict | None = None) -> LiveSopEnrollment:
    if not request_key:
        raise ValueError("live_request_key_required")
    route = version.config.get("route_variant", "")
    journey = None
    nodes = version.config.get("nodes", [])
    if trigger_source == "passive_route" and route:
        journey = journey_for(db, state)
        if journey.route_variant != route or journey_context(journey).get("automatic_delivery_paused"):
            raise ValueError("delivery_snapshot_unknown")
        spec = route_snapshot_from_values(route, journey.slots)
        if state.ai_engine_version == "v2":
            from app.reception_v2.proactive_policy import silence_schedule_templates
            nodes = silence_schedule_templates(spec["sop"]["nodes"])
        else:
            nodes = frozen_sop_nodes(spec)
    reception = get_reception_configuration(db)
    initial_delivery_enabled = any(
        node.get("initial_delivery") is True for node in nodes
    )
    if not reception["silence"]["enabled"] and not initial_delivery_enabled:
        raise ValueError("silence_journey_disabled")
    _static_scope(db, state, version)
    sop = db.get(SopDefinition, version.sop_id)
    if not sop or sop.status != "running":
        raise ValueError("sop_not_running")
    key = _subject(state)
    query = select(LiveSopEnrollment).where(
        LiveSopEnrollment.sop_id == sop.id, LiveSopEnrollment.subject_key == key)
    repeated = db.scalar(query.where(LiveSopEnrollment.request_key == request_key))
    if repeated:
        return repeated
    existing = db.scalar(query.order_by(LiveSopEnrollment.round_number.desc()))
    if existing:
        if existing.status == "active":
            raise ValueError("sop_round_active")
        if existing.status == "attention_required":
            raise ValueError("sop_round_reconciliation_required")
        if not reenroll:
            raise ValueError("sop_reenrollment_required")
    elif reenroll:
        raise ValueError("sop_round_missing")
    now = utcnow()
    from app.reception_config import v2_silence_intervals
    configured_intervals = None
    if trigger_source == "passive_route":
        if state.ai_engine_version == "v2":
            configured_intervals = v2_silence_intervals(db)
        elif db.get(AppSetting, SETTING_KEY):
            configured_intervals = silence_intervals(db)
    incoming = db.scalars(select(MessageEvent).where(
        MessageEvent.conversation_state_id == state.id,
        MessageEvent.direction == "incoming", MessageEvent.private.is_(False))
        .order_by(MessageEvent.created_at)).all()
    customer_added = incoming[0].created_at if incoming else None
    last_customer = incoming[-1].created_at if incoming else None
    number = existing.round_number + 1 if existing else 1
    ttl = configured_silence_ttl_hours(float(version.config.get("ttl_hours") or 24), configured_intervals)
    enrollment = LiveSopEnrollment(
        sop_id=sop.id, sop_version_id=version.id, conversation_state_id=state.id,
        subject_key=key, round_number=number, request_key=request_key,
        trigger_source=trigger_source,
        engine_version=state.ai_engine_version,
        engine_release_id=state.ai_engine_release_id,
        allow_repeat_delivery=allow_repeat_delivery,
        enrolled_at=now, expires_at=iso(dt(now) + timedelta(hours=ttl)))
    db.add(enrollment)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        repeated = db.scalar(query.where(LiveSopEnrollment.request_key == request_key))
        if repeated:
            return repeated
        raise ValueError("sop_round_conflict") from exc
    previous = None
    source_nodes = configured_silence_nodes(
        nodes,
        configured_intervals,
        silence_enabled=reception["silence"]["enabled"],
    )
    if state.ai_engine_version == "v2" and trigger_source == "passive_route":
        source_nodes = [node for node in source_nodes if node.get("journey_trigger")]
    else:
        source_nodes = append_deferred_initial_follow_up(
            source_nodes, route, deferred_follow_up,
            slots=journey.slots if journey is not None else None,
        )
    if trigger_source == "passive_route" and state.ai_engine_version == "v1":
        source_nodes = expand_static_delivery_nodes(source_nodes)
    for source_node in source_nodes:
        node = dict(source_node)
        reason = None
        try:
            due = schedule_at(node, customer_added_at=customer_added, enrolled_at=now,
                              last_customer_at=last_customer)
        except ValueError as exc:
            due, reason = None, str(exc)
        if (due and previous is None and trigger_source == "passive_route"
                and node.get("initial_delivery") and not node.get("journey_trigger")):
            times = db.scalars(select(OutboundMessage.submitted_at).where(
                OutboundMessage.conversation_state_id == state.id,
                OutboundMessage.status.in_(["submitted", "sent", "delivered", "read"]),
                OutboundMessage.submitted_at.is_not(None))).all()
            if times:
                spec = route_snapshot_from_values(route, journey.slots) if journey is not None else None
                earliest = dt(max(times, key=dt)) + timedelta(seconds=float((spec or {}).get("initial_delivery_interval_seconds", 2)))
                due = iso(max(dt(due), earliest))
        if node.get("basis") == "previous_node" and node.get("schedule_type") == "relative":
            due = None
            if previous is None:
                reason = "previous_message_required"
        job = LiveSopJob(
            enrollment_id=enrollment.id, node_key=node["key"],
            predecessor_id=previous.id if previous and node.get("basis") == "previous_node" else None, scheduled_at=due,
            status="blocked" if reason else "waiting_dependency" if due is None else "scheduled",
            reason=reason, payload=node)
        db.add(job)
        db.flush()
        previous = job
    return enrollment


def enroll_model_route_sop(db, state: ConversationState, route_variant: str, *,
                           request_key: str,
                           deferred_follow_up: dict | None = None) -> dict:
    """Bind a model-selected route to its canonical published SOP.

    This function does not infer a route or customer intent. It only resolves the
    exact route identifier returned by the model to a reviewed, versioned SOP.
    Every failure is returned as trace data so a completed passive reply can
    never be retried merely because follow-up enrollment was unavailable.
    """
    ensure_route_packages_current()
    reception = get_reception_configuration(db)
    if route_variant and route_variant not in reception["routing"]["enabled_route_variants"]:
        return {"status": "not_applicable", "reason": "route_disabled_by_operator"}
    package = ROUTES.get(route_variant)
    if route_variant and not package:
        return {"status": "not_applicable", "reason": "model_route_unbound"}
    canonical_name = package["sop"]["name"] if package else UNCLASSIFIED_SOP_NAME
    try:
        sop = db.scalar(select(SopDefinition).where(
            SopDefinition.tenant_id == state.tenant_id,
            SopDefinition.name == canonical_name,
            SopDefinition.route_variant == route_variant,
            SopDefinition.status == "running",
        ).order_by(SopDefinition.id.desc()))
        if not sop:
            return {"status": "unavailable", "reason": "canonical_sop_missing"}
        version = db.scalar(select(SopVersion).where(
            SopVersion.sop_id == sop.id,
            SopVersion.version == sop.version,
        ))
        if not version:
            return {"status": "unavailable", "reason": "canonical_sop_version_missing"}
        subject_key = _subject(state)
        existing = db.scalar(select(LiveSopEnrollment).where(
            LiveSopEnrollment.sop_id == sop.id,
            LiveSopEnrollment.subject_key == subject_key,
            LiveSopEnrollment.request_key == request_key,
        ))
        latest = db.scalar(select(LiveSopEnrollment).where(
            LiveSopEnrollment.sop_id == sop.id,
            LiveSopEnrollment.subject_key == subject_key,
        ).order_by(LiveSopEnrollment.round_number.desc()))
        resume_after_customer = bool(
            latest
            and (
                (latest.status == "cancelled" and latest.exit_reason == "customer_new_message")
                or latest.status == "completed"
            )
        )
        enrollment = enroll_live_sop(
            db,
            state,
            version,
            request_key=request_key,
            reenroll=resume_after_customer,
            trigger_source="passive_route",
            deferred_follow_up=deferred_follow_up,
        )
        db.commit()
        return {
            "status": "existing" if existing else "enrolled",
            "sop_id": sop.id,
            "sop_version_id": version.id,
            "enrollment_id": enrollment.id,
            "route_variant": route_variant,
        }
    except (ReplyBlocked, ValueError) as exc:
        db.rollback()
        return {"status": "blocked", "reason": str(exc), "route_variant": route_variant}
    except Exception as exc:
        db.rollback()
        return {
            "status": "error",
            "reason": type(exc).__name__,
            "route_variant": route_variant,
        }


def reconcile_model_route_sops(limit: int = 100) -> int:
    """Recover the small gap between a submitted reply and SOP enrollment."""
    handled = 0
    with SessionLocal() as db:
        jobs = db.scalars(select(LiveReplyJob).where(
            LiveReplyJob.status == "submitted",
        ).order_by(LiveReplyJob.id.desc()).limit(limit)).all()
        for job in jobs:
            trace = dict(job.trace or {})
            if "sop_enrollment" in trace:
                continue
            decision = job.decision or {}
            route_variant = str(decision.get("route_variant") or "")
            skip_silence = "skip_silence_enrollment" in set(decision.get("safety_flags") or [])
            if (
                decision.get("action") != "reply"
                or skip_silence
                or (route_variant and route_variant not in ROUTES)
            ):
                job.trace = {**trace, "sop_enrollment": {
                    "status": "not_applicable",
                    "reason": "decision_skips_silence_enrollment" if skip_silence else "model_route_unbound",
                }}
                db.commit()
                handled += 1
                continue
            state = db.get(ConversationState, job.conversation_state_id)
            result = enroll_model_route_sop(
                db,
                state,
                route_variant,
                request_key=f"passive-route:{job.id}:{route_variant or 'unclassified'}",
            )
            job = db.get(LiveReplyJob, job.id)
            job.trace = {**(job.trace or {}), "sop_enrollment": result}
            db.commit()
            handled += 1
    return handled


def _stop_enrollment(db, enrollment: LiveSopEnrollment, reason: str, status: str = "cancelled") -> None:
    enrollment.status, enrollment.exit_reason, enrollment.completed_at = status, reason, utcnow()
    db.execute(update(LiveSopJob).where(
        LiveSopJob.enrollment_id == enrollment.id,
        LiveSopJob.status.in_(["scheduled", "waiting_dependency", "processing"])).values(
            status="cancelled" if status == "cancelled" else "blocked", reason=reason, completed_at=utcnow()))


def cancel_live_sop_on_customer_message(db, state: ConversationState, message: MessageEvent) -> int:
    """Immediately end active silence timers when a real customer speaks."""
    if message.direction != "incoming" or message.private:
        return 0
    rows = db.scalars(select(LiveSopEnrollment).where(
        LiveSopEnrollment.conversation_state_id == state.id,
        LiveSopEnrollment.status == "active",
    )).all()
    cancelled = 0
    for enrollment in rows:
        if dt(message.created_at) <= dt(enrollment.enrolled_at):
            continue
        _stop_enrollment(db, enrollment, "customer_new_message")
        cancelled += 1
    if cancelled:
        db.commit()
    return cancelled


def cancel_live_sop_on_control_change(db, state: ConversationState, reason: str) -> int:
    """Stop every pending silence action when an external control disables AI."""
    rows = db.scalars(select(LiveSopEnrollment).where(
        LiveSopEnrollment.conversation_state_id == state.id,
        LiveSopEnrollment.status == "active",
    )).all()
    for enrollment in rows:
        _stop_enrollment(db, enrollment, reason)
    if rows:
        db.commit()
    return len(rows)


def _material(db, item: dict, version: SopVersion, tenant_id: int):
    info = material_info(db, item, version.config.get("route_variant", ""), tenant_id)
    media = db.get(StoredMedia, info["media_id"])
    asset = by_media(db, media, asset_key=info.get("asset_key"))
    if not material_live_approved(db, asset, media, expected_hash=info["media_hash"]):
        raise ReplyBlocked("material_not_approved_for_live")
    info = material_info(db, item, version.config.get("route_variant", ""), tenant_id)
    if not Path(media.storage_path).is_file():
        raise ReplyBlocked("material_unavailable")
    return info, media


def _previous_media_delivery(db, state: ConversationState, info: dict):
    digest = info.get("media_hash")
    if not digest:
        return None
    rows = db.scalars(select(OutboundMessage).where(
        OutboundMessage.conversation_state_id == state.id,
        OutboundMessage.media_id.is_not(None),
        OutboundMessage.status.in_(["submission_unknown", "submitted", "sent", "delivered", "read"]))).all()
    for row in rows:
        recorded_hash = ((row.content_attributes or {}).get("_delivery_item") or {}).get("asset_hash")
        if recorded_hash:
            if recorded_hash == digest:
                return row
            continue
    return None


def _media_previously_sent(db, state: ConversationState, info: dict) -> bool:
    return _previous_media_delivery(db, state, info) is not None


def _guard_delivery_history(db, state, enrollment, journey):
    if journey_context(journey).get("automatic_delivery_paused"):
        reason = "automatic_delivery_paused"
        ensure_handoff(db, state, reason, "Delivery history or route snapshot requires review.")
        _stop_enrollment(db, enrollment, reason, "attention_required")
        db.commit()
        raise ReplyBlocked(reason)


def _pin_delivery_assets(db, state, enrollment, journey):
    if not journey.route_variant:
        return
    try:
        pin_route_assets(db, journey, state.tenant_id)
    except ValueError as exc:
        reason = str(exc)
        ensure_handoff(db, state, reason, "Approved delivery assets require review.")
        _stop_enrollment(db, enrollment, reason, "attention_required")
        db.commit()
        raise ReplyBlocked(reason) from exc


def _submit_part(db, client, state: ConversationState, enrollment: LiveSopEnrollment,
                 job: LiveSopJob, item: dict, media: StoredMedia | None, version: SopVersion):
    # Re-read Chatwoot controls at the irreversible boundary for every part.
    _verified_remote(db, state, version, enrollment, client)
    journey = journey_for(db, state)
    _guard_delivery_history(db, state, enrollment, journey)
    _pin_delivery_assets(db, state, enrollment, journey)
    key = f"live-sop:{enrollment.id}:{job.node_key}:{item['key']}"
    existing = db.scalar(select(OutboundMessage).where(OutboundMessage.idempotency_key == key))
    content = str(item.get("content") or "")
    out = OutboundMessage(
        conversation_state_id=state.id, idempotency_key=key, source_type="sop", source_id=enrollment.id,
        content=content, status="submission_unknown", content_type=media.media_type if media else "text",
        media_id=media.id if media else None)
    payload = job.payload or {}
    group_keys = (payload.get("model_decision") or {}).get("covered_content_groups") or (
        [payload["content_group_key"]] if payload.get("content_group_key") else []
    )
    attach_delivery_item(db, out, journey, group_keys,
                         asset_key=str(item.get("asset_key") or ""), item_id=key)
    if existing:
        fields = ("conversation_state_id", "source_type", "source_id", "content", "content_type", "media_id")
        public_item = (existing.content_attributes or {}).get("delivery_item")
        if (any(getattr(existing, field) != getattr(out, field) for field in fields)
                or (existing.content_attributes or {}).get("_delivery_item") != out.content_attributes["_delivery_item"]
                or not isinstance(public_item, dict)
                or any(public_item.get(name) != value for name, value in out.content_attributes["delivery_item"].items())):
            raise ReplyBlocked("delivery_item_identity_mismatch")
        if existing.status in ("submitted", "sent", "delivered", "read"):
            return existing
        raise ReplyBlocked("submission_unknown_reconcile_required" if existing.status == "submission_unknown" else "previous_submission_failed")
    try:
        duplicate = find_existing_delivery(db, out, journey)
    except ValueError as exc:
        reason = str(exc)
        if reason == "channel_send_failed":
            ensure_handoff(db, state, reason, "Previous delivery failed; automatic retry is blocked.")
            _stop_enrollment(db, enrollment, reason, "attention_required")
            db.commit()
        raise ReplyBlocked(reason) from exc
    if duplicate:
        record_delivery_progress(db, duplicate)
        db.commit()
        return duplicate
    db.add(out)
    db.commit()
    if media:
        # Refresh approval after the durable reservation, immediately before sending.
        try:
            db.refresh(media)
            info, checked_media = _material(db, {**item, "media_id": out.media_id,
                "media_hash": (out.content_attributes.get("_delivery_item") or {}).get("asset_hash") or item.get("media_hash"),
                "content_type": out.content_type}, version, state.tenant_id)
            if checked_media.id != out.media_id:
                raise ReplyBlocked("delivery_item_identity_mismatch")
            media = checked_media
        except (ReplyBlocked, ValueError) as exc:
            out.status, out.error_code = "blocked", str(exc)
            db.commit()
            raise ReplyBlocked(str(exc)) from exc
    try:
        result = payload_dict(client.create_attachment_message(
            state.chatwoot_conversation_id, content, media.storage_path, media.mime_type)
            if media else client.create_text_message(state.chatwoot_conversation_id, content))
        if not isinstance(result.get("id"), int):
            raise ReplyBlocked("submission_unknown_reconcile_required")
        out.chatwoot_message_id = result["id"]
        out.status = receipt_status(result.get("status"), "submitted") or "submitted"
        record_delivery_progress(db, out)
        out.submitted_at = utcnow()
        message = db.scalar(select(MessageEvent).where(
            MessageEvent.conversation_state_id == state.id,
            MessageEvent.chatwoot_message_id == result["id"]))
        if not message:
            message = MessageEvent(conversation_state_id=state.id, chatwoot_message_id=result["id"], direction="outgoing")
            db.add(message)
        message.content, message.status, message.attribution = content, out.status, "sop"
        message.content_type = media.media_type if media else "text"
        message.attachments = result.get("attachments") or []
        message.content_attributes = {
            key: value for key, value in {
                **(message.content_attributes or {}), **(out.content_attributes or {}),
            }.items() if not key.startswith("_")
        }
        state.last_message = content or f"[{message.content_type}]"
        state.updated_at = utcnow()
        reservation = db.scalar(select(TouchReservation).where(TouchReservation.owner_key == f"live-sop:{enrollment.id}"))
        if out.status == "failed":
            pause_failed_delivery(db, out)
        elif reservation:
            reservation.status, reservation.confirmed_at = "submitted", utcnow()
        db.commit()
    except ReplyBlocked:
        out.error_code = "submission_unknown_reconcile_required"
        db.commit()
        raise
    except Exception as exc:
        out.error_code = "submission_unknown_reconcile_required"
        db.commit()
        raise ReplyBlocked("submission_unknown_reconcile_required") from exc
    if out.status == "failed":
        raise ReplyBlocked("channel_send_failed")
    return out


def _advance_after_terminal(db, job: LiveSopJob, enrollment: LiveSopEnrollment) -> None:
    successor = db.scalar(select(LiveSopJob).where(LiveSopJob.predecessor_id == job.id))
    if successor and successor.status == "waiting_dependency":
        anchor = job.confirmed_at
        if (enrollment.trigger_source == "passive_route" and successor.payload.get("initial_delivery")
                and not successor.payload.get("journey_trigger")):
            anchor = job.payload.get("initial_delivery_anchor_at") or anchor
        successor.scheduled_at = iso(dt(anchor) + relative_delay(successor.payload))
        successor.status = "scheduled"
    jobs = db.scalars(select(LiveSopJob).where(LiveSopJob.enrollment_id == enrollment.id)).all()
    if all(item.status in LIVE_TERMINAL for item in jobs):
        enrollment.status, enrollment.completed_at = "completed", utcnow()
    db.commit()


def _complete_job(db, job: LiveSopJob, enrollment: LiveSopEnrollment) -> None:
    job.status, job.reason, job.confirmed_at, job.completed_at = "submitted", None, utcnow(), utcnow()
    _advance_after_terminal(db, job, enrollment)


def _reserve_live_touch(db, state: ConversationState, enrollment: LiveSopEnrollment,
                        version: SopVersion, *, continuation: bool) -> bool:
    owner = f"live-sop:{enrollment.id}"
    if not enrollment.allow_repeat_delivery and enrollment.trigger_source != "passive_route":
        return reserve_touch(db, _touch_subject(state), owner, utcnow(),
                             version.config.get("frequency_hours", 24), continuation=continuation)
    contact_key = _touch_subject(state)
    reservation = db.scalar(select(TouchReservation).where(TouchReservation.contact_key == contact_key))
    if reservation and reservation.status == "submission_unknown":
        return False
    expiry = iso(dt(utcnow()) + timedelta(hours=max(24, version.config.get("frequency_hours", 24))))
    if reservation:
        reservation.owner_key, reservation.status = owner, "reserved"
        reservation.reserved_at, reservation.expires_at, reservation.confirmed_at = utcnow(), expiry, None
    else:
        db.add(TouchReservation(contact_key=contact_key, owner_key=owner, status="reserved",
                                reserved_at=utcnow(), expires_at=expiry))
    return True


def recover_live_sop_jobs(db) -> None:
    for job in db.scalars(select(LiveSopJob).where(LiveSopJob.status == "processing")).all():
        unknown = db.scalar(select(OutboundMessage.id).where(
            OutboundMessage.source_type == "sop", OutboundMessage.source_id == job.enrollment_id,
            OutboundMessage.idempotency_key.like(f"live-sop:{job.enrollment_id}:{job.node_key}:%"),
            OutboundMessage.status == "submission_unknown"))
        job.status = "submission_unknown" if unknown else "scheduled"
        job.reason = "submission_unknown_reconcile_required" if unknown else "worker_recovered"
    db.commit()


def _initial_live_skip_anchor(db, state, enrollment, job):
    if (enrollment.trigger_source != "passive_route" or not job.payload.get("initial_delivery")
            or job.payload.get("journey_trigger")):
        return
    times = db.scalars(select(OutboundMessage.submitted_at).where(
        OutboundMessage.conversation_state_id == state.id,
        OutboundMessage.status.in_(["submitted", "sent", "delivered", "read"]),
        OutboundMessage.submitted_at.is_not(None))).all()
    anchor = max(times, key=dt, default=enrollment.enrolled_at)
    job.payload = {**job.payload, "initial_delivery_anchor_at": anchor}


def _deliver_static_live_job(db, client, state, enrollment, version, job) -> None:
    """Keep manually authored live SOPs deterministic and backwards compatible."""
    journey = journey_for(db, state)
    resolved_payload = dict(job.payload or {})
    job.payload = resolved_payload
    group_key = resolved_payload.get("content_group_key")
    if group_key and automatic_content_already_covered(group_key, journey.sent_groups):
        job.status, job.reason, job.confirmed_at, job.completed_at = (
            "already_provided", "content_group_already_provided", utcnow(), utcnow()
        )
        _initial_live_skip_anchor(db, state, enrollment, job)
        evidence = db.scalars(select(OutboundMessage).where(
            OutboundMessage.conversation_state_id == state.id,
            OutboundMessage.status.in_(["sent", "delivered", "read"]))).all()
        job.payload = {**job.payload, "reused_outbound_ids": [
            row.id for row in evidence if automatic_content_already_covered(group_key,
                ((row.content_attributes or {}).get("_delivery_item") or {}).get("group_keys") or [])
        ]}
        _advance_after_terminal(db, job, enrollment)
        return
    skip_slots = list((job.payload or {}).get("skip_if_slots_present") or [])
    known_slots = journey.slots or {}
    if skip_slots and all(
        key in known_slots and known_slots[key] not in (None, "", [], {})
        for key in skip_slots
    ):
        job.status, job.reason = "already_provided", "required_slots_already_known"
        job.confirmed_at = job.completed_at = utcnow()
        _advance_after_terminal(db, job, enrollment)
        return
    items = content_items(job.payload)
    prepared = []
    for item in items:
        if item.get("content_type", "text") == "text":
            prepared.append((item, None, None))
        else:
            info, media = _material(db, item, version, state.tenant_id)
            prepared.append((item, info, media))
    predecessor = db.get(LiveSopJob, job.predecessor_id) if job.predecessor_id else None
    partial = db.scalar(select(OutboundMessage.id).where(
        OutboundMessage.idempotency_key.like(f"live-sop:{enrollment.id}:{job.node_key}:%"),
        OutboundMessage.status.in_(["submitted", "sent", "delivered", "read"])))
    if not _reserve_live_touch(db, state, enrollment, version, continuation=bool(predecessor or partial)):
        raise ReplyBlocked("contact_frequency_limit")
    db.commit()
    submitted_parts = 0
    reused_ids = []
    interval_seconds = int((job.payload or {}).get("delivery_interval_seconds") or 0)
    for item, info, media in prepared:
        if info and not enrollment.allow_repeat_delivery and _media_previously_sent(db, state, info):
            old = _previous_media_delivery(db, state, info)
            if old:
                reused_ids.append(old.id)
            continue
        if submitted_parts and interval_seconds:
            time.sleep(interval_seconds)
        out = _submit_part(db, client, state, enrollment, job, {**item, **(info or {})}, media, version)
        if out.idempotency_key != f"live-sop:{enrollment.id}:{job.node_key}:{item['key']}":
            reused_ids.append(out.id)
            continue
        submitted_parts += 1
    if not submitted_parts and reused_ids:
        job.status, job.reason, job.confirmed_at, job.completed_at = "already_provided", "material_already_provided", utcnow(), utcnow()
        job.payload = {**job.payload, "reused_outbound_ids": reused_ids}
        _initial_live_skip_anchor(db, state, enrollment, job)
        _advance_after_terminal(db, job, enrollment)
        return
    _complete_job(db, job, enrollment)
    deferred = (job.payload or {}).get("deferred_follow_up") or {}
    if deferred.get("type") == "contact":
        capture = db.scalar(select(LeadCaptureState).where(
            LeadCaptureState.conversation_state_id == state.id
        ))
        if capture:
            mark_requested(capture)
            db.commit()


def _execute_dynamic_plan(db, client, state, enrollment, version, job, journey):
    plan = (job.payload or {}).get("dynamic_delivery_plan")
    if (not isinstance(plan, dict) or plan.get("schema_version") != 1
            or plan.get("decision") != (job.payload or {}).get("model_decision")
            or plan.get("route") != journey.route_variant
            or plan.get("route_spec") != route_snapshot_from_values(journey.route_variant, journey.slots)):
        raise ReplyBlocked("delivery_plan_identity_mismatch")
    items = plan["items"]
    decision = plan["decision"]
    if decision.get("action") != "handoff":
        partial = db.scalar(select(OutboundMessage.id).where(
            OutboundMessage.idempotency_key.like(f"live-sop:{enrollment.id}:{job.node_key}:%"),
            OutboundMessage.status.in_(["submitted", "sent", "delivered", "read"])))
        if not _reserve_live_touch(db, state, enrollment, version,
                                   continuation=bool(job.predecessor_id or partial)):
            raise ReplyBlocked("contact_frequency_limit")
        db.commit()
    for index, item in enumerate(items):
        if index and plan["interval_seconds"]:
            time.sleep(plan["interval_seconds"])
        media = None
        if item.get("content_type", "text") != "text":
            _, media = _material(db, item, version, state.tenant_id)
        _submit_part(db, client, state, enrollment, job, item, media, version)
    if decision.get("action") == "handoff":
        reason = decision.get("handoff_reason") or "ai_handoff"
        ensure_handoff(db, state, reason, "Silence journey requires human assistance.")
        _stop_enrollment(db, enrollment, reason, "attention_required")
        job.status, job.reason, job.confirmed_at, job.completed_at = "blocked", reason, utcnow(), utcnow()
        db.commit()
        return
    if plan.get("contact_requested"):
        capture = db.scalar(select(LeadCaptureState).where(LeadCaptureState.conversation_state_id == state.id))
        if capture:
            mark_requested(capture)
    job.payload = {**job.payload, "model_trace": {**job.payload.get("model_trace", {}), "outbound": bool(items)}}
    _complete_job(db, job, enrollment)


def _save_dynamic_plan(db, job, journey, items, interval_seconds, contact_requested=False):
    job.payload = {**job.payload, "dynamic_delivery_plan": {
        "schema_version": 1, "decision": job.payload["model_decision"],
        "route": journey.route_variant,
        "route_spec": route_snapshot_from_values(journey.route_variant, journey.slots),
        "items": items, "interval_seconds": interval_seconds, "contact_requested": contact_requested,
    }}
    db.commit()


def process_due_live_sop(job_id: int | None = None) -> bool:
    with SessionLocal() as db:
        if not live_silence_enabled(db):
            return False
        query = select(LiveSopJob).where(
            LiveSopJob.status == "scheduled", LiveSopJob.scheduled_at <= utcnow())
        if job_id is not None:
            query = query.where(LiveSopJob.id == job_id)
        job = db.scalar(query.order_by(LiveSopJob.scheduled_at, LiveSopJob.id).limit(1))
        if not job:
            return False
        claimed = db.execute(update(LiveSopJob).where(
            LiveSopJob.id == job.id, LiveSopJob.status == "scheduled").values(
                status="processing", attempts=LiveSopJob.attempts + 1))
        db.commit()
        if not claimed.rowcount:
            return True
        job_id = job.id
    client = None
    with SessionLocal() as db:
        job = db.get(LiveSopJob, job_id)
        enrollment = db.get(LiveSopEnrollment, job.enrollment_id)
        version = db.get(SopVersion, enrollment.sop_version_id)
        sop = db.get(SopDefinition, enrollment.sop_id)
        state = db.get(ConversationState, enrollment.conversation_state_id)
        try:
            if not sop or sop.status != "running" or enrollment.status != "active":
                raise ReplyBlocked("sop_not_running")
            if dt(utcnow()) > dt(enrollment.expires_at) or (job.scheduled_at and dt(utcnow()) > dt(job.scheduled_at) + timedelta(minutes=15)):
                raise ReplyBlocked("expired")
            _static_scope(db, state, version)
            connection = db.scalar(select(ChatwootConnection).where(ChatwootConnection.tenant_id == state.tenant_id))
            db.commit()
            client = client_for(connection)
            snap = _verified_remote(db, state, version, enrollment, client)
            journey = journey_for(db, state)
            _guard_delivery_history(db, state, enrollment, journey)
            _pin_delivery_assets(db, state, enrollment, journey)
            if not (job.payload or {}).get("journey_trigger"):
                route = (version.config or {}).get("route_variant", "")
                if route and journey.route_variant != route:
                    reason = "delivery_snapshot_unknown" if not journey.route_variant else "delivery_snapshot_mismatch"
                    ensure_handoff(db, state, reason, "Static SOP has no bound route snapshot; review its published version.")
                    _stop_enrollment(db, enrollment, reason, "attention_required")
                    db.commit()
                    raise ReplyBlocked(reason)
                _guard_delivery_history(db, state, enrollment, journey)
                _pin_delivery_assets(db, state, enrollment, journey)
                _deliver_static_live_job(db, client, state, enrollment, version, job)
                return True
            if "dynamic_delivery_plan" in (job.payload or {}):
                _execute_dynamic_plan(db, client, state, enrollment, version, job, journey)
                return True
            if db.scalar(select(OutboundMessage.id).where(
                    OutboundMessage.idempotency_key.like(f"live-sop:{enrollment.id}:{job.node_key}:%"))):
                raise ReplyBlocked("delivery_plan_missing_reconcile_required")
            journey = journey_for(db, state)
            history, history_trace = fetch_complete_customer_history(client, snap["remote"])
            latest_customer = next((item for item in reversed(history) if item["direction"] == "incoming"), None)
            if not latest_customer:
                raise ReplyBlocked("trusted_customer_message_missing")
            capture = db.scalar(select(LeadCaptureState).where(
                LeadCaptureState.conversation_state_id == state.id
            ))
            approved = {
                asset.asset_key for asset in catalog_assets(db, state.tenant_id)
                if asset.metadata_json.get("live_approved") is True
            }
            context = {
                "module": "silence_touch",
                "now": utcnow(),
                "last_customer_at": latest_customer.get("created_at"),
                "engine_version": enrollment.engine_version,
                "customer_text": latest_customer.get("content", ""),
                "context_messages": history,
                "context_complete": True,
                "route_variant": journey.route_variant,
                "memory": {
                    key: value for key, value in (journey.slots or {}).items()
                    if key != "_profile_meta"
                },
                "journey": journey_context(journey),
                "route_playbook": playbook_prompt(),
                "reception_policy": effective_reception_policy(db),
                "lead_capture": {
                    "status": capture.status if capture else "not_started",
                    "request_count": capture.request_count if capture else 0,
                    "captured_kinds": capture.captured_kinds if capture else [],
                },
                "available_materials": [
                    item for item in candidate_materials(db, state.tenant_id)
                    if item["key"] in approved
                ],
                "touch_index": sum(
                    1 for item in db.scalars(select(LiveSopJob).where(
                        LiveSopJob.enrollment_id == enrollment.id,
                        LiveSopJob.id <= job.id,
                    )).all()
                    if (item.payload or {}).get("journey_trigger")
                ),
                "scheduled_at": job.scheduled_at,
            }
            db.commit()
            decision, calls, _, trace = generate_decision(context)
            decision, journey = prepare_route_reply(
                db, state, decision, journey.last_trigger_message_id
            )
            _guard_delivery_history(db, state, enrollment, journey)
            _pin_delivery_assets(db, state, enrollment, journey)
            decision, capture, contact_requested, _ = apply_model_policy(
                db, state, decision, history, latest_customer.get("content", "")
            )
            job.payload = {
                **(job.payload or {}),
                "model_decision": safe_decision_json(decision),
                "model_trace": {**history_trace, **trace, "outbound": False},
                "model_attempts": len(calls),
            }
            if decision.wakeup_action == "defer":
                from app.reception_v2.scheduling import defer_job
                if not defer_job(job, utcnow(), decision.defer_minutes):
                    _stop_enrollment(db, enrollment, 'v2_defer_limit')
                    job.status, job.reason = 'skipped', 'v2_defer_limit'
                db.commit()
                return True
            if decision.action == "no_action":
                reason = next(
                    (
                        flag for flag in (decision.safety_flags or [])
                        if str(flag).startswith("silence_")
                    ),
                    "silence_no_relevant_content",
                )
                now = utcnow()
                if "stop_automation" in (decision.safety_flags or []):
                    from app.live_reply import stop_customer_automation
                    stop_customer_automation(db, state)
                    _stop_enrollment(db, enrollment, reason)
                    job.status, job.reason = "blocked", reason
                elif enrollment.engine_version == "v2" and decision.wakeup_action == "skip":
                    _stop_enrollment(db, enrollment, "v2_skipped")
                    job.status, job.reason = "skipped", "v2_skipped"
                else:
                    job.status = "verification_blocked" if "silence_verification_failed_no_action" in (decision.safety_flags or []) else "skipped"
                    job.reason = reason
                    job.confirmed_at = job.completed_at = now
                    _advance_after_terminal(db, job, enrollment)
                db.commit()
                return True
            if decision.action == "handoff":
                items = [{
                        "key": "model-handoff",
                        "content_type": "text",
                        "content": decision.reply,
                    }] if decision.reply else []
                _save_dynamic_plan(db, job, journey, items, 0)
                _execute_dynamic_plan(db, client, state, enrollment, version, job, journey)
                return True
            route_spec = route_snapshot_from_values(journey.route_variant, journey.slots)
            materials = resolve_materials(
                db, decision.material_keys, decision.route_variant, state.tenant_id, snapshot=route_spec
            ) if decision.material_keys else []
            question = str(decision.follow_up_question or "").strip()
            body = str(decision.reply_body or "").strip() if question else str(decision.reply_body or decision.reply or "").strip()
            follow_up_group = deferred_follow_up_group(decision.route_variant, {
                "type": decision.follow_up_type, "field": decision.follow_up_field,
                "question": question,
            }, slots=journey.slots) if question else None
            interval_seconds = float((route_spec or {}).get("initial_delivery_interval_seconds", 2))
            parts = ordered_delivery_parts(
                body, materials,
                delivery_mode_for(decision.route_variant, decision.covered_content_groups, decision.material_keys, route_spec=route_spec or {}),
                plan_id=f"live-sop:{enrollment.id}:{job.node_key}",
                content_group_key=decision.content_group_key or "",
                interval_seconds=interval_seconds,
                follow_up_question=question,
                follow_up_type=decision.follow_up_type,
                follow_up_field=decision.follow_up_field,
                follow_up_group_key=follow_up_group or "",
            )
            items = [
                {**(part.material or {}), "key": f"model-image-{(part.material or {}).get('asset_key') or index}"}
                if part.kind == "media" else {
                    "key": "model-follow-up" if part.is_follow_up else "model-text",
                    "content_type": "text", "content": part.content,
                    "is_follow_up": part.is_follow_up,
                }
                for index, part in enumerate(parts)
            ]
            _save_dynamic_plan(db, job, journey, items, interval_seconds, contact_requested)
            _execute_dynamic_plan(db, client, state, enrollment, version, job, journey)
            return True
        except EvaluationCallError as exc:
            db.rollback()
            job = db.get(LiveSopJob, job_id)
            enrollment = db.get(LiveSopEnrollment, job.enrollment_id)
            if reason == 'customer_requested_time':
                from app.customer_contact_policy import contact_constraint
                from app.reception_v2.scheduling import defer_job
                state = db.get(ConversationState, enrollment.conversation_state_id)
                _, delay = contact_constraint({'journey': journey_context(journey_for(db, state)), 'now': utcnow()})
                if not defer_job(job, utcnow(), delay or 1):
                    _stop_enrollment(db, enrollment, 'v2_defer_limit')
                    job.status, job.reason = 'skipped', 'v2_defer_limit'
                db.commit()
                return True
            state = db.get(ConversationState, enrollment.conversation_state_id)
            job.status, job.reason = "skipped_model_failure", "model_generation_or_verification_failed"
            job.confirmed_at = job.completed_at = utcnow()
            job.payload = {
                **(job.payload or {}),
                "model_attempts": len(exc.logs),
                "model_error": exc.code,
                "model_trace": {"outbound": False},
            }
            create_notification(
                db,
                "silence_touch.model_failed",
                "沉默触达生成或事实核验失败",
                f"第 {job.node_key} 次沉默触达已跳过，后续节点仍会继续。错误：{exc.code}",
                state.chatwoot_conversation_id,
            )
            _advance_after_terminal(db, job, enrollment)
            return True
        except ReplyBlocked as exc:
            reason = str(exc)
            db.rollback()
            job = db.get(LiveSopJob, job_id)
            enrollment = db.get(LiveSopEnrollment, job.enrollment_id)
            if reason in {'automatic_window_closed', 'channel_cannot_reply', 'expired'}:
                state = db.get(ConversationState, enrollment.conversation_state_id)
                pending_journey = db.scalar(select(ConversationJourney).where(ConversationJourney.conversation_state_id == state.id))
                preferences = ((pending_journey.slots or {}).get('_v2_state') or {}) if pending_journey else {}
                if preferences.get('contact_at') and not preferences.get('proactive_opt_out'):
                    task = ensure_handoff(db, state, 'customer_contact_outside_window',
                        '约定联系无法在自动渠道窗口完成，请按客户约定时间跟进，之前勿打扰：' + preferences['contact_at'])
                    task.sla_due_at = preferences['contact_at']
            if reason in {"channel_send_failed", "automatic_delivery_paused"} or enrollment.status == "attention_required":
                job.status, job.reason, job.completed_at = "blocked", reason, utcnow()
            elif reason in RETRYABLE_BLOCKS and dt(utcnow()) + timedelta(seconds=30) <= dt(enrollment.expires_at):
                delay = 30
                if reason == "outside_contact_hours":
                    local = dt(utcnow()).astimezone(ZoneInfo("Asia/Shanghai"))
                    target = local.replace(hour=9, minute=0, second=0, microsecond=0)
                    if local.hour >= 21:
                        target += timedelta(days=1)
                    delay = max(30, int((target - local).total_seconds()))
                job.status, job.reason = "scheduled", reason
                job.scheduled_at = iso(dt(utcnow()) + timedelta(seconds=delay))
            elif reason == "submission_unknown_reconcile_required":
                job.status, job.reason, job.completed_at = "submission_unknown", reason, utcnow()
                _stop_enrollment(db, enrollment, reason, "attention_required")
                job.status = "submission_unknown"
            else:
                job.status, job.reason, job.completed_at = "blocked", reason, utcnow()
                _stop_enrollment(db, enrollment, reason)
                job.status = "blocked"
            db.commit()
            return True
        except Exception as exc:
            db.rollback()
            job = db.get(LiveSopJob, job_id)
            if job is not None:
                enrollment = db.get(LiveSopEnrollment, job.enrollment_id)
                state = db.get(ConversationState, enrollment.conversation_state_id)
                job.status = "blocked"
                job.reason = "silence_touch_unexpected_failure"
                job.completed_at = utcnow()
                job.payload = {
                    **(job.payload or {}),
                    "model_trace": {"outbound": False},
                    "unexpected_error": type(exc).__name__,
                }
                create_notification(
                    db,
                    "silence_touch.unexpected_failure",
                    "沉默触达发生未知异常",
                    f"沉默触达已停止并等待人工核对。错误类型：{type(exc).__name__}",
                    state.chatwoot_conversation_id,
                )
                _stop_enrollment(
                    db, enrollment, "silence_touch_unexpected_failure", "attention_required"
                )
                job.status = "blocked"
                db.commit()
            return True
        finally:
            if client:
                client.close()
