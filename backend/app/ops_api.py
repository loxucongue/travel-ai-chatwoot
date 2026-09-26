import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api import get_connection
from app.chatwoot_service import client_for
from app.auth import current_user, require_csrf, require_super_admin_csrf, super_admin
from app.config import settings
from app.chatwoot import ChatwootError
from app.conversation_policy import AI_CONTROL_LABEL, compute_state, observe_ai_label
from app.db import get_db
from app.models import (
    AiRun,
    AppSetting,
    AppSession,
    AuditLog,
    ChatwootAgent,
    ChatwootLabel,
    Contact,
    ConversationState,
    HandoffTask,
    InboxBinding,
    LabelEvent,
    MessageEvent,
    Notification,
    NotificationRead,
    OutboundMessage,
    SopDefinition,
    SopEnrollment,
    SopJob,
    StoredMedia,
    SyncJob,
    Tenant,
    User,
    UserInboxScope,
    utcnow,
)
from app.operations import allowed_inbox_ids, audit, can_access_conversation, ensure_handoff, is_admin, save_setting, setting_value
from app.ops_schemas import (
    AiAdapterSettings,
    AiReceptionRolloutSettings,
    GlobalMessageSendingSettings,
    HandoffAssign,
    HandoffCreate,
    HandoffVersion,
    LabelMappings,
    NotificationSettings,
    SopCreate,
    SopEnroll,
    SopUpdate,
    UserCreate,
    UserUpdate,
)
from app.outbound_control import GLOBAL_MESSAGE_SETTING_KEY, global_message_sending_enabled
from app.reception_rollout import AI_RECEPTION_ROLLOUT_KEY, reception_rollout
from app.security import encrypt_secret, hash_password

router = APIRouter(prefix="/v1")


@router.get("/settings/global-message-sending")
def get_global_message_sending(user: User = Depends(current_user), db: Session = Depends(get_db)):
    enabled = global_message_sending_enabled(db)
    return {
        "enabled": enabled,
        "runtime_outbound_enabled": settings.outbound_enabled,
        "effective_enabled": settings.outbound_enabled and enabled,
    }


@router.patch("/settings/global-message-sending")
def set_global_message_sending(
    payload: GlobalMessageSendingSettings,
    user: User = Depends(require_super_admin_csrf),
    db: Session = Depends(get_db),
):
    save_setting(db, GLOBAL_MESSAGE_SETTING_KEY, {"enabled": payload.enabled})
    audit(db, user, "settings.global_message_sending", "settings", details={"enabled": payload.enabled})
    db.commit()

    cancelled_replies = cancelled_sop_jobs = cancelled_enrollments = 0
    if not payload.enabled:
        from app.automation_models import LiveSopEnrollment, LiveSopJob
        from app.live_reply_models import LiveReplyJob

        now = utcnow()
        for job in db.scalars(select(LiveReplyJob).where(
            LiveReplyJob.status.in_(["queued", "processing", "retry"])
        )).all():
            job.status = "cancelled"
            job.error_code = "global_message_sending_disabled"
            job.completed_at = now
            cancelled_replies += 1
        for enrollment in db.scalars(select(LiveSopEnrollment).where(
            LiveSopEnrollment.status == "active"
        )).all():
            enrollment.status = "cancelled"
            enrollment.exit_reason = "global_message_sending_disabled"
            enrollment.completed_at = now
            cancelled_enrollments += 1
            for job in db.scalars(select(LiveSopJob).where(
                LiveSopJob.enrollment_id == enrollment.id,
                LiveSopJob.status.in_(["scheduled", "processing", "retry"]),
            )).all():
                job.status = "cancelled"
                job.reason = "global_message_sending_disabled"
                job.completed_at = now
                cancelled_sop_jobs += 1
        db.commit()

    return {
        "enabled": payload.enabled,
        "runtime_outbound_enabled": settings.outbound_enabled,
        "effective_enabled": settings.outbound_enabled and payload.enabled,
        "cancelled_reply_jobs": cancelled_replies,
        "cancelled_sop_jobs": cancelled_sop_jobs,
        "cancelled_enrollments": cancelled_enrollments,
    }


@router.get("/settings/ai-reception-rollout")
def get_ai_reception_rollout(
    _user: User = Depends(super_admin),
    db: Session = Depends(get_db),
):
    rollout = reception_rollout(db)
    return {
        **rollout.to_dict(),
        "require_ai_label": True,
        "global_message_sending_enabled": global_message_sending_enabled(db),
    }


@router.patch("/settings/ai-reception-rollout")
def set_ai_reception_rollout(
    payload: AiReceptionRolloutSettings,
    user: User = Depends(require_super_admin_csrf),
    db: Session = Depends(get_db),
):
    ids = sorted(set(payload.conversation_ids))
    if any(item <= 0 for item in ids):
        raise HTTPException(422, detail={"code": "invalid_conversation_id", "message": "会话 ID 必须是正整数"})
    if payload.allowlist_enabled and not ids:
        raise HTTPException(422, detail={"code": "allowlist_empty", "message": "开启白名单时至少保留一个会话 ID"})
    if not payload.allowlist_enabled and not payload.confirm_ai_label_scope:
        raise HTTPException(422, detail={
            "code": "ai_label_scope_confirmation_required",
            "message": "关闭白名单后，所有带 ai 标签的合格会话均可接待，请再次确认",
        })

    previous = reception_rollout(db)
    value = {"allowlist_enabled": payload.allowlist_enabled, "conversation_ids": ids}
    save_setting(db, AI_RECEPTION_ROLLOUT_KEY, value)
    audit(db, user, "settings.ai_reception_rollout", "settings", details={
        "previous": previous.to_dict(),
        "current": {**value, "scope": "allowlist" if payload.allowlist_enabled else "ai_label"},
    })
    db.commit()

    cancelled_replies = cancelled_enrollments = cancelled_sop_jobs = 0
    if payload.allowlist_enabled:
        from app.automation_models import LiveSopEnrollment, LiveSopJob
        from app.live_reply_models import LiveReplyJob

        now = utcnow()
        for job in db.scalars(select(LiveReplyJob).where(
            LiveReplyJob.status.in_(["queued", "processing", "retry"])
        )).all():
            conversation = db.get(ConversationState, job.conversation_state_id)
            if conversation and conversation.chatwoot_conversation_id not in ids:
                job.status = "cancelled"
                job.error_code = "ai_reception_allowlist_changed"
                job.completed_at = now
                cancelled_replies += 1
        for enrollment in db.scalars(select(LiveSopEnrollment).where(
            LiveSopEnrollment.status == "active"
        )).all():
            conversation = db.get(ConversationState, enrollment.conversation_state_id)
            if not conversation or conversation.chatwoot_conversation_id in ids:
                continue
            enrollment.status = "cancelled"
            enrollment.exit_reason = "ai_reception_allowlist_changed"
            enrollment.completed_at = now
            cancelled_enrollments += 1
            for job in db.scalars(select(LiveSopJob).where(
                LiveSopJob.enrollment_id == enrollment.id,
                LiveSopJob.status.in_(["scheduled", "waiting_dependency", "processing", "retry"]),
            )).all():
                job.status = "cancelled"
                job.reason = "ai_reception_allowlist_changed"
                job.completed_at = now
                cancelled_sop_jobs += 1
        db.commit()

    rollout = reception_rollout(db)
    return {
        **rollout.to_dict(),
        "require_ai_label": True,
        "global_message_sending_enabled": global_message_sending_enabled(db),
        "cancelled_reply_jobs": cancelled_replies,
        "cancelled_sop_jobs": cancelled_sop_jobs,
        "cancelled_enrollments": cancelled_enrollments,
    }


def manager(user: User = Depends(current_user)) -> User:
    if user.role not in ("admin", "super_admin", "supervisor"):
        raise HTTPException(403, detail={"code": "permission_denied", "message": "需要主管权限"})
    return user


def manager_csrf(user: User = Depends(require_csrf)) -> User:
    if user.role not in ("admin", "super_admin", "supervisor"):
        raise HTTPException(403, detail={"code": "permission_denied", "message": "需要主管权限"})
    return user


def user_payload(db: Session, row: User) -> dict:
    scopes = list(db.scalars(select(UserInboxScope.inbox_binding_id).where(UserInboxScope.user_id == row.id)).all())
    return {"id": row.id, "email": row.email, "display_name": row.display_name, "role": "admin" if row.role == "super_admin" else row.role, "active": row.active, "must_change_password": row.must_change_password, "chatwoot_agent_id": row.chatwoot_agent_id, "inbox_binding_ids": scopes, "created_at": row.created_at}


