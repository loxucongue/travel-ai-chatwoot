from __future__ import annotations
import json
import re
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select, update, func, delete
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from app.auth import current_user, require_csrf, require_super_admin_csrf
from app.asset_narratives import normalized_asset_narrative
from app.db import get_db
from app.models import User, Tenant, InboxBinding, ConversationState, MessageEvent, StoredMedia, MaterialAsset, KnowledgeVersion, AuditLog, WebKnowledgeSource, WebKnowledgeRevision, utcnow
from app.automation_models import *
from app.live_reply_models import LiveReplyJob
from app.config import settings
from app.material_library import replace_asset_binding
from app.operations import allowed_inbox_ids, is_admin, audit
from app.route_packages import ROUTES, ROUTE_PACKAGES, RoutePackageError, RUNTIME_PACKAGE_ROOT, _validate, install_route_package, ensure_route_packages_current, remove_runtime_route_package, route_package_summary
from app.security import encrypt_secret
from app.reception_config import ReceptionConfiguration, live_silence_enabled, get_reception_configuration, put_reception_configuration
from app.reception_rollout import reception_rollout
from app.reception_v3 import release_id
from app.reception_v3 import service
from app.runtime_settings import reply_policy, dt, iso
from app.web_knowledge import WebKnowledgeError, normalize_public_url, publish_revision, refresh_source, revision_knowledge_modules, web_knowledge_publish_enabled, web_knowledge_refresh_enabled
def simulation_state(row): return row.controls.get('simulation', {})


def session_json(db, row):
    records = db.scalars(select(AutomationRun).where(AutomationRun.session_id == row.id)
                         .order_by(AutomationRun.id.desc()).limit(30)).all()
    value = service.state(row)
    simulation = simulation_state(row)
    if value.get('handoff'):
        stage = 'handoff'
    elif not row.controls.get('route_variant'):
        stage = 'route_selection'
    elif value.get('delivery_kind') == 'introduction':
        stage = 'route_presentation'
    elif not row.memory.get('party_size'):
        stage = 'needs_discovery'
    else:
        stage = 'concern_resolution'
    return {'id': row.id, 'mode': row.mode, 'environment': row.environment,
            'engine_version': row.engine_version, 'engine_release_id': row.engine_release_id,
            'generation': row.generation, 'virtual_now': row.virtual_now,
            'messages': safe_text(row.messages), 'memory': safe_text({k: {'value': v} for k,v in row.memory.items()}),
            'controls': safe_text(row.controls),
            'simulation': {**simulation, 'next_event_at': value.get('delivery_due_at') or value.get('next_check_at'),
                           'next_event_name': '线路介绍' if value.get('delivery_kind') == 'introduction' else '沉默评估'},
            'reception_state': {'journey_stage': stage, 'customer_profile': safe_text(row.memory),
                                'next_touch_at': value.get('next_check_at'), 'last_warning': value.get('last_error'),
                                'can_retry': bool(value.get('failed_event'))},
            'pending': bool(value.get('pending_event') and not value.get('failed_event')),
            'runs': [run_json(x) for x in records], 'jobs': [], 'enrollments': [], 'cycles': [], 'outbound': False}

router = APIRouter(prefix="/v1")



def manager(user: User = Depends(current_user)) -> User:
    if user.role not in ("admin", "super_admin", "supervisor"): raise HTTPException(403, detail={"code":"permission_denied", "message":"需要管理员或主管权限"})
    return user


def manager_write(user: User = Depends(require_csrf)) -> User:
    return manager(user)


def _web_revision_json(row: WebKnowledgeRevision, *, include_content: bool = False) -> dict:
    modules = revision_knowledge_modules(row)
    value = {
        "id": row.id,
        "revision_number": row.revision_number,
        "title": row.title,
        "final_url": row.final_url,
        "content_hash": row.content_hash,
        "content_length": row.content_length,
        "status": row.status,
        "fetched_at": row.fetched_at,
        "published_at": row.published_at,
        "knowledge_module_count": len(modules),
        "fact_count": sum(len(item.get("facts") or []) for item in modules),
    }
    if include_content:
        value["knowledge_modules"] = modules
        try:
            manifest = json.loads(row.content)
        except (TypeError, ValueError):
            manifest = {}
        value["site_inventory"] = manifest.get("site_inventory") or []
        value["excluded_items"] = manifest.get("excluded_items") or []
    return value


def _web_source_json(db: Session, row: WebKnowledgeSource, *, include_revisions: bool = False) -> dict:
    latest = db.get(WebKnowledgeRevision, row.latest_revision_id) if row.latest_revision_id else None
    published = db.get(WebKnowledgeRevision, row.published_revision_id) if row.published_revision_id else None
    value = {
        "id": row.id,
        "name": row.name,
        "url": row.url,
        "description": row.description,
        "match_keywords": row.match_keywords or [],
        "auth_type": row.auth_type or "none",
        "password_configured": bool(row.auth_secret),
        "status": row.status,
        "ai_enabled": row.ai_enabled,
        "runtime_scope": row.runtime_scope or "disabled",
        "sync_status": row.sync_status,
        "last_error": row.last_error,
        "last_checked_at": row.last_checked_at,
        "last_changed_at": row.last_changed_at,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "latest_revision": _web_revision_json(latest) if latest else None,
        "published_revision": _web_revision_json(published) if published else None,
    }
    if include_revisions:
        revisions = db.scalars(select(WebKnowledgeRevision).where(
            WebKnowledgeRevision.source_id == row.id
        ).order_by(WebKnowledgeRevision.revision_number.desc())).all()
        value["revisions"] = [_web_revision_json(item) for item in revisions]
    return value


class RoutePackageImportInput(BaseModel):
    package: dict


class RouteFactInput(BaseModel):
    id: str = Field(min_length=1, max_length=160)
    text: str = Field(min_length=1, max_length=4000)
    source_ref: str = Field(min_length=1, max_length=500)


