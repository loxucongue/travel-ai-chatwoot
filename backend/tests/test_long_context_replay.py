from sqlalchemy import select, func

from app.deepseek_evaluation import EvaluationDecision
from app.models import (ConversationState, EvaluationCase, EvaluationDataset, EvaluationResult, EvaluationRun,
                        InboxBinding, KnowledgeVersion, OutboundMessage)


def test_sequential_replay_uses_previous_ai_not_future_human_and_resumes_idempotently(session_factory, monkeypatch):
    from app import long_context_replay as replay
    from app import decision_service
    monkeypatch.setattr(replay, "SessionLocal", session_factory)
    calls = []
    def model(payload):
        calls.append(payload)
        assert payload["context_complete"] is True
        assert "human-reference" not in str(payload)
        return EvaluationDecision("reply", "unclassified", "other", reply=f"AI draft {len(calls)}",
            slots={"party_size": 2}, slot_evidence={"party_size": "兩位"}), [], "test-hash", {}
    monkeypatch.setattr(decision_service, "generate_decision", model)
    with session_factory() as db:
        db.add(InboxBinding(id=1, tenant_id=1, chatwoot_inbox_id=128859, name="test"))
        db.add(KnowledgeVersion(id=1, tenant_id=1, version_key="test", title="test", content_hash="test"))
        db.flush()
        db.add(ConversationState(id=1, tenant_id=1, inbox_binding_id=1, chatwoot_conversation_id=27))
        dataset = EvaluationDataset(tenant_id=1, knowledge_version_id=1, name="source", filter_version="test", created_by=1)
        db.add(dataset)
        db.flush()
        for i, text in enumerate(["兩位", "明年三月", "想看行程"]):
            db.add(EvaluationCase(dataset_id=dataset.id, conversation_state_id=1, case_key=str(i),
                target_message_ids=[i+1], customer_text=text, reference_answer="human-reference",
                context_messages=[] if i == 0 else [{"direction": "outgoing", "content": "human-reference"}]))
        db.commit()
        dataset_id = dataset.id
    replay.sequential_replay(dataset_id)
    replay.sequential_replay(dataset_id)
    assert len(calls) == 3
    assert calls[1]["memory"]["party_size"] == {"value": 2, "quote": "兩位"}
    assert calls[1]["context_messages"][1]["content"] == "AI draft 1"
    assert len(calls[2]["context_messages"]) == 4
    with session_factory() as db:
        run = db.scalar(select(EvaluationRun))
        assert run.status == "completed" and run.completed_cases == 3
        assert db.scalar(select(func.count()).select_from(EvaluationResult)) == 3
        assert db.scalar(select(OutboundMessage)) is None


def test_targeted_repair_preserves_original_calls_and_skips_successes(session_factory, monkeypatch):
    from app import long_context_replay as replay, decision_service
    from app.evaluation_service import create_run, refresh_run
    from app.models import ModelCallLog, User
    monkeypatch.setattr(replay, "SessionLocal", session_factory)
    calls = []
    def model(payload):
        calls.append(payload["customer_text"])
        return EvaluationDecision("handoff", "unclassified", "other", reply="draft"), [], "hash", {}
    monkeypatch.setattr(decision_service, "generate_decision", model)
    with session_factory() as db:
        db.add(InboxBinding(id=1, tenant_id=1, chatwoot_inbox_id=128859, name="test"))
        db.add(KnowledgeVersion(id=1, tenant_id=1, version_key="test", title="test", content_hash="test"))
        db.flush()
        db.add(ConversationState(id=1, tenant_id=1, inbox_binding_id=1, chatwoot_conversation_id=27))
        dataset = EvaluationDataset(tenant_id=1, knowledge_version_id=1, name=replay.NAME, filter_version="test", created_by=1)
        db.add(dataset)
        db.flush()
        for text in ("successful", "failed-case"):
            db.add(EvaluationCase(dataset_id=dataset.id, conversation_state_id=1, case_key=text, customer_text=text))
        db.flush()
        run = create_run(db, dataset, db.get(User, 1))
        db.flush()
        results = db.scalars(select(EvaluationResult).order_by(EvaluationResult.id)).all()
        results[0].status = "completed"
        results[1].status, results[1].error_code = "failed", "deepseek_invalid_route_variant"
        db.add(ModelCallLog(evaluation_result_id=results[1].id, request_hash="old", model="test", prompt_version="test", status="failed"))
        db.commit()
        refresh_run(db, run.id)
    replay.retry_failed_once()
    replay.retry_failed_once()
    assert calls == ["failed-case"]
    with session_factory() as db:
        result = db.scalars(select(EvaluationResult).order_by(EvaluationResult.id.desc())).first()
        assert result.status == "completed" and result.automatic_scores["original_failure"]["error"] == "deepseek_invalid_route_variant"
        assert db.scalar(select(func.count()).select_from(ModelCallLog)) == 1
        assert db.scalar(select(OutboundMessage)) is None