def replace_scopes(db: Session, user_id: int, ids: list[int]) -> None:
    for row in db.scalars(select(UserInboxScope).where(UserInboxScope.user_id == user_id)).all():
        db.delete(row)
    for inbox_id in sorted(set(ids)):
        if not db.get(InboxBinding, inbox_id):
            raise HTTPException(422, detail={"code": "invalid_inbox_scope", "message": f"Inbox {inbox_id} 不存在"})
        db.add(UserInboxScope(user_id=user_id, inbox_binding_id=inbox_id, scope_type="all"))


@router.get("/permissions")
def permissions(user: User = Depends(current_user)):
    role = "admin" if user.role == "super_admin" else user.role
    matrix = {
        "admin": ["*"],
        "supervisor": ["conversations:*", "handoffs:*", "sops:*", "bi:read"],
        "agent": ["conversations:read", "conversations:label", "handoffs:own"],
    }
    return {"role": role, "permissions": matrix.get(role, []), "roles": matrix}


@router.get("/users")
def users(user: User = Depends(super_admin), db: Session = Depends(get_db)):
    return {"items": [user_payload(db, row) for row in db.scalars(select(User).order_by(User.created_at)).all()]}


@router.post("/users")
def create_user(payload: UserCreate, user: User = Depends(require_super_admin_csrf), db: Session = Depends(get_db)):
    temporary = secrets.token_urlsafe(12)
    row = User(email=payload.email.lower(), display_name=payload.display_name.strip(), password_hash=hash_password(temporary), role=payload.role, active=True, must_change_password=True, chatwoot_agent_id=payload.chatwoot_agent_id)
    db.add(row)
    try:
        db.flush()
        replace_scopes(db, row.id, payload.inbox_binding_ids)
        audit(db, user, "user.create", "user", row.id, {"role": payload.role})
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, detail={"code": "user_conflict", "message": "邮箱或 Chatwoot 客服绑定已存在"}) from exc
    return {**user_payload(db, row), "temporary_password": temporary}


@router.patch("/users/{user_id}")
def update_user(user_id: int, payload: UserUpdate, user: User = Depends(require_super_admin_csrf), db: Session = Depends(get_db)):
    row = db.get(User, user_id)
    if not row:
        raise HTTPException(404, detail={"code": "user_not_found", "message": "用户不存在"})
    for field in ("display_name", "role", "active", "chatwoot_agent_id"):
        value = getattr(payload, field)
        if value is not None:
            setattr(row, field, value)
    try:
        if payload.inbox_binding_ids is not None:
            replace_scopes(db, row.id, payload.inbox_binding_ids)
        audit(db, user, "user.update", "user", row.id)
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, detail={"code": "user_conflict", "message": "Chatwoot 客服绑定已存在"}) from exc
    return user_payload(db, row)


@router.post("/users/{user_id}/reset-password")
def reset_password(user_id: int, user: User = Depends(require_super_admin_csrf), db: Session = Depends(get_db)):
    row = db.get(User, user_id)
    if not row:
        raise HTTPException(404, detail={"code": "user_not_found", "message": "用户不存在"})
    temporary = secrets.token_urlsafe(12)
    row.password_hash, row.must_change_password = hash_password(temporary), True
    db.execute(update(AppSession).where(
        AppSession.user_id == row.id, AppSession.revoked_at.is_(None),
    ).values(revoked_at=utcnow()))
    audit(db, user, "user.reset_password", "user", row.id)
    db.commit()
    return {"temporary_password": temporary}


@router.get("/settings/resources")
def resources(user: User = Depends(manager), db: Session = Depends(get_db)):
    return {
        "agents": [{"id": x.chatwoot_agent_id, "name": x.name, "email": x.email, "availability_status": x.availability_status, "inbox_ids": x.inbox_ids} for x in db.scalars(select(ChatwootAgent).order_by(ChatwootAgent.name)).all()],
        "labels": [{"id": x.chatwoot_label_id, "title": x.title, "color": x.color} for x in db.scalars(select(ChatwootLabel).order_by(ChatwootLabel.title)).all()],
    }


@router.get("/settings/label-mappings")
def get_label_mappings(user: User = Depends(manager), db: Session = Depends(get_db)):
    return setting_value(db, "label_mappings", LabelMappings().model_dump())


@router.patch("/settings/label-mappings")
def set_label_mappings(payload: LabelMappings, user: User = Depends(require_super_admin_csrf), db: Session = Depends(get_db)):
    save_setting(db, "label_mappings", payload.model_dump())
    audit(db, user, "settings.label_mappings", "settings")
    db.commit()
    return payload.model_dump()


@router.get("/settings/notifications")
def get_notification_settings(user: User = Depends(super_admin), db: Session = Depends(get_db)):
    value = setting_value(db, "notification_settings", {"enabled": False, "channel": "webhook", "agent_id": None, "bot_id": None, "url": None, "event_types": ["handoff.created", "handoff.overdue"]})
    return {**value, "secret_configured": bool(value.get("encrypted_secret")), "encrypted_secret": None}


@router.patch("/settings/notifications")
def set_notification_settings(payload: NotificationSettings, user: User = Depends(require_super_admin_csrf), db: Session = Depends(get_db)):
    current = setting_value(db, "notification_settings", {})
    value = payload.model_dump(exclude={"secret"}, mode="json")
    value["encrypted_secret"] = current.get("encrypted_secret")
    if payload.secret:
        value["encrypted_secret"] = encrypt_secret(payload.secret).decode()
    save_setting(db, "notification_settings", value)
    audit(db, user, "settings.notifications", "settings")
    db.commit()
    return {**value, "encrypted_secret": None, "secret_configured": bool(value.get("encrypted_secret"))}


@router.get("/settings/ai-adapter")
def get_ai_adapter(user: User = Depends(super_admin), db: Session = Depends(get_db)):
    value = setting_value(db, "ai_adapter", AiAdapterSettings().model_dump())
    return {**value, "token_configured": bool(value.get("encrypted_token")), "encrypted_token": None}


@router.patch("/settings/ai-adapter")
def set_ai_adapter(payload: AiAdapterSettings, user: User = Depends(require_super_admin_csrf), db: Session = Depends(get_db)):
    current = setting_value(db, "ai_adapter", {})
    value = payload.model_dump(exclude={"bearer_token"}, mode="json")
    value["url"] = str(payload.url) if payload.url else None
    value["encrypted_token"] = current.get("encrypted_token")
    if payload.bearer_token:
        value["encrypted_token"] = encrypt_secret(payload.bearer_token).decode()
    save_setting(db, "ai_adapter", value)
    tenant = db.scalar(select(Tenant))
    if tenant:
        tenant.ai_enabled = payload.enabled
        for conversation in db.scalars(select(ConversationState).where(ConversationState.tenant_id == tenant.id)).all():
            conversation.effective_ai_state, conversation.effective_state_reason = compute_state(
                tenant, conversation.inbox, conversation.labels, conversation.contact.labels if conversation.contact else [],
                conversation.can_reply, conversation.ai_mode, conversation.ai_sync_status, conversation.ai_label_present)
            conversation.version += 1
    audit(db, user, "settings.ai_adapter", "settings", details={"adapter": payload.adapter, "enabled": payload.enabled})
    db.commit()
    return {**value, "encrypted_token": None, "token_configured": bool(value.get("encrypted_token"))}


@router.post("/settings/history-sync")
def start_history_sync(user: User = Depends(require_super_admin_csrf), db: Session = Depends(get_db)):
    running = db.scalar(select(SyncJob).where(SyncJob.kind == "history", SyncJob.status.in_(["pending", "running", "paused"])).order_by(SyncJob.id.desc()))
    if running:
        if running.status == "paused":
            running.status, running.updated_at = "pending", utcnow()
            db.commit()
        return sync_job_json(running)
    tenant = db.scalar(select(Tenant))
    job = SyncJob(tenant_id=tenant.id)
    db.add(job)
    audit(db, user, "history_sync.start", "sync_job")
    db.commit()
    return sync_job_json(job)


@router.get("/settings/history-sync/status")
def history_sync_status(user: User = Depends(manager), db: Session = Depends(get_db)):
    job = db.scalar(select(SyncJob).where(SyncJob.kind == "history").order_by(SyncJob.id.desc()))
    return sync_job_json(job) if job else {"status": "not_started", "phase": "idle", "total_items": 0, "completed_items": 0, "failed_items": 0}


