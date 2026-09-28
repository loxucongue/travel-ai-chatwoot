"""Readiness of the configured runtime, distinct from API/database liveness."""
from datetime import datetime, timezone

from sqlalchemy import func, select

from app.config import settings
from app.automation_models import AutomationRun, AutomationSession
from app.models import ChatwootConnection, NotificationDelivery, WebhookEvent, WorkerHeartbeat
from app.operations import setting_value
from app.outbound_control import global_message_sending_enabled


def age_seconds(value):
    if not value:
        return None
    try:
        at = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        return max(0, int((datetime.now(timezone.utc) - at).total_seconds()))
    except (ValueError, TypeError):
        return None


def runtime_health(db):
    required = (["live-reply-worker", "live-maintenance-worker"] if settings.app_profile == "live_reply"
                else ["playground-worker"] if settings.app_profile == "evaluation" else [])
    if settings.app_profile == "live_reply" and settings.relay_enabled:
        required.append("live-relay-worker")
    workers = {}
    reasons = []
    for name in required:
        beat = db.get(WorkerHeartbeat, name)
        age = age_seconds(beat.last_seen_at if beat else None)
        healthy = age is not None and age <= settings.worker_stale_seconds
        workers[name] = {"last_seen_at": beat.last_seen_at if beat else None, "age_seconds": age, "healthy": healthy}
        if not healthy:
            reasons.append(f"worker_stale:{name}")

    def queue(model, statuses, time_column):
        query = select(func.count(), func.min(time_column)).select_from(model).where(model.status.in_(statuses))
        if model is AutomationRun:
            query = query.where(AutomationRun.session_id.in_(select(AutomationSession.id).where(AutomationSession.environment == 'live')))
        count, oldest = db.execute(query).one()
        return {"count": count, "oldest_age_seconds": age_seconds(oldest)}

    queues = {
        "events": queue(WebhookEvent, ["pending", "retry"], WebhookEvent.received_at),
        "replies": queue(AutomationRun, ["processing"], AutomationRun.created_at),
        "notifications": queue(NotificationDelivery, ["pending", "retry"], NotificationDelivery.available_at),
    }
    failures = {
        "events": db.scalar(select(func.count()).select_from(WebhookEvent).where(WebhookEvent.status == "dead")),
        "notifications": db.scalar(select(func.count()).select_from(NotificationDelivery).where(NotificationDelivery.status.in_(["dead", "submission_unknown"]))),
    }
    for name, item in queues.items():
        if (item["oldest_age_seconds"] or 0) >= 300:
            reasons.append(f"queue_overdue:{name}")
    for name, count in failures.items():
        if count:
            reasons.append(f"failed_tasks:{name}")
    connection = db.scalar(select(ChatwootConnection))
    armed = bool(setting_value(db, "live_reply", {}).get("armed_at"))
    configured = settings.app_profile == "live_reply" and settings.outbound_enabled and armed
    if settings.app_profile == "live_reply":
        if not configured:
            reasons.append("live_reply_not_armed")
        if not connection or not connection.encrypted_api_token:
            reasons.append("chatwoot_not_configured")
    primary = workers.get(required[0], {}) if required else {}
    sending = global_message_sending_enabled(db)
    return {
        "status": "degraded" if reasons else "ok", "database": "ok",
        "app_profile": settings.app_profile, "workers": workers, "reasons": reasons,
        "worker_type": required[0] if required else None,
        "worker_last_seen_at": primary.get("last_seen_at"),
        "reactive_live_configured": bool(configured and sending),
        "reactive_live_enabled": bool(configured and sending and not reasons),
        "customer_sending_paused": not sending,
        "chatwoot_status": connection.status if connection else "unconfigured",
        "last_webhook_at": connection.last_webhook_at if connection else None,
        "pending_events": queues["events"]["count"], "queues": queues, "failed_tasks": failures,
    }
