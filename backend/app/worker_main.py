import logging
import json
import socket
import time
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError

from app.chatwoot_service import client_for
from app.ai_adapter import decide
from app.chatwoot import ChatwootError, normalize_collection
from app.config import settings
from app.conversation_policy import AI_CONTROL_LABEL, compute_state, has_ai_label, observe_ai_label
from app.db import SessionLocal
from app.history_sync import sync_conversation_history
from app.models import (
    AiRun,
    AppSetting,
    ChatwootConnection,
    Contact,
    ConversationState,
    HandoffTask,
    InboxBinding,
    LabelEvent,
    MessageEvent,
    OutboundMessage,
    SopDefinition,
    SopEnrollment,
    SopJob,
    StoredMedia,
    SyncJob,
    Tenant,
    WebhookEvent,
    WorkerHeartbeat,
    utcnow,
)
from app.operations import audit, create_notification, ensure_handoff, setting_value
from app.outbound_control import global_message_sending_enabled
from app.reception_rollout import default_engine_assignment, reception_conversation_allowed
from app.relay import cleanup_relay_once, poll_relay_once

logger = logging.getLogger("worker")
WORKER_ID = f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"


from app.conversation_mirror import (schedule_sop_enrollment, enroll_matching_sops, unwrap, contact_labels, extract_context, upsert_mirrors)