@router.post("/settings/history-sync/pause")
def pause_history_sync(user: User = Depends(require_super_admin_csrf), db: Session = Depends(get_db)):
    job = db.scalar(select(SyncJob).where(SyncJob.kind == "history", SyncJob.status.in_(["pending", "running"])).order_by(SyncJob.id.desc()))
    if not job:
        raise HTTPException(409, detail={"code": "history_sync_not_running", "message": "当前没有运行中的历史回填任务"})
    job.status, job.updated_at = "paused", utcnow()
    audit(db, user, "history_sync.pause", "sync_job", job.id)
    db.commit()
    return sync_job_json(job)


def sync_job_json(job: SyncJob) -> dict:
    return {"id": job.id, "status": job.status, "phase": job.phase, "current_page": job.current_page, "total_items": job.total_items, "completed_items": job.completed_items, "failed_items": job.failed_items, "error_code": job.error_code, "updated_at": job.updated_at, "completed_at": job.completed_at}


def visible_task(db: Session, user: User, task_id: int) -> tuple[HandoffTask, ConversationState]:
    task = db.get(HandoffTask, task_id)
    conversation = db.get(ConversationState, task.conversation_state_id) if task else None
    if not task or not conversation or not can_access_conversation(db, user, conversation):
        raise HTTPException(404, detail={"code": "handoff_not_found", "message": "接管任务不存在"})
    return task, conversation


def handoff_json(db: Session, task: HandoffTask) -> dict:
    conversation = db.get(ConversationState, task.conversation_state_id)
    contact = conversation.contact
    assignee = db.get(User, task.assignee_user_id) if task.assignee_user_id else None
    return {"id": task.id, "conversation_id": conversation.chatwoot_conversation_id, "customer_name": contact.name if contact else "未知客户", "inbox": conversation.inbox.name, "inbox_id": conversation.inbox.chatwoot_inbox_id, "reason_code": task.reason_code, "reason_detail": task.reason_detail, "priority": task.priority, "status": task.status, "assignee": assignee.display_name if assignee else None, "assignee_user_id": task.assignee_user_id, "assignment_pending": task.assignment_target_user_id is not None, "assignment_target_agent_id": task.assignment_target_agent_id, "sla_due_at": task.sla_due_at, "created_at": task.created_at, "claimed_at": task.claimed_at, "completed_at": task.completed_at, "version": task.version, "labels": conversation.labels}


@router.get("/handoffs")
def handoffs(status: str = "pending", q: str = "", page: int = 1, page_size: int = 25, user: User = Depends(current_user), db: Session = Depends(get_db)):
    tasks = db.scalars(select(HandoffTask).order_by(HandoffTask.created_at.desc())).all()
    allowed = allowed_inbox_ids(db, user)
    tasks = [x for x in tasks if (allowed is None or db.get(ConversationState, x.conversation_state_id).inbox_binding_id in allowed)]
    counts = {key: sum(1 for x in tasks if x.status == key) for key in ("pending", "claimed", "completed", "cancelled")}
    tasks = [x for x in tasks if not status or x.status == status]
    if q:
        term = q.casefold()
        tasks = [x for x in tasks if term in str(handoff_json(db, x)).casefold()]
    total = len(tasks)
    return {"items": [handoff_json(db, x) for x in tasks[(page - 1) * page_size: page * page_size]], "counts": counts, "total": total, "page": page}


@router.post("/handoffs")
def create_handoff(payload: HandoffCreate, user: User = Depends(require_csrf), db: Session = Depends(get_db)):
    conversation = db.scalar(select(ConversationState).where(ConversationState.chatwoot_conversation_id == payload.conversation_id))
    if not conversation or not can_access_conversation(db, user, conversation):
        raise HTTPException(404, detail={"code": "conversation_not_found", "message": "会话不存在"})
    task = ensure_handoff(db, conversation, payload.reason_code, payload.reason_detail, payload.priority)
    mappings = setting_value(db, "label_mappings", LabelMappings().model_dump())
    handoff_label = mappings["handoff_labels"][0] if mappings["handoff_labels"] else None
    if handoff_label and handoff_label not in conversation.labels:
        client = client_for(get_connection(db))
        try:
            current = client.get_conversation_labels(conversation.chatwoot_conversation_id)
            labels = current.get("payload", []) if isinstance(current, dict) else current
            final = list(dict.fromkeys([*labels, handoff_label]))
            client.set_conversation_labels(conversation.chatwoot_conversation_id, final)
            conversation.labels = final
        finally:
            client.close()
    audit(db, user, "handoff.create", "handoff", task.id)
    db.commit()
    return handoff_json(db, task)


def finish_assignment(db: Session, task_id: int, target: User, agent_id: int, actor: User, reserved_version: int) -> dict:
    changed = db.execute(update(HandoffTask).where(
        HandoffTask.id == task_id, HandoffTask.version == reserved_version,
        HandoffTask.assignment_target_user_id == target.id,
        HandoffTask.assignment_target_agent_id == agent_id,
        HandoffTask.status.in_(["pending", "claimed"]),
    ).values(status="claimed", assignee_user_id=target.id, chatwoot_assignee_id=agent_id,
             assignment_target_user_id=None, assignment_target_agent_id=None,
             claimed_at=utcnow(), updated_at=utcnow(), version=reserved_version + 1))
    if not changed.rowcount:
        db.rollback()
        raise HTTPException(409, detail={"code": "handoff_conflict", "message": "任务已变化，请刷新并核对分配结果"})
    db.expire_all()
    task = db.get(HandoffTask, task_id)
    conversation = db.get(ConversationState, task.conversation_state_id)
    conversation.assignee_id, conversation.assignee_name = agent_id, target.display_name
    audit(db, actor, "handoff.claim", "handoff", task.id, {"target_user_id": target.id})
    db.commit()
    return handoff_json(db, task)


def claim_for(db: Session, task: HandoffTask, conversation: ConversationState, target: User, actor: User, expected_version: int) -> dict:
    if not target.chatwoot_agent_id:
        raise HTTPException(422, detail={"code": "agent_not_bound", "message": "该用户尚未绑定 Chatwoot 客服"})
    if not target.active or not can_access_conversation(db, target, conversation):
        raise HTTPException(422, detail={"code": "invalid_assignee", "message": "目标客服无权访问该会话"})
    task_id, remote_id, agent_id = task.id, conversation.chatwoot_conversation_id, target.chatwoot_agent_id
    client = client_for(get_connection(db))
    try:
        claimed = db.execute(update(HandoffTask).where(
            HandoffTask.id == task_id, HandoffTask.version == expected_version,
            HandoffTask.status.in_(["pending", "claimed"]),
            HandoffTask.assignment_target_user_id.is_(None),
        ).values(assignment_target_user_id=target.id, assignment_target_agent_id=agent_id,
                 version=expected_version + 1, updated_at=utcnow()))
        if not claimed.rowcount:
            db.rollback()
            raise HTTPException(409, detail={"code": "handoff_conflict", "message": "任务已更新或分配结果待核对，请刷新"})
        # Persist the reservation before HTTP; do not hold SQLite's write lock
        # across the network. Pending/claimed still blocks automatic replies.
        db.commit()
        try:
            client.assign_conversation(remote_id, agent_id)
        except Exception as exc:
            db.rollback()
            # Only explicit rejections are safe to release. Timeouts, 5xx and
            # process interruption retain the intent for read-only reconciliation.
            if isinstance(exc, ChatwootError) and exc.status_code in {400, 401, 403, 404, 422, 429}:
                db.execute(update(HandoffTask).where(
                    HandoffTask.id == task_id, HandoffTask.version == expected_version + 1,
                    HandoffTask.assignment_target_user_id == target.id,
                ).values(assignment_target_user_id=None, assignment_target_agent_id=None,
                         version=expected_version + 2, updated_at=utcnow()))
                db.commit()
                raise HTTPException(502, detail={"code": "assignment_rejected", "message": "Chatwoot 拒绝分配，请刷新后检查客服配置"}) from exc
            raise HTTPException(502, detail={"code": "assignment_unknown", "message": "分配结果待核对，请点击核对分配；不要重复领取"}) from exc
    finally:
        client.close()
    return finish_assignment(db, task_id, target, agent_id, actor, expected_version + 1)


