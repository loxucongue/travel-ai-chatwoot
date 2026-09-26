from __future__ import annotations

import time
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy import update
from sqlalchemy.orm import Session

from app.auth import current_user, require_csrf, require_super_admin_csrf, super_admin
from app.business_knowledge import BRANCHES, seed_business_knowledge
from app.config import settings
from app.customer_journey_dataset import (
    DEFAULT_DATASET_VERSION,
    MAX_RUN_CASES,
    dataset_summary,
    run_dataset_cases,
    selected_cases,
)
from app.db import get_db
from app.evaluation_dataset import build_dataset
from app.deepseek_evaluation import EvaluationCallError
from app.evaluation_service import create_run
from app.models import EvaluationCase, EvaluationDataset, EvaluationResult, EvaluationRun, MaterialAsset, RouteBranch, SyncJob, Tenant, User, utcnow


from app.automation_api import safe_text

router = APIRouter(prefix="/v1")


class DatasetCreate(BaseModel):
    name: str = Field(default="林芝桃花真实会话评测", min_length=1, max_length=200)
    inbox_id: int = 128859
    module: Literal["reply", "sop", "wakeup"] = "reply"
    all_topics: bool = True


class RunCreate(BaseModel):
    dataset_id: int


class ReviewUpdate(BaseModel):
    branch_correct: bool | None = None
    answered_question: bool | None = None
    facts_supported: bool | None = None
    tone_appropriate: bool | None = None
    should_handoff: bool | None = None
    unsafe_or_privacy_issue: bool | None = None
    notes: str = Field(default="", max_length=4000)


class PlaygroundMessage(BaseModel):
    role: Literal["customer", "assistant"]
    content: str = Field(min_length=1, max_length=4000)


class PlaygroundReplyRequest(BaseModel):
    customer_message: str = Field(min_length=1, max_length=4000)
    messages: list[PlaygroundMessage] = Field(default_factory=list, max_length=2000)
    scenario: Literal[
        "auto", "peach_11d", "peach_9d", "private_group",
        "other_peak", "other_no_peak", "other_destination",
    ] = "auto"
    engine_version: Literal["v1", "v2"] = Field(default_factory=lambda: settings.ai_engine_default)


class PlaygroundCompareRequest(BaseModel):
    customer_message: str = Field(min_length=1, max_length=4000)
    messages: list[PlaygroundMessage] = Field(default_factory=list, max_length=2000)
    scenario: Literal[
        "auto", "peach_11d", "peach_9d", "private_group",
        "other_peak", "other_no_peak", "other_destination",
    ] = "auto"


class CustomerJourneyDatasetRunRequest(BaseModel):
    dataset_version: str = DEFAULT_DATASET_VERSION
    case_ids: list[str] = Field(default_factory=list, max_length=MAX_RUN_CASES)
    suites: list[Literal["answer", "journey", "shadow"]] = Field(default_factory=list, max_length=3)
    outbound: bool = False


def playground_user(user: User = Depends(current_user)) -> User:
    if user.role not in ("super_admin", "admin", "supervisor"):
        raise HTTPException(403, detail={"code": "permission_denied", "message": "权限不足"})
    return user


def playground_csrf(user: User = Depends(require_csrf)) -> User:
    if user.role not in ("super_admin", "admin", "supervisor"):
        raise HTTPException(403, detail={"code": "permission_denied", "message": "权限不足"})
    return user


@router.get("/evaluation/customer-journey-dataset")
def customer_journey_dataset(_user: User = Depends(playground_user)):
    try:
        return dataset_summary(DEFAULT_DATASET_VERSION)
    except FileNotFoundError as exc:
        raise HTTPException(404, detail={"code": str(exc), "message": "真实聊天测试数据集尚未生成"}) from exc


@router.post("/evaluation/customer-journey-dataset/run")
def run_customer_journey_dataset(
    payload: CustomerJourneyDatasetRunRequest,
    _user: User = Depends(playground_csrf),
    db: Session = Depends(get_db),
):
    if payload.outbound:
        raise HTTPException(422, detail={"code": "dataset_run_must_be_read_only", "message": "测试集运行必须保持 outbound=false"})
    try:
        cases = selected_cases(
            payload.dataset_version,
            payload.case_ids or None,
            list(payload.suites) or None,
        )
    except FileNotFoundError as exc:
        raise HTTPException(404, detail={"code": str(exc), "message": "真实聊天测试数据集尚未生成"}) from exc
    if not cases:
        raise HTTPException(422, detail={"code": "dataset_cases_empty", "message": "没有匹配的测试用例"})
    if len(cases) > MAX_RUN_CASES:
        cases = cases[:MAX_RUN_CASES]
    return {
        "dataset_version": payload.dataset_version,
        "case_limit": MAX_RUN_CASES,
        **run_dataset_cases(db, cases),
    }


