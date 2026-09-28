import json
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.auth import current_user, require_csrf, require_super_admin_csrf, super_admin
from app.chatwoot import ChatwootError, normalize_collection
from app.chatwoot_service import client_for, payload_dict, string_payload
from app.config import settings
from app.conversation_policy import AI_CONTROL_LABEL, compute_state, observe_ai_label
from app.db import get_db
from app.runtime_settings import dt
from app.history_sync import attachment_placeholder, message_timestamp, sync_conversation_history
from app.live_reply_models import LiveReplyJob
from app.conversation_views import capture_json
from app.lead_capture_models import LeadCaptureState
from app.models import (
    AppSession,
    AppSetting,
    ChatwootConnection,
    ChatwootAgent,
    ChatwootLabel,
    ChatwootTeam,
    Contact,
    ConversationJourney,
    ConversationState,
    InboxBinding,
    MessageEvent,
    OutboundMessage,
    Tenant,
    User,
    UserInboxScope,
    utcnow,
)
from app.operations import audit
from app.outbound_control import global_message_sending_enabled
from app.conversation_views import journey_context
from app.route_packages import route_quick_reply_titles
from app.schemas import (
    ChatwootConfigRequest,
    ConversationAssignmentRequest,
    ConversationAiModeRequest,
    ConversationLabelsRequest,
    ConversationQuickReplyRequest,
    InboxPolicyRequest,
    LabelCreateRequest,
    LoginRequest,
    ChangePasswordRequest,
    WebhookConfigRequest,
)
from app.security import digest, encrypt_secret, expires_at, hash_password, random_token, verify_password, session_csrf_token
from app.webhook_ingest import AccountMismatchError, ingest_payload

router = APIRouter(prefix="/v1")
REQUIRED_EVENTS = {"message_created", "message_updated", "conversation_updated", "conversation_status_changed", "contact_updated"}
ALLOWED_EVENTS = REQUIRED_EVENTS | {"conversation_created", "contact_created"}


def user_json(user: User) -> dict:
    role = "admin" if user.role == "super_admin" else user.role
    permissions = ["*"] if role == "admin" else (["conversations:*", "handoffs:*", "sops:*", "bi:read"] if role == "supervisor" else ["conversations:read", "handoffs:own"])
    return {"id": user.id, "email": user.email, "display_name": user.display_name, "roles": [role], "permissions": permissions, "must_change_password": user.must_change_password, "chatwoot_agent_id": user.chatwoot_agent_id, "inbox_scopes": []}


def get_connection(db: Session) -> ChatwootConnection:
    connection = db.scalar(select(ChatwootConnection))
    if not connection or not connection.encrypted_api_token:
        raise HTTPException(409, detail={"code": "chatwoot_not_configured", "message": "Chatwoot 尚未配置"})
    return connection


def webhook_url(connection: ChatwootConnection) -> str:
    base_url = settings.public_api_base_url.rstrip("/")
    return f"{base_url}/v1/webhooks/chatwoot/{connection.connection_key}"


def assignment_json(remote: dict) -> dict:
    meta = remote.get("meta") if isinstance(remote.get("meta"), dict) else {}
    assignee = meta.get("assignee") if isinstance(meta.get("assignee"), dict) else None
    team = meta.get("team") if isinstance(meta.get("team"), dict) else None
    return {
        "assignee": {
            "id": int(assignee["id"]),
            "name": assignee.get("name") or assignee.get("available_name") or f"Agent {assignee['id']}",
            "availability_status": assignee.get("availability_status"),
            "thumbnail": assignee.get("thumbnail") or assignee.get("avatar_url"),
        } if assignee and assignee.get("id") else None,
        "team": {"id": int(team["id"]), "name": team.get("name") or f"Team {team['id']}"} if team and team.get("id") else None,
    }


def sync_local_labels(
    db: Session,
    row: ConversationState,
    labels: list[str],
    *,
    source: str = "chatwoot",
    ai_label_catalog_exists: bool = True,
) -> None:
    normalized = list(dict.fromkeys(labels))
    ai_state_changed = False
    if not ai_label_catalog_exists and row.ai_mode == "enabled":
        ai_state_changed = row.ai_label_present or row.ai_sync_status != "conflict"
        row.ai_label_present = False
        row.ai_sync_status = "conflict"
    else:
        ai_state_changed = observe_ai_label(row, normalized, source)
    tenant = db.get(Tenant, row.tenant_id)
    inbox = db.get(InboxBinding, row.inbox_binding_id)
    contact = db.get(Contact, row.contact_id) if row.contact_id else None
    state, reason = compute_state(
        tenant,
        inbox,
        normalized,
        contact.labels if contact else [],
        row.can_reply,
        row.ai_mode,
        row.ai_sync_status,
        row.ai_label_present,
    )
    if ai_state_changed or normalized != (row.labels or []) or state != row.effective_ai_state or reason != row.effective_state_reason:
        row.version += 1
    row.labels = normalized
    row.effective_ai_state = state
    row.effective_state_reason = reason
    row.updated_at = utcnow()