@router.post("/handoffs/{task_id}/reconcile-assignment")
def reconcile_assignment(task_id: int, payload: HandoffVersion, user: User = Depends(require_csrf), db: Session = Depends(get_db)):
    task, conversation = visible_task(db, user, task_id)
    if task.version != payload.version or task.assignment_target_user_id is None:
        raise HTTPException(409, detail={"code": "handoff_conflict", "message": "任务已变化，请刷新"})
    target = db.get(User, task.assignment_target_user_id)
    agent_id = task.assignment_target_agent_id
    client = client_for(get_connection(db))
    db.commit()
    try:
        remote = client.get_conversation(conversation.chatwoot_conversation_id)
        remote = remote.get("payload", remote)
        assignee = (remote.get("meta") or {}).get("assignee") or {}
        if remote.get("id") != conversation.chatwoot_conversation_id or assignee.get("id") != agent_id:
            raise HTTPException(409, detail={"code": "assignment_not_confirmed", "message": f"尚未确认目标客服。请在 Chatwoot 核对并分配给客服 #{agent_id}，再核对分配"})
    except ChatwootError as exc:
        raise HTTPException(502, detail={"code": "assignment_unknown", "message": "暂时无法读取 Chatwoot，请稍后核对"}) from exc
    finally:
        client.close()
    return finish_assignment(db, task_id, target, agent_id, user, payload.version)


@router.post("/handoffs/{task_id}/claim")
def claim_handoff(task_id: int, payload: HandoffVersion, user: User = Depends(require_csrf), db: Session = Depends(get_db)):
    task, conversation = visible_task(db, user, task_id)
    return claim_for(db, task, conversation, user, user, payload.version)


@router.post("/handoffs/{task_id}/assign")
def assign_handoff(task_id: int, payload: HandoffAssign, user: User = Depends(manager_csrf), db: Session = Depends(get_db)):
    task, conversation = visible_task(db, user, task_id)
    target = db.get(User, payload.user_id)
    if not target or not target.active:
        raise HTTPException(422, detail={"code": "invalid_assignee", "message": "目标客服不可用"})
    return claim_for(db, task, conversation, target, user, payload.version)


@router.post("/handoffs/{task_id}/complete")
def complete_handoff(task_id: int, payload: HandoffVersion, user: User = Depends(require_csrf), db: Session = Depends(get_db)):
    task, _ = visible_task(db, user, task_id)
    if task.version != payload.version or task.status != "claimed" or task.assignment_target_user_id is not None:
        raise HTTPException(409, detail={"code": "handoff_conflict", "message": "任务状态已变化"})
    if user.role == "agent" and task.assignee_user_id != user.id:
        raise HTTPException(403, detail={"code": "permission_denied", "message": "只能完成自己的任务"})
    changed = db.execute(update(HandoffTask).where(
        HandoffTask.id == task_id, HandoffTask.version == payload.version,
        HandoffTask.status == "claimed", HandoffTask.assignment_target_user_id.is_(None),
    ).values(status="completed", completed_at=utcnow(), completed_by=user.id,
             updated_at=utcnow(), version=payload.version + 1))
    if not changed.rowcount:
        db.rollback()
        raise HTTPException(409, detail={"code": "handoff_conflict", "message": "任务已被其他人更新"})
    db.refresh(task)
    audit(db, user, "handoff.complete", "handoff", task.id)
    db.commit()
    return handoff_json(db, task)


@router.post("/handoffs/{task_id}/restore-ai")
def restore_ai(task_id: int, payload: HandoffVersion, user: User = Depends(manager_csrf), db: Session = Depends(get_db)):
    task, conversation = visible_task(db, user, task_id)
    if task.version != payload.version or task.status not in ("completed", "cancelled"):
        raise HTTPException(409, detail={"code": "handoff_conflict", "message": "请先完成当前人工任务后再恢复 AI"})
    active_task = db.scalar(select(HandoffTask.id).where(HandoffTask.conversation_state_id == conversation.id, HandoffTask.status.in_(["pending", "claimed"])))
    if active_task:
        raise HTTPException(409, detail={"code": "handoff_conflict", "message": "该会话仍有未完成的人工任务"})
    unresolved = db.scalars(select(OutboundMessage.id).where(
        OutboundMessage.conversation_state_id == conversation.id,
        OutboundMessage.status.in_(["submission_unknown", "unknown"]),
    )).all()
    if unresolved:
        raise HTTPException(409, detail={
            "code": "delivery_reconciliation_required",
            "message": "存在发送结果未知的消息，请先核对渠道送达记录；尚未恢复 AI，也不会自动重发。",
            "outbound_ids": list(unresolved),
        })
    mappings = setting_value(db, "label_mappings", LabelMappings().model_dump())
    client = client_for(get_connection(db))
    try:
        catalog = client.list_labels()
        catalog = catalog.get("payload", []) if isinstance(catalog, dict) else catalog
        if not any(str(item.get("title", "")).casefold() == AI_CONTROL_LABEL for item in catalog or []):
            client.create_label(AI_CONTROL_LABEL, "AI 自动接管会话", "#16A34A", True)
        current = client.get_conversation_labels(conversation.chatwoot_conversation_id)
        labels = current.get("payload", []) if isinstance(current, dict) else current
        final = [x for x in labels if x not in mappings["handoff_labels"] and str(x).casefold() != AI_CONTROL_LABEL]
        final.append(AI_CONTROL_LABEL)
        client.set_conversation_labels(conversation.chatwoot_conversation_id, final)
    finally:
        client.close()
    conversation.labels = final
    conversation.ai_mode, conversation.ai_mode_source, conversation.ai_mode_updated_at = "enabled", "platform", utcnow()
    observe_ai_label(conversation, final, "platform")
    tenant = db.get(Tenant, conversation.tenant_id)
    conversation.effective_ai_state, conversation.effective_state_reason = compute_state(
        tenant,
        conversation.inbox,
        final,
        conversation.contact.labels if conversation.contact else [],
        conversation.can_reply,
        conversation.ai_mode,
        conversation.ai_sync_status,
        conversation.ai_label_present,
    )
    conversation.version += 1
    audit(db, user, "handoff.restore_ai", "handoff", task.id)
    db.commit()
    return {"ok": True, "labels": final, "ai_state": conversation.effective_ai_state}


def sop_json(db: Session, row: SopDefinition) -> dict:
    enrollments = db.scalar(select(func.count()).select_from(SopEnrollment).where(SopEnrollment.sop_id == row.id)) or 0
    sent = db.scalar(select(func.count()).select_from(SopJob).join(SopEnrollment, SopJob.enrollment_id == SopEnrollment.id).where(SopEnrollment.sop_id == row.id, SopJob.status.in_(["submitted", "delivered"]))) or 0
    blocked = db.scalar(select(func.count()).select_from(SopJob).join(SopEnrollment, SopJob.enrollment_id == SopEnrollment.id).where(SopEnrollment.sop_id == row.id, SopJob.status == "skipped")) or 0
    from app.automation_models import (LiveSopEnrollment, LiveSopJob, RehearsalEnrollment,
                                       RehearsalJob, SopVersion)
    simulated = select(RehearsalJob).join(RehearsalEnrollment).where(RehearsalEnrollment.sop_id == row.id)
    simulated_enrolled = db.scalar(select(func.count()).select_from(RehearsalEnrollment).where(RehearsalEnrollment.sop_id == row.id)) or 0
    simulated_sent = db.scalar(select(func.count()).select_from(simulated.where(RehearsalJob.status == "simulated_delivered").subquery())) or 0
    simulated_blocked = db.scalar(select(func.count()).select_from(simulated.where(RehearsalJob.status.in_(["blocked", "skipped", "cancelled", "expired"])).subquery())) or 0
    live_enrolled = db.scalar(select(func.count()).select_from(LiveSopEnrollment).where(LiveSopEnrollment.sop_id == row.id)) or 0
    live_sent = db.scalar(select(func.count()).select_from(LiveSopJob).join(
        LiveSopEnrollment, LiveSopJob.enrollment_id == LiveSopEnrollment.id).where(
            LiveSopEnrollment.sop_id == row.id, LiveSopJob.status == "submitted")) or 0
    live_blocked = db.scalar(select(func.count()).select_from(LiveSopJob).join(
        LiveSopEnrollment, LiveSopJob.enrollment_id == LiveSopEnrollment.id).where(
            LiveSopEnrollment.sop_id == row.id,
            LiveSopJob.status.in_(["blocked", "cancelled", "failed", "submission_unknown"]))) or 0
    published = db.scalar(select(SopVersion).where(SopVersion.sop_id == row.id).order_by(SopVersion.version.desc()))
    return {"id": row.id, "name": row.name, "description": row.description, "status": row.status,
            "version": row.version, "published_version": published.version if published else None,
            "published_nodes": published.config.get("nodes", []) if published else [],
            "published_at": published.created_at if published else None,
            "has_unpublished_changes": not published or published.version != row.version,
            "dry_run": row.dry_run, "live_enabled": row.live_enabled, "trigger_type": row.trigger_type,
            "trigger_labels": row.trigger_labels, "inbox_ids": row.inbox_ids, "nodes": row.nodes,
            "exit_labels": row.exit_labels, "stop_on_incoming": row.stop_on_incoming,
            "frequency_hours": row.frequency_hours, "route_variant": row.route_variant,
            "test_conversation_ids": row.test_conversation_ids, "enrolled": enrollments, "sent": sent,
            "blocked": blocked, "rehearsal_enrolled": simulated_enrolled,
            "rehearsal_sent": simulated_sent, "rehearsal_blocked": simulated_blocked,
            "live_enrolled": live_enrolled, "live_sent": live_sent, "live_blocked": live_blocked,
            "updated_at": row.updated_at}


