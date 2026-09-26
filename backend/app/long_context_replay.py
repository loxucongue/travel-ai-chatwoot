"""Resumable, isolated full-history and sequential AI replay. Never sends to Chatwoot."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import time

from sqlalchemy import func, select

from app.config import settings
from app.db import SessionLocal
from app.deepseek_evaluation import close_deepseek_transport
from app.evaluation_dataset import build_dataset
from app.evaluation_service import create_run, process_next_result, refresh_run
from app.models import (ConversationState, EvaluationCase, EvaluationDataset, EvaluationResult,
                        EvaluationRun, HandoffTask, MessageEvent, OutboundMessage, SyncJob, User)

NAME = "full-context-20260827-v1"
TARGET = Path("../output/long-context-20260827")


def guard():
    if not (settings.app_profile == "evaluation" and settings.outbound_mode == "disabled"
            and not settings.chatwoot_write_enabled and "/replays/" in settings.database_url.replace("\\", "/")):
        raise RuntimeError("isolated_read_only_replay_required")


def counts(db):
    return {name: db.scalar(select(func.count()).select_from(model)) for name, model in (
        ("conversations", ConversationState), ("messages", MessageEvent),
        ("outbound", OutboundMessage), ("handoffs", HandoffTask))}


def progress(**data):
    print(json.dumps(data), flush=True)


def wait_for_sync():
    while True:
        with SessionLocal() as db:
            job = db.scalar(select(SyncJob).where(SyncJob.kind == "evaluation_history").order_by(SyncJob.id.desc()))
            if not job:
                raise RuntimeError("complete_sync_required")
            if job.status == "completed" and not job.failed_items:
                return
            if job.status not in ("pending", "running"):
                raise RuntimeError(f"history_sync_incomplete:{job.status}")
        time.sleep(10)


def full_replay():
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.role.in_(["admin", "super_admin"])))
        dataset = db.scalar(select(EvaluationDataset).where(EvaluationDataset.name == NAME))
        if not dataset:
            baseline = counts(db)
            dataset = build_dataset(db, user, NAME, settings.evaluation_inbox_id, "reply", True, [26], history_verified=True)
            dataset.filter_config = {**dataset.filter_config, "baseline": baseline, "test_conversation_ids": [26]}
        run = create_run(db, dataset, user)
        db.commit()
        run_id, dataset_id = run.id, dataset.id
        progress(phase="full", run=run_id, dataset=dataset_id, total=run.total_cases)
    while True:
        with SessionLocal() as db:
            if not process_next_result(db, run_id):
                break
            run = db.get(EvaluationRun, run_id)
            progress(phase="full", completed=run.completed_cases, failed=run.failed_cases, total=run.total_cases)
    with SessionLocal() as db:
        if db.get(EvaluationRun, run_id).status != "completed":
            raise RuntimeError("replay_paused_or_incomplete")
    return dataset_id


def sequential_replay(source_id: int):
    # Counterfactual continuity: real customer turns, previous AI drafts instead of future human answers.
    with SessionLocal() as db:
        source = db.get(EvaluationDataset, source_id)
        user = db.get(User, source.created_by)
        dataset = db.scalar(select(EvaluationDataset).where(EvaluationDataset.name == NAME + "-sequential"))
        if not dataset:
            dataset = EvaluationDataset(tenant_id=source.tenant_id, knowledge_version_id=source.knowledge_version_id,
                name=NAME + "-sequential", filter_version="sequential-ai-prefix-v1", status="ready", created_by=user.id,
                snapshot_at=source.snapshot_at, filter_config={"module": "reply", "context_complete": True,
                    "sequential_ai_context": True, "source_dataset_id": source_id, "baseline": counts(db),
                    "handoff_continuation": "counterfactual_draft_only"})
            db.add(dataset)
            db.flush()
        groups = defaultdict(list)
        for case in db.scalars(select(EvaluationCase).where(EvaluationCase.dataset_id == source_id).order_by(EvaluationCase.id)):
            groups[case.conversation_state_id].append(case)
        chosen = sorted(groups, key=lambda key: len(groups[key]), reverse=True)[:8]
        dataset.case_count = sum(len(groups[key]) for key in chosen)
        dataset.filter_config = {**dataset.filter_config, "conversation_count": len(chosen), "selection": "eight_most_customer_turns"}
        run = create_run(db, dataset, user)
        run.total_cases = dataset.case_count
        # create_run initially sees zero cases; new cases/results are appended as their AI prefix becomes known.
        db.commit()
        run_id, dataset_id = run.id, dataset.id
        sources = {key: [item.id for item in groups[key]] for key in chosen}
    for conversation_id, source_cases in sources.items():
        history = []
        for turn_index, source_case_id in enumerate(source_cases):
            with SessionLocal() as db:
                original = db.get(EvaluationCase, source_case_id)
                if turn_index == 0:
                    history = list(original.context_messages)
                key = f"sequential:{original.case_key}"
                case = db.scalar(select(EvaluationCase).where(EvaluationCase.dataset_id == dataset_id, EvaluationCase.case_key == key))
                if not case:
                    case = EvaluationCase(dataset_id=dataset_id, conversation_state_id=conversation_id, case_key=key,
                        target_message_ids=original.target_message_ids, customer_text=original.customer_text,
                        context_messages=list(history), reference_answer=original.reference_answer)
                    db.add(case)
                    db.flush()
                    db.add(EvaluationResult(run_id=run_id, case_id=case.id))
                    db.commit()
                result = db.scalar(select(EvaluationResult).where(EvaluationResult.run_id == run_id, EvaluationResult.case_id == case.id))
                if result.status == "pending":
                    process_next_result(db, run_id)
                    db.refresh(result)
                if result.status not in ("completed", "failed"):
                    raise RuntimeError("sequential_result_not_finished")
                history.append({"direction": "incoming", "content": original.customer_text, "id": original.target_message_ids[0]})
                if result.reply:
                    history.append({"direction": "outgoing", "content": result.reply, "source": "previous_ai_draft"})
                run = db.get(EvaluationRun, run_id)
                progress(phase="sequential", completed=run.completed_cases, failed=run.failed_cases, total=run.total_cases)
    with SessionLocal() as db:
        refresh_run(db, run_id)


def export_report():
    from app.automation_api import safe_text
    TARGET.mkdir(parents=True, exist_ok=True)
    summary = {"safety": {"profile": settings.app_profile, "outbound_mode": settings.outbound_mode,
                           "chatwoot_write_enabled": settings.chatwoot_write_enabled}, "modules": []}
    with SessionLocal() as db:
        current = counts(db)
        summary["counts"] = current
        for dataset in db.scalars(select(EvaluationDataset).where(EvaluationDataset.name.in_([NAME, NAME + "-sequential"]))):
            run = db.scalar(select(EvaluationRun).where(EvaluationRun.dataset_id == dataset.id))
            if not run:
                continue
            module = "sequential" if dataset.filter_config.get("sequential_ai_context") else "full"
            cases, memory_checks = [], Counter()
            for result, case, conversation in db.execute(select(EvaluationResult, EvaluationCase, ConversationState)
                    .join(EvaluationCase, EvaluationCase.id == EvaluationResult.case_id)
                    .join(ConversationState, ConversationState.id == EvaluationCase.conversation_state_id)
                    .where(EvaluationResult.run_id == run.id).order_by(EvaluationCase.id)):
                for slot, check in result.automatic_scores.get("model_memory_checks", {}).items():
                    memory_checks[f"{slot}_checked"] += 1
                    memory_checks[f"{slot}_matches_memory"] += bool(check.get("matches_memory"))
                cases.append(safe_text({"case_id": case.id, "conversation_id": conversation.chatwoot_conversation_id,
                    "question": case.customer_text, "context": case.context_messages, "reference": case.reference_answer,
                    "draft": result.reply, "action": result.action, "branch": result.branch, "intent": result.intent,
                    "slots": result.slots, "reason": result.handoff_reason, "evidence": result.evidence_refs,
                    "flags": result.safety_flags, "status": result.status, "error": result.error_code,
                    "trace": result.automatic_scores, "review": result.review or {"status": "pending"}}))
            sizes = [len(case["context"]) for case in cases]
            baseline = dataset.filter_config["baseline"]
            item = {"module": module, "dataset_id": dataset.id, "run_id": run.id, "snapshot_at": dataset.snapshot_at,
                    "status": run.status, "cases": len(cases), "conversations": len({case["conversation_id"] for case in cases}),
                    "context_max_messages": max(sizes, default=0), "context_over_30": sum(size > 30 for size in sizes),
                    "context_over_100": sum(size > 100 for size in sizes), "metrics": run.metrics,
                    "model_memory_continuity_not_accuracy": dict(memory_checks), "filter": dataset.filter_config,
                    "outbound_delta": current["outbound"] - baseline["outbound"], "handoff_delta": current["handoffs"] - baseline["handoffs"]}
            (TARGET / f"{module}-cases.json").write_text(json.dumps({**item, "results": cases}, ensure_ascii=False, indent=2), encoding="utf-8")
            summary["modules"].append(item)
        (TARGET / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    progress(report=str(TARGET.resolve()), modules=[{"module": m["module"], "cases": m["cases"], "status": m["status"]} for m in summary["modules"]])


def retry_failed_once():
    """Repair independent real-history cases; never rewrite the prefix of a sequential chain."""
    with SessionLocal() as db:
        runs = db.scalars(select(EvaluationRun).join(EvaluationDataset).where(
            EvaluationDataset.name == NAME)).all()
        if any(run.status != "completed" for run in runs):
            raise RuntimeError("wait_for_replay_completion_before_repair")
        run_ids = [run.id for run in runs]
    for run_id in run_ids:
        with SessionLocal() as db:
            failed_ids = db.scalars(select(EvaluationResult.id).where(
                EvaluationResult.run_id == run_id, EvaluationResult.status == "failed")).all()
        for result_id in failed_ids:
            with SessionLocal() as db:
                result = db.get(EvaluationResult, result_id)
                if result.automatic_scores.get("targeted_retry_done"):
                    continue
                previous = {"status": result.status, "error": result.error_code, "completed_at": result.completed_at}
                result.status, result.error_code, result.completed_at = "pending", None, None
                run = db.get(EvaluationRun, run_id)
                run.status, run.completed_at = "running", None
                db.commit()
                process_next_result(db, run_id)
                db.refresh(result)
                result.automatic_scores = {**result.automatic_scores, "targeted_retry_done": True, "original_failure": previous}
                db.commit()
                refresh_run(db, run_id)
                progress(phase="targeted_repair", run=run_id, result=result_id, status=result.status)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["run", "report", "retry-failed"])
    args = parser.parse_args()
    guard()
    try:
        if args.action == "run":
            wait_for_sync()
            source_id = full_replay()
            export_report()
            sequential_replay(source_id)
        elif args.action == "retry-failed":
            retry_failed_once()
        export_report()
    finally:
        close_deepseek_transport()


if __name__ == "__main__":
    main()