class WebKnowledgeSourceInput(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    url: str = Field(min_length=8, max_length=1200)
    description: str = Field(default="", max_length=2000)
    match_keywords: list[str] = Field(default_factory=list, max_length=50)


class WebKnowledgeSourceUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    url: str | None = Field(default=None, min_length=8, max_length=1200)
    description: str | None = Field(default=None, max_length=2000)
    match_keywords: list[str] | None = Field(default=None, max_length=50)
    ai_enabled: bool | None = None
    status: Literal["active", "paused", "archived"] | None = None


class WebKnowledgeAuthInput(BaseModel):
    password: str = Field(min_length=1, max_length=500)


class RouteContentGroupInput(BaseModel):
    key: str = Field(min_length=1, max_length=120)
    purpose: str = Field(default="", max_length=500)
    approved_text: str = Field(min_length=1, max_length=10000)
    asset_keys: list[str] = Field(default_factory=list, max_length=30)
    evidence_refs: list[str] = Field(default_factory=list, max_length=100)
    initial_delivery: bool = False
    delivery_mode: Literal["text_only", "assets_only", "text_then_assets", "assets_then_text"] = "assets_then_text"


class RouteFixedAnswerInput(BaseModel):
    id: str = Field(min_length=2, max_length=80, pattern=r"^[a-z0-9][a-z0-9_-]+$")
    name: str = Field(min_length=1, max_length=200)
    status: Literal["active", "pending_review", "disabled"] = "pending_review"
    priority: int = Field(default=100, ge=0, le=1000)
    topics: list[str] = Field(min_length=1, max_length=20)
    party_size_min: int | None = Field(default=None, ge=1, le=100)
    party_size_max: int | None = Field(default=None, ge=1, le=100)
    content_group_key: str = Field(min_length=1, max_length=120)
    answer_text: str = Field(min_length=1, max_length=10000)
    fact_ids: list[str] = Field(default_factory=list, max_length=100)
    asset_ids: list[str] = Field(default_factory=list, max_length=30)
    source_ref: str = Field(min_length=1, max_length=500)
    answer_origin: Literal["website_verbatim", "operator_approved"] = "operator_approved"
    positive_examples: list[str] = Field(default_factory=list, max_length=50)
    negative_examples: list[str] = Field(default_factory=list, max_length=50)


class RouteContentUpdate(BaseModel):
    base_package_version: str | None = Field(default=None, min_length=1, max_length=200)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    selection_title: str | None = Field(default=None, min_length=1, max_length=20)
    match_keywords: list[str] | None = Field(default=None, max_length=50)
    default_entry_message: str = Field(min_length=1, max_length=1000)
    ai_guidance: str = Field(default="", max_length=3000)
    initial_delivery_interval_seconds: int = Field(default=2, ge=1, le=30)
    knowledge_facts: list[RouteFactInput] = Field(min_length=1, max_length=300)
    content_groups: list[RouteContentGroupInput] = Field(min_length=1, max_length=100)
    content_sequence: list[str] | None = Field(default=None, max_length=100)
    fixed_answers: list[RouteFixedAnswerInput] | None = Field(default=None, max_length=100)


class RouteAssetUpdate(BaseModel):
    display_name: str = Field(min_length=1, max_length=200)
    usage: str = Field(default="", max_length=1000)
    what_it_shows: str = Field(default="", max_length=1000)
    feature_points: list[str] = Field(default_factory=list, max_length=8)
    customer_value: str = Field(default="", max_length=1000)
    recommended_caption: str = Field(default="", max_length=1200)
    avoid_claims: list[str] = Field(default_factory=list, max_length=12)


class RouteAssetReplace(BaseModel):
    media_id: int = Field(gt=0)


def fail(code: str, status: int = 409):
    raise HTTPException(status, detail={"code":code, "message":code})


def _primary_tenant(db: Session) -> Tenant:
    tenant = db.scalar(select(Tenant).order_by(Tenant.id))
    if not tenant:
        fail("tenant_not_found", 404)
    return tenant


@router.get("/knowledge/web-sources")
def web_knowledge_sources(user: User = Depends(manager), db: Session = Depends(get_db)):
    tenant = _primary_tenant(db)
    rows = db.scalars(select(WebKnowledgeSource).where(
        WebKnowledgeSource.tenant_id == tenant.id,
        WebKnowledgeSource.status != "archived",
    ).order_by(WebKnowledgeSource.id.desc())).all()
    return {
        "items": [_web_source_json(db, row) for row in rows],
        "capabilities": {
            "refresh_enabled": web_knowledge_refresh_enabled(),
            "publish_enabled": web_knowledge_publish_enabled(),
        },
    }


@router.post("/knowledge/web-sources")
def create_web_knowledge_source(payload: WebKnowledgeSourceInput, user: User = Depends(manager_write), db: Session = Depends(get_db)):
    tenant = _primary_tenant(db)
    try:
        url = normalize_public_url(payload.url)
    except WebKnowledgeError as exc:
        fail(str(exc), 422)
    row = WebKnowledgeSource(
        tenant_id=tenant.id,
        name=payload.name.strip(),
        url=url,
        description=payload.description.strip(),
        match_keywords=list(dict.fromkeys(item.strip() for item in payload.match_keywords if item.strip())),
        created_by=user.id,
    )
    db.add(row)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        fail("website_url_already_exists", 409)
    audit(db, user, "web_knowledge.create", "web_knowledge_source", row.id, {"url": row.url})
    db.commit()
    return _web_source_json(db, row, include_revisions=True)


@router.get("/knowledge/web-sources/{source_id}")
def web_knowledge_source_detail(source_id: int, user: User = Depends(manager), db: Session = Depends(get_db)):
    tenant = _primary_tenant(db)
    row = db.scalar(select(WebKnowledgeSource).where(
        WebKnowledgeSource.id == source_id,
        WebKnowledgeSource.tenant_id == tenant.id,
    ))
    if not row:
        fail("website_source_not_found", 404)
    return _web_source_json(db, row, include_revisions=True)


@router.patch("/knowledge/web-sources/{source_id}")
def update_web_knowledge_source(source_id: int, payload: WebKnowledgeSourceUpdate, user: User = Depends(manager_write), db: Session = Depends(get_db)):
    tenant = _primary_tenant(db)
    row = db.scalar(select(WebKnowledgeSource).where(
        WebKnowledgeSource.id == source_id,
        WebKnowledgeSource.tenant_id == tenant.id,
    ))
    if not row:
        fail("website_source_not_found", 404)
    changes = payload.model_dump(exclude_unset=True)
    if "url" in changes:
        try:
            changes["url"] = normalize_public_url(str(changes["url"]))
        except WebKnowledgeError as exc:
            fail(str(exc), 422)
    if "match_keywords" in changes:
        changes["match_keywords"] = list(dict.fromkeys(item.strip() for item in changes["match_keywords"] if item.strip()))
    for key, value in changes.items():
        setattr(row, key, value.strip() if isinstance(value, str) and key in {"name", "description"} else value)
    row.updated_at = utcnow()
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        fail("website_url_already_exists", 409)
    audit(db, user, "web_knowledge.update", "web_knowledge_source", row.id, changes)
    db.commit()
    return _web_source_json(db, row, include_revisions=True)


@router.post("/knowledge/web-sources/{source_id}/refresh")
def refresh_web_knowledge_source(source_id: int, user: User = Depends(manager_write), db: Session = Depends(get_db)):
    if not web_knowledge_refresh_enabled():
        fail("website_refresh_temporarily_disabled", 409)
    tenant = _primary_tenant(db)
    row = db.scalar(select(WebKnowledgeSource).where(
        WebKnowledgeSource.id == source_id,
        WebKnowledgeSource.tenant_id == tenant.id,
        WebKnowledgeSource.status != "archived",
    ))
    if not row:
        fail("website_source_not_found", 404)
    try:
        revision, changed = refresh_source(db, row, user.id)
    except WebKnowledgeError as exc:
        db.commit()
        fail(str(exc), 422)
    audit(db, user, "web_knowledge.refresh", "web_knowledge_source", row.id, {
        "revision_id": revision.id, "changed": changed, "content_hash": revision.content_hash,
    })
    db.commit()
    return {"source": _web_source_json(db, row, include_revisions=True), "changed": changed}


@router.put("/knowledge/web-sources/{source_id}/wordpress-password")
def set_web_knowledge_password(source_id: int, payload: WebKnowledgeAuthInput, user: User = Depends(manager_write), db: Session = Depends(get_db)):
    tenant = _primary_tenant(db)
    row = db.scalar(select(WebKnowledgeSource).where(
        WebKnowledgeSource.id == source_id,
        WebKnowledgeSource.tenant_id == tenant.id,
        WebKnowledgeSource.status != "archived",
    ))
    if not row:
        fail("website_source_not_found", 404)
    row.auth_type = "wordpress_post_password"
    row.auth_secret = encrypt_secret(payload.password)
    row.updated_at = utcnow()
    audit(db, user, "web_knowledge.password_set", "web_knowledge_source", row.id, {"auth_type": row.auth_type})
    db.commit()
    return _web_source_json(db, row, include_revisions=True)


@router.delete("/knowledge/web-sources/{source_id}/wordpress-password")
def clear_web_knowledge_password(source_id: int, user: User = Depends(manager_write), db: Session = Depends(get_db)):
    tenant = _primary_tenant(db)
    row = db.scalar(select(WebKnowledgeSource).where(
        WebKnowledgeSource.id == source_id,
        WebKnowledgeSource.tenant_id == tenant.id,
    ))
    if not row:
        fail("website_source_not_found", 404)
    row.auth_type = "none"
    row.auth_secret = None
    row.updated_at = utcnow()
    audit(db, user, "web_knowledge.password_cleared", "web_knowledge_source", row.id, {})
    db.commit()
    return _web_source_json(db, row, include_revisions=True)


@router.get("/knowledge/web-revisions/{revision_id}")
def web_knowledge_revision(revision_id: int, user: User = Depends(manager), db: Session = Depends(get_db)):
    tenant = _primary_tenant(db)
    row = db.scalar(
        select(WebKnowledgeRevision)
        .join(WebKnowledgeSource, WebKnowledgeSource.id == WebKnowledgeRevision.source_id)
        .where(WebKnowledgeRevision.id == revision_id, WebKnowledgeSource.tenant_id == tenant.id)
    )
    if not row:
        fail("website_revision_not_found", 404)
    return _web_revision_json(row, include_content=True)


@router.get("/knowledge/web-sources/{source_id}/usage")
def web_knowledge_usage(
    source_id: int,
    limit: int = Query(default=50, ge=1, le=200),
    user: User = Depends(manager),
    db: Session = Depends(get_db),
):
    tenant = db.scalar(select(Tenant).order_by(Tenant.id))
    source = db.scalar(select(WebKnowledgeSource).where(
        WebKnowledgeSource.id == source_id,
        WebKnowledgeSource.tenant_id == (tenant.id if tenant else -1),
    ))
    if not source:
        fail("website_source_not_found", 404)

    items = []
    live_rows = db.execute(
        select(LiveReplyJob, ConversationState)
        .join(ConversationState, ConversationState.id == LiveReplyJob.conversation_state_id)
        .where(ConversationState.tenant_id == source.tenant_id)
        .order_by(LiveReplyJob.id.desc())
        .limit(limit)
    ).all()
    for row, conversation in live_rows:
        usage = (row.trace or {}).get("knowledge_usage")
        if not isinstance(usage, dict):
            continue
        items.append({
            "key": f"live:{row.id}", "environment": "live",
            "conversation_id": conversation.chatwoot_conversation_id,
            "session_id": None, "run_id": row.id,
            "module": "reply", "status": row.status,
            "outbound": bool((row.trace or {}).get("outbound")),
            "created_at": row.created_at, "completed_at": row.completed_at,
            "usage": usage,
        })

    rehearsal_rows = db.execute(
        select(AutomationRun, AutomationSession)
        .join(AutomationSession, AutomationSession.id == AutomationRun.session_id)
        .order_by(AutomationRun.id.desc())
        .limit(limit)
    ).all()
    for row, session in rehearsal_rows:
        usage = (row.trace or {}).get("knowledge_usage")
        if not isinstance(usage, dict):
            continue
        items.append({
            "key": f"playground:{row.id}", "environment": "playground",
            "conversation_id": None, "session_id": session.id, "run_id": row.id,
            "module": row.module, "status": row.status, "outbound": False,
            "created_at": row.created_at, "completed_at": row.completed_at,
            "usage": usage,
        })
    items.sort(key=lambda item: item.get("created_at") or "", reverse=True)
    items = items[:limit]
    return {
        "items": items,
        "summary": {
            "total": len(items),
            "used": sum(item["usage"].get("status") == "used" for item in items),
            "retrieved_not_used": sum(item["usage"].get("status") == "retrieved_not_used" for item in items),
            "no_match": sum(item["usage"].get("status") == "no_match" for item in items),
            "disabled": sum(item["usage"].get("status") == "disabled" for item in items),
        },
        "outbound": False,
    }


@router.post("/knowledge/web-sources/{source_id}/revisions/{revision_id}/publish")
def publish_web_knowledge_revision(source_id: int, revision_id: int, user: User = Depends(manager_write), db: Session = Depends(get_db), runtime_scope: Literal["playground", "live"] = "playground"):
    if not web_knowledge_publish_enabled():
        fail("website_publish_temporarily_disabled", 409)
    tenant = _primary_tenant(db)
    source = db.scalar(select(WebKnowledgeSource).where(
        WebKnowledgeSource.id == source_id,
        WebKnowledgeSource.tenant_id == tenant.id,
    ))
    revision = db.get(WebKnowledgeRevision, revision_id)
    if not source or not revision or revision.source_id != source.id:
        fail("website_revision_not_found", 404)
    publish_revision(db, source, revision, runtime_scope=runtime_scope)
    audit(db, user, "web_knowledge.publish", "web_knowledge_revision", revision.id, {
        "source_id": source.id, "content_hash": revision.content_hash,
        "runtime_scope": "playground",
    })
    db.commit()
    return _web_source_json(db, source, include_revisions=True)


@router.post("/knowledge/web-sources/{source_id}/disable")
def disable_web_knowledge_source(source_id: int, user: User = Depends(manager_write), db: Session = Depends(get_db)):
    tenant = _primary_tenant(db)
    source = db.scalar(select(WebKnowledgeSource).where(
        WebKnowledgeSource.id == source_id,
        WebKnowledgeSource.tenant_id == tenant.id,
    ))
    if not source:
        fail("website_source_not_found", 404)
    source.ai_enabled = False
    source.runtime_scope = "disabled"
    source.status = "paused"
    source.updated_at = utcnow()
    audit(db, user, "web_knowledge.disable", "web_knowledge_source", source.id, {
        "published_revision_id": source.published_revision_id,
    })
    db.commit()
    return _web_source_json(db, source, include_revisions=True)


def scope(db, user, inbox_id):
    allowed = allowed_inbox_ids(db, user)
    if inbox_id is None:
        if allowed is not None: fail("inbox_required", 403)
    elif not db.get(InboxBinding, inbox_id) or (allowed is not None and inbox_id not in allowed): fail("inbox_forbidden",403)


def own_session(db, user, session_id):
    row = db.get(AutomationSession, session_id)
    if not row or row.owner_id != user.id: fail("session_not_found",404)
    scope(db,user,row.inbox_binding_id)
    return row


def safe_text(value):
    if isinstance(value, list): return [safe_text(x) for x in value]
    if isinstance(value, dict): return {k:("[联系方式已脱敏]" if k.lower() in {"email","phone","phone_number","wechat","wechat_id","line","line_id"} and v else safe_text(v)) for k,v in value.items()}
    if not isinstance(value, str): return value
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:T[\d:.]+(?:Z|[+-]\d{2}:\d{2})?)?", value): return value
    value = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[邮箱已脱敏]", value)
    value = re.sub(r"(?<!\w)(?:\+?\d[ ()-]?){8,15}(?!\w)", "[号码已脱敏]", value)
    return re.sub(r"(?i)((?:\bline\b|\bwechat\b|微信|\bweixin\b)\s*(?:id|帳號|账号|號碼|号码)?\s*(?:[:：=]|是|為|为|叫|\s)\s*)[A-Za-z0-9_.@+-]+", r"\1[已脱敏]", value)