@router.get("/sops/options")
def sop_options(user: User = Depends(manager), db: Session = Depends(get_db)):
    from app.automation_service import blocking_labels
    rollout = reception_rollout(db)
    return {"labels": [{"title": x.title, "color": x.color} for x in db.scalars(select(ChatwootLabel).order_by(ChatwootLabel.title))],
            "locked_labels": sorted(blocking_labels(db)),
            "delivery_mode": f"live_{rollout.scope}" if settings.live_sop_enabled else "rehearsal_only",
            "live_sop_scope": rollout.scope,
            "live_test_enabled": bool(
                settings.live_sop_enabled
                and (
                    not rollout.allowlist_enabled
                    or rollout.conversation_ids
                )
            ),
            "live_test_conversation_ids": sorted(rollout.conversation_ids) if user.role in ("admin", "super_admin") else []}


@router.get("/sops")
def sops(status: str = "", q: str = "", user: User = Depends(manager), db: Session = Depends(get_db)):
    rows = db.scalars(select(SopDefinition).order_by(SopDefinition.updated_at.desc())).all()
    allowed=allowed_inbox_ids(db,user)
    if allowed is not None:
        remote=set(db.scalars(select(InboxBinding.chatwoot_inbox_id).where(InboxBinding.id.in_(allowed))).all())
        rows=[x for x in rows if x.inbox_ids and set(x.inbox_ids).issubset(remote)]
    if status:
        rows = [x for x in rows if x.status == status]
    if q:
        rows = [x for x in rows if q.casefold() in f"{x.name} {x.description}".casefold()]
    return {"items": [sop_json(db, x) for x in rows]}


@router.post("/sops")
def create_sop(payload: SopCreate, user: User = Depends(manager_csrf), db: Session = Depends(get_db)):
    tenant = db.scalar(select(Tenant))
    row = SopDefinition(tenant_id=tenant.id, created_by=user.id, **payload.model_dump(mode="json"))
    from app.automation_api import sop_scope
    sop_scope(db,user,row)
    if settings.app_profile=="evaluation":row.dry_run,row.live_enabled=True,False
    db.add(row)
    audit(db, user, "sop.create", "sop", details={"name": payload.name})
    db.commit()
    return sop_json(db, row)


@router.get("/sops/{sop_id}")
def get_sop(sop_id: int, user: User = Depends(manager), db: Session = Depends(get_db)):
    row = db.get(SopDefinition, sop_id)
    if not row:
        raise HTTPException(404, detail={"code": "sop_not_found", "message": "SOP 不存在"})
    from app.automation_api import sop_scope
    sop_scope(db, user, row)
    return sop_json(db, row)


@router.patch("/sops/{sop_id}")
def update_sop(sop_id: int, payload: SopUpdate, user: User = Depends(manager_csrf), db: Session = Depends(get_db)):
    row = db.get(SopDefinition, sop_id)
    if not row or row.status == "archived":
        raise HTTPException(404, detail={"code": "sop_not_found", "message": "SOP 不存在"})
    from app.automation_api import sop_scope
    sop_scope(db, user, row)
    if payload.expected_version is not None and payload.expected_version != row.version:
        raise HTTPException(409, detail={"code":"version_conflict", "message":"策略已更新，请刷新"})
    old_version=row.version
    if not db.execute(update(SopDefinition).where(SopDefinition.id==row.id,SopDefinition.version==old_version).values(version=old_version+1)).rowcount:
        raise HTTPException(409,detail={"code":"version_conflict","message":"策略已更新，请刷新"})
    for key, value in payload.model_dump(mode="json", exclude={"expected_version"}).items():
        setattr(row, key, value)
    if settings.app_profile=="evaluation":row.dry_run,row.live_enabled=True,False
    sop_scope(db, user, row)
    row.version, row.updated_at = old_version + 1, utcnow()
    audit(db, user, "sop.update", "sop", row.id)
    db.commit()
    return sop_json(db, row)


def sop_errors(row: SopDefinition) -> list[str]:
    errors = []
    if row.trigger_type in ("label", "stage") and not row.trigger_labels:
        errors.append("标签触发至少需要一个入组标签")
    if set(row.trigger_labels) & set(row.exit_labels):
        errors.append("入组标签不能同时作为退出标签")
    if not row.nodes:
        errors.append("至少需要一个消息节点")
    keys = [node.get("key") for node in row.nodes]
    if len(keys) != len(set(keys)):
        errors.append("节点 key 不能重复")
    from app.sop_schedule import content_items
    for index, node in enumerate(row.nodes):
        items = content_items(node)
        if not items or any(not item.get("content", "").strip() and not item.get("media_id") for item in items):
            errors.append(f"节点 {node.get('key')} 缺少消息内容")
        item_keys = [item.get("key") for item in items]
        if len(item_keys) != len(set(item_keys)):
            errors.append(f"节点 {node.get('key')} 的消息 key 不能重复")
        if index == 0 and node.get("basis") == "previous_node" and node.get("schedule_type") == "relative":
            errors.append("第一个节点不能相对于上一组发送")
        if node.get("schedule_type") == "fixed" and not node.get("fixed_at"):
            errors.append(f"节点 {node.get('key')} 缺少固定时间")
    return errors


@router.post("/sops/{sop_id}/validate")
def validate_sop(sop_id: int, user: User = Depends(manager_csrf), db: Session = Depends(get_db)):
    row = db.get(SopDefinition, sop_id)
    from app.automation_api import sop_scope
    from app.automation_service import media_error, dt
    sop_scope(db, user, row)
    errors = sop_errors(row) if row else ["SOP 不存在"]
    if row:
        from app.automation_service import blocking_labels
        known = set(db.scalars(select(ChatwootLabel.title).where(ChatwootLabel.tenant_id == row.tenant_id)).all())
        if set(row.trigger_labels) & blocking_labels(db):
            errors.append("入组标签不能使用人工、退订、留资等安全阻断标签")
        unknown = (set(row.trigger_labels) | set(row.exit_labels)) - known
        if unknown:
            errors.append("标签尚未同步或不存在：" + "、".join(sorted(unknown)))
        known_inboxes = set(db.scalars(select(InboxBinding.chatwoot_inbox_id).where(InboxBinding.tenant_id == row.tenant_id)).all())
        if set(row.inbox_ids) - known_inboxes:
            errors.append("适用收件箱不存在")
    for node in row.nodes if row else []:
        problem = media_error(db,node)
        if problem: errors.append(f"{node['key']}: {problem}")
        if node.get("schedule_type")=="fixed" and node.get("fixed_at"):
            try: dt(node["fixed_at"])
            except ValueError: errors.append("固定时间格式错误")
    if row:
        from app.material_library import freeze_nodes
        try:
            freeze_nodes(db, row.nodes, row.route_variant or "", row.tenant_id)
        except ValueError as exc:
            errors.append(str(exc))
    warnings = ["当前策略为演练模式"] if row and row.dry_run else []
    if row and any(n.get("schedule_type") == "calendar_day" and n.get("day_number", 1) > 1 for n in row.nodes):
        warnings.append("第2天及以后的节点可能超出 Facebook/IG 自动消息窗口，到期时仍会阻断")
    if row and len(row.nodes) > 1:
        warnings.append("同一轮节点按各自时间执行；跨轮次、其他 SOP 与唤醒共享联系人频控")
    return {"valid": not errors, "errors": errors, "warnings": warnings}


@router.post("/sops/{sop_id}/publish")
def publish_sop(sop_id: int, user: User = Depends(require_super_admin_csrf), db: Session = Depends(get_db)):
    row = db.get(SopDefinition, sop_id)
    if not row:
        raise HTTPException(404, detail={"code": "sop_not_found", "message": "SOP 不存在"})
    errors = validate_sop(sop_id,user,db)["errors"]
    if errors:
        raise HTTPException(422, detail={"code": "sop_invalid", "message": "；".join(errors)})
    row.status, row.version, row.updated_at = "running", row.version + 1, utcnow()
    from app.automation_service import sop_snapshot
    sop_snapshot(db,row,user.id)
    audit(db, user, "sop.publish", "sop", row.id, {"dry_run": row.dry_run, "live_enabled": row.live_enabled})
    db.commit()
    return sop_json(db, row)


