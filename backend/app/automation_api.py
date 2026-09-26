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
from app.models import User, Tenant, InboxBinding, ConversationState, MessageEvent, SopDefinition, StoredMedia, MaterialAsset, KnowledgeVersion, AuditLog, WebKnowledgeSource, WebKnowledgeRevision, utcnow
from app.advisor_voice import ADVISOR_VOICE_VERSION
from app.automation_models import *
from app.live_reply_models import LiveReplyJob
from app.automation_service import (DEFAULT_REPLY, DEFAULT_WAKEUP, reply_policy, add_customer_message, cancel_generation, dt, iso, gate,
    confirm_draft, create_cycle, queue_wakeup, enroll_rehearsal, advance_sops, sop_snapshot, media_error, reserve_touch, touch_key,
    apply_controls, trigger_sops, enrollment_allowed, subject_key, start_journey, start_open_journey,
    change_journey_status, simulation_state, set_simulation_state)
from app.config import settings
from app.decision_service import VALIDATOR_VERSION, generate_decision
from app.deepseek_evaluation import EvaluationCallError
from app.decision_knowledge import FACTS
from app.realtime_reply_pipeline import REALTIME_REPLY_PROMPT_VERSION
from app.reply_generation import REPLY_GENERATOR_PROMPT_VERSION
from app.reply_fact_verification import FACT_VERIFIER_PROMPT_VERSION
from app.reply_planning import PLANNER_VERSION
from app.reply_understanding import UNDERSTANDING_PROMPT_VERSION
from app.silence_generation import SILENCE_GENERATOR_PROMPT_VERSION
from app.silence_planning import SILENCE_PLANNER_VERSION
from app.silence_touch_pipeline import SILENCE_TOUCH_PROMPT_VERSION
from app.material_library import candidate_materials, freeze_nodes, replace_asset_binding
from app.operations import allowed_inbox_ids, is_admin, audit, save_setting, setting_value
from app.ops_schemas import SopNode
from app.route_packages import (JOURNEY_POLICY, ROUTE_PACKAGES, RoutePackageError,
                                RUNTIME_PACKAGE_ROOT, _validate, install_route_package,
                                ensure_route_packages_current, remove_runtime_route_package,
                                route_package_summary)
from app.route_reply import ROUTES, journey_context_from_values, normalize_journey_stage, playbook_prompt, prepare_route_reply_values
from app.security import encrypt_secret
from app.service_knowledge import SERVICE_KNOWLEDGE, SERVICE_KNOWLEDGE_VERSION
from app.reception_config import (
    ReceptionConfiguration,
    configured_silence_nodes,
    effective_reception_policy,
    get_reception_configuration,
    put_reception_configuration,
    silence_intervals,
)
from app.reception_rollout import reception_rollout
from app.reception_v2 import ENGINE_RELEASE_ID as V2_ENGINE_RELEASE_ID
from app.sop_schedule import schedule_preview
from app.web_knowledge import (
    WebKnowledgeError,
    enrich_context_with_web_knowledge,
    normalize_public_url,
    publish_revision,
    refresh_source,
    revision_knowledge_modules,
    web_knowledge_publish_enabled,
    web_knowledge_refresh_enabled,
)

router = APIRouter(prefix="/v1")


def _runtime_knowledge_modules() -> list[dict]:
    sources = {item["id"]: item for item in SERVICE_KNOWLEDGE.get("sources", [])}

    def source_url(source_ref: str) -> str:
        source_id = source_ref.split("#", 1)[0]
        if source_id in sources:
            return str(sources[source_id].get("url") or "")
        return source_ref if source_ref.startswith("https://") else ""

    def fact_json(fact: dict) -> dict:
        source_ref = str(fact.get("source") or "")
        source = sources.get(source_ref.split("#", 1)[0], {})
        return {
            "id": fact["id"],
            "text": fact["text"],
            "source_url": source_url(source_ref),
            "source_quote": source_ref.split("#", 1)[1] if "#" in source_ref else "平台已审核规则",
            "verified_at": str(source.get("fetched_at") or "").split("T", 1)[0] or "2026-09-12",
        }

    service_facts = [fact for fact in FACTS if str(fact.get("id") or "").startswith("service.")]
    health_ids = {
        "service.medical_preparedness", "service.medical_logistics",
        "service.website_medication_precautions", "service.medical_support",
        "service.altitude_symptoms", "service.altitude_response", "service.medication",
    }
    return [
        {
            "kind": "runtime_knowledge_module",
            "key": "runtime_health_and_medical",
            "title": "高原健康、用藥與就醫協助",
            "summary": "跨兩條線路共用的健康邊界、導遊協助、正規就醫及用藥注意事項。",
            "topics": ["高原反應", "隨隊醫師", "用藥", "送醫", "供氧邊界"],
            "runtime_scope": "live_and_playground",
            "version": SERVICE_KNOWLEDGE_VERSION,
            "facts": [fact_json(fact) for fact in service_facts if fact["id"] in health_ids],
            "fixed_answers": [
                {key: answer.get(key) for key in ("id", "name", "status", "answer_text", "source_ref", "positive_examples")}
                for answer in SERVICE_KNOWLEDGE.get("fixed_answers", [])
            ],
        },
        {
            "kind": "runtime_knowledge_module",
            "key": "runtime_reception_boundaries",
            "title": "接待、安全與即時核對邊界",
            "summary": "限制留資、即時名額、天候、客製需求及醫療承諾的系統級規則。",
            "topics": ["聯絡方式", "即時名額", "天候路況", "客製行程", "安全承諾"],
            "runtime_scope": "live_and_playground",
            "version": SERVICE_KNOWLEDGE_VERSION,
            "facts": [fact_json(fact) for fact in service_facts if fact["id"] not in health_ids],
        },
    ]


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


class SchedulePreviewInput(BaseModel):
    nodes: list[SopNode] = Field(max_length=20)
    customer_added_at: str
    frequency_hours: int = Field(default=24, ge=1, le=720)


class RouteSimulationMessage(BaseModel):
    role: Literal["customer", "assistant"] = "customer"
    content: str = Field(min_length=1, max_length=4000)


class RouteProductSimulationInput(BaseModel):
    messages: list[RouteSimulationMessage] = Field(min_length=1, max_length=30)
    scenario: Literal[
        "normal",
        "price_hotel",
        "route_switch",
        "large_group",
        "eleven_people",
        "other_destination",
        "contact_channel",
        "contact_value",
        "attachment_question",
    ] = "normal"


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
    delete_sop_group_keys: list[str] = Field(default_factory=list, max_length=100)
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


@router.post("/sops/schedule-preview")
def preview_schedule(payload: SchedulePreviewInput, user: User = Depends(manager_write)):
    try:
        added_at = iso(dt(payload.customer_added_at))
    except ValueError:
        fail("customer_added_time_invalid", 422)
    return {"items": schedule_preview([n.model_dump() for n in payload.nodes], added_at, payload.frequency_hours),
            "timezone": "Asia/Shanghai", "customer_added_source": "first_public_customer_message_in_inbox", "outbound": False}


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


