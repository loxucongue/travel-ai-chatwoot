from __future__ import annotations

import hashlib
from collections import Counter
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.config import settings
from app.deepseek_evaluation import EvaluationCallError, EvaluationDecision
from app.models import EvaluationCase, EvaluationDataset, EvaluationResult, EvaluationRun, ModelCallLog, User, utcnow
from app.realtime_reply_pipeline import REALTIME_REPLY_PROMPT_VERSION
from app.silence_touch_pipeline import SILENCE_TOUCH_PROMPT_VERSION


def create_run(db: Session, dataset: EvaluationDataset, user: User) -> EvaluationRun:
    module = dataset.filter_config.get("module", "reply")
    model = "fixed-approved-content" if module == "sop" else settings.deepseek_model
    prompt_version = (
        "fixed-approved-content"
        if module == "sop"
        else SILENCE_TOUCH_PROMPT_VERSION
        if module in {"silence_touch", "wakeup"}
        else REALTIME_REPLY_PROMPT_VERSION
    )
    key = hashlib.sha256(f"{dataset.id}:{model}:{prompt_version}".encode()).hexdigest()
    existing = db.scalar(select(EvaluationRun).where(EvaluationRun.idempotency_key == key))
    if existing:
        return existing
    cases = list(db.scalars(select(EvaluationCase).where(EvaluationCase.dataset_id == dataset.id)).all())
    run = EvaluationRun(dataset_id=dataset.id, idempotency_key=key, model=model, prompt_version=prompt_version, total_cases=len(cases), created_by=user.id)
    db.add(run)
    db.flush()
    for case in cases:
        db.add(EvaluationResult(run_id=run.id, case_id=case.id))
    db.flush()
    return run


def process_next_result(db: Session, run_id: int | None = None) -> bool:
    stale_before = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
    db.execute(update(EvaluationResult).where(EvaluationResult.status == "processing", EvaluationResult.completed_at < stale_before).values(status="pending", completed_at=None))
    db.commit()
    query = select(EvaluationResult).join(EvaluationRun).where(EvaluationResult.status == "pending", EvaluationRun.status.in_(["pending", "running"]))
    if run_id is not None:
        query = query.where(EvaluationRun.id == run_id)
    result = db.scalar(query.order_by(EvaluationResult.id).limit(1))
    if not result:
        return False
    run = db.get(EvaluationRun, result.run_id)
    case = db.get(EvaluationCase, result.case_id)
    if run.status == "pending":
        run.status, run.started_at = "running", utcnow()
    claimed = db.execute(update(EvaluationResult).where(EvaluationResult.id == result.id, EvaluationResult.status == "pending").values(status="processing", completed_at=utcnow())).rowcount
    db.commit()
    if not claimed:
        return True
    dataset = db.get(EvaluationDataset, run.dataset_id)
    module = (dataset.filter_config or {}).get("module", "reply")
    payload = {"customer_text": case.customer_text, "context_messages": case.context_messages, "module": module,
               "context_complete": dataset.filter_config.get("context_complete", False)}
    if module == "reply":
        from app.material_library import candidate_materials, catalog_assets
        approved = {asset.asset_key for asset in catalog_assets(db, dataset.tenant_id) if asset.metadata_json.get("live_approved") is True}
        payload["available_materials"] = [item for item in candidate_materials(db, dataset.tenant_id) if item["key"] in approved]
        if dataset.filter_config.get("sequential_ai_context"):
            previous = db.scalar(select(EvaluationResult).join(EvaluationCase).where(
                EvaluationResult.run_id == run.id, EvaluationCase.conversation_state_id == case.conversation_state_id,
                EvaluationCase.id < case.id, EvaluationResult.status == "completed").order_by(EvaluationCase.id.desc()).limit(1))
            if previous:
                payload["route_variant"] = previous.automatic_scores.get("route_variant", "")
                quotes = previous.automatic_scores.get("slot_evidence", {})
                payload["memory"] = {key: {"value": value, "quote": quotes[key]} for key, value in previous.slots.items() if quotes.get(key)}
    db.commit()
    logs: list[dict] = []
    digest = hashlib.sha256(case.case_key.encode()).hexdigest()
    try:
        from app.decision_service import generate_decision
        if module == "sop":
            from app.sop_replay import replay_sop_case
            decision, logs, digest, trace = replay_sop_case(payload)
        else:
            decision, logs, digest, trace = generate_decision(payload)
        write_decision(result, decision, case)
        result.automatic_scores = {**result.automatic_scores, **trace, "module": module, "wakeup_action": decision.wakeup_action, "slot_evidence": decision.slot_evidence, "route_variant": decision.route_variant, "material_keys": decision.material_keys, "review_status": "pending"}
        result.status, result.completed_at = "completed", utcnow()
    except Exception as exc:
        if isinstance(exc, EvaluationCallError):
            logs, digest = exc.logs, exc.digest
        fallback = EvaluationDecision(
            action="no_action",
            branch="unclassified",
            intent="other",
            reply=None,
            handoff_reason=None,
            safety_flags=["model_error"],
            confidence=0,
        )
        write_decision(result, fallback, case)
        result.status, result.error_code, result.completed_at = "failed", getattr(exc, "code", type(exc).__name__), utcnow()
    for item in logs:
        db.add(ModelCallLog(evaluation_result_id=result.id, request_hash=digest, model=run.model, prompt_version=run.prompt_version, **item))
    db.commit()
    refresh_run(db, run.id)
    return True