@router.post("/sops/{sop_id}/pause")
def pause_sop(sop_id: int, user: User = Depends(manager_csrf), db: Session = Depends(get_db)):
    row = db.get(SopDefinition, sop_id)
    if not row:
        raise HTTPException(404, detail={"code": "sop_not_found", "message": "SOP 不存在"})
    from app.automation_api import sop_scope
    sop_scope(db,user,row)
    row.status, row.updated_at = "paused", utcnow()
    audit(db, user, "sop.pause", "sop", row.id)
    db.commit()
    return sop_json(db, row)


def schedule_for(node: dict, base: datetime, previous: datetime) -> datetime:
    if node.get("schedule_type") == "fixed":
        return datetime.fromisoformat(node["fixed_at"].replace("Z", "+00:00"))
    basis = previous if node.get("basis") == "previous_node" else base
    return basis + timedelta(minutes=int(node.get("delay_minutes") or 0))


@router.post("/sops/{sop_id}/enroll")
def enroll_sop(sop_id: int, payload: SopEnroll, user: User = Depends(manager_csrf), db: Session = Depends(get_db)):
    sop = db.get(SopDefinition, sop_id)
    if not sop or sop.status not in ("running", "draft"):
        raise HTTPException(409, detail={"code": "sop_not_available", "message": "SOP 当前不可入组"})
    from app.automation_api import sop_scope, build_session, SessionCreate, published_scope
    from app.automation_service import enroll_rehearsal, subject_key
    from app.automation_models import SopVersion, AutomationSession, RehearsalEnrollment
    sop_scope(db,user,sop)
    if payload.allow_repeat_delivery and payload.environment != "live_test":
        raise HTTPException(422, detail={"code": "repeat_delivery_live_test_only",
                                        "message": "重复发送仅允许真实白名单测试使用"})
    if payload.environment in ("playground", "shadow"):
        version=db.scalar(select(SopVersion).where(SopVersion.sop_id==sop_id).order_by(SopVersion.version.desc()))
        if not version: raise HTTPException(409,detail={"code":"published_version_required","message":"请先发布审核版本"})
        if sop.status != "running":raise HTTPException(409,detail={"code":"sop_not_running","message":"策略尚未运行"})
        published_scope(db,user,version.config)
        selected=[]
        for conversation_id in set(payload.conversation_ids):
            conversation=db.scalar(select(ConversationState).where(ConversationState.chatwoot_conversation_id==conversation_id))
            if not conversation or not can_access_conversation(db,user,conversation):
                raise HTTPException(403,detail={"code":"inbox_forbidden","message":"无法访问该会话"})
            if version.config.get("inbox_ids") and conversation.inbox.chatwoot_inbox_id not in version.config["inbox_ids"]:
                raise HTTPException(422,detail={"code":"sop_inbox_mismatch","message":"客户不在已发布策略的适用收件箱内"})
            selected.append(conversation)
        session_ids=[]
        rounds=[]
        for conversation in selected:
            query=select(AutomationSession).where(AutomationSession.conversation_state_id==conversation.id,AutomationSession.environment==payload.environment,AutomationSession.mode=="sop")
            if payload.environment == "playground":query=query.where(AutomationSession.owner_id==user.id)
            existing=db.scalar(query)
            if not existing:
                existing=build_session(SessionCreate(mode="sop",conversation_id=conversation.id),user,db)
                existing.environment=payload.environment
            if payload.request_key:
                repeated=db.scalar(select(RehearsalEnrollment).where(RehearsalEnrollment.sop_id==sop.id,RehearsalEnrollment.subject_key==subject_key(db,existing),RehearsalEnrollment.request_key==payload.request_key))
                if repeated:
                    session_ids.append(repeated.session_id)
                    rounds.append({"enrollment_id":repeated.id,"round_number":repeated.round_number,"status":repeated.status})
                    continue
            if payload.reenroll:
                refreshed=build_session(SessionCreate(mode="sop",conversation_id=conversation.id),user,db)
                existing.messages=refreshed.messages
                existing.controls={**existing.controls,**refreshed.controls}
                db.delete(refreshed)
            if payload.environment == "shadow":
                from app.automation_shadow import mirror_controls
                existing.controls={**existing.controls,**mirror_controls(conversation,db.get(Tenant,conversation.tenant_id),db),"observed_labels":conversation.labels,"shadow_reply_enabled":False}
            existing.virtual_now=utcnow()
            try:
                enrollment=enroll_rehearsal(db,existing,version,reenroll=payload.reenroll,request_key=payload.request_key)
            except ValueError as exc:
                db.rollback()
                raise HTTPException(409,detail={"code":str(exc),"message":{"sop_reenrollment_required":"本轮已结束，请明确选择重新入组", "sop_round_active":"已有进行中的轮次，不能重复入组"}.get(str(exc),str(exc))}) from exc
            session_ids.append(enrollment.session_id)
            rounds.append({"enrollment_id":enrollment.id,"round_number":enrollment.round_number,"status":enrollment.status})
        db.commit()
        return {"enrolled":len(session_ids),"session_ids":session_ids,"rounds":rounds,"outbound":False}
    if payload.environment == "live_test":
        rollout = reception_rollout(db)
        if user.role not in ("admin", "super_admin"):
            raise HTTPException(403, detail={"code": "admin_required", "message": "真实测试仅管理员可操作"})
        if not payload.confirm_live_delivery:
            raise HTTPException(422, detail={"code": "live_confirmation_required", "message": "请确认本次操作会向 Facebook 真实发送"})
        if not settings.live_sop_enabled:
            raise HTTPException(409, detail={"code": "live_sop_not_armed", "message": "真实 SOP 测试开关尚未启用"})
        if rollout.allowlist_enabled and not rollout.conversation_ids:
            raise HTTPException(409, detail={"code": "live_sop_not_armed", "message": "真实 SOP 白名单尚未配置"})
        if not payload.request_key:
            raise HTTPException(422, detail={"code": "request_key_required", "message": "真实入组必须带幂等请求编号"})
        version = db.scalar(select(SopVersion).where(SopVersion.sop_id == sop_id).order_by(SopVersion.version.desc()))
        if not version or sop.status != "running":
            raise HTTPException(409, detail={"code": "published_version_required", "message": "请先发布并运行审核版本"})
        published_scope(db, user, version.config)
        selected = []
        for conversation_id in set(payload.conversation_ids):
            conversation = db.scalar(select(ConversationState).where(
                ConversationState.chatwoot_conversation_id == conversation_id))
            if not conversation or not can_access_conversation(db, user, conversation):
                raise HTTPException(403, detail={"code": "inbox_forbidden", "message": "无法访问该会话"})
            if rollout.allowlist_enabled and conversation_id not in rollout.conversation_ids:
                raise HTTPException(422, detail={"code": "test_conversation_required", "message": "该会话不在当前 AI 接待白名单中"})
            selected.append(conversation)
        from app.live_sop import enroll_live_sop, validate_live_target
        for conversation in selected:
            try:
                validate_live_target(db, conversation, version)
            except Exception as exc:
                code = str(exc)
                raise HTTPException(409, detail={"code": code, "message": f"会话 #{conversation.chatwoot_conversation_id} 实发前检查未通过：{code}"}) from exc
        rounds = []
        for conversation in selected:
            try:
                enrollment = enroll_live_sop(
                    db, conversation, version, request_key=payload.request_key,
                    reenroll=payload.reenroll, allow_repeat_delivery=payload.allow_repeat_delivery)
            except ValueError as exc:
                db.rollback()
                code = str(exc)
                message = {"sop_reenrollment_required": "本轮已结束，请明确选择重新入组",
                           "sop_round_active": "已有进行中的真实轮次，不能重复入组",
                           "sop_round_reconciliation_required": "上一轮存在发送结果未知的内容，请先人工核对，禁止重新入组"}.get(code, code)
                raise HTTPException(409, detail={"code": code, "message": message}) from exc
            rounds.append({"enrollment_id": enrollment.id, "round_number": enrollment.round_number, "status": enrollment.status})
        audit(db, user, "sop.live_test_enroll", "sop", sop.id, {
            "conversation_ids": sorted(set(payload.conversation_ids)), "request_key": payload.request_key,
            "allow_repeat_delivery": payload.allow_repeat_delivery})
        db.commit()
        return {"enrolled": len(rounds), "rounds": rounds, "outbound": True, "environment": "live_test"}
    raise HTTPException(422, detail="unsupported_sop_environment")