@router.get("/knowledge/runtime-modules")
def runtime_knowledge_modules(user: User = Depends(manager)):
    items = _runtime_knowledge_modules()
    return {
        "items": items,
        "summary": {
            "modules": len(items),
            "facts": sum(len(item["facts"]) for item in items),
            "scope": "live_and_playground",
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
def publish_web_knowledge_revision(source_id: int, revision_id: int, user: User = Depends(manager_write), db: Session = Depends(get_db)):
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
    publish_revision(db, source, revision, runtime_scope="playground")
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


def session_json(db,row):
    runs = db.scalars(select(AutomationRun).where(AutomationRun.session_id==row.id).order_by(AutomationRun.id.desc()).limit(30)).all()
    jobs = db.scalars(select(RehearsalJob).join(RehearsalEnrollment).where(RehearsalEnrollment.session_id==row.id).order_by(RehearsalJob.id)).all()
    cycles = db.scalars(select(SilenceCycle).where(SilenceCycle.session_id==row.id).order_by(SilenceCycle.id.desc())).all()
    enrollments = db.scalars(select(RehearsalEnrollment).where(RehearsalEnrollment.session_id == row.id)).all()
    simulation = simulation_state(row)
    start = dt(simulation["start_virtual_at"]) if simulation.get("start_virtual_at") else None
    end = dt(simulation["end_virtual_at"]) if simulation.get("end_virtual_at") else None
    elapsed = max(0, (dt(row.virtual_now) - start).total_seconds() / 60) if start else 0
    duration = max(1, int(simulation.get("duration_minutes") or 1))
    next_job = next((x for x in jobs if x.status in ("scheduled", "waiting_dependency")), None)
    simulation_view = {
        **simulation,
        "elapsed_minutes": round(min(duration, elapsed), 2),
        "progress_percent": round(min(100, elapsed / duration * 100), 1),
        "next_event_at": next_job.scheduled_at if next_job else None,
        "next_event_name": next_job.node_key if next_job else None,
        "remaining_minutes": round(max(0, (end - dt(row.virtual_now)).total_seconds() / 60), 2) if end else None,
    }
    last_silence = next((x for x in runs if x.module == "silence_touch"), None)
    next_touch = next((x for x in jobs if x.status in ("scheduled", "waiting_dependency", "model_pending")), None)
    journey = dict((row.controls or {}).get("journey") or {})
    reception_state = {
        "journey_stage": normalize_journey_stage(journey.get("stage", "route_selection")),
        "customer_profile": safe_text(row.memory),
        "last_touch": safe_text(last_silence.decision) if last_silence else None,
        "next_touch_at": next_touch.scheduled_at if next_touch else None,
        "next_touch_status": next_touch.status if next_touch else None,
        "last_warning": next((safe_text(x.get("content")) for x in reversed((row.controls or {}).get("timeline_events", [])) if x.get("event_type") == "silence_model_warning"), None),
    }
    return {"id":row.id,"mode":row.mode,"environment":row.environment,"engine_version":row.engine_version,"engine_release_id":row.engine_release_id,"generation":row.generation,"virtual_now":row.virtual_now,"messages":safe_text(row.messages),"memory":safe_text(row.memory),"controls":safe_text(row.controls),"simulation":simulation_view,"reception_state":reception_state,"pending":bool(row.due_at),"runs":[run_json(x) for x in runs],"jobs":[{"id":x.id,"enrollment_id":x.enrollment_id,"node_key":x.node_key,"scheduled_at":x.scheduled_at,"status":x.status,"reason":x.reason,"confirmed_at":x.confirmed_at,"payload":safe_text(x.payload)} for x in jobs],"enrollments":[{"id":x.id,"sop_id":x.sop_id,"version_id":x.sop_version_id,"round_number":x.round_number,"status":x.status,"enrolled_at":x.enrolled_at} for x in enrollments],"cycles":[cycle_json(x) for x in cycles],"outbound":False}


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


class ConversationEngineUpdate(BaseModel):
    engine_version: Literal["v1", "v2"]
    expected_version: int = Field(ge=1)


@router.patch("/automation/conversations/{conversation_state_id}/engine")
def update_conversation_engine(
    conversation_state_id: int,
    payload: ConversationEngineUpdate,
    user: User = Depends(manager_write),
    db: Session = Depends(get_db),
):
    state = db.get(ConversationState, conversation_state_id)
    if not state:
        fail("conversation_not_found", 404)
    scope(db, user, state.inbox_binding_id)
    if state.version != payload.expected_version:
        fail("version_conflict", 409)
    if state.ai_engine_version == payload.engine_version:
        return {
            "conversation_state_id": state.id,
            "engine_version": state.ai_engine_version,
            "engine_release_id": state.ai_engine_release_id,
            "version": state.version,
            "changed": False,
        }

    previous = state.ai_engine_version
    now = utcnow()
    release_id = V2_ENGINE_RELEASE_ID if payload.engine_version == "v2" else "v1"
    changed = db.execute(update(ConversationState).where(
        ConversationState.id == state.id,
        ConversationState.version == payload.expected_version,
        ConversationState.ai_engine_version == previous,
    ).values(
        ai_engine_version=payload.engine_version,
        ai_engine_release_id=release_id,
        version=ConversationState.version + 1,
        updated_at=now,
    ))
    if changed.rowcount != 1:
        db.rollback()
        fail("version_conflict", 409)

    reply_in_flight = db.scalar(select(LiveReplyJob.id).where(
        LiveReplyJob.conversation_state_id == state.id,
        LiveReplyJob.status.in_(["processing", "submission_unknown"]),
    ))
    sop_in_flight = db.scalar(
        select(LiveSopJob.id).join(LiveSopEnrollment).where(
            LiveSopEnrollment.conversation_state_id == state.id,
            LiveSopJob.status.in_(["processing", "submission_unknown"]),
        )
    )
    if reply_in_flight or sop_in_flight:
        db.rollback()
        fail("engine_switch_busy", 409)

    db.execute(update(LiveReplyJob).where(
        LiveReplyJob.conversation_state_id == state.id,
        LiveReplyJob.status == "queued",
    ).values(status="cancelled", error_code="engine_version_changed", completed_at=now))
    enrollments = db.scalars(select(LiveSopEnrollment).where(
        LiveSopEnrollment.conversation_state_id == state.id,
        LiveSopEnrollment.status == "active",
    )).all()
    enrollment_ids = [row.id for row in enrollments]
    if enrollment_ids:
        db.execute(update(LiveSopJob).where(
            LiveSopJob.enrollment_id.in_(enrollment_ids),
            LiveSopJob.status.in_(["scheduled", "waiting_dependency"]),
        ).values(status="cancelled", reason="engine_version_changed", completed_at=now))
        for enrollment in enrollments:
            enrollment.status = "stopped"
            enrollment.exit_reason = "engine_version_changed"
            enrollment.completed_at = now

    from app.reception_v2.engine_projection import project_engine_state
    projection = project_engine_state(db, state, previous, payload.engine_version, now)
    db.refresh(state)
    audit(db, user, "conversation.engine_changed", "conversation_state", state.id, {
        "from": previous,
        "to": state.ai_engine_version,
        "cancelled_enrollment_ids": enrollment_ids,
        "projection": projection,
    })
    db.commit()
    return {
        "conversation_state_id": state.id,
        "engine_version": state.ai_engine_version,
        "engine_release_id": state.ai_engine_release_id,
        "version": state.version,
        "changed": True,
    }


@router.get("/automation/reply-policy")
def get_reply_policy(inbox_binding_id: int | None=None,user:User=Depends(manager),db:Session=Depends(get_db)):
    scope(db,user,inbox_binding_id)
    config,version = reply_policy(db,inbox_binding_id)
    key = f"inbox:{inbox_binding_id}" if inbox_binding_id else "global"
    local = db.scalar(select(ReplyPolicy).where(ReplyPolicy.scope_key==key))
    return {**config,"version":local.version if local else 1,"effective_version":version,"inbox_binding_id":inbox_binding_id}


@router.patch("/automation/reply-policy")
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


class SessionCreate(BaseModel):
    mode:Literal["reply","sop","wakeup","journey"]="reply"
    inbox_binding_id:int|None=None
    virtual_now:str|None=None
    conversation_id:int|None=None
    wakeup_policy_id:int|None=None
    route_variant: str = Field(default="", max_length=80)
    duration_minutes:int=Field(default=525600,ge=5,le=525600)
    speed_multiplier:int=Field(default=1,ge=1,le=3600)
    entry_message:str=Field(default="",max_length=4000)
    sop_version_id:int|None=None
    engine_version:Literal["v1","v2"]=Field(default_factory=lambda: settings.ai_engine_default)


def journey_versions(db: Session, user: User, inbox_binding_id: int | None, route: str | None = None) -> list[tuple[SopVersion, SopDefinition]]:
    scope(db, user, inbox_binding_id)
    inbox = db.get(InboxBinding, inbox_binding_id) if inbox_binding_id else None
    items = []
    for sop in db.scalars(select(SopDefinition).where(SopDefinition.status == "running").order_by(SopDefinition.id)).all():
        version = db.scalar(select(SopVersion).where(SopVersion.sop_id == sop.id).order_by(SopVersion.version.desc()).limit(1))
        if not version:
            continue
        config = version.config or {}
        if not config.get("route_variant") or (route and config.get("route_variant") != route):
            continue
        if config.get("inbox_ids") and (not inbox or inbox.chatwoot_inbox_id not in config["inbox_ids"]):
            continue
        try:
            published_scope(db, user, config)
        except HTTPException:
            continue
        items.append((version, sop))
    package_sop_names = {package["sop"]["name"] for package in ROUTE_PACKAGES.values()}
    return sorted(
        items,
        key=lambda item: (item[1].name in package_sop_names, item[0].version, item[0].id),
        reverse=True,
    )


@router.get("/playground/options")
def playground_options(inbox_binding_id:int|None=None,user:User=Depends(manager),db:Session=Depends(get_db)):
    versions = journey_versions(db, user, inbox_binding_id)
    strategies = [{
        "version_id": version.id,
        "sop_id": sop.id,
        "name": version.config.get("name") or sop.name,
        "version": version.version,
        "route_variant": version.config.get("route_variant"),
        "nodes": [{
            "key": node.get("key"),
            "schedule_type": node.get("schedule_type"),
            "basis": node.get("basis"),
            "delay_minutes": node.get("delay_minutes"),
            "day_number": node.get("day_number"),
            "message_count": len(node.get("messages") or [node]),
        } for node in version.config.get("nodes", [])],
    } for version, sop in versions]
    return {
        "routes": [{"id": key, "name": value["name"],
                    "default_entry_message": value["default_entry_message"],
                    "package_version": value["package_version"]}
                   for key, value in ROUTES.items()],
        "strategies": strategies,
        "outbound": False,
    }


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

        sop_bindings = []
        sops = db.scalars(select(SopDefinition).where(
            SopDefinition.route_variant == key
        ).order_by(SopDefinition.id.desc())).all()
        for sop in sops:
            published = db.scalar(select(SopVersion).where(
                SopVersion.sop_id == sop.id
            ).order_by(SopVersion.version.desc()).limit(1))
            sop_bindings.append({
                "id": sop.id,
                "name": sop.name,
                "status": sop.status,
                "draft_version": sop.version,
                "published_version": published.version if published else None,
                "published_nodes": len((published.config or {}).get("nodes", [])) if published else 0,
                "dry_run": sop.dry_run,
                "live_enabled": sop.live_enabled,
                "is_default": sop.name == package["runtime_sop"]["name"],
            })

        source = package.get("source", {})
        missing_assets = [asset["key"] for asset in assets if not asset["available"]]
        default_sop = next((sop for sop in sop_bindings if sop["is_default"]), None)
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
            "policies": package["policies"],
            "journey_policy": effective_reception_policy(db),
            "assets": assets,
            "default_sop": {
                "name": package["runtime_sop"]["name"],
                "description": package["runtime_sop"].get("description", ""),
                "trigger_type": package["runtime_sop"].get("trigger_type", "manual"),
                "trigger_labels": package["runtime_sop"].get("trigger_labels", []),
                "exit_labels": package["runtime_sop"].get("exit_labels", []),
                "stop_on_incoming": package["runtime_sop"]["stop_on_incoming"],
                "frequency_hours": package["runtime_sop"].get("frequency_hours", 24),
                "nodes": configured_silence_nodes(
                    package["runtime_sop"]["nodes"],
                    silence_intervals(db),
                    silence_enabled=bool(get_reception_configuration(db)["silence"]["enabled"]),
                ),
            },
            "sop_bindings": sop_bindings,
            "readiness": {
                "knowledge_imported": version is not None,
                "assets_ready": len(assets) - len(missing_assets),
                "assets_total": len(assets),
                "missing_assets": missing_assets,
                "default_sop_published": bool(default_sop and default_sop["published_version"]),
                "ai_reply_ready": version is not None and not missing_assets,
                "sop_ready": bool(default_sop and default_sop["published_version"] and not missing_assets),
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
            "label": "v6.4",
            "prompt_version": REALTIME_REPLY_PROMPT_VERSION,
            "validator_version": VALIDATOR_VERSION,
            "status": "published",
            "published_at": history[0].created_at if history else None,
            "published_by": history[0].user_id if history else None,
        },
        "model_nodes": {
            "advisor_voice": ADVISOR_VOICE_VERSION,
            "customer_understanding": UNDERSTANDING_PROMPT_VERSION,
            "business_planner": PLANNER_VERSION,
            "reply_generation": REPLY_GENERATOR_PROMPT_VERSION,
            "reply_fact_verification": FACT_VERIFIER_PROMPT_VERSION,
            "silence_planner": SILENCE_PLANNER_VERSION,
            "silence_generation": SILENCE_GENERATOR_PROMPT_VERSION,
            "silence_touch": SILENCE_TOUCH_PROMPT_VERSION,
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


@router.put("/automation/reception-config")
def update_reception_config(
    payload: ReceptionConfiguration,
    user: User = Depends(manager_write),
    db: Session = Depends(get_db),
):
    before = get_reception_configuration(db)
    value = put_reception_configuration(db, payload)
    audit(db, user, "reception_config.updated", "app_setting", "route_reception_config", {
        "before": before,
        "after": value,
    })
    db.commit()
    return {
        "config": value,
        "applies_to": "new_ai_decisions_and_new_sop_enrollments",
        "existing_active_sop_rounds_unchanged": True,
        "outbound": False,
    }


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
        source = active_package["runtime_sop"]
        nodes = freeze_nodes(db, source["nodes"], route_id, tenant.id)
        inbox_ids = list(db.scalars(select(InboxBinding.chatwoot_inbox_id).where(
            InboxBinding.tenant_id == tenant.id,
            InboxBinding.ai_enabled.is_(True),
        )).all())
        values = {
            "description": source.get("description", ""),
            "status": "running",
            "dry_run": False,
            "live_enabled": True,
            "trigger_type": source.get("trigger_type", "manual"),
            "trigger_labels": source.get("trigger_labels", []),
            "inbox_ids": inbox_ids,
            "nodes": nodes,
            "exit_labels": source.get("exit_labels", []),
            "stop_on_incoming": bool(source.get("stop_on_incoming", True)),
            "frequency_hours": int(source.get("frequency_hours", 24)),
            "route_variant": route_id,
            "test_conversation_ids": [],
        }
        sop = db.scalar(select(SopDefinition).where(
            SopDefinition.tenant_id == tenant.id,
            SopDefinition.name == source["name"],
        ))
        if sop is None:
            sop = SopDefinition(
                tenant_id=tenant.id,
                name=source["name"],
                version=1,
                created_by=user.id,
                **values,
            )
            db.add(sop)
            db.flush()
            sop_snapshot(db, sop, user.id)
        else:
            changed = any(getattr(sop, key) != value for key, value in values.items())
            if changed:
                for key, value in values.items():
                    setattr(sop, key, value)
                sop.version += 1
                sop.updated_at = utcnow()
                sop_snapshot(db, sop, user.id)
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
        "sop_nodes": len(package["sop"]["nodes"]),
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
    confirmed_sop_deletions = set(payload.delete_sop_group_keys)
    if (len(confirmed_sop_deletions) != len(payload.delete_sop_group_keys)
            or not confirmed_sop_deletions <= removed):
        fail("route_content_sop_deletion_invalid", 422)
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

    def operator_node(key, group):
        text = {"key": "text", "content_type": "text", "content": group["approved_text"]}
        assets = [{"key": f"image_{index}", "content_type": "image", "content": "", "asset_key": asset}
                  for index, asset in enumerate(group.get("asset_keys", []), 1)]
        mode = group.get("delivery_mode", "text_only")
        messages = ([text] if mode == "text_only" else assets if mode == "assets_only"
                    else [text, *assets] if mode == "text_then_assets" else [*assets, text])
        return {
            "key": f"operator_content_{key}", "content_group_key": key,
            "operator_managed": True, "schedule_type": "relative",
            "basis": "previous_node", "delay_minutes": 0, "messages": messages,
        }

    # Native same-key source nodes require explicit, version-bound consent.
    # Other SOP references remain protected; exact owned derivatives are rebuilt.
    source_nodes = []
    for node in package["sop"]["nodes"]:
        key = node.get("content_group_key")
        if key in confirmed_sop_deletions and node.get("key") == key:
            continue
        old_group = current["content_groups"].get(key, {})
        if old_group.get("operator_managed") and node == operator_node(key, old_group):
            continue
        source_nodes.append(node)
    referenced_groups = {node.get("content_group_key") for node in source_nodes}
    for key in content_sequence:
        group = package["content_groups"][key]
        if group.get("operator_managed") and key not in referenced_groups:
            source_nodes.append(operator_node(key, group))
    package["sop"]["nodes"] = source_nodes
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
            if field not in {"runtime_sop", "source_path", "fixed_answers"}:
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


class MaterialApproval(BaseModel):
    expected_hash: str = Field(min_length=64, max_length=64)
    review_notes: str = Field(min_length=10, max_length=2000)


@router.post('/automation/route-products/{route_variant}/assets/{asset_key}/approve')
def approve_route_asset(route_variant: str, asset_key: str, payload: MaterialApproval,
                        user: User = Depends(manager_write), db: Session = Depends(get_db)):
    import hashlib
    _, asset = _route_asset(db, route_variant, asset_key)
    path = Path(asset.source_path)
    if not asset.available or not path.is_file():
        fail('material_unavailable', 422)
    if payload.expected_hash != asset.file_hash or hashlib.sha256(path.read_bytes()).hexdigest() != payload.expected_hash:
        fail('material_changed_before_approval', 409)
    before = deepcopy(asset.metadata_json or {})
    approved = {**before, 'review_state':'evaluation_ready', 'live_approved':True,
        'reviewed_by':user.id, 'reviewed_at':utcnow(), 'review_notes':payload.review_notes,
        'approved_file_hash':payload.expected_hash}
    statement = update(MaterialAsset).where(MaterialAsset.id == asset.id,
        MaterialAsset.file_hash == payload.expected_hash, MaterialAsset.metadata_json == before,
        MaterialAsset.source_path == str(path), MaterialAsset.available.is_(True)
    ).values(metadata_json=approved).execution_options(synchronize_session=False)
    with db.no_autoflush:
        if db.execute(statement).rowcount != 1:
            fail('material_changed_before_approval', 409)
    db.refresh(asset)
    audit(db,user,'route_asset.approved','asset',asset.id,{'hash':payload.expected_hash,'review_notes':payload.review_notes})
    db.commit()
    return {'key':asset.asset_key,'approved':True,'outbound':False}


def _simulation_customer_message(payload: RouteProductSimulationInput) -> RouteSimulationMessage:
    for message in reversed(payload.messages):
        if message.role == "customer":
            return message
    fail("customer_message_required", 422)


def _simulation_history(payload: RouteProductSimulationInput) -> list[dict]:
    rows = payload.messages[:-1] if payload.messages[-1].role == "customer" else payload.messages
    return [
        {
            "direction": "incoming" if item.role == "customer" else "outgoing",
            "content": item.content,
            "content_type": "text",
            "private": False,
        }
        for item in rows
    ]


def _simulation_attachments(payload: RouteProductSimulationInput) -> list[dict]:
    if payload.scenario == "attachment_question":
        return [{"file_type": "image", "extension": "jpg", "content_type": "image"}]
    return []


def _next_sop_preview(decision, progress: dict) -> dict:
    route = progress.get("route_variant") or decision.route_variant
    if decision.action != "reply" or route not in ROUTE_PACKAGES:
        return {"will_enroll": False, "next_node": None, "after_minutes": None}
    nodes = ROUTE_PACKAGES[route]["runtime_sop"]["nodes"]
    if not nodes:
        return {"will_enroll": False, "next_node": None, "after_minutes": None}
    covered = set(progress.get("sent_content_groups") or []) | set(
        decision.covered_content_groups or []
    )
    node = next(
        (
            item for item in nodes
            if not item.get("content_group_key")
            or item.get("content_group_key") not in covered
        ),
        None,
    )
    if not node:
        return {"will_enroll": False, "next_node": None, "after_minutes": None}
    return {
        "will_enroll": True,
        "next_node": node.get("journey_trigger") or node.get("key"),
        "after_minutes": node.get("delay_minutes"),
    }


def _blocked_reason(decision) -> str | None:
    if decision.action == "handoff":
        return decision.handoff_reason or "handoff"
    if decision.action == "no_action":
        return "model_no_action"
    return None


@router.post("/automation/route-products/{route_variant}/simulate")
def simulate_route_product(
    route_variant: str,
    payload: RouteProductSimulationInput,
    user: User = Depends(manager_write),
    db: Session = Depends(get_db),
):
    """Run the production model contract against a route scenario without writing Chatwoot."""
    if route_variant not in ROUTE_PACKAGES:
        fail("route_product_not_found", 404)
    customer = _simulation_customer_message(payload)
    tenant = db.scalar(select(Tenant).order_by(Tenant.id))
    current_slots: dict = {}
    sent_groups: list[str] = []
    context = {
        "module": "reply",
        "customer_text": customer.content,
        "context_messages": _simulation_history(payload),
        "context_complete": True,
        "route_variant": route_variant,
        "memory": current_slots,
        "journey": journey_context_from_values(route_variant, "route_selection", current_slots, sent_groups),
        "route_playbook": playbook_prompt(),
        "current_attachments": _simulation_attachments(payload),
        "lead_capture": {
            "status": "not_started",
            "request_count": 0,
            "captured_kinds": [],
        },
        "available_materials": candidate_materials(db, tenant.id if tenant else None),
        "reception_policy": effective_reception_policy(db),
    }
    context = enrich_context_with_web_knowledge(
        db, tenant.id if tenant else None, context, environment="playground"
    )
    try:
        decision, logs, digest, trace = generate_decision(context)
    except EvaluationCallError as exc:
        raise HTTPException(503, detail={
            "code": exc.code,
            "message": exc.code,
            "outbound": False,
            "request_hash": exc.digest,
            "attempts": len(exc.logs),
        }) from exc
    except ValueError as exc:
        raise HTTPException(503, detail={
            "code": str(exc),
            "message": str(exc),
            "outbound": False,
        }) from exc

    decision, progress = prepare_route_reply_values(
        decision,
        current_route=route_variant,
        stage="route_selection",
        slots=current_slots,
        sent_groups=sent_groups,
    )
    return {
        "action": decision.action,
        "reply": safe_text(decision.reply),
        "route_variant": decision.route_variant,
        "slots": safe_text(decision.slots),
        "missing_slots": safe_text(decision.missing_slots),
        "lead_action": decision.lead_action,
        "journey_stage": decision.journey_stage,
        "touch_goal": decision.touch_goal,
        "touch_reason": safe_text(decision.touch_reason),
        "profile_updates": safe_text(decision.profile_updates),
        "handoff_reason": safe_text(decision.handoff_reason),
        "covered_content_groups": decision.covered_content_groups,
        "content_group_key": decision.content_group_key,
        "material_keys": decision.material_keys,
        "reply_options": decision.reply_options,
        "safety_flags": decision.safety_flags,
        "next_sop_preview": _next_sop_preview(decision, progress),
        "blocked_reason": _blocked_reason(decision),
        "scenario": payload.scenario,
        "prompt_version": trace.get("prompt_version", REALTIME_REPLY_PROMPT_VERSION),
        "validator_version": VALIDATOR_VERSION,
        "model": settings.deepseek_model,
        "trace": trace,
        "request_hash": digest,
        "attempts": len(logs),
        "outbound": False,
    }


def build_session(payload:SessionCreate,user:User,db:Session):
    inbox_id=payload.inbox_binding_id
    history=[]
    conversation=None
    controls={"can_reply":True,"ai_enabled":True,"channel":"facebook","labels":[],"human":False,"permission_source":"simulated", "history_complete": True, "history_source": "isolated_session"}
    if payload.route_variant:
        controls["route_variant"] = payload.route_variant
    clock=iso(dt(payload.virtual_now)) if payload.virtual_now else utcnow()
    if payload.conversation_id:
        conversation=db.get(ConversationState,payload.conversation_id)
        if not conversation:fail("conversation_not_found",404)
        inbox_id=conversation.inbox_binding_id
        scope(db,user,inbox_id)
        history_query=select(MessageEvent).join(ConversationState).where(ConversationState.tenant_id==conversation.tenant_id,ConversationState.inbox_binding_id==inbox_id,MessageEvent.private.is_(False),MessageEvent.direction.in_(["incoming","outgoing"]),MessageEvent.created_at<=clock)
        history_query=history_query.where(ConversationState.contact_id==conversation.contact_id) if conversation.contact_id else history_query.where(ConversationState.id==conversation.id)
        rows=db.scalars(history_query.order_by(MessageEvent.created_at,MessageEvent.id)).all()
        history=[{"id":x.chatwoot_message_id,"direction":x.direction,"content":x.content,"content_type":x.content_type,"status":x.status,"created_at":x.created_at} for x in rows]
        controls.update({"can_reply":conversation.can_reply,"labels":conversation.labels,"channel":conversation.inbox.channel_type,"permission_source":"current_mirror_simulated_past", "history_complete": False, "history_source": "all_available_local_mirror"})
        first_query = select(func.min(MessageEvent.created_at)).join(ConversationState).where(
            ConversationState.inbox_binding_id == inbox_id, MessageEvent.direction == "incoming",
            MessageEvent.private.is_(False), MessageEvent.created_at <= clock)
        first_query = first_query.where(ConversationState.contact_id == conversation.contact_id) if conversation.contact_id else first_query.where(ConversationState.id == conversation.id)
        controls.update({"customer_added_at": db.scalar(first_query), "customer_added_source": "first_public_customer_message_in_inbox"})
    scope(db,user,inbox_id)
    if inbox_id and not conversation:
        controls["channel"]=db.get(InboxBinding,inbox_id).channel_type
    if payload.wakeup_policy_id:
        policy=db.get(WakeupPolicy,payload.wakeup_policy_id)
        if not policy:fail("policy_not_found",404)
        wake_scope(db,user,policy.config.get("inbox_ids",[]))
        if policy.config.get("inbox_ids") and inbox_id not in policy.config["inbox_ids"]:fail("inbox_forbidden",403)
        controls["wakeup_policy_id"]=policy.id
    release_id = V2_ENGINE_RELEASE_ID if payload.engine_version == "v2" else "v1"
    row=AutomationSession(owner_id=user.id,inbox_binding_id=inbox_id,conversation_state_id=conversation.id if conversation else None,mode=payload.mode,engine_version=payload.engine_version,engine_release_id=release_id,virtual_now=clock,controls=controls,messages=history)
    db.add(row)
    db.flush()
    return row


@router.post("/playground/sessions",status_code=201)
def create_session(payload:SessionCreate,user:User=Depends(manager_write),db:Session=Depends(get_db)):
    row=build_session(payload,user,db)
    if payload.mode == "journey":
        # A rehearsal represents wall-clock customer behaviour.  Keep this
        # authoritative on the server so an old cached UI cannot silently turn
        # one minute into a few milliseconds by posting a legacy multiplier.
        journey_speed_multiplier = 1
        if payload.route_variant:
            candidates = journey_versions(db, user, row.inbox_binding_id, payload.route_variant)
            if payload.sop_version_id:
                selected = next((version for version, _ in candidates if version.id == payload.sop_version_id), None)
                if not selected:
                    fail("journey_sop_version_invalid", 422)
            else:
                selected = candidates[0][0] if candidates else None
            if not selected:
                fail("journey_sop_missing", 422)
            entry = payload.entry_message.strip() or ROUTES[payload.route_variant]["default_entry_message"]
            try:
                start_journey(
                    db,
                    row,
                    selected,
                    duration_minutes=payload.duration_minutes,
                    speed_multiplier=journey_speed_multiplier,
                    entry_message=entry,
                )
            except ValueError as exc:
                fail(str(exc), 422)
        else:
            try:
                start_open_journey(
                    db,
                    row,
                    duration_minutes=payload.duration_minutes,
                    speed_multiplier=journey_speed_multiplier,
                    entry_message=payload.entry_message.strip() or "你好，我想咨询旅行行程",
                )
            except ValueError as exc:
                fail(str(exc), 422)
    db.commit()
    return session_json(db,row)


@router.get("/playground/sessions")
def sessions(user:User=Depends(manager),db:Session=Depends(get_db)):
    q=select(AutomationSession).where(AutomationSession.owner_id==user.id, AutomationSession.environment == "playground")
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
    key = subject_key(db, row)
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
    if row.environment == "shadow":fail("shadow_session_read_only",403)
    if row.mode == "journey" and simulation_state(row).get("status") != "running":
        fail("journey_not_running")
    if not payload.content.strip() and payload.content_type=="text":fail("text_required",422)
    add_customer_message(db,row,payload.content,payload.client_key,payload.content_type)
    db.commit()
    return session_json(db,row)


@router.post("/playground/sessions/{session_id}/control/{action}")
def journey_action(session_id:int,action:Literal["pause","resume","stop"],user:User=Depends(manager_write),db:Session=Depends(get_db)):
    row=own_session(db,user,session_id)
    if row.environment != "playground" or row.mode != "journey":
        fail("journey_session_required", 422)
    try:
        change_journey_status(db, row, action)
    except ValueError as exc:
        fail(str(exc))
    db.commit()
    return session_json(db,row)


class AdvanceInput(BaseModel):
    minutes:float=Field(default=0,ge=0,le=10080)
    generation:int
    labels:list[str]|None=Field(default=None,max_length=50)
    human:bool|None=None
    can_reply:bool|None=None
    ai_enabled:bool|None=None


@router.post("/playground/sessions/{session_id}/advance")
def advance(session_id:int,payload:AdvanceInput,user:User=Depends(manager_write),db:Session=Depends(get_db)):
    row=own_session(db,user,session_id)
    if row.environment == "shadow":fail("shadow_session_read_only",403)
    if row.mode == "journey":fail("journey_uses_automatic_clock",422)
    if row.generation!=payload.generation:fail("version_conflict")
    changes=payload.model_dump(exclude={"minutes","generation"},exclude_none=True)
    if not db.execute(update(AutomationSession).where(AutomationSession.id==row.id,AutomationSession.generation==payload.generation,AutomationSession.virtual_now==row.virtual_now).values(virtual_now=iso(dt(row.virtual_now)+timedelta(minutes=payload.minutes)))).rowcount:fail("version_conflict")
    added_labels = apply_controls(db, row, changes)
    trigger_sops(db, row, added_labels=added_labels)
    advance_sops(db,row)
    cycle=create_cycle(db,row)
    if cycle:queue_wakeup(db,row,cycle)
    db.commit()
    return session_json(db,row)


@router.post("/playground/sessions/{session_id}/advance-next")
def advance_journey_to_next_touch(session_id:int,user:User=Depends(manager_write),db:Session=Depends(get_db)):
    """Fast-forward an isolated journey to its next scheduled silence node."""
    row=own_session(db,user,session_id)
    if row.environment != "playground" or row.mode != "journey":
        fail("journey_playground_required",422)
    state=simulation_state(row)
    if state.get("status") != "running":
        fail("journey_not_running",422)
    active_run=db.scalar(select(AutomationRun.id).where(
        AutomationRun.session_id==row.id,
        AutomationRun.status.in_(["pending","processing"]),
    ))
    if row.due_at or active_run:
        fail("journey_busy",409)
    next_due=db.scalar(select(RehearsalJob.scheduled_at).join(
        RehearsalEnrollment,RehearsalJob.enrollment_id==RehearsalEnrollment.id,
    ).where(
        RehearsalEnrollment.session_id==row.id,
        RehearsalEnrollment.status=="active",
        RehearsalJob.status=="scheduled",
        RehearsalJob.scheduled_at.is_not(None),
    ).order_by(RehearsalJob.scheduled_at,RehearsalJob.id).limit(1))
    if not next_due:
        fail("journey_next_touch_missing",422)
    row.virtual_now=next_due
    state["last_wall_at"]=utcnow()
    set_simulation_state(row,state)
    advance_sops(db,row)
    db.commit()
    return session_json(db,row)


@router.post("/playground/sessions/{session_id}/reset")
def reset(session_id:int,user:User=Depends(manager_write),db:Session=Depends(get_db)):
    row=own_session(db,user,session_id)
    if row.environment == "shadow":fail("shadow_session_read_only",403)
    cancel_generation(db,row,"session_reset")
    row.generation+=1
    row.messages,row.memory,row.due_at,row.batch_started_at=[],{},None,None
    row.controls = {k: v for k, v in row.controls.items() if k not in ("customer_added_at", "customer_added_source")}
    db.commit()
    return session_json(db,row)


class ConfirmInput(BaseModel):
    message_id:str


@router.post("/playground/sessions/{session_id}/confirm")
def confirm(session_id:int,payload:ConfirmInput,user:User=Depends(manager_write),db:Session=Depends(get_db)):
    row=own_session(db,user,session_id)
    if row.environment == "shadow":fail("shadow_session_read_only",403)
    try:confirm_draft(db,row,payload.message_id)
    except ValueError as exc:fail(str(exc))
    db.commit()
    return session_json(db,row)


class EnrollInput(BaseModel):
    version_id:int
    reenroll:bool=False
    request_key:str|None=Field(default=None,min_length=1,max_length=80)


@router.post("/playground/sessions/{session_id}/sop")
def enroll(session_id:int,payload:EnrollInput,user:User=Depends(manager_write),db:Session=Depends(get_db)):
    row=own_session(db,user,session_id)
    if row.environment == "shadow":fail("shadow_session_read_only",403)
    version=db.get(SopVersion,payload.version_id)
    if not version:fail("version_not_found",404)
    published_scope(db, user, version.config)
    if db.get(SopDefinition,version.sop_id).status != "running":fail("sop_not_running")
    if version.config.get("inbox_ids") and (not row.inbox_binding_id or db.get(InboxBinding,row.inbox_binding_id).chatwoot_inbox_id not in version.config["inbox_ids"]):fail("sop_inbox_mismatch",422)
    try:
        enroll_rehearsal(db,row,version,reenroll=payload.reenroll,request_key=payload.request_key)
    except ValueError as exc:
        fail(str(exc))
    advance_sops(db,row)
    db.commit()
    return session_json(db,row)


def sop_scope(db,user,sop):
    if not sop:fail("sop_not_found",404)
    published_scope(db,user,{"inbox_ids":sop.inbox_ids})


def published_scope(db,user,config):
    allowed=allowed_inbox_ids(db,user)
    if allowed is not None:
        remote=set(db.scalars(select(InboxBinding.chatwoot_inbox_id).where(InboxBinding.id.in_(allowed))).all())
        if not config.get("inbox_ids") or not set(config["inbox_ids"]).issubset(remote):fail("inbox_forbidden",403)


@router.get("/sops/{sop_id}/versions")
def versions(sop_id:int,user:User=Depends(manager),db:Session=Depends(get_db)):
    sop_scope(db,user,db.get(SopDefinition,sop_id))
    items=[]
    for x in db.scalars(select(SopVersion).where(SopVersion.sop_id==sop_id).order_by(SopVersion.version.desc())).all():
        try:published_scope(db,user,x.config)
        except HTTPException:continue
        items.append({"id":x.id,"version":x.version,"config":x.config,"created_at":x.created_at})
    return {"items":items}


@router.get("/sops/{sop_id}/preview")
def preview(sop_id:int,user:User=Depends(manager),db:Session=Depends(get_db)):
    sop=db.get(SopDefinition,sop_id)
    sop_scope(db,user,sop)
    version=db.scalar(select(SopVersion).where(SopVersion.sop_id==sop_id).order_by(SopVersion.version.desc()))
    config=version.config if version else {"inbox_ids":sop.inbox_ids,"trigger_type":sop.trigger_type,"trigger_labels":sop.trigger_labels}
    published_scope(db,user,config)
    q=select(ConversationState)
    allowed=allowed_inbox_ids(db,user)
    if allowed is not None:q=q.where(ConversationState.inbox_binding_id.in_(allowed))
    items=[]
    for x in db.scalars(q).all():
        if config.get("inbox_ids") and x.inbox.chatwoot_inbox_id not in config["inbox_ids"]:continue
        if config.get("test_conversation_ids") and x.chatwoot_conversation_id not in config["test_conversation_ids"]:continue
        if config.get("trigger_type") in ("label","stage") and not set(config.get("trigger_labels",[]))&set(x.labels):continue
        items.append({"id":x.id,"conversation_id":x.chatwoot_conversation_id,"inbox_id":x.inbox.chatwoot_inbox_id,"labels":x.labels,"can_reply":x.can_reply,"delivery_eligible":"requires_fresh_preflight"})
    return {"items":items,"total":len(items),"version":version.version if version else None,"outbound":False}


@router.post("/sops/{sop_id}/resume")
def resume_sop(sop_id:int,user:User=Depends(require_super_admin_csrf),db:Session=Depends(get_db)):
    row=db.get(SopDefinition,sop_id)
    if not row or not db.scalar(select(SopVersion.id).where(SopVersion.sop_id==sop_id)):fail("published_version_required")
    row.status="running"
    audit(db,user,"sop.resume","sop",sop_id)
    db.commit()
    return {"id":sop_id,"status":row.status}


class WakeConfig(BaseModel):
    name:str=Field(min_length=1,max_length=200)
    version:int=Field(default=1,ge=1)
    inbox_ids:list[int]=Field(default_factory=list,max_length=50)
    threshold_minutes:int=Field(default=120,ge=1,le=1380)
    frequency_hours:int=Field(default=24,ge=24,le=720)


def wake_json(row):return {"id":row.id,"name":row.name,"version":row.version,"status":row.status,**row.config}


def wake_scope(db,user,ids):
    if not ids and not is_admin(user):fail("inbox_required",403)
    for i in ids:scope(db,user,i)


@router.get("/wakeup/policies")
def policies(user:User=Depends(manager),db:Session=Depends(get_db)):
    allowed=allowed_inbox_ids(db,user)
    return {"items":[wake_json(x) for x in db.scalars(select(WakeupPolicy).order_by(WakeupPolicy.id.desc())).all() if allowed is None or (x.config.get("inbox_ids") and set(x.config["inbox_ids"]).issubset(allowed))]}


@router.post("/wakeup/policies",status_code=201)
def new_policy(payload:WakeConfig,user:User=Depends(manager_write),db:Session=Depends(get_db)):
    wake_scope(db,user,payload.inbox_ids)
    row=WakeupPolicy(name=payload.name,created_by=user.id,config={**DEFAULT_WAKEUP,**payload.model_dump(exclude={"name","version"})})
    db.add(row)
    db.flush()
    audit(db,user,"wakeup.create","policy",row.id)
    db.commit()
    return wake_json(row)


@router.patch("/wakeup/policies/{policy_id}")
def edit_policy(policy_id:int,payload:WakeConfig,user:User=Depends(manager_write),db:Session=Depends(get_db)):
    row=db.get(WakeupPolicy,policy_id)
    if not row:fail("policy_not_found",404)
    wake_scope(db,user,row.config.get("inbox_ids",[]))
    wake_scope(db,user,payload.inbox_ids)
    if not db.execute(update(WakeupPolicy).where(WakeupPolicy.id==policy_id,WakeupPolicy.version==payload.version).values(name=payload.name,version=payload.version+1,status="draft",config={**DEFAULT_WAKEUP,**payload.model_dump(exclude={"name","version"})})).rowcount:fail("version_conflict")
    audit(db,user,"wakeup.update","policy",policy_id)
    db.commit()
    return wake_json(row)


@router.post("/wakeup/policies/{policy_id}/{action}")
def policy_action(policy_id:int,action:Literal["publish","pause","resume"],user:User=Depends(require_super_admin_csrf),db:Session=Depends(get_db)):
    row=db.get(WakeupPolicy,policy_id)
    if not row:fail("policy_not_found",404)
    row.status="paused" if action=="pause" else "running"
    row.updated_at=utcnow()
    audit(db,user,"wakeup."+action,"policy",policy_id)
    db.commit()
    return wake_json(row)


def cycle_json(x):return {"id":x.id,"session_id":x.session_id,"generation":x.generation,"policy_id":x.policy_id,"customer_at":x.customer_at,"reply_at":x.reply_at,"due_at":x.due_at,"expires_at":x.expires_at,"status":x.status,"reason":x.reason,"run_id":x.run_id,"evaluation_count":x.evaluation_count}


@router.get("/wakeup/cycles")
def cycles(user:User=Depends(manager),db:Session=Depends(get_db)):
    q=select(SilenceCycle).join(AutomationSession).where(AutomationSession.owner_id==user.id)
    allowed=allowed_inbox_ids(db,user)
    if allowed is not None:q=q.where(AutomationSession.inbox_binding_id.in_(allowed))
    return {"items":[cycle_json(x) for x in db.scalars(q.order_by(SilenceCycle.id.desc()).limit(200)).all()]}


@router.get("/wakeup/candidates")
def historical_candidates(policy_id:int|None=None,user:User=Depends(manager),db:Session=Depends(get_db)):
    from app.automation_service import confirmed
    policy=db.get(WakeupPolicy,policy_id) if policy_id else None
    if policy_id and not policy:fail("policy_not_found",404)
    if policy:wake_scope(db,user,policy.config.get("inbox_ids",[]))
    config={**DEFAULT_WAKEUP,**(policy.config if policy else {})}
    allowed=allowed_inbox_ids(db,user)
    q=select(ConversationState)
    if allowed is not None:q=q.where(ConversationState.inbox_binding_id.in_(allowed))
    if config.get("inbox_ids"):q=q.where(ConversationState.inbox_binding_id.in_(config["inbox_ids"]))
    items=[]
    now=utcnow()
    for conv in db.scalars(q).all():
        rows=db.scalars(select(MessageEvent).where(MessageEvent.conversation_state_id==conv.id,MessageEvent.private.is_(False),MessageEvent.direction.in_(["incoming","outgoing"])).order_by(MessageEvent.created_at.desc(),MessageEvent.id.desc())).all()
        messages=[{"direction":x.direction,"content":x.content,"status":x.status,"created_at":x.created_at} for x in reversed(rows)]
        incoming=[x for x in messages if x["direction"]=="incoming"]
        if not incoming:continue
        last=incoming[-1]
        replies=[x for x in messages if confirmed(x) and dt(x["created_at"])>=dt(last["created_at"])]
        if not replies:continue
        due=iso(dt(replies[-1]["created_at"])+timedelta(minutes=config["threshold_minutes"]))
        shadow=AutomationSession(messages=messages,controls={"can_reply":conv.can_reply,"channel":conv.inbox.channel_type,"labels":conv.labels,"human":conv.effective_ai_state=="HUMAN_HANDOFF","ai_enabled":conv.ai_mode!="off"},virtual_now=now)
        reason=gate(shadow,now,True)
        if dt(now)<dt(due):reason=reason or "threshold_not_reached"
        items.append({"conversation_id":conv.id,"chatwoot_conversation_id":conv.chatwoot_conversation_id,"inbox_binding_id":conv.inbox_binding_id,"last_customer_at":last["created_at"],"last_reply_at":replies[-1]["created_at"],"due_at":due,"status":"blocked" if reason else "candidate","reason":reason,"permission_source":"current_mirror_requires_fresh_preflight"})
    return {"items":items,"total":len(items),"outbound":False}


@router.get("/wakeup/executions")
def wake_executions(user:User=Depends(manager),db:Session=Depends(get_db)):
    return runs("wakeup",1,user,db)


class SimulateInput(BaseModel):
    session_id:int
    policy_id:int|None=None


@router.post("/wakeup/simulate")
def simulate(payload:SimulateInput,user:User=Depends(manager_write),db:Session=Depends(get_db)):
    session=own_session(db,user,payload.session_id)
    policy=db.get(WakeupPolicy,payload.policy_id) if payload.policy_id else None
    if payload.policy_id and not policy:fail("policy_not_found",404)
    if policy:
        wake_scope(db,user,policy.config.get("inbox_ids",[]))
        if policy.config.get("inbox_ids") and session.inbox_binding_id not in policy.config["inbox_ids"]:fail("inbox_forbidden",403)
    cycle=create_cycle(db,session,policy)
    if not cycle:fail("confirmed_reply_required",422)
    queue_wakeup(db,session,cycle)
    db.commit()
    return cycle_json(cycle)


@router.post("/wakeup/cycles/{cycle_id}/{action}")
def cycle_action(cycle_id:int,action:Literal["exclude","confirm"],user:User=Depends(manager_write),db:Session=Depends(get_db)):
    cycle=db.get(SilenceCycle,cycle_id)
    if not cycle:fail("cycle_not_found",404)
    session=own_session(db,user,cycle.session_id)
    if action=="exclude":
        cycle.status,cycle.reason="excluded","operator_excluded"
    else:
        reason=gate(session,session.virtual_now,True)
        if reason:fail(reason)
        if cycle.policy_id:
            policy=db.get(WakeupPolicy,cycle.policy_id)
            if not policy or policy.status!="running" or policy.version!=cycle.policy_snapshot.get("version"):fail("wakeup_policy_changed")
        if cycle.status!="draft" or cycle.generation!=session.generation:fail("cycle_not_sendable")
        if not reserve_touch(db,subject_key(db, session),f"wakeup:{cycle.id}",session.virtual_now,cycle.policy_snapshot.get("frequency_hours",24)):fail("contact_frequency_limit")
        cycle.status="simulated_delivered"
    db.commit()
    return cycle_json(cycle)


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
        ready=asset.available and meta.get("review_state")=="evaluation_ready" and Path(asset.source_path).is_file()
        if route_variant and route_variant not in meta.get("route_variants",[]):continue
        items.append({"id":asset.id,"key":asset.asset_key,"name":asset.display_name,"topic":asset.usage,
            "media_id":meta.get("stored_media_id"),"media_hash":asset.file_hash,"content_type":asset.media_type,
            "content_family":meta.get("content_family"),"route_variants":meta.get("route_variants",[]),
            "ready":ready,"review_state":meta.get("review_state"),"notes":meta.get("review_notes","")})
    return {"items":items,"routes":ROUTES,"outbound":False}
