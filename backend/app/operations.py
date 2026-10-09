from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AppSetting,
    AuditLog,
    ConversationState,
    HandoffTask,
    Notification,
    NotificationDelivery,
    Tenant,
    User,
    UserInboxScope,
    utcnow,
)


ADMIN_ROLES = {"admin", "super_admin"}


def is_admin(user: User) -> bool:
    return user.role in ADMIN_ROLES


def allowed_inbox_ids(db: Session, user: User) -> list[int] | None:
    if is_admin(user):
        return None
    return list(db.scalars(select(UserInboxScope.inbox_binding_id).where(UserInboxScope.user_id == user.id)).all())


def can_access_conversation(db: Session, user: User, conversation: ConversationState) -> bool:
    allowed = allowed_inbox_ids(db, user)
    return allowed is None or conversation.inbox_binding_id in allowed


def setting_value(db: Session, key: str, default: dict) -> dict:
    row = db.get(AppSetting, key)
    return {**default, **(row.value if row else {})}


def save_setting(db: Session, key: str, value: dict) -> None:
    row = db.get(AppSetting, key) or AppSetting(key=key)
    row.value = value
    db.add(row)


def audit(db: Session, user: User | None, action: str, resource_type: str, resource_id: object = None, details: dict | None = None) -> None:
    tenant = db.scalar(select(Tenant))
    if tenant:
        db.add(AuditLog(tenant_id=tenant.id, user_id=user.id if user else None, action=action, resource_type=resource_type, resource_id=str(resource_id) if resource_id is not None else None, details=details or {}))


def create_notification(db: Session, event_type: str, title: str, body: str, conversation_id: int | None = None) -> Notification:
    tenant = db.scalar(select(Tenant))
    row = Notification(tenant_id=tenant.id, event_type=event_type, title=title, body=body, conversation_id=conversation_id)
    db.add(row)
    db.flush()
    config = setting_value(db, "notification_settings", {"enabled": False, "event_types": []})
    if config.get("enabled") and event_type in config.get("event_types", []):
        db.add(NotificationDelivery(notification_id=row.id))
    return row


def ensure_handoff(
    db: Session,
    conversation: ConversationState,
    reason_code: str,
    reason_detail: str = "",
    priority: str = "P2",
    *, dispatch: bool = True,
) -> HandoffTask:
    existing = db.scalar(select(HandoffTask).where(HandoffTask.conversation_state_id == conversation.id, HandoffTask.status.in_(["pending", "claimed"])))
    if conversation.effective_ai_state != "HUMAN_HANDOFF":
        conversation.effective_ai_state = "HUMAN_HANDOFF"
        conversation.effective_state_reason = f"handoff:{reason_code}"
        conversation.version += 1
        conversation.updated_at = utcnow()
    if existing:
        if reason_detail and reason_detail != existing.reason_detail:
            existing.reason_detail = reason_detail
            existing.updated_at = utcnow()
            existing.version += 1
        return existing
    sla = setting_value(db, "handoff_policy", {"sla_minutes": 30})
    due = (datetime.now(timezone.utc) + timedelta(minutes=int(sla.get("sla_minutes", 30)))).isoformat()
    task = HandoffTask(conversation_state_id=conversation.id, reason_code=reason_code, reason_detail=reason_detail, priority=priority, sla_due_at=due)
    db.add(task)
    db.flush()
    routed = None
    if dispatch:
        from app.advisor_assignment import emit, handoff_event
        from app.reception_v3.live import session_for
        session = session_for(db, conversation.id)
        routed = emit(db, conversation, [handoff_event(reason_code), 'handoff.created'], f'handoff:{task.id}',
                      {**(session.memory if session else {}),
                       'route_variant': (session.controls if session else {}).get('route_variant',''),
                       'summary': reason_detail or reason_code})
    if dispatch and not (routed and routed.rule_id):
        create_notification(db, "handoff.created", "新的人工接管任务", reason_detail or reason_code, conversation.chatwoot_conversation_id)
    return task