def raise_chatwoot_error(exc: ChatwootError, message: str) -> None:
    raise HTTPException(502, detail={"code": exc.code, "message": message}) from exc


@router.post("/auth/login")
def login(payload: LoginRequest, response: Response, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.email == payload.email.lower()))
    if not user or not user.active or not verify_password(user.password_hash, payload.password):
        raise HTTPException(401, detail={"code": "invalid_credentials", "message": "邮箱或密码错误"})
    session_token, csrf_token = random_token(), random_token()
    db.add(AppSession(session_hash=digest(session_token), csrf_hash=digest(csrf_token), user_id=user.id, expires_at=expires_at()))
    db.commit()
    response.set_cookie(
        "aiops_session",
        session_token,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
        domain=settings.session_cookie_domain or None,
        max_age=settings.session_ttl_hours * 3600,
    )
    return {"user": user_json(user), "csrf_token": csrf_token}


@router.post("/auth/logout", status_code=204)
def logout(response: Response, aiops_session: str | None = Cookie(default=None), user: User = Depends(require_csrf), db: Session = Depends(get_db)):
    session = db.scalar(select(AppSession).where(AppSession.session_hash == digest(aiops_session or "")))
    if session:
        session.revoked_at = utcnow()
        db.commit()
    response.delete_cookie("aiops_session", domain=settings.session_cookie_domain or None)


@router.get("/auth/me")
def me(user: User = Depends(current_user)):
    return user_json(user)


@router.get("/auth/csrf")
def csrf(response: Response, aiops_session: str | None = Cookie(default=None), user: User = Depends(current_user), db: Session = Depends(get_db)):
    session = db.scalar(select(AppSession).where(AppSession.session_hash == digest(aiops_session or ""), AppSession.revoked_at.is_(None)))
    if not session:
        raise HTTPException(401, detail={"code": "session_expired", "message": "登录已过期"})
    response.headers["Cache-Control"] = "no-store"
    # Preserve legacy login tokens so an already open tab remains valid.
    return {"csrf_token": session_csrf_token(aiops_session or ""), "expires_at": session.expires_at}


@router.post("/auth/change-password")
def change_password(payload: ChangePasswordRequest, user: User = Depends(require_csrf), db: Session = Depends(get_db), aiops_session: str | None = Cookie(default=None)):
    if not verify_password(user.password_hash, payload.current_password):
        raise HTTPException(422, detail={"code": "invalid_current_password", "message": "当前密码不正确"})
    if payload.current_password == payload.new_password:
        raise HTTPException(422, detail={"code": "password_unchanged", "message": "新密码不能与当前密码相同"})
    user.password_hash = hash_password(payload.new_password)
    user.must_change_password = False
    db.execute(update(AppSession).where(
        AppSession.user_id == user.id, AppSession.revoked_at.is_(None),
        AppSession.session_hash != digest(aiops_session or ""),
    ).values(revoked_at=utcnow()))
    db.commit()
    return user_json(user)


@router.get("/settings/chatwoot")
def read_chatwoot(user: User = Depends(super_admin), db: Session = Depends(get_db)):
    value = db.scalar(select(ChatwootConnection))
    if not value:
        return {"configured": False, "base_url": "https://app.chatwoot.com", "account_id": 180474}
    return {"configured": bool(value.encrypted_api_token), "base_url": value.base_url, "account_id": value.account_id, "token_last4": value.token_last4, "status": value.status, "last_tested_at": value.last_tested_at, "last_error": value.last_error, "webhook_id": value.webhook_id, "webhook_url": webhook_url(value)}


@router.put("/settings/chatwoot")
def save_chatwoot(payload: ChatwootConfigRequest, user: User = Depends(require_super_admin_csrf), db: Session = Depends(get_db)):
    tenant = db.scalar(select(Tenant))
    if not tenant:
        tenant = Tenant(name="china2go")
        db.add(tenant)
        db.flush()
    value = db.scalar(select(ChatwootConnection))
    if not value:
        value = ChatwootConnection(tenant_id=tenant.id, connection_key=secrets.token_urlsafe(32))
        db.add(value)
    value.base_url = str(payload.base_url).rstrip("/")
    value.account_id = payload.account_id
    if payload.api_token:
        value.encrypted_api_token = encrypt_secret(payload.api_token)
        value.token_last4 = payload.api_token[-4:]
    value.status = "configured" if value.encrypted_api_token else "unconfigured"
    db.commit()
    return read_chatwoot(user, db)


@router.post("/settings/chatwoot/test")
def test_chatwoot(user: User = Depends(require_super_admin_csrf), db: Session = Depends(get_db)):
    connection = get_connection(db)
    client = client_for(connection)
    try:
        inboxes = normalize_collection(client.list_inboxes())
        connection.status = "connected"
        connection.last_error = None
        connection.last_tested_at = utcnow()
        db.commit()
        return {"ok": True, "inbox_count": len(inboxes), "tested_at": connection.last_tested_at}
    except ChatwootError as exc:
        connection.status = "error"
        connection.last_error = exc.code
        connection.last_tested_at = utcnow()
        db.commit()
        raise HTTPException(502, detail={"code": exc.code, "message": "Chatwoot 连接测试失败"}) from exc
    finally:
        client.close()