def process_event(event_id: int) -> int | None:
    if settings.app_profile == "live_reply":
        raise RuntimeError("legacy_sender_disabled_use_live_reply")
    with SessionLocal() as db:
        event = db.get(WebhookEvent, event_id)
        row, message, ctx, conversation_created = upsert_mirrors(db, event)
        db.commit()
        history_state_id = row.id if conversation_created and row else None
        if not row or not message or ctx["event"] != "message_created":
            return history_state_id
        external_echo = bool((event.payload.get("content_attributes") or {}).get("external_echo"))
        if message.direction != "incoming" or message.private or external_echo or message.content_type != "text":
            return history_state_id
        if not settings.outbound_enabled or not global_message_sending_enabled(db):
            logger.info("automatic_reply_skipped outbound_mode=%s event_id=%s", settings.outbound_mode, event_id)
            return history_state_id
        if not reception_conversation_allowed(db, row.chatwoot_conversation_id):
            logger.info("automatic_reply_skipped rollout_scope event_id=%s", event_id)
            return history_state_id
        existing = db.scalar(select(AiRun).where(AiRun.trigger_message_id == message.id))
        if existing:
            return history_state_id
        run = AiRun(conversation_state_id=row.id, trigger_message_id=message.id)
        db.add(run)
        db.commit()
        if row.effective_ai_state != "AI_ACTIVE":
            run.status, run.action, run.error_code, run.completed_at = "skipped", "no_action", row.effective_state_reason, utcnow()
            db.commit()
            return history_state_id
        history = db.scalars(select(MessageEvent).where(MessageEvent.conversation_state_id == row.id).order_by(MessageEvent.created_at.desc()).limit(30)).all()
        try:
            decision = decide(db, {
                "conversation": {"id": row.chatwoot_conversation_id, "status": row.status, "can_reply": row.can_reply, "labels": row.labels},
                "contact": {"id": row.contact.chatwoot_contact_id if row.contact else None, "name": row.contact.name if row.contact else "未知客户", "labels": row.contact.labels if row.contact else []},
                "inbox": {"id": row.inbox.chatwoot_inbox_id, "name": row.inbox.name, "channel": row.inbox.channel_type},
                "message": {"id": message.chatwoot_message_id, "content": message.content, "content_type": message.content_type},
                "history": [{"direction": x.direction, "content": x.content, "created_at": x.created_at} for x in reversed(history)],
            })
        except Exception as exc:
            run.status, run.error_code, run.completed_at = "failed", type(exc).__name__, utcnow()
            config = setting_value(db, "ai_adapter", {"failure_handoff_threshold": 3})
            failures = len(db.scalars(select(AiRun).where(AiRun.conversation_state_id == row.id, AiRun.status == "failed")).all()) + 1
            if failures >= int(config.get("failure_handoff_threshold", 3)):
                ensure_handoff(db, row, "ai_error", f"AI 连续失败 {failures} 次", "P1")
            db.commit()
            return history_state_id
        action, reply = decision.action, decision.reply
        run.action = action
        if action == "handoff":
            ensure_handoff(db, row, "ai_handoff", decision.handoff_reason or "AI 请求转人工")
            run.status, run.completed_at = "completed", utcnow()
            db.commit()
            return history_state_id
        if action != "reply" or not reply:
            run.status, run.completed_at = "completed", utcnow()
            db.commit()
            return history_state_id
        outbound = OutboundMessage(conversation_state_id=row.id, ai_run_id=run.id, idempotency_key=f"ai:{run.id}:reply", content=reply)
        db.add(outbound)
        db.commit()
        state_version = row.version
        connection = db.scalar(select(ChatwootConnection).where(ChatwootConnection.tenant_id == row.tenant_id))

    client = client_for(connection)
    try:
        remote = unwrap(client.get_conversation(row.chatwoot_conversation_id))
        remote_labels = remote.get("labels") if isinstance(remote.get("labels"), list) else row.labels
        remote_can_reply = bool(remote.get("can_reply", row.can_reply))
        with SessionLocal() as db:
            current = db.get(ConversationState, row.id)
            tenant = db.get(Tenant, current.tenant_id)
            inbox = db.get(InboxBinding, current.inbox_binding_id)
            contact = db.get(Contact, current.contact_id) if current.contact_id else None
            catalog_exists = True
            if current.ai_mode == "enabled" and current.ai_label_present and not has_ai_label(remote_labels):
                catalog = normalize_collection(client.list_labels())
                catalog_exists = any(str(item.get("title", "")).casefold() == AI_CONTROL_LABEL for item in catalog)
            if not catalog_exists and current.ai_mode == "enabled":
                current.ai_label_present = False
                current.ai_sync_status = "conflict"
            else:
                observe_ai_label(current, remote_labels, "chatwoot")
            current.labels = list(dict.fromkeys(str(label) for label in remote_labels))
            current.can_reply = remote_can_reply
            state, reason = compute_state(
                tenant, inbox, current.labels, contact.labels if contact else [], remote_can_reply,
                current.ai_mode, current.ai_sync_status, current.ai_label_present,
            )
            if state != current.effective_ai_state or reason != current.effective_state_reason:
                current.version += 1
            current.effective_ai_state, current.effective_state_reason = state, reason
            current.updated_at = utcnow()
            if (current.version != state_version or state != "AI_ACTIVE"
                    or not reception_conversation_allowed(db, current.chatwoot_conversation_id)):
                run_db, outbound_db = db.get(AiRun, run.id), db.get(OutboundMessage, outbound.id)
                block_reason = reason if state != "AI_ACTIVE" else "test_conversation_required"
                run_db.status, run_db.error_code, run_db.completed_at = "discarded", block_reason, utcnow()
                outbound_db.status, outbound_db.error_code = "cancelled", block_reason
                db.commit()
                return history_state_id
        result = unwrap(client.create_text_message(row.chatwoot_conversation_id, reply))
        message_id = result.get("id")
        with SessionLocal() as db:
            run_db, outbound_db = db.get(AiRun, run.id), db.get(OutboundMessage, outbound.id)
            run_db.status, run_db.completed_at = "completed", utcnow()
            outbound_db.status, outbound_db.submitted_at = "submitted", utcnow()
            outbound_db.chatwoot_message_id = int(message_id) if message_id else None
            db.commit()
    except ChatwootError as exc:
        with SessionLocal() as db:
            run_db, outbound_db = db.get(AiRun, run.id), db.get(OutboundMessage, outbound.id)
            run_db.status, run_db.error_code, run_db.completed_at = "failed", exc.code, utcnow()
            outbound_db.status, outbound_db.error_code = "failed", exc.code
            db.commit()
    finally:
        client.close()
    return history_state_id


