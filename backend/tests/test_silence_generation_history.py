"""History/task boundary checks and opt-in, model-only incident replay."""
import json
import os
from pathlib import Path

import pytest


@pytest.mark.parametrize("status,expected", [("simulated_delivered", True), ("delivered", True),
                                           ("failed", False), ("draft", False),
                                           ("submission_unknown", False)])
def test_historical_response_hint_requires_following_nonfailed_answer(status, expected):
    from app.silence_generation import _conversation_history
    source = {"customer_text": "有隨隊醫生嗎？", "context_messages": [
        {"direction": "incoming", "content": "有隨隊醫生嗎？"},
        {"direction": "outgoing", "content": "沒有安排隨隊醫師。", "status": status},
    ]}
    history = _conversation_history(source)
    assert history["advisor_response_after_last_customer"] is expected
    assert history["is_current_task"] is False
    source["context_messages"].reverse()
    assert not _conversation_history(source)["advisor_response_after_last_customer"]
    source["context_messages"].append({"direction": "incoming", "content": "另一個問題"})
    assert not _conversation_history(source)["advisor_response_after_last_customer"]


def test_answered_medical_history_is_not_silence_task_and_repeat_still_blocked(monkeypatch):
    from test_split_silence_pipeline import completed_initial_context
    import app.silence_generation as generator
    from app.silence_planning import build_silence_plan
    from app.reply_generation import GeneratedReply

    source = completed_initial_context()
    answer = "我們沒有安排隨隊醫師。導遊有接受高原旅遊急救培訓，車上備有血氧儀、氧氣瓶和急救包。"
    source["customer_text"] = "有隨隊醫生嗎？"
    source["context_messages"] = [
        {"direction": "incoming", "content": source["customer_text"]},
        {"direction": "outgoing", "content": answer, "status": "delivered"},
    ]
    captured = {}
    def capture(**kwargs):
        captured.update(kwargs)
        return "captured"
    monkeypatch.setattr(generator, "call_json_node", capture)
    plan = build_silence_plan(source)
    generator.call_silence_generator(source, plan)
    payload = captured["input_data"]
    assert "customer_last_message" not in payload
    assert payload["conversation_history"]["advisor_response_after_last_customer"] is True
    assert payload["silence_plan"]["content_group_keys"] == ["landmarks"]
    assert payload["silence_plan"]["task_type"] == "proactive_silence_touch_without_new_customer_message"
    with pytest.raises(ValueError, match="reply_repeats_recent_advisor_message"):
        generator._reject_recent_advisor_repeat(
            GeneratedReply(body=answer, follow_up=None, used_fact_ids=[], asset_ids=[]), source["context_messages"])


def incident_context(run_id):
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session
    from app.automation_models import AutomationRun, AutomationSession
    from app.models import Tenant
    from app.material_library import candidate_materials, tenant_for_session
    from app.reception_config import effective_reception_policy
    from app.route_reply import journey_context_from_values, playbook_prompt

    path = Path(__file__).resolve().parents[2] / "output/delivery-consistency-20260913/acceptance.db"
    engine = create_engine(f"sqlite:///file:{path.as_posix()}?mode=ro&uri=true")
    try:
        with Session(engine) as db:
            run = db.get(AutomationRun, run_id)
            assert run.session_id == 1154
            session = db.get(AutomationSession, 1154)
            context = dict(run.input_snapshot)
            tenant = tenant_for_session(db, session) or db.scalar(select(Tenant.id).order_by(Tenant.id))
            context["available_materials"] = candidate_materials(db, tenant)
            controls = session.controls
            journey = controls["journey"]
            route = journey["route_variant"]
            context["route_variant"] = controls.get("route_variant", "")
            context["journey"] = journey_context_from_values(
                route, journey["stage"], journey["slots"], journey.get("sent_content_groups", []))
            context["route_playbook"] = playbook_prompt(route, journey["slots"])
            context["reception_policy"] = effective_reception_policy(db)
            context["lead_capture"] = dict(controls.get("lead_capture") or {"status": "not_started"})
            return context
    finally:
        engine.dispose()


@pytest.mark.skipif(os.getenv("VERIFY_SILENCE_1154") != "1", reason="Paid isolated incident replay")
@pytest.mark.parametrize("run_id", [3551, 3552])
def test_real_incident_replay_without_outbound(monkeypatch, run_id):
    import app.silence_generation as generator
    from app.route_packages import route_catalog_context
    from app.silence_planning import build_silence_plan
    from app.reply_fact_verification import call_reply_fact_verifier

    context = incident_context(run_id)
    assert context["customer_text"] == "有隨隊醫生嗎？"
    original = generator._parse

    def capture(value, **kwargs):
        # Log only model copy, never the full customer/configuration snapshot.
        print(json.dumps({"run": run_id, "raw_body": value.get("body")}, ensure_ascii=True))
        return original(value, **kwargs)

    monkeypatch.setattr(generator, "_parse", capture)
    journey = context["journey"]
    with route_catalog_context(journey["route_variant"], journey["slots"],
                               available_materials=context["available_materials"]):
        plan = build_silence_plan(context)
        assert plan.reply_plan.allowed_content_group_keys == ["landmarks"]
        generated, _, _ = generator.call_silence_generator(context, plan)
        assert generated.asset_ids
        assert not any(word in generated.body for word in ("醫師", "醫生", "急救", "血氧"))
        verdict, _, _ = call_reply_fact_verifier(context, plan.reply_plan, generated)
        assert verdict.supported and verdict.relevant, verdict