def run_json(row):
    return {"id":row.id,"session_id":row.session_id,"module":row.module,"generation":row.generation,"status":row.status,"decision":safe_text(row.decision),"trace":safe_text(row.trace),"error_code":row.error_code,"created_at":row.created_at,"completed_at":row.completed_at,"outbound":False}


class ReplyConfig(BaseModel):
    version: int = Field(ge=1)
    inbox_binding_id: int | None = None
    enabled: bool = True
    merge_wait_seconds: int = Field(default=2,ge=1,le=5)
    merge_max_seconds: int = Field(default=5,ge=1,le=5)
    backlog_seconds: int = Field(default=300,ge=30,le=300)
    @model_validator(mode="after")
    def order(self):
        if self.merge_wait_seconds > self.merge_max_seconds: raise ValueError("merge_wait_exceeds_max")
        return self


@router.get("/settings/reply-timing")
def get_reply_policy(inbox_binding_id: int | None=None,user:User=Depends(manager),db:Session=Depends(get_db)):
    scope(db,user,inbox_binding_id)
    config,version = reply_policy(db,inbox_binding_id)
    key = f"inbox:{inbox_binding_id}" if inbox_binding_id else "global"
    local = db.scalar(select(ReplyPolicy).where(ReplyPolicy.scope_key==key))
    return {**config,"version":local.version if local else 1,"effective_version":version,"inbox_binding_id":inbox_binding_id}