def heartbeat() -> None:
    with SessionLocal() as db:
        row = db.get(WorkerHeartbeat, WORKER_ID) or WorkerHeartbeat(worker_id=WORKER_ID)
        row.last_seen_at = utcnow()
        db.add(row)
        db.commit()


def claim_event() -> int | None:
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        row = db.scalar(select(WebhookEvent).where(or_(WebhookEvent.status == "pending", WebhookEvent.status == "retry", (WebhookEvent.status == "processing") & (WebhookEvent.lease_expires_at < now.isoformat())), WebhookEvent.available_at <= now.isoformat()).order_by(WebhookEvent.id).limit(1))
        if not row:
            return None
        row.status, row.lease_owner = "processing", WORKER_ID
        row.lease_expires_at = (now + timedelta(seconds=settings.worker_lease_seconds)).isoformat()
        row.attempts += 1
        db.commit()
        return row.id


def finish_event(event_id: int, error: Exception | None = None) -> None:
    with SessionLocal() as db:
        row = db.get(WebhookEvent, event_id)
        if error:
            row.error_code = type(error).__name__
            row.status = "dead" if row.attempts >= 8 else "retry"
            delay = min(900, 2 ** row.attempts)
            row.available_at = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()
            logger.exception("event_failed id=%s code=%s", event_id, row.error_code)
        else:
            row.status, row.processed_at = "completed", utcnow()
        row.lease_owner = row.lease_expires_at = None
        db.commit()


def conversation_page(value: object) -> tuple[list[dict], int]:
    if not isinstance(value, dict):
        return [], 0
    data = value.get("data") if isinstance(value.get("data"), dict) else value
    payload = data.get("payload") if isinstance(data, dict) else []
    meta = data.get("meta") if isinstance(data, dict) else {}
    return (payload if isinstance(payload, list) else []), int((meta or {}).get("all_count") or 0)


def process_history_job() -> bool:
    with SessionLocal() as db:
        job = db.scalar(select(SyncJob).where(SyncJob.kind == "history", SyncJob.status.in_(["pending", "running"])).order_by(SyncJob.id).limit(1))
        if not job:
            return False
        job.status, job.updated_at = "running", utcnow()
        connection = db.scalar(select(ChatwootConnection).where(ChatwootConnection.tenant_id == job.tenant_id))
        tenant = db.get(Tenant, job.tenant_id)
        page = job.current_page
        db.commit()
    client = client_for(connection)
    try:
        items, total = conversation_page(client.list_conversations(page))
    except Exception as exc:
        with SessionLocal() as db:
            job = db.get(SyncJob, job.id)
            job.failed_items += 1
            job.error_code, job.updated_at = type(exc).__name__, utcnow()
            if job.failed_items >= 8:
                job.status = "failed"
            db.commit()
        return True
    finally:
        client.close()
    if not items:
        with SessionLocal() as db:
            job = db.get(SyncJob, job.id)
            if job.stable_passes < 1:
                job.stable_passes, job.current_page, job.phase = job.stable_passes + 1, 1, "verification"
                job.completed_items = 0
            else:
                job.status, job.phase, job.completed_at = "completed", "done", utcnow()
            job.updated_at = utcnow()
            db.commit()
        return True
    state_ids: list[int] = []
    with SessionLocal() as db:
        job = db.get(SyncJob, job.id)
        job.total_items = max(job.total_items, total)
        for item in items:
            conversation_id = int(item["id"])
            inbox_id = int(item.get("inbox_id") or 0)
            inbox = db.scalar(select(InboxBinding).where(InboxBinding.tenant_id == tenant.id, InboxBinding.chatwoot_inbox_id == inbox_id))
            if not inbox:
                job.failed_items += 1
                continue
            sender = ((item.get("meta") or {}).get("sender") or {})
            contact = None
            if sender.get("id"):
                contact = db.scalar(select(Contact).where(Contact.tenant_id == tenant.id, Contact.chatwoot_contact_id == int(sender["id"])))
                if not contact:
                    contact = Contact(tenant_id=tenant.id, chatwoot_contact_id=int(sender["id"]), name=sender.get("name") or "未知客户")
                    db.add(contact)
                    db.flush()
                contact.name = sender.get("name") or contact.name
                contact.email = sender.get("email") or contact.email
                contact.phone_number = sender.get("phone_number") or contact.phone_number
            state = db.scalar(select(ConversationState).where(ConversationState.tenant_id == tenant.id, ConversationState.chatwoot_conversation_id == conversation_id))
            if not state:
                state = ConversationState(
                    tenant_id=tenant.id,
                    inbox_binding_id=inbox.id,
                    contact_id=contact.id if contact else None,
                    chatwoot_conversation_id=conversation_id,
                    **default_engine_assignment(),
                )
                db.add(state)
                db.flush()
            state.inbox_binding_id = inbox.id
            if contact: state.contact_id = contact.id
            state.status = str(item.get("status") or state.status)
            state.can_reply = bool(item.get("can_reply", state.can_reply))
            state.labels = [str(x) for x in item.get("labels", [])]
            state.updated_at = utcnow()
            assignee = ((item.get("meta") or {}).get("assignee") or {})
            state.assignee_id = int(assignee["id"]) if assignee.get("id") else None
            state.assignee_name = assignee.get("name") or assignee.get("available_name")
            state_ids.append(state.id)
        job.current_page += 1
        job.completed_items += len(items)
        job.error_code, job.updated_at = None, utcnow()
        db.commit()
    for state_id in state_ids:
        try:
            sync_conversation_history(state_id)
        except Exception:
            with SessionLocal() as db:
                row = db.get(SyncJob, job.id)
                row.failed_items += 1
                db.commit()
        time.sleep(0.15)
    return True


