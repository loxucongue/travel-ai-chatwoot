"""Durable internal notifications; never sends a public customer message."""
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select, update

from app.chatwoot import ChatwootError, normalize_collection
from app.chatwoot_service import client_for, payload_dict
from app.config import settings
from app.db import SessionLocal
from app.models import (ChatwootConnection, ConversationState, HandoffTask,
                        Notification, NotificationDelivery, utcnow)
from app.operations import create_notification, setting_value
from app.security import decrypt_secret


def process_notification_delivery(session_factory=None) -> bool:
    # Evaluation must never dispatch even if a copied DB contains live settings.
    if settings.app_profile == "evaluation" or not settings.outbound_enabled:
        return False
    factory = session_factory or SessionLocal
    with factory() as db:
        delivery = db.scalar(select(NotificationDelivery).where(
            NotificationDelivery.status.in_(["pending", "retry"]),
            NotificationDelivery.available_at <= utcnow()).order_by(NotificationDelivery.id).limit(1))
        if not delivery:
            return False
        config = setting_value(db, "notification_settings", {"enabled": False, "channel": "webhook"})
        if not config.get("enabled"):
            return False  # Pause, preserving queued reminders for re-enabling.
        notification = db.get(Notification, delivery.notification_id)
        if notification.event_type == 'handoff.overdue':
            conversation = db.scalar(select(ConversationState).where(
                ConversationState.tenant_id == notification.tenant_id,
                ConversationState.chatwoot_conversation_id == notification.conversation_id))
            task = db.scalar(select(HandoffTask).where(HandoffTask.conversation_state_id == conversation.id,
                HandoffTask.status == 'pending', HandoffTask.claimed_at.is_(None),
                HandoffTask.sla_due_at < utcnow())) if conversation else None
            if not task:
                delivery.status, delivery.error_code = 'cancelled', 'handoff_already_acknowledged'
                db.commit()
                return True
        channel = config.get("channel", "webhook")
        target = None
        if channel == "chatwoot":
            target = db.scalar(select(ChatwootConnection).where(ChatwootConnection.tenant_id == notification.tenant_id))
        claimed = db.execute(update(NotificationDelivery).where(
            NotificationDelivery.id == delivery.id, NotificationDelivery.status.in_(["pending", "retry"])
        ).values(status="submission_unknown", attempts=NotificationDelivery.attempts + 1, error_code="dispatch_started"))
        if not claimed.rowcount:
            db.rollback()
            return False
        delivery_id = delivery.id
        payload = {"event": notification.event_type, "notification_id": notification.id,
                   "title": notification.title, "body": notification.body,
                   "conversation_id": notification.conversation_id, "created_at": notification.created_at}
        # Persist before network I/O. A process crash must not blindly replay a note.
        db.commit()
    client = None
    submitted = False
    error = None
    status = "delivered"
    try:
        if channel == "chatwoot":
            if not target or not config.get("agent_id") or not config.get("bot_id") or not payload["conversation_id"]:
                raise ValueError("notification_target_incomplete")
            client = client_for(target)
            remote = payload_dict(client.get_conversation(payload["conversation_id"]))
            if (settings.app_profile == "live_reply" and
                    (target.account_id != settings.live_reply_account_id or remote.get("inbox_id") != settings.live_reply_inbox_id)):
                raise ValueError("notification_scope_mismatch")
            agents = normalize_collection(client.list_inbox_agents(remote["inbox_id"]))
            assignee = (remote.get("meta") or {}).get("assignee") or {}
            if (remote.get("meta") or {}).get("assignee_type") == 'AgentBot':
                assignee = {}
            agent_id = int(assignee.get('id') or config["agent_id"])
            if not any(int(agent.get("id", 0)) == agent_id for agent in agents):
                raise ValueError("notification_agent_not_in_inbox")
            # Keep an existing human owner; only assign an unowned new handoff.
            if config.get("assign_on_handoff") and payload['event'] == 'handoff.created' and not assignee.get('id'):
                client.assign_conversation(payload["conversation_id"], agent_id)
            content = (f"[@顾问](mention://user/{agent_id}/advisor)\n"
                       f"{payload['title']}\n{payload['body']}\n"
                       f"[ai-notification:{payload['notification_id']}]")
            submitted = True
            result = payload_dict(client.create_private_notification(
                payload["conversation_id"], content, int(config["bot_id"])))
            if not result.get("id") or result.get("private") is not True:
                raise ValueError("notification_receipt_unknown")
            sender = result.get("sender") or {}
            if sender.get("type") != "agent_bot" or int(sender.get("id", 0)) != int(config["bot_id"]):
                raise ValueError("notification_bot_not_confirmed")
        elif channel == "webhook":
            if not config.get("url"):
                raise ValueError("notification_webhook_url_required")
            secret = decrypt_secret(config["encrypted_secret"].encode()) if config.get("encrypted_secret") else ""
            raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
            signature = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
            response = httpx.post(config["url"], content=raw, headers={
                "Content-Type": "application/json", "X-China2Go-Signature": f"sha256={signature}",
                "Idempotency-Key": f"notification:{payload['notification_id']}",
            }, timeout=10)
            response.raise_for_status()
        else:
            raise ValueError("notification_channel_invalid")
    except Exception as exc:
        error = exc.code if isinstance(exc, ChatwootError) else (str(exc) if isinstance(exc, ValueError) else type(exc).__name__)
        status = "submission_unknown" if channel == "chatwoot" and submitted else "retry"
        # Invalid configuration is terminal; uncertain private submissions stay manual.
        if isinstance(exc, ValueError) and not submitted:
            status = "dead"
    finally:
        if client:
            client.close()
    with factory() as db:
        delivery = db.get(NotificationDelivery, delivery_id)
        delivery.status = "dead" if status == "retry" and delivery.attempts >= 5 else status
        delivery.error_code = (error or "")[:160] or None
        if status == "delivered":
            delivery.delivered_at = utcnow()
        elif status == "retry":
            delivery.available_at = (datetime.now(timezone.utc) + timedelta(seconds=min(900, 2 ** delivery.attempts))).isoformat()
        db.commit()
    return True


def process_handoff_overdue(session_factory=None) -> bool:
    factory = session_factory or SessionLocal
    with factory() as db:
        # Exact body equality avoids confusing task 1 with task 10. Scan beyond
        # previously notified tasks so the first 50 never starve the remainder.
        tasks = db.scalars(select(HandoffTask).where(
            HandoffTask.status == "pending", HandoffTask.claimed_at.is_(None), HandoffTask.sla_due_at < utcnow()
        ).order_by(HandoffTask.sla_due_at)).all()
        created = 0
        for task in tasks:
            conversation = db.get(ConversationState, task.conversation_state_id)
            body = f"handoff:{task.id} · 会话 #{conversation.chatwoot_conversation_id} 已超过 SLA"
            if db.scalar(select(Notification.id).where(Notification.event_type == "handoff.overdue", Notification.body == body)):
                continue
            create_notification(db, "handoff.overdue", "人工接管任务已超时", body, conversation.chatwoot_conversation_id)
            created += 1
            if created >= 50:
                break
        db.commit()
        return bool(created)