@router.patch("/settings/reply-timing")
def save_reply_policy(payload:ReplyConfig,user:User=Depends(manager_write),db:Session=Depends(get_db)):
    scope(db,user,payload.inbox_binding_id)
    key = f"inbox:{payload.inbox_binding_id}" if payload.inbox_binding_id else "global"
    row=db.scalar(select(ReplyPolicy).where(ReplyPolicy.scope_key==key))
    config=payload.model_dump(exclude={"version","inbox_binding_id"})
    if row:
        if not db.execute(update(ReplyPolicy).where(ReplyPolicy.id==row.id,ReplyPolicy.version==payload.version).values(config=config,version=payload.version+1,updated_at=utcnow())).rowcount: fail("version_conflict")
    else:
        if payload.version != 1: fail("version_conflict")
        db.add(ReplyPolicy(scope_key=key,config=config,version=2))
    audit(db,user,"reply_policy.update","policy",key)
    db.commit()
    return get_reply_policy(payload.inbox_binding_id,user,db)


@router.get("/automation/runs")
def runs(module:Literal["reply","wakeup"]|None=None,page:int=Query(1,ge=1),user:User=Depends(manager),db:Session=Depends(get_db)):
    q=select(AutomationRun).join(AutomationSession).where(AutomationSession.owner_id==user.id)
    allowed=allowed_inbox_ids(db,user)
    if allowed is not None:q=q.where(AutomationSession.inbox_binding_id.in_(allowed))
    if module:q=q.where(AutomationRun.module==module)
    count=db.scalar(select(func.count()).select_from(q.subquery()))
    return {"items":[run_json(x) for x in db.scalars(q.order_by(AutomationRun.id.desc()).offset((page-1)*30).limit(30)).all()],"total":count,"page":page}


@router.get("/automation/route-products")
def route_products(user: User = Depends(manager), db: Session = Depends(get_db)):
    """Return the effective, reviewed product packages and their runtime readiness."""
    summaries = {item["route_variant"]: item for item in route_package_summary()}
    items = []
    for key in summaries:
        package = ROUTE_PACKAGES[key]
        version = db.scalar(select(KnowledgeVersion).where(
            KnowledgeVersion.version_key == package["knowledge_version"]
        ))
        asset_rows = db.scalars(select(MaterialAsset).where(
            MaterialAsset.knowledge_version_id == version.id
        )).all() if version else []
        asset_map = {row.asset_key: row for row in asset_rows}
        asset_keys = list(dict.fromkeys(
            asset_key
            for group in package["content_groups"].values()
            for asset_key in group.get("asset_keys", [])
        ))
        assets = []
        for asset_key in asset_keys:
            row = asset_map.get(asset_key)
            metadata = row.metadata_json if row else {}
            media_id = metadata.get("stored_media_id")
            media = db.get(StoredMedia, media_id) if media_id else None
            available = bool(
                row and row.available and media and Path(media.storage_path).is_file()
            )
            assets.append({
                "key": asset_key,
                "id": row.id if row else None,
                "display_name": row.display_name if row else asset_key,
                "media_type": row.media_type if row else "image",
                "usage": row.usage if row else "",
                **normalized_asset_narrative(metadata),
                "content_group_key": metadata.get("content_group_key", ""),
                "available": available,
                "live_approved": metadata.get('live_approved') is True,
                "review_state": metadata.get('review_state', 'pending'),
                "file_hash": row.file_hash if row else '',
                "media_id": media.id if available else None,
                "preview_url": f"/media/{media.id}/preview" if available else None,
            })

        source = package.get("source", {})
        missing_assets = [asset["key"] for asset in assets if not asset["available"]]
        items.append({
            **summaries[key],
            "managed_by": "versioned_route_package",
            "editable": True,
            "source": {
                "url": source.get("url"),
                "snapshot_sha256": source.get("snapshot_sha256"),
                "snapshot_manifest": Path(source.get("snapshot_manifest", "")).name,
                "branch_document": Path(source.get("branch_content", "")).name,
            },
            "required_slots": package["required_slots"],
            "ai_guidance": package.get("ai_guidance", ""),
            "initial_delivery_interval_seconds": package.get(
                "initial_delivery_interval_seconds", 2
            ),
            "knowledge_facts": package["knowledge_facts"],
            "content_sequence": package["content_sequence"],
            "content_groups": [{
                "key": group_key,
                "purpose": group["purpose"],
                "approved_text": group["approved_text"],
                "asset_keys": group.get("asset_keys", []),
                "evidence_refs": group.get("evidence_refs", []),
                "initial_delivery": bool(group.get("initial_delivery", False)),
                "delivery_mode": group.get("delivery_mode", "assets_then_text"),
                "sequence": package["content_sequence"].index(group_key) + 1
                if group_key in package["content_sequence"] else None,
            } for group_key, group in package["content_groups"].items()],
            "fixed_answers": deepcopy(package.get("fixed_answers", [])),
            "assets": assets,
            "readiness": {
                "knowledge_imported": version is not None,
                "assets_ready": len(assets) - len(missing_assets),
                "assets_total": len(assets),
                "missing_assets": missing_assets,
                "ai_reply_ready": version is not None and not missing_assets,
                "sop_ready": not missing_assets,
            },
            "versions": [{
                "version": package["package_version"],
                "status": "current",
                "summary": "当前已发布线路版本",
                "created_at": None,
                "user_id": None,
            }, *[{
                "version": (row.details or {}).get("package_version", "历史发布"),
                "status": "history",
                "summary": (row.details or {}).get("name", "线路资料发布"),
                "created_at": row.created_at,
                "user_id": row.user_id,
            } for row in db.scalars(select(AuditLog).where(
                AuditLog.action == "route_package.published",
                AuditLog.resource_type == "route_package",
                AuditLog.resource_id == key,
            ).order_by(AuditLog.id.desc()).limit(10)).all()
                if (row.details or {}).get("package_version") != package["package_version"]]],
        })
    return {"items": items, "outbound": False}