def process_notification_delivery() -> bool:
    from app.notification_dispatch import process_notification_delivery as dispatch
    return dispatch(SessionLocal)


def process_handoff_overdue() -> bool:
    from app.notification_dispatch import process_handoff_overdue as dispatch
    return dispatch(SessionLocal)


def process_sop_job() -> bool:
    if settings.app_profile == "live_reply":
        return False
    with SessionLocal() as db:
        job = db.scalar(select(SopJob).where(SopJob.status == "scheduled", SopJob.scheduled_at <= utcnow()).order_by(SopJob.scheduled_at).limit(1))
        if not job:
            return False
        if not settings.outbound_enabled or not global_message_sending_enabled(db):
            reason = "global_message_sending_disabled" if settings.outbound_enabled else "outbound_disabled"
            job.status, job.skip_reason, job.completed_at = "skipped", reason, utcnow()
            db.commit()
            return True
        enrollment = db.get(SopEnrollment, job.enrollment_id)
        sop = db.get(SopDefinition, enrollment.sop_id)
        conversation = db.get(ConversationState, enrollment.conversation_state_id)
        node = next((x for x in sop.nodes if x.get("key") == job.node_key), None)
        reason = None
        mappings = setting_value(db, "label_mappings", {"sop_whitelist_label": "SOP测试白名单"})
        if sop.status != "running" or enrollment.status != "active": reason = "sop_not_running"
        elif not node: reason = "node_missing"
        elif not conversation.can_reply: reason = "channel_cannot_reply"
        elif conversation.effective_ai_state != "AI_ACTIVE": reason = "ai_or_handoff_blocked"
        elif not reception_conversation_allowed(db, conversation.chatwoot_conversation_id): reason = "test_conversation_required"
        elif set(conversation.labels) & set(sop.exit_labels): reason = "exit_label"
        elif sop.stop_on_incoming and conversation.last_customer_message_at and conversation.last_customer_message_at > enrollment.enrolled_at: reason = "customer_replied"
        elif sop.dry_run: reason = "dry_run"
        elif not sop.live_enabled or mappings.get("sop_whitelist_label") not in conversation.labels: reason = "blocked_not_whitelisted"
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=sop.frequency_hours)).isoformat()
        recent = db.scalar(select(OutboundMessage).where(OutboundMessage.conversation_state_id == conversation.id, OutboundMessage.source_type == "sop", OutboundMessage.created_at >= cutoff).limit(1))
        if not reason and recent: reason = "frequency_limited"
        if reason:
            job.status, job.skip_reason, job.completed_at = ("dry_run" if reason == "dry_run" else "skipped"), reason, utcnow()
            db.commit()
            return True
        outbound = OutboundMessage(conversation_state_id=conversation.id, idempotency_key=f"sop:{job.id}", content=node.get("content") or "", source_type="sop", source_id=sop.id, content_type=node.get("content_type") or "text", media_id=node.get("media_id"))
        db.add(outbound)
        db.flush()
        job.attempts += 1
        connection = db.scalar(select(ChatwootConnection).where(ChatwootConnection.tenant_id == conversation.tenant_id))
        conversation_id, outbound_id, job_id = conversation.chatwoot_conversation_id, outbound.id, job.id
        media = db.get(StoredMedia, outbound.media_id) if outbound.media_id else None
        db.commit()
    client = client_for(connection)
    try:
        with SessionLocal() as db:
            current = db.get(ConversationState, enrollment.conversation_state_id)
            if (not current or current.effective_ai_state != "AI_ACTIVE"
                    or not reception_conversation_allowed(db, conversation_id)):
                raise RuntimeError("test_conversation_required")
        result = unwrap(client.create_attachment_message(conversation_id, outbound.content, media.storage_path, media.mime_type) if media else client.create_text_message(conversation_id, outbound.content))
        message_id, error = result.get("id"), None
    except Exception as exc:
        message_id, error = None, type(exc).__name__
    finally:
        client.close()
    with SessionLocal() as db:
        job, outbound = db.get(SopJob, job_id), db.get(OutboundMessage, outbound_id)
        if error:
            job.status, job.skip_reason, job.completed_at = "failed", error, utcnow()
            outbound.status, outbound.error_code = "failed", error
        else:
            job.status, job.completed_at = "submitted", utcnow()
            outbound.status, outbound.submitted_at, outbound.chatwoot_message_id = "submitted", utcnow(), int(message_id) if message_id else None
            job.outbound_message_id = outbound.id
        db.commit()
    return True