@router.get("/sops/{sop_id}/enrollment-candidates")
def enrollment_candidates(sop_id: int, page: int = 1, q: str = "", environment: str = "shadow", user: User = Depends(manager), db: Session = Depends(get_db)):
    from app.automation_api import published_scope
    from app.automation_models import LiveSopEnrollment, SopVersion, RehearsalEnrollment
    version=db.scalar(select(SopVersion).where(SopVersion.sop_id==sop_id).order_by(SopVersion.version.desc()))
    if not version:raise HTTPException(409,detail={"code":"published_version_required","message":"请先发布策略"})
    published_scope(db,user,version.config)
    if environment not in ("shadow", "playground", "live_test") or page < 1:raise HTTPException(422,detail="invalid_filter")
    if environment == "live_test" and user.role not in ("admin", "super_admin"):
        raise HTTPException(403, detail="admin_required")
    query=select(ConversationState)
    allowed=allowed_inbox_ids(db,user)
    if allowed is not None:query=query.where(ConversationState.inbox_binding_id.in_(allowed))
    if version.config.get("inbox_ids"):
        query=query.where(ConversationState.inbox_binding_id.in_(select(InboxBinding.id).where(InboxBinding.chatwoot_inbox_id.in_(version.config["inbox_ids"]))))
    rollout = reception_rollout(db)
    if environment == "live_test":
        if rollout.allowlist_enabled:
            query=query.where(ConversationState.chatwoot_conversation_id.in_(rollout.conversation_ids or {-1}))
        else:
            query=query.where(
                ConversationState.ai_mode == "enabled",
                ConversationState.ai_label_present.is_(True),
                ConversationState.effective_ai_state == "AI_ACTIVE",
            )
    if q.strip():
        query=query.where(ConversationState.chatwoot_conversation_id==int(q)) if q.isdigit() else query.where(ConversationState.contact.has(Contact.name.contains(q.strip())))
    total=db.scalar(select(func.count()).select_from(query.subquery()))
    items=[]
    for conversation in db.scalars(query.order_by(ConversationState.updated_at.desc()).offset((page-1)*25).limit(25)):
        identity=f"contact:{conversation.tenant_id}:{conversation.contact_id}" if conversation.contact_id else f"conversation:{conversation.tenant_id}:{conversation.id}"
        if environment == "live_test":
            last=db.scalar(select(LiveSopEnrollment).where(
                LiveSopEnrollment.sop_id == sop_id,
                LiveSopEnrollment.subject_key == f"live:{identity}").order_by(LiveSopEnrollment.round_number.desc()))
        else:
            last=db.scalar(select(RehearsalEnrollment).where(RehearsalEnrollment.sop_id==sop_id,RehearsalEnrollment.subject_key==f"{environment}:{identity}").order_by(RehearsalEnrollment.round_number.desc()))
        items.append({"id":conversation.chatwoot_conversation_id,"name":conversation.contact.name if conversation.contact else "未知客户","inbox":conversation.inbox.name,"round_number":last.round_number if last else 0,"round_status":last.status if last else None})
    return {"items":items,"total":total,"page":page,"page_size":25,"published_version":version.version}


@router.get("/sops/{sop_id}/executions")
def sop_executions(sop_id: int, user: User = Depends(manager), db: Session = Depends(get_db)):
    from app.automation_api import sop_scope
    from app.automation_models import (AutomationSession, LiveSopEnrollment, LiveSopJob,
                                       RehearsalJob, RehearsalEnrollment, SopVersion)
    from app.sop_schedule import content_items
    sop_scope(db,user,db.get(SopDefinition,sop_id))
    rows = db.scalars(select(SopJob).join(SopEnrollment, SopJob.enrollment_id == SopEnrollment.id).where(SopEnrollment.sop_id == sop_id).order_by(SopJob.scheduled_at.desc())).all()
    items = []
    for job in rows:
        enrollment = db.get(SopEnrollment, job.enrollment_id)
        conversation = db.get(ConversationState, enrollment.conversation_state_id)
        items.append({"id": job.id, "conversation_id": conversation.chatwoot_conversation_id, "node_key": job.node_key, "scheduled_at": job.scheduled_at, "status": job.status, "skip_reason": job.skip_reason, "attempts": job.attempts, "outbound_message_id": job.outbound_message_id})
    simulated=db.execute(select(RehearsalJob,AutomationSession,RehearsalEnrollment).join(RehearsalEnrollment,RehearsalJob.enrollment_id==RehearsalEnrollment.id).join(SopVersion,RehearsalEnrollment.sop_version_id==SopVersion.id).join(AutomationSession,RehearsalEnrollment.session_id==AutomationSession.id).where(SopVersion.sop_id==sop_id)).all()
    for job,session,enrollment in simulated:
        if session.inbox_binding_id and allowed_inbox_ids(db,user) is not None and session.inbox_binding_id not in allowed_inbox_ids(db,user):continue
        if not session.conversation_state_id and session.owner_id != user.id and not is_admin(user):continue
        conversation=db.get(ConversationState,session.conversation_state_id) if session.conversation_state_id else None
        items.append({"id":f"sim:{job.id}","conversation_id":conversation.chatwoot_conversation_id if conversation else None,"node_key":job.node_key,"scheduled_at":job.scheduled_at,"status":job.status,"skip_reason":job.reason,"attempts":0,"outbound_message_id":None,"session_id":session.id,"round_number":enrollment.round_number,"environment":session.environment,"delivery_items":job.payload.get("delivery_items",[])})
    live = db.execute(select(LiveSopJob, LiveSopEnrollment).join(
        LiveSopEnrollment, LiveSopJob.enrollment_id == LiveSopEnrollment.id).where(
            LiveSopEnrollment.sop_id == sop_id)).all()
    for job, enrollment in live:
        conversation = db.get(ConversationState, enrollment.conversation_state_id)
        delivery_items = []
        for item in content_items(job.payload):
            out = db.scalar(select(OutboundMessage).where(
                OutboundMessage.idempotency_key == f"live-sop:{enrollment.id}:{job.node_key}:{item.get('key')}"))
            if out:
                delivery_items.append({"key": item.get("key"), "status": out.status,
                                       "outbound_message_id": out.id,
                                       "chatwoot_message_id": out.chatwoot_message_id})
        items.append({"id": f"live:{job.id}", "conversation_id": conversation.chatwoot_conversation_id,
                      "node_key": job.node_key, "scheduled_at": job.scheduled_at, "status": job.status,
                      "skip_reason": job.reason, "attempts": job.attempts, "round_number": enrollment.round_number,
                      "environment": "live_test", "allow_repeat_delivery": enrollment.allow_repeat_delivery,
                      "delivery_items": delivery_items})
    return {"items": items}


@router.post("/media")
async def upload_media(file: UploadFile = File(...), user: User = Depends(manager_csrf), db: Session = Depends(get_db)):
    allowed = {"image": "image/", "video": "video/", "audio": "audio/"}
    mime = file.content_type or "application/octet-stream"
    media_type = next((key for key, prefix in allowed.items() if mime.startswith(prefix)), "file")
    content = await file.read(20 * 1024 * 1024 + 1)
    if not content:
        raise HTTPException(422, detail={"code": "media_empty", "message": "不能上传空文件"})
    if len(content) > 20 * 1024 * 1024:
        raise HTTPException(413, detail={"code": "media_too_large", "message": "附件不能超过 20MB"})
    tenant = db.scalar(select(Tenant))
    safe_name = f"{secrets.token_hex(12)}{Path(file.filename or 'file').suffix.lower()}"
    target = Path(settings.upload_dir) / safe_name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    row = StoredMedia(tenant_id=tenant.id, original_name=file.filename or safe_name, media_type=media_type, mime_type=mime, file_size=len(content), storage_path=str(target.resolve()), created_by=user.id)
    db.add(row)
    audit(db, user, "media.upload", "media", details={"name": row.original_name, "size": row.file_size})
    db.commit()
    return {"id": row.id, "name": row.original_name, "media_type": row.media_type, "mime_type": row.mime_type, "file_size": row.file_size}


def date_floor(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=max(0, days - 1))).date().isoformat()