@router.post("/settings/chatwoot/sync")
def sync_chatwoot(user: User = Depends(require_super_admin_csrf), db: Session = Depends(get_db)):
    connection = get_connection(db)
    client = client_for(connection)
    try:
        inboxes = normalize_collection(client.list_inboxes())
        tenant = db.get(Tenant, connection.tenant_id)
        for item in inboxes:
            inbox_id = int(item["id"])
            binding = db.scalar(select(InboxBinding).where(InboxBinding.tenant_id == tenant.id, InboxBinding.chatwoot_inbox_id == inbox_id))
            if not binding:
                binding = InboxBinding(tenant_id=tenant.id, chatwoot_inbox_id=inbox_id, name=item.get("name", f"Inbox {inbox_id}"))
                db.add(binding)
            binding.name = item.get("name", binding.name)
            channel = item.get("channel_type") or item.get("channel", {}).get("type") or "unknown"
            binding.channel_type = str(channel)
            binding.last_synced_at = utcnow()
        agents, teams, labels = normalize_collection(client.list_agents()), normalize_collection(client.list_teams()), normalize_collection(client.list_labels())
        inbox_members: dict[int, list[int]] = {}
        for inbox_item in inboxes:
            inbox_id = int(inbox_item["id"])
            inbox_members[inbox_id] = [int(a["id"]) for a in normalize_collection(client.list_inbox_agents(inbox_id)) if a.get("id")]
        for item in agents:
            agent_id = int(item["id"])
            row = db.scalar(select(ChatwootAgent).where(ChatwootAgent.tenant_id == tenant.id, ChatwootAgent.chatwoot_agent_id == agent_id))
            if not row:
                row = ChatwootAgent(tenant_id=tenant.id, chatwoot_agent_id=agent_id, name=str(item.get("name") or item.get("available_name") or agent_id))
                db.add(row)
            row.name = str(item.get("name") or item.get("available_name") or row.name)
            row.email = item.get("email")
            row.availability_status = str(item.get("availability_status") or "offline")
            row.role = str(item.get("role") or "agent")
            row.inbox_ids = [inbox_id for inbox_id, member_ids in inbox_members.items() if agent_id in member_ids]
            row.last_synced_at = utcnow()
        for item in teams:
            team_id = int(item["id"])
            row = db.scalar(select(ChatwootTeam).where(ChatwootTeam.tenant_id == tenant.id, ChatwootTeam.chatwoot_team_id == team_id))
            if not row:
                row = ChatwootTeam(tenant_id=tenant.id, chatwoot_team_id=team_id, name=str(item.get("name") or team_id))
                db.add(row)
            row.name, row.last_synced_at = str(item.get("name") or row.name), utcnow()
        for item in labels:
            label_id = int(item["id"])
            row = db.scalar(select(ChatwootLabel).where(ChatwootLabel.tenant_id == tenant.id, ChatwootLabel.chatwoot_label_id == label_id))
            if not row:
                row = ChatwootLabel(tenant_id=tenant.id, chatwoot_label_id=label_id, title=str(item.get("title") or label_id))
                db.add(row)
            row.title = str(item.get("title") or row.title)
            row.description = str(item.get("description") or "")
            row.color = str(item.get("color") or "#64748B")
            row.show_on_sidebar = bool(item.get("show_on_sidebar", True))
            row.last_synced_at = utcnow()
        db.commit()
        return {"inboxes": len(inboxes), "agents": len(agents), "teams": len(teams), "labels": len(labels), "synced_at": utcnow()}
    finally:
        client.close()


@router.get("/settings/inboxes")
def list_inboxes(user: User = Depends(current_user), db: Session = Depends(get_db)):
    query = select(InboxBinding).order_by(InboxBinding.id)
    allowed = allowed_binding_ids(db, user)
    if allowed is not None:
        query = query.where(InboxBinding.id.in_(allowed))
    rows = db.scalars(query).all()
    return [{"id": row.id, "chatwoot_inbox_id": row.chatwoot_inbox_id, "name": row.name, "channel_type": row.channel_type, "ai_enabled": row.ai_enabled, "status": row.status, "last_synced_at": row.last_synced_at} for row in rows]


@router.patch("/settings/inboxes/{binding_id}")
def update_inbox(binding_id: int, payload: InboxPolicyRequest, user: User = Depends(require_super_admin_csrf), db: Session = Depends(get_db)):
    row = db.get(InboxBinding, binding_id)
    if not row:
        raise HTTPException(404, detail={"code": "inbox_not_found", "message": "Inbox 不存在"})
    row.ai_enabled = payload.ai_enabled
    db.commit()
    return {"id": row.id, "ai_enabled": row.ai_enabled}


@router.get("/settings/chatwoot/webhook")
def read_webhook(user: User = Depends(super_admin), db: Session = Depends(get_db)):
    connection = get_connection(db)
    setting = db.get(AppSetting, "webhook_events")
    return {"webhook_id": connection.webhook_id, "url": webhook_url(connection), "events": setting.value.get("events", []) if setting else [], "last_received_at": connection.last_webhook_at}