def write_decision(result: EvaluationResult, decision: EvaluationDecision, case: EvaluationCase) -> None:
    result.action, result.branch, result.intent, result.reply = decision.action, decision.branch, decision.intent, decision.reply
    result.slots, result.missing_slots, result.handoff_reason = decision.slots, decision.missing_slots, decision.handoff_reason
    result.evidence_refs, result.safety_flags, result.confidence = decision.evidence_refs, decision.safety_flags, decision.confidence
    result.automatic_scores = {"schema_valid": True, "fact_support_status": "pending_review", "reference_is_ground_truth": False}


def refresh_run(db: Session, run_id: int) -> None:
    run = db.get(EvaluationRun, run_id)
    completed = db.scalar(select(func.count(EvaluationResult.id)).where(EvaluationResult.run_id == run_id, EvaluationResult.status == "completed")) or 0
    failed = db.scalar(select(func.count(EvaluationResult.id)).where(EvaluationResult.run_id == run_id, EvaluationResult.status == "failed")) or 0
    run.completed_cases, run.failed_cases = completed, failed
    done = completed + failed
    processed_results = db.scalars(select(EvaluationResult).where(EvaluationResult.run_id == run_id, EvaluationResult.status.in_(["completed", "failed"]))).all()
    handoffs = sum(1 for item in processed_results if item.action == "handoff")
    unsafe_blocked = sum(1 for item in processed_results if (item.safety_flags or []) and item.action == "handoff")
    run.metrics = {
        "processed": done,
        "schema_valid_rate": round(completed / done, 4) if done else 0,
        "handoff_rate": round(handoffs / done, 4) if done else 0,
        "unsafe_cases_blocked": unsafe_blocked,
        "outbound_requests": 0,
        "review_pending": sum(not item.review for item in processed_results),
        "failure_reasons": dict(Counter(item.error_code for item in processed_results if item.error_code)),
        "handoff_reasons": dict(Counter(item.handoff_reason for item in processed_results if item.handoff_reason)),
        "intents": dict(Counter(item.intent for item in processed_results)),
        "evidence_cited_cases": sum(bool(item.evidence_refs) for item in processed_results),
        "actions": dict(Counter(item.action for item in processed_results)),
        "wakeup_actions": dict(Counter(item.automatic_scores.get("wakeup_action") for item in processed_results if item.automatic_scores.get("wakeup_action"))),
        "scheduler_assertions_passed": sum(item.automatic_scores.get("assertions_passed",0) for item in processed_results),
        "accuracy": None,
    }
    calls = db.scalars(select(ModelCallLog).join(EvaluationResult).where(EvaluationResult.run_id == run_id)).all()
    durations = sorted(item.automatic_scores.get("total_ms",0) for item in processed_results if item.status=="completed")
    run.metrics = {**run.metrics, "latency_basis":"latest_decision_including_retries", "request_count": len(calls), "request_outcomes":dict(Counter(x.status for x in calls)), "request_errors":dict(Counter(x.error_code for x in calls if x.error_code)), "input_tokens": sum(x.input_tokens or 0 for x in calls), "output_tokens": sum(x.output_tokens or 0 for x in calls), "p50_ms": durations[int((len(durations)-1)*.5)] if durations else None, "p95_ms": durations[int((len(durations)-1)*.95)] if durations else None}
    if done >= run.total_cases:
        run.status, run.completed_at = "completed", utcnow()
    db.commit()