def _page(page: int, page_size: int) -> tuple[int, int]:
    size = max(1, min(page_size, 100))
    return max(0, page - 1) * size, size


def _dataset(row: EvaluationDataset) -> dict:
    return {"id": row.id, "name": row.name, "status": row.status, "case_count": row.case_count, "filter_version": row.filter_version, "filter_config": row.filter_config, "snapshot_at": row.snapshot_at, "created_at": row.created_at}


def _run(row: EvaluationRun) -> dict:
    return {"id": row.id, "dataset_id": row.dataset_id, "model": row.model, "prompt_version": row.prompt_version, "status": row.status, "total_cases": row.total_cases, "completed_cases": row.completed_cases, "failed_cases": row.failed_cases, "metrics": row.metrics, "error_code": row.error_code, "created_at": row.created_at, "started_at": row.started_at, "completed_at": row.completed_at}


@router.post("/evaluation/chatwoot-sync", status_code=202)
def start_sync(user: User = Depends(require_super_admin_csrf), db: Session = Depends(get_db)):
    running = db.scalar(select(SyncJob).where(SyncJob.kind == "evaluation_history", SyncJob.status.in_(["pending", "running"])))
    if running:
        return {"id": running.id, "status": running.status, "phase": running.phase}
    tenant = db.scalar(select(Tenant))
    job = SyncJob(tenant_id=tenant.id, kind="evaluation_history", phase="backup")
    db.add(job)
    db.commit()
    return {"id": job.id, "status": job.status, "phase": job.phase}


@router.get("/evaluation/chatwoot-sync/{job_id}")
def sync_status(job_id: int, _user: User = Depends(super_admin), db: Session = Depends(get_db)):
    job = db.get(SyncJob, job_id)
    if not job or job.kind != "evaluation_history":
        raise HTTPException(404, detail={"code": "sync_job_not_found", "message": "同步任务不存在"})
    return {"id": job.id, "status": job.status, "phase": job.phase, "current_page": job.current_page, "total_items": job.total_items, "completed_items": job.completed_items, "failed_items": job.failed_items, "error_code": job.error_code, "updated_at": job.updated_at, "completed_at": job.completed_at}


@router.post("/evaluation/datasets", status_code=201)
def create_dataset(payload: DatasetCreate, user: User = Depends(require_super_admin_csrf), db: Session = Depends(get_db)):
    try:
        dataset = build_dataset(db, user, payload.name, payload.inbox_id, payload.module, payload.all_topics)
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(422, detail={"code": str(exc), "message": "无法创建评测数据集"}) from exc
    return _dataset(dataset)


@router.get("/evaluation/datasets")
def datasets(_user: User = Depends(super_admin), db: Session = Depends(get_db)):
    return [_dataset(row) for row in db.scalars(select(EvaluationDataset).order_by(EvaluationDataset.id.desc())).all()]


@router.get("/evaluation/datasets/{dataset_id}/cases")
def dataset_cases(dataset_id: int, page: int = 1, page_size: int = 50, _user: User = Depends(super_admin), db: Session = Depends(get_db)):
    dataset = db.get(EvaluationDataset, dataset_id)
    if not dataset:
        raise HTTPException(404, detail={"code": "dataset_not_found", "message": "数据集不存在"})
    offset, size = _page(page, page_size)
    rows = db.scalars(select(EvaluationCase).where(EvaluationCase.dataset_id == dataset_id).order_by(EvaluationCase.id).offset(offset).limit(size)).all()
    return {"items": [{"id": row.id, "conversation_state_id": row.conversation_state_id, "target_message_ids": row.target_message_ids, "customer_text": safe_text(row.customer_text), "context_messages": safe_text(row.context_messages), "reference_answer": safe_text(row.reference_answer), "expected_branch": row.expected_branch, "expected_handoff": row.expected_handoff} for row in rows], "total": dataset.case_count, "page": page, "page_size": size}


@router.post("/evaluation/runs", status_code=202)
def start_run(payload: RunCreate, user: User = Depends(require_super_admin_csrf), db: Session = Depends(get_db)):
    dataset = db.get(EvaluationDataset, payload.dataset_id)
    if not dataset or dataset.status != "ready":
        raise HTTPException(422, detail={"code": "dataset_not_ready", "message": "数据集尚未就绪"})
    run = create_run(db, dataset, user)
    db.commit()
    return _run(run)


@router.get("/evaluation/runs")
def runs(_user: User = Depends(super_admin), db: Session = Depends(get_db)):
    return [_run(row) for row in db.scalars(select(EvaluationRun).order_by(EvaluationRun.id.desc())).all()]