@router.put("/settings/chatwoot/webhook")
def save_webhook(payload: WebhookConfigRequest, user: User = Depends(require_super_admin_csrf), db: Session = Depends(get_db)):
    events = set(payload.events)
    if not REQUIRED_EVENTS.issubset(events) or not events.issubset(ALLOWED_EVENTS):
        raise HTTPException(422, detail={"code": "invalid_webhook_events", "message": "Webhook 事件配置不完整"})
    connection = get_connection(db)
    url = webhook_url(connection)
    client = client_for(connection)
    try:
        result = client.update_webhook(connection.webhook_id, url, sorted(events)) if connection.webhook_id else client.create_webhook(url, sorted(events))
        webhook = result
        if isinstance(result, dict) and isinstance(result.get("payload"), dict):
            webhook = result["payload"].get("webhook", result["payload"])
        webhook_id = webhook.get("id") if isinstance(webhook, dict) else None
        if webhook_id:
            connection.webhook_id = int(webhook_id)
        row = db.get(AppSetting, "webhook_events") or AppSetting(key="webhook_events")
        row.value = {"events": sorted(events)}
        db.add(row)
        db.commit()
        return {"webhook_id": connection.webhook_id, "url": url, "events": sorted(events)}
    finally:
        client.close()


@router.post("/webhooks/chatwoot/{connection_key}", status_code=202)
async def receive_webhook(connection_key: str, request: Request, db: Session = Depends(get_db)):
    connection = db.scalar(select(ChatwootConnection).where(ChatwootConnection.connection_key == connection_key))
    if not connection:
        raise HTTPException(404, detail={"code": "webhook_not_found", "message": "Webhook 不存在"})
    raw = await request.body()
    if len(raw) > 2 * 1024 * 1024:
        raise HTTPException(413, detail={"code": "payload_too_large", "message": "Payload 过大"})
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(400, detail={"code": "invalid_json", "message": "JSON 无效"}) from exc
    try:
        result = ingest_payload(db, connection, payload, raw)
        return {"accepted": True, "duplicate": result.duplicate}
    except AccountMismatchError as exc:
        raise HTTPException(403, detail={"code": "account_mismatch", "message": "Account 不匹配"}) from exc


def allowed_binding_ids(db: Session, user: User) -> list[int] | None:
    if user.role in ("super_admin", "admin"):
        return None
    return list(db.scalars(select(UserInboxScope.inbox_binding_id).where(UserInboxScope.user_id == user.id)).all())


def visible_conversation(db: Session, user: User, conversation_id: int) -> ConversationState:
    query = select(ConversationState).where(ConversationState.chatwoot_conversation_id == conversation_id)
    allowed = allowed_binding_ids(db, user)
    if allowed is not None:
        query = query.where(ConversationState.inbox_binding_id.in_(allowed))
    row = db.scalar(query)
    if not row:
        raise HTTPException(404, detail={"code": "conversation_not_found", "message": "会话不存在"})
    return row


def conversation_json(row: ConversationState, account_id: int) -> dict:
    contact = row.contact
    return {"id": row.chatwoot_conversation_id, "name": contact.name if contact else "未知客户", "channel": row.inbox.channel_type, "inbox": row.inbox.name, "inbox_id": row.inbox.chatwoot_inbox_id, "last_message": row.last_message, "ai_state": row.effective_ai_state, "ai_reason": row.effective_state_reason, "ai_mode": row.ai_mode, "ai_mode_source": row.ai_mode_source, "ai_label_present": row.ai_label_present, "ai_sync_status": row.ai_sync_status, "labels": row.labels, "can_reply": row.can_reply, "status": row.status, "version": row.version, "updated_at": row.updated_at, "chatwoot_url": f"https://app.chatwoot.com/app/accounts/{account_id}/conversations/{row.chatwoot_conversation_id}"}