def run() -> None:
    if settings.app_profile == "live_reply":
        raise RuntimeError("use_live_reply_worker_no_proactive_senders")
    if settings.app_profile == "evaluation" or not settings.outbound_enabled:
        raise RuntimeError("customer_sending_worker_disabled_use_evaluation_worker")
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logger.info("worker_started id=%s", WORKER_ID)
    next_relay_poll = 0.0
    next_relay_cleanup = 0.0
    while True:
        heartbeat()
        now = time.monotonic()
        if settings.relay_enabled and now >= next_relay_poll:
            try:
                imported = poll_relay_once()
                if imported:
                    logger.info("relay_events_imported count=%s", imported)
            except Exception as exc:
                logger.warning("relay_poll_failed code=%s", type(exc).__name__)
            next_relay_poll = now + settings.relay_poll_interval_seconds
        if settings.relay_enabled and now >= next_relay_cleanup:
            try:
                deleted = cleanup_relay_once()
                if deleted:
                    logger.info("relay_events_cleaned count=%s", deleted)
            except Exception as exc:
                logger.warning("relay_cleanup_failed code=%s", type(exc).__name__)
            next_relay_cleanup = now + settings.relay_cleanup_interval_seconds
        event_id = claim_event()
        if event_id is None:
            worked = process_handoff_overdue() or process_sop_job() or process_notification_delivery() or process_history_job()
            if worked:
                continue
            time.sleep(settings.worker_poll_interval_seconds)
            continue
        try:
            history_state_id = process_event(event_id)
            finish_event(event_id)
            if history_state_id:
                try:
                    result = sync_conversation_history(history_state_id)
                    logger.info(
                        "conversation_history_synced state_id=%s fetched=%s inserted=%s updated=%s",
                        history_state_id,
                        result.fetched,
                        result.inserted,
                        result.updated,
                    )
                except Exception as exc:
                    logger.warning(
                        "conversation_history_sync_failed state_id=%s code=%s",
                        history_state_id,
                        type(exc).__name__,
                    )
        except Exception as exc:
            finish_event(event_id, exc)


if __name__ == "__main__":
    run()