@router.get("/automation/reception-config")
def reception_config(user: User = Depends(manager), db: Session = Depends(get_db)):
    history = db.scalars(select(AuditLog).where(
        AuditLog.action == "reception_config.updated",
        AuditLog.resource_type == "app_setting",
        AuditLog.resource_id == "route_reception_config",
    ).order_by(AuditLog.id.desc()).limit(20)).all()
    return {
        "config": get_reception_configuration(db),
        "version": {
            "label": release_id(),
            "status": "published",
            "published_at": history[0].created_at if history else None,
            "published_by": history[0].user_id if history else None,
        },
        "runtime": {
            "live_silence_enabled": live_silence_enabled(db),
            "model": settings.deepseek_model,
            "timeout_seconds": settings.deepseek_timeout_seconds,
            "concurrency": settings.live_reply_concurrency,
        },
        "history": [{
            "id": row.id,
            "label": f"配置版本 {index + 1}",
            "summary": "更新全局 AI 接待策略",
            "user_id": row.user_id,
            "created_at": row.created_at,
        } for index, row in enumerate(history)],
        "draft_imports": [],
        "hard_guards": {
            "rollout_scope": reception_rollout(db).scope,
            "require_ai_label": True,
            "respect_channel_window": True,
            "stop_on_human_reply": True,
            "deduplicate_messages": True,
            "deduplicate_materials": True,
            "evaluation_outbound": False,
        },
        "outbound": False,
    }


@router.patch("/automation/reception-config")
def update_reception_config(
    payload: dict,
    user: User = Depends(manager_write),
    db: Session = Depends(get_db),
):
    before = get_reception_configuration(db)
    editable = {
        "reply": {"opening_items", "opening_interval_seconds", "goal", "tone", "tone_guidance", "custom_guidance", "opening_character_limit"},
        "lead_capture": {"enabled", "channels"},
        "routing": {"enabled_route_variants", "allow_route_switch", "preserve_profile_on_switch", "outside_catalog_action"},
        "handoff": {"large_group_enabled", "large_group_minimum"},
        "silence": {"enabled", "live_enabled", "intervals_minutes", "max_proactive_messages_per_day", "active_start", "active_end"},
        "common_scripts": None,
    }
    value = deepcopy(before)
    for section, changes in payload.items():
        if section not in editable:
            fail("configuration_field_not_editable", 422)
        allowed = editable[section]
        if allowed is None:
            value[section] = changes
        else:
            if not isinstance(changes, dict) or set(changes) - allowed:
                fail("configuration_field_not_editable", 422)
            value[section].update(changes)
    try:
        candidate = ReceptionConfiguration.model_validate(value)
    except ValueError as exc:
        raise HTTPException(422, detail={"code": "configuration_invalid", "message": str(exc)}) from exc
    try:
        value = put_reception_configuration(db, candidate)
    except ValueError as exc:
        raise HTTPException(422, detail={'code': str(exc), 'message': str(exc)}) from exc
    audit(db, user, "reception_config.updated", "app_setting", "route_reception_config", {"before": before, "after": value})
    db.commit()
    return {"config": value, "applies_to": "next_v3_turn_and_new_delivery_plan", "outbound": False}


@router.post("/automation/route-products/import")
def import_route_product_draft(
    payload: RoutePackageImportInput,
    user: User = Depends(manager_write),
    db: Session = Depends(get_db),
):
    encoded = json.dumps(payload.package, ensure_ascii=False).encode("utf-8")
    if len(encoded) > 1_000_000:
        fail("route_package_too_large", 413)
    try:
        package = _validate(deepcopy(payload.package), Path("uploaded-route-package.json"))
    except (RoutePackageError, TypeError, ValueError) as exc:
        raise HTTPException(422, detail={
            "code": "route_package_invalid",
            "message": str(exc),
        }) from exc
    route_id = package["route_variant"]
    tenant = db.scalar(select(Tenant).order_by(Tenant.id))
    version = db.scalar(select(KnowledgeVersion).where(
        KnowledgeVersion.tenant_id == tenant.id,
        KnowledgeVersion.version_key == package["knowledge_version"],
    )) if tenant else None
    if not tenant or not version:
        fail("route_knowledge_version_missing", 422)
    asset_keys = {
        key
        for group in package["content_groups"].values()
        for key in group.get("asset_keys", [])
    }
    assets = {
        row.asset_key: row
        for row in db.scalars(select(MaterialAsset).where(
            MaterialAsset.knowledge_version_id == version.id,
            MaterialAsset.asset_key.in_(asset_keys) if asset_keys else True,
        )).all()
    }
    missing = sorted(asset_keys - set(assets))
    if missing:
        raise HTTPException(422, detail={
            "code": "route_assets_missing",
            "message": f"请先上传线路使用的素材：{', '.join(missing)}",
        })
    previous_package = deepcopy(ROUTE_PACKAGES.get(route_id))
    previous_was_runtime = bool(
        previous_package
        and Path(previous_package.get("source_path", "")).is_relative_to(RUNTIME_PACKAGE_ROOT)
    )
    try:
        install_route_package(package)
        active_package = ROUTE_PACKAGES[route_id]
        for asset in assets.values():
            metadata = dict(asset.metadata_json or {})
            metadata["route_variants"] = list(dict.fromkeys([
                *(metadata.get("route_variants") or []), route_id,
            ]))
            asset.metadata_json = metadata
        db.flush()
        current_config = get_reception_configuration(db)
        if route_id not in current_config["routing"]["enabled_route_variants"]:
            current_config["routing"]["enabled_route_variants"].append(route_id)
            put_reception_configuration(db, ReceptionConfiguration.model_validate(current_config))
    except Exception:
        db.rollback()
        if previous_package is not None and previous_was_runtime:
            install_route_package(previous_package)
        else:
            remove_runtime_route_package(route_id)
        raise

    summary = {
        "route_variant": route_id,
        "name": package["name"],
        "package_version": package["package_version"],
        "knowledge_version": package["knowledge_version"],
        "facts": len(package["knowledge_facts"]),
        "content_groups": len(package["content_groups"]),
        "sop_nodes": len(package["content_sequence"]),
        "activation_supported": True,
        "status": "published",
    }
    audit(db, user, "route_package.published", "route_package", route_id, summary)
    db.commit()
    return {
        "draft": summary,
        "message": "线路资料已校验、发布并加入 AI 接待。",
        "outbound": False,
    }