@router.get("/conversations")
def conversations(
    q: str = "",
    label: str = "",
    ai_state: str = "",
    inbox_id: int | None = None,
    days: int = 0,
    page: int = 1,
    page_size: int = 25,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    query = select(ConversationState).order_by(ConversationState.updated_at.desc())
    allowed = allowed_binding_ids(db, user)
    if allowed is not None:
        query = query.where(ConversationState.inbox_binding_id.in_(allowed))
    rows = db.scalars(query).all()
    if q:
        q_lower = q.lower()
        rows = [row for row in rows if q_lower in row.last_message.lower() or (row.contact and q_lower in row.contact.name.lower()) or q_lower in str(row.chatwoot_conversation_id)]
    if label:
        rows = [row for row in rows if label in (row.labels or [])]
    if ai_state:
        rows = [row for row in rows if row.effective_ai_state == ai_state]
    if inbox_id is not None:
        rows = [row for row in rows if row.inbox.chatwoot_inbox_id == inbox_id]
    if days > 0:
        since = (datetime.now(timezone.utc) - timedelta(days=min(days, 3650))).isoformat()
        rows = [row for row in rows if row.updated_at >= since]
    total = len(rows)
    rows = rows[(page - 1) * page_size: page * page_size]
    connection = db.scalar(select(ChatwootConnection))
    account_id = connection.account_id if connection else 180474
    return {"items": [conversation_json(row, account_id) for row in rows], "page": page, "page_size": page_size, "total": total}


@router.get("/conversations/filters")
def conversation_filters(user: User = Depends(current_user), db: Session = Depends(get_db)):
    allowed = allowed_binding_ids(db, user)
    inbox_query = select(InboxBinding).order_by(InboxBinding.name)
    if allowed is not None:
        inbox_query = inbox_query.where(InboxBinding.id.in_(allowed))
    inboxes = db.scalars(inbox_query).all()
    labels = db.scalars(select(ChatwootLabel).order_by(ChatwootLabel.title)).all()
    state_query = select(ConversationState.effective_ai_state).distinct().order_by(ConversationState.effective_ai_state)
    if allowed is not None:
        state_query = state_query.where(ConversationState.inbox_binding_id.in_(allowed))
    states = [value for value in db.scalars(state_query).all() if value]
    return {
        "inboxes": [{"id": row.chatwoot_inbox_id, "name": row.name, "channel": row.channel_type} for row in inboxes],
        "labels": [{"title": row.title, "color": row.color} for row in labels],
        "ai_states": states,
    }


@router.get("/conversations/{conversation_id}")
def conversation_detail(conversation_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    row = visible_conversation(db, user, conversation_id)
    connection = db.scalar(select(ChatwootConnection))
    result = conversation_json(row, connection.account_id if connection else 180474)
    contact = row.contact
    can_view_pii = user.role in ("super_admin", "admin", "supervisor") or (
        user.role == "agent" and user.chatwoot_agent_id and row.assignee_id == user.chatwoot_agent_id
    )

    def masked(value: str | None, kind: str) -> str | None:
        if not value or can_view_pii:
            return value
        if kind == "email" and "@" in value:
            name, domain = value.split("@", 1)
            return f"{name[:1]}***@{domain}"
        digits = "".join(character for character in value if character.isdigit())
        return f"***{digits[-4:]}" if digits else "***"

    result["contact"] = {
        "name": contact.name if contact else "未知客户",
        "email": masked(contact.email if contact else None, "email"),
        "phone_number": masked(contact.phone_number if contact else None, "phone"),
        "pii_masked": bool(contact and not can_view_pii),
    }
    latest = db.scalar(select(LiveReplyJob).where(LiveReplyJob.conversation_state_id == row.id)
                       .order_by(LiveReplyJob.id.desc()).limit(1))
    # Operational metadata only; never expose model prompts, decisions or raw errors here.
    result["latest_ai_reply"] = None if latest is None else {
        "status": "retrying" if latest.status == "queued" and latest.error_code == "model_retry_scheduled" else latest.status,
        "failure_kind": "model_timeout" if "timeout" in (latest.error_code or "").lower() else "reply_failed",
        "created_at": latest.created_at, "completed_at": latest.completed_at,
        "model_ms": latest.trace.get("model_ms"), "request_count": latest.trace.get("request_count"),
        "model_http_request_count": latest.trace.get("model_http_request_count"),
        "engine_version": latest.engine_version, "engine_release_id": latest.engine_release_id,
    }
    lead_capture = db.scalar(select(LeadCaptureState).where(
        LeadCaptureState.conversation_state_id == row.id
    ))
    result["lead_capture"] = capture_json(lead_capture)
    journey = db.scalar(select(ConversationJourney).where(
        ConversationJourney.conversation_state_id == row.id
    ))
    result["route_journey"] = journey_context(journey) if journey else {
        "knowledge_version": None,
        "route_variant": "",
        "stage": "route_selection",
        "slots": {},
        "sent_content_groups": [],
        "next_content_group": None,
        "allowed_content_groups": [],
    }
    from app.reception_v3.live import session_for
    from app.reception_v3.service import state as v3_state
    from app.automation_models import AutomationRun
    session = session_for(db, row.id)
    if session:
        value = v3_state(session)
        result['route_journey'] = {
            'route_variant': session.controls.get('route_variant', ''),
            'stage': 'handoff' if value.get('handoff') else value.get('delivery_kind') or 'consulting',
            'slots': session.memory if can_view_pii else {},
            'sent_content_groups': [], 'next_content_group': None, 'allowed_content_groups': [],
        }
        run = db.scalar(select(AutomationRun).where(AutomationRun.session_id == session.id)
                        .order_by(AutomationRun.id.desc()))
        result['latest_ai_reply'] = None if run is None else {
            'status': run.status, 'created_at': run.created_at, 'completed_at': run.completed_at,
            'model_ms': run.trace.get('total_ms'), 'model_http_request_count': run.trace.get('model_http_request_count'),
            'engine_version': 'v3', 'engine_release_id': session.engine_release_id,
        }
    return result


@router.post("/conversations/{conversation_id}/quick-replies")
def send_conversation_quick_replies(
    conversation_id: int,
    payload: ConversationQuickReplyRequest,
    user: User = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    row = visible_conversation(db, user, conversation_id)
    if not global_message_sending_enabled(db):
        raise HTTPException(409, detail={
            "code": "global_message_sending_disabled",
            "message": "全局消息发送已关闭，当前不会向客户发送任何消息",
        })
    if row.inbox.channel_type != "Channel::FacebookPage":
        raise HTTPException(422, detail={"code": "quick_replies_channel_unsupported", "message": "当前仅支持 Facebook Messenger"})

    business_key = f"quick-reply:{row.id}:{payload.request_key}"
    existing = db.scalar(select(OutboundMessage).where(OutboundMessage.idempotency_key == business_key))
    if existing:
        return {
            "ok": existing.status not in {"failed"},
            "duplicate": True,
            "id": existing.id,
            "chatwoot_message_id": existing.chatwoot_message_id,
            "status": existing.status,
            "content_type": existing.content_type,
            "content_attributes": existing.content_attributes,
        }

    connection = get_connection(db)
    client = client_for(connection)
    attributes = {"items": [{"title": title, "value": title} for title in payload.options]}
    try:
        remote = payload_dict(client.get_conversation(conversation_id))
        if remote.get("can_reply") is not True:
            raise HTTPException(409, detail={"code": "conversation_cannot_reply", "message": "Chatwoot 当前不允许回复该会话"})
        messages = normalize_collection(client.get_messages(conversation_id))
        incoming = [
            message for message in messages
            if message.get("message_type") in (0, "incoming")
            and not message.get("private")
            and not (message.get("content_attributes") or {}).get("external_echo")
        ]
        if not incoming:
            raise HTTPException(409, detail={"code": "automatic_window_unknown", "message": "无法确认客户最近一次发言时间"})
        latest_customer_at = max(dt(message_timestamp(message.get("created_at"))) for message in incoming)
        if dt(utcnow()) >= latest_customer_at + timedelta(hours=24, minutes=-5):
            raise HTTPException(409, detail={"code": "automatic_window_closed", "message": "客户最后发言已超过 24 小时，不能发送自动快捷选项"})

        outbound = OutboundMessage(
            conversation_state_id=row.id,
            idempotency_key=business_key,
            source_type="manual",
            source_id=user.id,
            content=payload.content,
            content_type="input_select",
            content_attributes=attributes,
            status="submission_unknown",
        )
        db.add(outbound)
        db.commit()

        try:
            result = payload_dict(client.create_input_select_message(conversation_id, payload.content, payload.options))
        except ChatwootError as exc:
            outbound.error_code = exc.code
            if exc.status_code < 500:
                outbound.status = "failed"
            audit(db, user, "conversation.quick_replies.failed", "conversation", conversation_id, {
                "outbound_id": outbound.id,
                "error_code": exc.code,
            })
            db.commit()
            raise_chatwoot_error(exc, "发送 Facebook 快捷选项失败")

        message_id = result.get("id")
        if not isinstance(message_id, int):
            outbound.error_code = "submission_unknown_reconcile_required"
            db.commit()
            raise HTTPException(502, detail={"code": outbound.error_code, "message": "Chatwoot 未返回消息编号，需要人工核对"})

        outbound.chatwoot_message_id = message_id
        outbound.status = result.get("status") if result.get("status") in {"sent", "delivered", "read", "failed"} else "submitted"
        outbound.submitted_at = utcnow()
        message = db.scalar(select(MessageEvent).where(
            MessageEvent.conversation_state_id == row.id,
            MessageEvent.chatwoot_message_id == message_id,
        ))
        if message is None:
            message = MessageEvent(
                conversation_state_id=row.id,
                chatwoot_message_id=message_id,
                direction="outgoing",
            )
            db.add(message)
        message.content = payload.content
        message.content_type = "input_select"
        message.content_attributes = attributes
        message.status = outbound.status
        message.attribution = "manual"
        row.last_message = payload.content
        row.updated_at = utcnow()
        audit(db, user, "conversation.quick_replies.send", "conversation", conversation_id, {
            "outbound_id": outbound.id,
            "chatwoot_message_id": message_id,
            "option_count": len(payload.options),
        })
        db.commit()
        return {
            "ok": outbound.status != "failed",
            "duplicate": False,
            "id": outbound.id,
            "chatwoot_message_id": message_id,
            "status": outbound.status,
            "content_type": outbound.content_type,
            "content_attributes": attributes,
        }
    finally:
        client.close()


@router.get("/route-quick-replies")
def route_quick_reply_options(user: User = Depends(current_user)):
    return {
        "content": "请问您想咨询哪条线路？",
        "options": route_quick_reply_titles(),
        "channel": "facebook",
    }


@router.get("/conversations/{conversation_id}/controls")
def conversation_controls(conversation_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    row = visible_conversation(db, user, conversation_id)
    connection = get_connection(db)
    client = client_for(connection)
    try:
        remote = payload_dict(client.get_conversation(conversation_id))
        agents = normalize_collection(client.list_inbox_agents(row.inbox.chatwoot_inbox_id))
        labels = normalize_collection(client.list_labels())
        current_labels = string_payload(client.get_conversation_labels(conversation_id))
        ai_label_catalog_exists = any(str(label.get("title", "")).casefold() == AI_CONTROL_LABEL for label in labels)
        if current_labels != (row.labels or []) or row.ai_mode == "enabled" and not ai_label_catalog_exists:
            sync_local_labels(db, row, current_labels, ai_label_catalog_exists=ai_label_catalog_exists)
            db.commit()
        return {
            **assignment_json(remote),
            "agents": [
                {
                    "id": int(agent["id"]),
                    "name": agent.get("name") or agent.get("available_name") or f"Agent {agent['id']}",
                    "availability_status": agent.get("availability_status") or "offline",
                    "role": agent.get("role"),
                    "thumbnail": agent.get("thumbnail"),
                }
                for agent in agents if agent.get("id")
            ],
            "labels": [
                {
                    "id": int(label["id"]),
                    "title": str(label.get("title", "")),
                    "description": str(label.get("description") or ""),
                    "color": str(label.get("color") or "#64748B"),
                    "show_on_sidebar": bool(label.get("show_on_sidebar", False)),
                }
                for label in labels if label.get("id") and label.get("title")
            ],
            "conversation_labels": current_labels,
            "ai_mode": row.ai_mode,
            "ai_label_present": row.ai_label_present,
            "ai_sync_status": row.ai_sync_status,
            "can_create_labels": user.role == "super_admin",
        }
    except ChatwootError as exc:
        raise_chatwoot_error(exc, "无法读取 Chatwoot 会话控制信息")
    finally:
        client.close()


@router.put("/conversations/{conversation_id}/assignment")
def update_conversation_assignment(
    conversation_id: int,
    payload: ConversationAssignmentRequest,
    user: User = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    row = visible_conversation(db, user, conversation_id)
    connection = get_connection(db)
    client = client_for(connection)
    try:
        agents = normalize_collection(client.list_inbox_agents(row.inbox.chatwoot_inbox_id))
        if payload.assignee_id is not None and not any(int(agent.get("id", 0)) == payload.assignee_id for agent in agents):
            raise HTTPException(422, detail={"code": "agent_not_in_inbox", "message": "该客服不属于当前 Inbox"})
        if payload.pause_ai and payload.assignee_id is not None:
            current_labels = string_payload(client.get_conversation_labels(conversation_id))
            if "人工接管" not in current_labels:
                current_labels.append("人工接管")
                client.set_conversation_labels(conversation_id, current_labels)
            sync_local_labels(db, row, current_labels, source="platform")
            db.commit()
        client.assign_conversation(conversation_id, payload.assignee_id)
        remote = payload_dict(client.get_conversation(conversation_id))
        return {"ok": True, **assignment_json(remote), "labels": row.labels}
    except HTTPException:
        raise
    except ChatwootError as exc:
        raise_chatwoot_error(exc, "分配客服失败")
    finally:
        client.close()


@router.put("/conversations/{conversation_id}/labels")
def update_conversation_labels(
    conversation_id: int,
    payload: ConversationLabelsRequest,
    user: User = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    row = visible_conversation(db, user, conversation_id)
    connection = get_connection(db)
    client = client_for(connection)
    try:
        catalog = normalize_collection(client.list_labels())
        allowed = {str(label.get("title")) for label in catalog if label.get("title")}
        requested = list(dict.fromkeys(label.strip() for label in payload.labels if label.strip()))
        unknown = sorted(set(requested) - allowed)
        if unknown:
            raise HTTPException(422, detail={"code": "unknown_labels", "message": f"标签不存在：{', '.join(unknown)}"})
        current = string_payload(client.get_conversation_labels(conversation_id))
        base = set(payload.base_labels)
        additions = [label for label in requested if label not in base]
        removals = base - set(requested)
        merged = [label for label in current if label not in removals]
        merged.extend(label for label in additions if label not in merged)
        result = string_payload(client.set_conversation_labels(conversation_id, merged))
        final_labels = result or merged
        sync_local_labels(db, row, final_labels, source="platform")
        audit(db, user, "conversation.labels.update", "conversation", conversation_id, {"labels": final_labels})
        db.commit()
        return {"ok": True, "labels": final_labels, "ai_state": row.effective_ai_state, "ai_reason": row.effective_state_reason, "version": row.version}
    except HTTPException:
        raise
    except ChatwootError as exc:
        raise_chatwoot_error(exc, "更新会话标签失败")
    finally:
        client.close()


@router.put("/conversations/{conversation_id}/ai-mode")
def update_conversation_ai_mode(
    conversation_id: int,
    payload: ConversationAiModeRequest,
    user: User = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    row = visible_conversation(db, user, conversation_id)
    desired_mode = "enabled" if payload.enabled else "disabled"
    row.ai_mode = desired_mode
    row.ai_mode_source = "platform"
    row.ai_sync_status = "pending"
    row.ai_mode_updated_at = utcnow()
    sync_local_labels(db, row, row.labels or [], source="platform")
    row.ai_sync_status = "pending"
    row.effective_ai_state = "AI_PAUSED_SYNC"
    row.effective_state_reason = "ai_label_sync:pending"
    db.commit()

    connection = get_connection(db)
    client = client_for(connection)
    try:
        catalog = normalize_collection(client.list_labels())
        ai_definition = next((item for item in catalog if str(item.get("title", "")).casefold() == AI_CONTROL_LABEL), None)
        if payload.enabled and not ai_definition:
            client.create_label(AI_CONTROL_LABEL, "AI 自动接管会话", "#16A34A", True)
        current = string_payload(client.get_conversation_labels(conversation_id))
        final = [
            label for label in current
            if label.casefold() != AI_CONTROL_LABEL and label != "AI关闭"
        ]
        if payload.enabled:
            final.append(AI_CONTROL_LABEL)
        result = string_payload(client.set_conversation_labels(conversation_id, final))
        final_labels = result or final
        sync_local_labels(db, row, final_labels, source="platform")
        from app.reception_v3.live import session_for
        from app.reception_v3 import service as v3_service
        session = session_for(db, row.id)
        if session:
            value = v3_service.state(session)
            if payload.enabled and not value.get('handoff'):
                v3_service.control(session, 'resume')
            elif not payload.enabled:
                v3_service.control(session, 'stop')
        audit(db, user, "conversation.ai_mode.update", "conversation", conversation_id, {
            "enabled": payload.enabled,
            "sync_status": row.ai_sync_status,
        })
        db.commit()
        return {
            "ok": True,
            "ai_mode": row.ai_mode,
            "ai_label_present": row.ai_label_present,
            "ai_sync_status": row.ai_sync_status,
            "ai_state": row.effective_ai_state,
            "ai_reason": row.effective_state_reason,
            "labels": row.labels,
            "version": row.version,
        }
    except ChatwootError as exc:
        row.ai_sync_status = "conflict"
        sync_local_labels(db, row, row.labels or [], source="platform", ai_label_catalog_exists=False)
        audit(db, user, "conversation.ai_mode.sync_failed", "conversation", conversation_id, {
            "enabled": payload.enabled,
            "error_code": exc.code,
        })
        db.commit()
        raise_chatwoot_error(exc, "同步 AI 接管标签失败，当前会话已停止 AI")
    finally:
        client.close()


@router.post("/labels")
def create_label(
    payload: LabelCreateRequest,
    user: User = Depends(require_super_admin_csrf),
    db: Session = Depends(get_db),
):
    connection = get_connection(db)
    client = client_for(connection)
    try:
        existing = normalize_collection(client.list_labels())
        title = payload.title.strip()
        if any(str(label.get("title", "")).casefold() == title.casefold() for label in existing):
            raise HTTPException(409, detail={"code": "label_exists", "message": "同名标签已存在"})
        result = payload_dict(client.create_label(title, payload.description.strip(), payload.color.upper(), payload.show_on_sidebar))
        return {
            "id": int(result["id"]),
            "title": result.get("title", title),
            "description": result.get("description", payload.description.strip()),
            "color": result.get("color", payload.color.upper()),
            "show_on_sidebar": bool(result.get("show_on_sidebar", payload.show_on_sidebar)),
        }
    except HTTPException:
        raise
    except ChatwootError as exc:
        raise_chatwoot_error(exc, "创建 Chatwoot 标签失败")
    finally:
        client.close()


@router.get("/conversations/{conversation_id}/messages")
def conversation_messages(conversation_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    row = visible_conversation(db, user, conversation_id)
    messages = db.scalars(select(MessageEvent).where(MessageEvent.conversation_state_id == row.id).order_by(MessageEvent.created_at)).all()
    items = []
    for item in messages:
        attachments = item.attachments or []
        placeholder, attachment_type = attachment_placeholder(attachments)
        effective_type = attachment_type if attachments and item.content_type == "text" else item.content_type
        items.append({
            "id": item.chatwoot_message_id,
            "direction": item.direction,
            "private": item.private,
            "content_type": effective_type,
            "content": item.content or (placeholder if attachments else ""),
            "status": item.status,
            "attribution": item.attribution,
            "attachments": attachments,
            "content_attributes": item.content_attributes or {},
            "created_at": item.created_at,
        })
    return {"items": items}


@router.post("/conversations/{conversation_id}/sync")
def sync_conversation(conversation_id: int, user: User = Depends(require_csrf), db: Session = Depends(get_db)):
    row = visible_conversation(db, user, conversation_id)
    result = sync_conversation_history(row.id)
    return {
        "ok": True,
        "fetched": result.fetched,
        "inserted": result.inserted,
        "updated": result.updated,
    }


@router.get("/health")
def health(db: Session = Depends(get_db)):
    db.scalar(select(Tenant.id).limit(1))
    return {"status": "ok", "database": "ok"}


@router.get("/health/details")
def health_details(user: User = Depends(current_user), db: Session = Depends(get_db)):
    from app.runtime_health import runtime_health
    return runtime_health(db)


@router.get("/health/ready")
def readiness(response: Response, db: Session = Depends(get_db)):
    from app.runtime_health import runtime_health
    result = runtime_health(db)
    response.status_code = 200 if result["status"] == "ok" else 503
    # Public probe exposes no account, queue or customer details.
    return {"status": result["status"]}