@router.get("/bi/overview")
def bi_overview(days: int = 7, inbox_id: int | None = None, user: User = Depends(manager), db: Session = Depends(get_db)):
    since = date_floor(days)
    conversations = db.scalars(select(ConversationState).where(ConversationState.updated_at >= since)).all()
    allowed = allowed_inbox_ids(db, user)
    conversations = [x for x in conversations if (allowed is None or x.inbox_binding_id in allowed) and (not inbox_id or x.inbox.chatwoot_inbox_id == inbox_id)]
    ids = [x.id for x in conversations]
    messages = db.scalars(select(MessageEvent).where(MessageEvent.conversation_state_id.in_(ids), MessageEvent.created_at >= since)).all() if ids else []
    ai_ids = {x.conversation_state_id for x in db.scalars(select(AiRun).where(AiRun.conversation_state_id.in_(ids), AiRun.status == "completed", AiRun.action == "reply")).all()} if ids else set()
    human_ids = {x.conversation_state_id for x in messages if x.direction == "outgoing" and x.attribution in ("human", "inferred_human")}
    handoffs = db.scalars(select(HandoffTask).where(HandoffTask.conversation_state_id.in_(ids), HandoffTask.created_at >= since)).all() if ids else []
    mappings = setting_value(db, "label_mappings", LabelMappings().model_dump())
    cutover = db.scalar(select(Tenant)).analytics_cutover_at or utcnow()
    # Event-time conversion metrics share authorization/inbox scope, but must not
    # exclude older conversations merely because their mirror was not updated.
    scoped_ids = select(ConversationState.id)
    if allowed is not None:
        scoped_ids = scoped_ids.where(ConversationState.inbox_binding_id.in_(allowed))
    if inbox_id:
        scoped_ids = scoped_ids.join(InboxBinding).where(InboxBinding.chatwoot_inbox_id == inbox_id)
    label_query = select(LabelEvent).where(LabelEvent.conversation_state_id.in_(scoped_ids), LabelEvent.created_at >= max(since, cutover), LabelEvent.action == "added")
    lead_ids = {x.conversation_state_id for x in db.scalars(label_query.where(LabelEvent.label.in_(mappings["lead_labels"]))).all()}
    conversion_ids = {x.conversation_state_id for x in db.scalars(label_query.where(LabelEvent.label.in_(mappings["conversion_labels"]))).all()}
    ai_runs = db.scalars(select(AiRun).where(AiRun.conversation_state_id.in_(ids), AiRun.created_at >= since)).all() if ids else []
    return {"range_days": days, "conversations": len(conversations), "incoming_messages": sum(x.direction == "incoming" and not x.private for x in messages), "outgoing_messages": sum(x.direction == "outgoing" and not x.private for x in messages), "ai_only": len(ai_ids - human_ids), "mixed": len(ai_ids & human_ids), "human_only": len(human_ids - ai_ids), "ai_handled": len(ai_ids), "handoffs": len(handoffs), "handoff_pending": sum(x.status == "pending" for x in handoffs), "handoff_overdue": sum(x.status in ("pending", "claimed") and x.sla_due_at and x.sla_due_at < utcnow() for x in handoffs), "leads": len(lead_ids), "conversions": len(conversion_ids), "ai_errors": sum(x.status == "failed" for x in ai_runs), "inferred_history": sum(x.attribution == "inferred_human" for x in messages)}


@router.get("/bi/trends")
def bi_trends(days: int = 7, user: User = Depends(manager), db: Session = Depends(get_db)):
    since = date_floor(days)
    query = select(MessageEvent).join(ConversationState, MessageEvent.conversation_state_id == ConversationState.id).where(MessageEvent.created_at >= since)
    allowed = allowed_inbox_ids(db, user)
    if allowed is not None:
        query = query.where(ConversationState.inbox_binding_id.in_(allowed))
    rows = db.scalars(query).all()
    buckets: dict[str, dict] = {}
    for row in rows:
        day = row.created_at[:10]
        bucket = buckets.setdefault(day, {"day": day, "incoming": 0, "ai": 0, "human": 0})
        if row.direction == "incoming": bucket["incoming"] += 1
        elif row.attribution == "ai": bucket["ai"] += 1
        elif row.direction == "outgoing": bucket["human"] += 1
    return {"items": [buckets[key] for key in sorted(buckets)]}


@router.get("/bi/funnel")
def bi_funnel(days: int = 30, user: User = Depends(manager), db: Session = Depends(get_db)):
    overview = bi_overview(days, None, user, db)
    return {"items": [{"name": "新增会话", "value": overview["conversations"]}, {"name": "AI 已参与", "value": overview["ai_handled"]}, {"name": "已留资", "value": overview["leads"]}, {"name": "已成交", "value": overview["conversions"]}]}


@router.get("/bi/handoff-reasons")
def bi_handoff_reasons(days: int = 30, user: User = Depends(manager), db: Session = Depends(get_db)):
    since = date_floor(days)
    query = select(HandoffTask.reason_code, func.count()).join(ConversationState, HandoffTask.conversation_state_id == ConversationState.id).where(HandoffTask.created_at >= since)
    allowed = allowed_inbox_ids(db, user)
    if allowed is not None:
        query = query.where(ConversationState.inbox_binding_id.in_(allowed))
    rows = db.execute(query.group_by(HandoffTask.reason_code)).all()
    return {"items": [{"reason": reason, "count": count} for reason, count in rows]}


@router.get("/bi/sop-performance")
def bi_sop_performance(user: User = Depends(manager), db: Session = Depends(get_db)):
    rows = db.scalars(select(SopDefinition).order_by(SopDefinition.updated_at.desc())).all()
    allowed = allowed_inbox_ids(db, user)
    if allowed is not None:
        chatwoot_ids = set(db.scalars(select(InboxBinding.chatwoot_inbox_id).where(InboxBinding.id.in_(allowed))).all())
        rows = [row for row in rows if not row.inbox_ids or bool(set(row.inbox_ids) & chatwoot_ids)]
    return {"items": [sop_json(db, row) for row in rows]}


def notification_visibility(db: Session, user: User):
    audience = or_(Notification.user_id.is_(None), Notification.user_id == user.id)
    allowed = allowed_inbox_ids(db, user)
    if allowed is None:
        return audience
    visible_conversation = select(ConversationState.id).where(
        ConversationState.tenant_id == Notification.tenant_id,
        ConversationState.chatwoot_conversation_id == Notification.conversation_id,
        ConversationState.inbox_binding_id.in_(allowed),
    ).exists()
    # Unscoped operational broadcasts are admin-only; personal system notices
    # remain visible to their recipient. Missing conversations fail closed.
    return and_(audience, or_(visible_conversation, and_(
        Notification.conversation_id.is_(None), Notification.user_id == user.id,
    )))


@router.get("/notifications")
def notifications(unread_only: bool = False, user: User = Depends(current_user), db: Session = Depends(get_db)):
    query = select(Notification, NotificationRead.read_at).outerjoin(NotificationRead, and_(
        NotificationRead.notification_id == Notification.id, NotificationRead.user_id == user.id,
    )).where(notification_visibility(db, user))
    unread = db.scalar(select(func.count()).select_from(query.where(NotificationRead.read_at.is_(None)).subquery()))
    if unread_only:
        query = query.where(NotificationRead.read_at.is_(None))
    rows = db.execute(query.order_by(Notification.created_at.desc(), Notification.id.desc()).limit(50)).all()
    return {"items": [{"id": x.id, "event_type": x.event_type, "title": x.title, "body": x.body, "conversation_id": x.conversation_id, "read_at": read_at, "created_at": x.created_at} for x, read_at in rows], "unread": unread}


@router.post("/notifications/{notification_id}/read")
def read_notification(notification_id: int, user: User = Depends(require_csrf), db: Session = Depends(get_db)):
    row = db.scalar(select(Notification).where(Notification.id == notification_id, notification_visibility(db, user)))
    if not row:
        raise HTTPException(404, detail={"code": "notification_not_found", "message": "通知不存在"})
    if not db.get(NotificationRead, (row.id, user.id)):
        try:
            with db.begin_nested():
                db.add(NotificationRead(notification_id=row.id, user_id=user.id))
                db.flush()
        except IntegrityError:
            if not db.get(NotificationRead, (row.id, user.id)):
                raise
    db.commit()
    return {"ok": True}


@router.get("/audit-logs")
def audit_logs(page: int = 1, page_size: int = 50, user: User = Depends(super_admin), db: Session = Depends(get_db)):
    rows = db.scalars(select(AuditLog).order_by(AuditLog.created_at.desc()).offset((page - 1) * page_size).limit(page_size)).all()
    return {"items": [{"id": x.id, "user_id": x.user_id, "action": x.action, "resource_type": x.resource_type, "resource_id": x.resource_id, "details": x.details, "created_at": x.created_at} for x in rows], "page": page}