@router.get("/evaluation/runs/{run_id}")
def get_run(run_id: int, _user: User = Depends(super_admin), db: Session = Depends(get_db)):
    row = db.get(EvaluationRun, run_id)
    if not row:
        raise HTTPException(404, detail={"code": "run_not_found", "message": "评测任务不存在"})
    return _run(row)


@router.get("/evaluation/runs/{run_id}/results")
def results(run_id: int, page: int = 1, page_size: int = 50, _user: User = Depends(super_admin), db: Session = Depends(get_db)):
    run = db.get(EvaluationRun, run_id)
    if not run:
        raise HTTPException(404, detail={"code": "run_not_found", "message": "评测任务不存在"})
    offset, size = _page(page, page_size)
    rows = db.execute(select(EvaluationResult, EvaluationCase).join(EvaluationCase, EvaluationCase.id == EvaluationResult.case_id).where(EvaluationResult.run_id == run_id).order_by(EvaluationResult.id).offset(offset).limit(size)).all()
    return {"items": [{"id": result.id, "case_id": case.id, "status": result.status, "customer_text": safe_text(case.customer_text), "context_messages": safe_text(case.context_messages), "reference_answer": safe_text(case.reference_answer), "action": result.action, "branch": result.branch, "intent": result.intent, "reply": safe_text(result.reply), "slots": safe_text(result.slots), "missing_slots": result.missing_slots, "handoff_reason": safe_text(result.handoff_reason), "evidence_refs": result.evidence_refs, "safety_flags": result.safety_flags, "confidence": result.confidence, "automatic_scores": safe_text(result.automatic_scores), "review": safe_text(result.review), "error_code": result.error_code} for result, case in rows], "total": run.total_cases, "page": page, "page_size": size}


@router.get("/evaluation/runs/{run_id}/metrics")
def metrics(run_id: int, _user: User = Depends(super_admin), db: Session = Depends(get_db)):
    row = db.get(EvaluationRun, run_id)
    if not row:
        raise HTTPException(404, detail={"code": "run_not_found", "message": "评测任务不存在"})
    reviewed = db.scalar(select(func.count(EvaluationResult.id)).where(EvaluationResult.run_id == run_id, EvaluationResult.review != {})) or 0
    return {**row.metrics, "reviewed": reviewed, "total": row.total_cases}


@router.post("/evaluation/runs/{run_id}/{action}")
def control_run(run_id: int, action: Literal["pause", "resume", "retry-failed"], _user: User = Depends(require_super_admin_csrf), db: Session = Depends(get_db)):
    row = db.get(EvaluationRun, run_id)
    if not row:
        raise HTTPException(404, detail={"code":"run_not_found", "message":"评测不存在"})
    if action == "retry-failed":
        db.execute(update(EvaluationResult).where(EvaluationResult.run_id == run_id, EvaluationResult.status == "failed").values(status="pending",error_code=None,completed_at=None))
    row.status = "paused" if action == "pause" else "running"
    if action != "pause": row.completed_at = None
    db.commit()
    return _run(row)


@router.get("/evaluation/runs/{run_id}/report")
def report(run_id: int, _user: User = Depends(super_admin), db: Session = Depends(get_db)):
    run = db.get(EvaluationRun, run_id)
    if not run: raise HTTPException(404, detail={"code":"run_not_found", "message":"评测不存在"})
    dataset = db.get(EvaluationDataset,run.dataset_id)
    return {"run":_run(run),"snapshot":_dataset(dataset),"limitations":["事实与业务效果待人工复核", "历史权限状态为模拟", "未进行真实客户发送", "原始旅游图片待补"]}


@router.post("/evaluation/cases/{case_id}/retry", status_code=202)
def retry_case(case_id: int, user: User = Depends(require_super_admin_csrf), db: Session = Depends(get_db)):
    result = db.scalar(select(EvaluationResult).join(EvaluationRun).where(EvaluationResult.case_id == case_id).order_by(EvaluationRun.id.desc()))
    if not result:
        raise HTTPException(404, detail={"code": "evaluation_result_not_found", "message": "评测结果不存在"})
    result.status, result.error_code, result.completed_at = "pending", None, None
    run = db.get(EvaluationRun, result.run_id)
    run.status, run.completed_at = "running", None
    db.commit()
    return {"id": result.id, "status": result.status}


@router.patch("/evaluation/results/{result_id}/review")
def review_result(result_id: int, payload: ReviewUpdate, _user: User = Depends(require_super_admin_csrf), db: Session = Depends(get_db)):
    result = db.get(EvaluationResult, result_id)
    if not result:
        raise HTTPException(404, detail={"code": "evaluation_result_not_found", "message": "评测结果不存在"})
    result.review = {**payload.model_dump(), "reviewed_at": utcnow()}
    db.commit()
    return {"id": result.id, "review": safe_text(result.review)}