@router.put("/automation/route-products/{route_variant}/content")
def update_route_product_content(
    route_variant: str,
    payload: RouteContentUpdate,
    user: User = Depends(manager_write),
    db: Session = Depends(get_db),
):
    """Edit operator-facing facts and approved content, then publish atomically."""
    ensure_route_packages_current()
    current = ROUTE_PACKAGES.get(route_variant)
    if not current:
        fail("route_product_not_found", 404)
    group_keys = [group.key for group in payload.content_groups]
    if len(group_keys) != len(set(group_keys)):
        fail("route_content_group_duplicate", 422)
    removed = set(current["content_groups"]) - set(group_keys)
    added = set(group_keys) - set(current["content_groups"])
    if (payload.base_package_version is not None
            and payload.base_package_version != current["package_version"]):
        raise HTTPException(409, detail={
            "code": "route_content_version_conflict",
            "message": "线路已有新版本，请刷新后重新编辑。",
        })
    if (removed or added) and payload.base_package_version is None:
        raise HTTPException(409, detail={
            "code": "route_content_version_required",
            "message": "新增或删除内容组需要当前线路版本，请刷新后重试。",
        })
    for group in payload.content_groups:
        if group.key in added and (
            not re.fullmatch(r"[a-z][a-z0-9_-]{0,119}", group.key)
            or not group.purpose.strip()
        ):
            fail("route_content_group_invalid", 422)
    content_sequence = (list(current["content_sequence"])
                        if payload.content_sequence is None else payload.content_sequence)
    if len(content_sequence) != len(set(content_sequence)):
        fail("route_content_sequence_duplicate", 422)
    if any(key not in set(group_keys) | removed for key in content_sequence):
        fail("route_content_sequence_unknown_group", 422)
    content_sequence = [key for key in content_sequence if key not in removed]
    if not content_sequence:
        raise HTTPException(422, detail={
            "code": "route_content_sequence_empty",
            "message": "至少保留一个主线内容组。",
        })
    fact_ids = [fact.id for fact in payload.knowledge_facts]
    if len(fact_ids) != len(set(fact_ids)):
        fail("route_fact_duplicate", 422)

    package = deepcopy(current)
    if payload.name is not None:
        package["name"] = payload.name.strip()
    if payload.selection_title is not None:
        package["selection_title"] = payload.selection_title.strip()
    if payload.match_keywords is not None:
        package["match_keywords"] = list(dict.fromkeys(
            keyword.strip() for keyword in payload.match_keywords if keyword.strip()
        ))
    package["default_entry_message"] = payload.default_entry_message.strip()
    package["ai_guidance"] = payload.ai_guidance.strip()
    package["initial_delivery_interval_seconds"] = payload.initial_delivery_interval_seconds
    package["knowledge_facts"] = [fact.model_dump() for fact in payload.knowledge_facts]
    package["content_sequence"] = list(content_sequence)
    package["content_groups"] = {
        group.key: {
            **deepcopy(current["content_groups"].get(group.key, {})),
            **({"operator_managed": True} if group.key in added else {}),
            "purpose": group.purpose.strip(),
            "approved_text": group.approved_text.strip(),
            "asset_keys": list(dict.fromkeys(group.asset_keys)),
            "evidence_refs": list(dict.fromkeys(group.evidence_refs)),
            "initial_delivery": group.initial_delivery,
            "delivery_mode": group.delivery_mode,
        }
        for group in payload.content_groups
    }
    if payload.fixed_answers is not None:
        package["fixed_answers"] = [answer.model_dump() for answer in payload.fixed_answers]

    remaining_assets = {asset for group in package["content_groups"].values()
                        for asset in group.get("asset_keys", [])}
    removed_assets = {asset for key in removed for asset in current["content_groups"][key].get("asset_keys", [])} - remaining_assets
    dependencies = [
        f"fixed_answers.{answer['id']}"
        for answer in package.get("fixed_answers", [])
        if (answer.get("content_group_key") in removed
            or removed_assets.intersection(answer.get("asset_ids", [])))
    ]

    def find_removed_refs(value, path):
        # Unknown package extensions are treated conservatively, including map keys.
        if isinstance(value, dict):
            for key, child in value.items():
                if key in removed:
                    dependencies.append(f"{path}.{key}")
                find_removed_refs(child, f"{path}.{key}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                find_removed_refs(child, f"{path}[{index}]")
        elif isinstance(value, str) and value in removed:
            dependencies.append(path)

    if removed:
        for field, value in package.items():
            # Runtime SOP is derived on publication, never edited in place.
            if field not in {"sop", "policies", "journey_policy", "runtime_sop", "source_path", "fixed_answers"}:
                find_removed_refs(value, field)
    if dependencies:
        raise HTTPException(422, detail={
            "code": "route_content_group_in_use",
            "message": "不能删除仍被引用的内容组，请先处理依赖：" + ", ".join(dependencies),
            "dependencies": dependencies,
        })
    from uuid import uuid4
    from datetime import datetime

    timestamp = datetime.fromisoformat(utcnow())
    previous_version = current["package_version"]
    previous_stamp = re.fullmatch(
        r"(\d{4}-\d{2}-\d{2})\.operator-v2-(\d{12})-[0-9a-f]{32}", previous_version,
    )
    if previous_stamp:
        floor = datetime.strptime("".join(previous_stamp.groups()), "%Y-%m-%d%H%M%S%f")
        timestamp = max(timestamp, floor.replace(tzinfo=timestamp.tzinfo) + timedelta(microseconds=1))
    nonce = uuid4().hex
    version = f"{timestamp:%Y-%m-%d}.operator-v2-{timestamp:%H%M%S%f}-{nonce}"
    if version <= previous_version:
        # v2 sorts above legacy operator-hex; a future-dated or custom version
        # still needs a strictly higher date rather than an inactive publication.
        try:
            floor = datetime.strptime(previous_version[:10], "%Y-%m-%d")
        except ValueError:
            fail("route_content_version_not_sortable", 409)
        timestamp = max(timestamp, floor.replace(tzinfo=timestamp.tzinfo) + timedelta(days=1))
        version = f"{timestamp:%Y-%m-%d}.operator-v2-{timestamp:%H%M%S%f}-{nonce}"
    package["package_version"] = version
    result = import_route_product_draft(RoutePackageImportInput(package=package), user, db)
    result["message"] = "线路事实与回复内容已保存并发布。"
    return result


def _route_asset(db: Session, route_variant: str, asset_key: str, *, create_missing=False):
    ensure_route_packages_current()
    package = ROUTE_PACKAGES.get(route_variant)
    if not package:
        fail("route_product_not_found", 404)
    referenced = {
        key
        for group in package["content_groups"].values()
        for key in group.get("asset_keys", [])
    }
    if asset_key not in referenced:
        fail("route_asset_not_found", 404)
    tenant = db.scalar(select(Tenant).order_by(Tenant.id))
    version = db.scalar(select(KnowledgeVersion).where(
        KnowledgeVersion.tenant_id == tenant.id,
        KnowledgeVersion.version_key == package["knowledge_version"],
    )) if tenant else None
    asset = db.scalar(select(MaterialAsset).where(
        MaterialAsset.knowledge_version_id == version.id,
        MaterialAsset.asset_key == asset_key,
    )) if version else None
    if tenant and create_missing and not asset:
        if not version:
            version = KnowledgeVersion(tenant_id=tenant.id, version_key=package['knowledge_version'], title=package['name'], content_hash='pending')
            db.add(version); db.flush()
        group_key, group = next((k,g) for k,g in package['content_groups'].items() if asset_key in g.get('asset_keys', []))
        shared_routes = [r for r,p in ROUTE_PACKAGES.items() if p['knowledge_version'] == package['knowledge_version']
            and any(asset_key in g.get('asset_keys', []) for g in p['content_groups'].values())]
        asset = MaterialAsset(knowledge_version_id=version.id, asset_key=asset_key, source_path='', display_name=asset_key,
            usage=group.get('purpose',''), available=False, metadata_json={'route_variants':shared_routes,
            'content_group_key':group_key,'content_family':asset_key,'review_state':'pending','live_approved':False})
        db.add(asset); db.flush()
    if not tenant or not asset:
        fail("route_asset_not_found", 404)
    return tenant, asset


@router.put("/automation/route-products/{route_variant}/assets/{asset_key}")
def update_route_asset(
    route_variant: str,
    asset_key: str,
    payload: RouteAssetUpdate,
    user: User = Depends(manager_write),
    db: Session = Depends(get_db),
):
    _, asset = _route_asset(db, route_variant, asset_key)
    metadata = dict(asset.metadata_json or {})
    before = {
        "display_name": asset.display_name,
        "usage": asset.usage,
        **normalized_asset_narrative(metadata),
    }
    asset.display_name = payload.display_name.strip()
    asset.usage = payload.usage.strip()
    narrative = normalized_asset_narrative({
        "what_it_shows": payload.what_it_shows,
        "feature_points": payload.feature_points,
        "customer_value": payload.customer_value,
        "recommended_caption": payload.recommended_caption,
        "avoid_claims": payload.avoid_claims,
    })
    asset.metadata_json = {**metadata, **narrative}
    audit(db, user, "route_asset.updated", "asset", asset.id, {
        "route_variant": route_variant, "asset_key": asset_key, "before": before,
        "after": {"display_name": asset.display_name, "usage": asset.usage, **narrative},
    })
    db.commit()
    return {"key": asset.asset_key, "display_name": asset.display_name,
            "usage": asset.usage, **narrative, "outbound": False}


@router.post("/automation/route-products/{route_variant}/assets/{asset_key}/replace")
def replace_route_asset(
    route_variant: str,
    asset_key: str,
    payload: RouteAssetReplace,
    user: User = Depends(manager_write),
    db: Session = Depends(get_db),
):
    tenant, asset = _route_asset(db, route_variant, asset_key, create_missing=True)
    media = db.get(StoredMedia, payload.media_id)
    if (not media or media.tenant_id != tenant.id or not (media.media_type == 'image' or
            (media.media_type == 'file' and media.mime_type == 'application/pdf'))
            or not Path(media.storage_path).is_file()):
        fail("material_unavailable", 422)
    previous_media_id = (asset.metadata_json or {}).get("stored_media_id")
    metadata = dict(asset.metadata_json or {})
    try:
        replace_asset_binding(db, asset, media, metadata_updates={
            "review_state": "evaluation_ready",
            "route_variants": list(dict.fromkeys([*(metadata.get("route_variants") or []), route_variant])),
            "replaced_at": utcnow(),
        })
    except ValueError as exc:
        fail(str(exc), 409 if str(exc) == "material_revision_conflict" else 422)
    metadata = asset.metadata_json
    audit(db, user, "route_asset.replaced", "asset", asset.id, {
        "route_variant": route_variant, "asset_key": asset_key,
        "previous_media_id": previous_media_id, "media_id": media.id,
    })
    db.commit()
    return {
        "key": asset.asset_key,
        "media_id": media.id,
        "preview_url": f"/media/{media.id}/preview",
        "available": asset.available,
        "affected_routes": metadata["route_variants"],
        "outbound": False,
    }






@router.get("/playground/sessions")
def sessions(user:User=Depends(manager),db:Session=Depends(get_db)):
    q=select(AutomationSession).where(AutomationSession.owner_id==user.id, AutomationSession.environment == "playground", AutomationSession.engine_version == "v3")
    allowed=allowed_inbox_ids(db,user)
    if allowed is not None:q=q.where(AutomationSession.inbox_binding_id.in_(allowed))
    return {"items":[{"id":x.id,"mode":x.mode,"created_at":x.created_at,"virtual_now":x.virtual_now,"engine_version":x.engine_version,
                      "route_variant":x.controls.get("route_variant", ""),"status":simulation_state(x).get("status"),
                      "duration_minutes":simulation_state(x).get("duration_minutes")} for x in db.scalars(q.order_by(AutomationSession.id.desc()).limit(50)).all()]}


@router.get("/playground/sessions/{session_id}")
def get_session(session_id:int,user:User=Depends(manager),db:Session=Depends(get_db)):
    return session_json(db,own_session(db,user,session_id))


@router.delete("/playground/sessions/{session_id}", status_code=204)
def delete_session(session_id:int,user:User=Depends(manager_write),db:Session=Depends(get_db)):
    row = own_session(db, user, session_id)
    if row.environment != "playground":
        fail("session_not_deletable", 403)
    key = f"playground:{row.id}"
    enrollment_ids = list(db.scalars(
        select(RehearsalEnrollment.id).where(RehearsalEnrollment.session_id == row.id)
    ).all())
    if enrollment_ids:
        db.execute(delete(RehearsalJob).where(RehearsalJob.enrollment_id.in_(enrollment_ids)))
    db.execute(delete(RehearsalEnrollment).where(RehearsalEnrollment.session_id == row.id))
    db.execute(delete(SilenceCycle).where(SilenceCycle.session_id == row.id))
    db.execute(delete(AutomationRun).where(AutomationRun.session_id == row.id))
    db.execute(delete(MaterialDelivery).where(MaterialDelivery.session_id == row.id))
    db.execute(delete(TouchReservation).where(TouchReservation.contact_key == key))
    db.delete(row)
    db.commit()
    return Response(status_code=204)


class MessageInput(BaseModel):
    content:str=Field(default="",max_length=4000)
    client_key:str=Field(min_length=1,max_length=80)
    content_type:Literal["text","image","audio","video","file"]="text"


@router.post("/playground/sessions/{session_id}/messages",status_code=202)
def message(session_id:int,payload:MessageInput,user:User=Depends(manager_write),db:Session=Depends(get_db)):
    row=own_session(db,user,session_id)
    if row.engine_version != 'v3' or row.environment != 'playground':
        fail('historical_session_read_only', 409)
    if row.environment == "shadow":fail("shadow_session_read_only",403)
    if row.mode == "journey" and simulation_state(row).get("status") != "running":
        fail("journey_not_running")
    if not payload.content.strip() and payload.content_type=="text":fail("text_required",422)
    service.add_message(db,row,payload.content,payload.client_key,payload.content_type)
    db.commit()
    return session_json(db,row)


@router.get("/media/{media_id}/preview")
def media_preview(media_id:int,user:User=Depends(manager),db:Session=Depends(get_db)):
    row=db.get(StoredMedia,media_id)
    if not row:fail("media_not_found",404)
    if row.created_by!=user.id and not is_admin(user):
        from app.material_library import by_media
        asset = by_media(db, row)
        allowed = allowed_inbox_ids(db, user) or []
        tenant_inbox = db.scalar(select(InboxBinding.id).where(InboxBinding.id.in_(allowed), InboxBinding.tenant_id == row.tenant_id))
        if not asset or not tenant_inbox:fail("media_not_found",404)
    if not Path(row.storage_path).is_file():fail("material_unavailable",404)
    # Never execute uploaded HTML/SVG as same-origin content.
    inline=row.mime_type in ("image/png","image/jpeg","image/webp","image/gif","video/mp4","video/webm","audio/mpeg","application/pdf")
    return FileResponse(row.storage_path,media_type=row.mime_type if inline else "application/octet-stream",
        filename=row.original_name if row.mime_type=="application/pdf" or not inline else None,
        content_disposition_type="inline" if inline else "attachment",
        headers={"X-Content-Type-Options":"nosniff", "Content-Security-Policy":"sandbox", "Cache-Control":"private, no-store",
                 **({"Content-Disposition":"inline"} if inline and row.mime_type!="application/pdf" else {})})


class AssetBind(BaseModel):
    media_id:int


@router.post("/knowledge/assets/{asset_id}/bind")
def bind_asset(asset_id:int,payload:AssetBind,user:User=Depends(require_super_admin_csrf),db:Session=Depends(get_db)):
    asset=db.get(MaterialAsset,asset_id)
    media=db.get(StoredMedia,payload.media_id)
    if (not asset or not media or not (media.media_type == 'image' or
            (media.media_type == 'file' and media.mime_type == 'application/pdf'))
            or not Path(media.storage_path).is_file()):fail("material_unavailable",422)
    from app.material_library import CATALOG_VERSION
    from app.models import KnowledgeVersion
    version = db.get(KnowledgeVersion, asset.knowledge_version_id)
    if version.tenant_id != media.tenant_id:fail("material_unavailable",422)
    if version.version_key == CATALOG_VERSION:fail("published_material_immutable",409)
    try:
        replace_asset_binding(db, asset, media, metadata_updates={"bound_at": utcnow()})
    except ValueError as exc:
        fail(str(exc), 409 if str(exc) == "material_revision_conflict" else 422)
    audit(db,user,"knowledge.asset_bind","asset",asset.id)
    db.commit()
    return {"id":asset.id,"key":asset.asset_key,"available":asset.available,"media_id":media.id}


@router.get("/materials")
def shared_materials(inbox_binding_id:int, route_variant:str="", user:User=Depends(manager), db:Session=Depends(get_db)):
    from app.material_library import catalog_assets, ROUTES
    scope(db,user,inbox_binding_id)
    inbox = db.get(InboxBinding,inbox_binding_id)
    items=[]
    for asset in catalog_assets(db,inbox.tenant_id):
        meta=asset.metadata_json
        ready=asset.available and Path(asset.source_path).is_file()
        if route_variant and route_variant not in meta.get("route_variants",[]):continue
        items.append({"id":asset.id,"key":asset.asset_key,"name":asset.display_name,"topic":asset.usage,
            "media_id":meta.get("stored_media_id"),"media_hash":asset.file_hash,"content_type":asset.media_type,
            "content_family":meta.get("content_family"),"route_variants":meta.get("route_variants",[]),
            "ready":ready,"review_state":meta.get("review_state"),"notes":meta.get("review_notes","")})
    return {"items":items,"routes":ROUTES,"outbound":False}



class SessionCreate(BaseModel):
    mode: Literal['journey'] = 'journey'
    engine_version: Literal['v3'] = 'v3'
    inbox_binding_id: int | None = None
    virtual_now: str | None = None
    route_variant: str = ''
    entry_message: str = Field(default='', max_length=4000)
    duration_minutes: int = Field(default=525600, ge=5, le=525600)


@router.post('/playground/sessions', status_code=201)
def create_session(payload: SessionCreate, user: User = Depends(manager_write), db: Session = Depends(get_db)):
    scope(db, user, payload.inbox_binding_id)
    if payload.route_variant and payload.route_variant not in ROUTES:
        fail('route_not_found', 422)
    row = AutomationSession(owner_id=user.id, inbox_binding_id=payload.inbox_binding_id,
                            environment='playground', mode='journey', engine_version='v3',
                            engine_release_id=release_id(), virtual_now=iso(dt(payload.virtual_now)) if payload.virtual_now else utcnow(),
                            controls={'route_variant': payload.route_variant}, messages=[], memory={})
    db.add(row)
    db.flush()
    service.start(db, row, payload.entry_message.strip() or '你好，我想咨询旅行行程', payload.duration_minutes)
    db.commit()
    return session_json(db, row)


@router.post('/playground/sessions/{session_id}/control/{action}')
def journey_action(session_id: int, action: Literal['pause','resume','stop'],
                   user: User = Depends(manager_write), db: Session = Depends(get_db)):
    row = own_session(db, user, session_id)
    service.control(row, action)
    db.commit()
    return session_json(db, row)


@router.post('/playground/sessions/{session_id}/advance-next')
def advance_journey_to_next_touch(session_id: int, user: User = Depends(manager_write), db: Session = Depends(get_db)):
    row = own_session(db, user, session_id)
    try:
        service.advance_next(row)
    except ValueError as exc:
        fail(str(exc))
    db.commit()
    return session_json(db, row)


@router.post('/playground/sessions/{session_id}/retry')
def retry_session(session_id: int, user: User = Depends(manager_write), db: Session = Depends(get_db)):
    row = own_session(db, user, session_id)
    value = service.state(row)
    if not value.get('failed_event'):
        fail('session_not_failed')
    value['pending_event'] = value.pop('failed_event')
    for key in ('attempts', 'retry_at', 'last_error'):
        value.pop(key, None)
    row.generation += 1
    service.save(row, value)
    row.due_at = utcnow()
    db.commit()
    return session_json(db, row)