@router.get("/knowledge/branches")
def branches(_user: User = Depends(super_admin), db: Session = Depends(get_db)):
    version = seed_business_knowledge(db)
    db.commit()
    rows = db.scalars(select(RouteBranch).where(RouteBranch.knowledge_version_id == version.id).order_by(RouteBranch.priority)).all()
    return [{"key": row.branch_key, "name": row.name, "description": row.description, "required_slots": row.required_slots, "complete": row.complete} for row in rows]


@router.get("/knowledge/assets")
def assets(_user: User = Depends(super_admin), db: Session = Depends(get_db)):
    version = seed_business_knowledge(db)
    db.commit()
    rows = db.scalars(select(MaterialAsset).where(MaterialAsset.knowledge_version_id == version.id).order_by(MaterialAsset.asset_key)).all()
    return [{"id":row.id,"key": row.asset_key, "display_name": row.display_name, "media_type": row.media_type, "usage": row.usage, "available": row.available, "metadata": row.metadata_json} for row in rows]


@router.get("/safety/outbound-status")
def outbound_status(_user: User = Depends(super_admin), db: Session = Depends(get_db)):
    from app.reception_rollout import reception_rollout
    rollout = reception_rollout(db)
    return {"app_profile": settings.app_profile, "outbound_mode": settings.outbound_mode,
            "chatwoot_write_enabled": settings.chatwoot_write_enabled,
            "outbound_enabled": settings.outbound_enabled,
            "evaluation_inbox_id": settings.evaluation_inbox_id,
            "live_sop_enabled": settings.live_sop_enabled,
            "live_sop_scope": rollout.scope,
            "live_sop_conversation_ids": sorted(rollout.conversation_ids)}


def _playground_decision(payload: PlaygroundReplyRequest) -> dict:
    history = [item.model_dump() for item in payload.messages]
    case = {
        "customer_text": payload.customer_message,
        "context_messages": history,
    }
    started = time.monotonic()
    try:
        from app.decision_service import generate_decision
        decision, logs, _digest, trace = generate_decision({
            "module": "reply", "environment": "playground", "engine_version": payload.engine_version, **case,
        })
    except ValueError as exc:
        code = str(exc)[:120]
        raise HTTPException(503, detail={"code": code, "message": "AI 服务尚未配置"}) from exc
    except EvaluationCallError as exc:
        raise HTTPException(502, detail={"code": exc.code, "message": "AI 暂时无法完成本次演练"}) from exc

    elapsed_ms = int((time.monotonic() - started) * 1000)
    last_log = logs[-1] if logs else {}
    branch_spec = next((item for item in BRANCHES if item["key"] == decision.branch), {"name":"尚未确定线路"})
    return {
        "action": decision.action,
        "branch": decision.branch,
        "branch_name": branch_spec["name"],
        "intent": decision.intent,
        "reply": safe_text(decision.reply),
        "slots": safe_text(decision.slots),
        "missing_slots": decision.missing_slots,
        "handoff_reason": safe_text(decision.handoff_reason),
        "evidence_refs": decision.evidence_refs,
        "safety_flags": decision.safety_flags,
        "confidence": decision.confidence,
        "elapsed_ms": elapsed_ms,
        "model": settings.deepseek_model,
        "prompt_version": trace["prompt_version"],
        "engine_version": trace.get("engine_version", payload.engine_version),
        "engine_release_id": trace.get("engine_release_id"),
        "environment": trace.get("environment", "playground"),
        "knowledge_versions": trace.get("route_knowledge_versions", {}),
        "model_http_request_count": trace.get("model_http_request_count"),
        "loaded_skills": trace.get("loaded_skills", []),
        "tool_calls": trace.get("tools", []),
        "attempts": len(logs),
        "input_tokens": trace.get("model_input_tokens", last_log.get("input_tokens")),
        "output_tokens": trace.get("model_output_tokens", last_log.get("output_tokens")),
        "outbound": False,
    }


@router.post("/evaluation/playground/reply")
def playground_reply(payload: PlaygroundReplyRequest, _user: User = Depends(playground_csrf)):
    return _playground_decision(payload)


@router.post("/evaluation/playground/compare")
def playground_compare(payload: PlaygroundCompareRequest, _user: User = Depends(playground_csrf)):
    common = payload.model_dump()
    results = {
        engine: _playground_decision(PlaygroundReplyRequest(**common, engine_version=engine))
        for engine in ("v1", "v2")
    }
    return {
        "v1": results["v1"],
        "v2": results["v2"],
        "comparison": {
            "elapsed_ms_delta": results["v2"]["elapsed_ms"] - results["v1"]["elapsed_ms"],
            "attempts_delta": results["v2"]["attempts"] - results["v1"]["attempts"],
            "same_action": results["v1"]["action"] == results["v2"]["action"],
            "same_branch": results["v1"]["branch"] == results["v2"]["branch"],
        },
        "outbound": False,
    }
