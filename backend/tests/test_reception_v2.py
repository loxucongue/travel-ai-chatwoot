import json

import pytest

from app.deepseek_evaluation import EvaluationCallError
from app.reception_v2.skill_registry import SkillRegistry
from app.reception_v2.tools import execute_tool
from app.reception_v2.flow_classifier import infer_route_variant, select_flow
from app.reception_v2.journey_memory import build_journey_memory
from app.reception_v2.proactive_policy import evaluate_proactive_eligibility
from app.reception_v2.journey_state_machine import allowed_next_stage, guard_decision_stage
from app.reception_v2 import runtime


def _tool_call(call_id, name, arguments):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def _final(**overrides):
    value = {
        'v2_events': [],
        "action": "reply", "branch": "peach_9d", "intent": "price",
        "reply": "9日行程目前是人民幣9,980元／人。", "route_variant": "peach_9d_2027",
        "evidence_refs": ["route.9.price"], "material_keys": [], "handoff_reason": None,
        "safety_flags": [], "confidence": .9, "slots": {}, "slot_evidence": {},
        "missing_slots": [], "lead_action": "none", "contact_values": {},
        "journey_stage": "value_building", "wakeup_action": None, "defer_minutes": 0,
    }
    value.update(overrides)
    return {"content": json.dumps(value, ensure_ascii=False)}


def test_skill_registry_is_versioned_and_route_specific():
    registry = SkillRegistry()
    names = {item["name"] for item in registry.index()}
    assert {"peach-9d-2027", "peach-11d-2027", "silence-followup"} <= names
    loaded = registry.load("peach-9d-2027")
    assert "先解决客户当前问题" in loaded["instructions"]
    assert len(registry.release_digest()) == 64


@pytest.mark.parametrize("context,expected", [
    ({"module": "reply", "customer_text": "9日和11日怎麼選", "journey": {}}, "route_selection"),
    ({"module": "reply", "customer_text": "會不會很累？", "route_variant": "peach_9d_2027"}, "concern_resolution"),
    ({"module": "reply", "customer_text": "請顧問聯繫我", "route_variant": "peach_9d_2027"}, "lead_handoff"),
    ({"module": "silence_touch", "customer_text": "", "route_variant": "peach_9d_2027"}, "silence_followup"),
])
def test_flow_classifier_bounds_v2_turn(context, expected):
    assert select_flow(context).name == expected


def test_explicit_route_detail_bypasses_route_selection():
    assert infer_route_variant("桃花9日一個人多少錢？") == "peach_9d_2027"
    selected = select_flow({"module": "reply", "customer_text": "桃花9日一個人多少錢？", "journey": {}})
    assert selected.name == "route_detail"
    assert "peach-9d-2027" in selected.allowed_skills


def test_fresh_generic_greeting_uses_operator_opening_without_model_call(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("generic opening must not call the model")

    monkeypatch.setattr(runtime, "_call", forbidden)
    result = runtime.run_v2_agent({
        "module": "reply",
        "customer_text": "您好",
        "context_messages": [{"direction": "incoming", "content": "您好"}],
        "reception_policy": {
            "operator_configuration": {
                "opening_messages": ["您好～這裡是已配置的開場。", "想先了解哪條行程呢？"],
                "opening_interval_seconds": 3,
            },
            "route_switch": {"allowed_routes": ["peach_9d_2027", "peach_11d_2027"]},
        },
    })
    decision, logs, _, trace = result
    assert decision.reply == "您好～這裡是已配置的開場。"
    assert decision.opening_messages == ["您好～這裡是已配置的開場。", "想先了解哪條行程呢？"]
    assert decision.opening_interval_seconds == 3
    assert logs == []
    assert trace["fast_path"] == "configured_opening"
    assert trace["request_count"] == 0


@pytest.mark.parametrize("text", ["您好，我想了解一下", "你好～"])
def test_generic_opening_accepts_polite_greeting_variants(monkeypatch, text):
    monkeypatch.setattr(runtime, "_call", lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("generic opening must not call the model")))
    decision, logs, _, trace = runtime.run_v2_agent({
        "module": "reply", "customer_text": text,
        "context_messages": [{"direction": "incoming", "content": text}],
        "reception_policy": {"operator_configuration": {"opening_message": "配置開場"}},
    })
    assert decision.reply == "配置開場"
    assert logs == [] and trace["fast_path"] == "configured_opening"


def test_first_trip_inquiry_uses_operator_opening_without_model_call(monkeypatch):
    monkeypatch.setattr(runtime, "_call", lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("first customer message must use the configured opening")))
    decision, logs, _, trace = runtime.run_v2_agent({
        "module": "reply",
        "customer_text": "你好，我想咨询旅行行程",
        "context_messages": [],
        "reception_policy": {"operator_configuration": {
            "opening_messages": ["配置开场一", "配置开场二"],
            "opening_interval_seconds": 4,
        }},
    })
    assert decision.opening_messages == ["配置开场一", "配置开场二"]
    assert decision.opening_interval_seconds == 4
    assert logs == [] and trace["fast_path"] == "configured_opening"


def test_specific_new_customer_question_does_not_use_operator_opening(monkeypatch):
    called = []

    def fake_call(*_args, **_kwargs):
        called.append(True)
        raise RuntimeError("sentinel")

    monkeypatch.setattr(runtime, "_call", fake_call)
    monkeypatch.setattr(runtime.settings, "deepseek_api_key", "test")
    with pytest.raises(RuntimeError, match="sentinel"):
        runtime.run_v2_agent({
            "module": "reply",
            "customer_text": "想了解9日價格",
            "context_messages": [{"direction": "incoming", "content": "之前的第一条消息"}],
            "reception_policy": {
                "operator_configuration": {"opening_message": "不應覆蓋具體問題"},
            },
        })
    assert called


def test_route_comparison_does_not_bind_single_route():
    assert infer_route_variant("9日和11日差在哪裡？") == ""


def test_flow_uses_durable_stage_before_contact_keywords():
    selected = select_flow({
        "module": "reply", "customer_text": "好的，我知道了",
        "route_variant": "peach_9d_2027",
        "journey": {"stage": "contact_requested", "route_variant": "peach_9d_2027"},
    })
    assert selected.name == "lead_handoff"
    assert selected.reason == "durable_lead_state"


def test_flow_uses_unresolved_topic_for_short_followup():
    selected = select_flow({
        "module": "reply", "customer_text": "嗯嗯",
        "route_variant": "peach_9d_2027",
        "journey": {"stage": "value_building", "route_variant": "peach_9d_2027"},
        "journey_memory": {"unresolved_topics": [{"topic": "hotel"}]},
    })
    assert selected.name == "concern_resolution"
    assert selected.reason == "journey_objection_or_unresolved_topic"


def test_flow_does_not_use_unresolved_topic_for_new_explicit_price_question():
    selected = select_flow({
        "module": "reply", "customer_text": "那價格是多少？",
        "route_variant": "peach_9d_2027",
        "journey": {"stage": "value_building", "route_variant": "peach_9d_2027"},
        "journey_memory": {"unresolved_topics": [{"topic": "hotel"}]},
    })
    assert selected.name == "route_detail"


def test_journey_state_machine_allows_customer_to_revisit_selection():
    assert allowed_next_stage("contact_requested", "captured") is True
    assert allowed_next_stage("contact_requested", "route_selection") is True
    decision = type("Decision", (), {
        "journey_stage": "route_selection", "safety_flags": []
    })()
    assert guard_decision_stage(decision, "contact_requested") is None
    assert decision.journey_stage == "route_selection"
    assert guard_decision_stage(decision, "handoff") == "journey_stage_transition_rejected"
    assert decision.journey_stage == "handoff"


def test_messages_inject_selected_flow_and_skill(monkeypatch):
    import app.reception_v2.runtime as runtime

    messages = runtime._messages({
        "module": "reply", "customer_text": "會不會很累？",
        "route_variant": "peach_9d_2027", "context_messages": [],
    }, SkillRegistry())
    assert "concern_resolution" in messages[0]["content"]
    assert "concern-resolution" in messages[0]["content"]


def test_journey_memory_exposes_unresolved_topic_and_new_followup_candidates():
    memory = build_journey_memory({
        "route_variant": "peach_9d_2027",
        "customer_text": "住什麼飯店？",
        "memory": {"party_size": 2, "_profile_meta": {"party_size": {"source_message_id": 10}}},
        "journey": {
            "route_variant": "peach_9d_2027",
            "stage": "value_building",
            "sent_content_groups": ["price"],
            "sent_asset_keys": [],
            "content_progress": {"price": {"text_delivered": True}},
        },
    })
    # A topic hint alone must not fabricate a durable unresolved customer question.
    assert memory["unresolved_topics"] == []
    assert memory["followup_candidates"][0] == "route.shared.hotel_reference"
    assert "route.shared.landmarks" in memory["followup_candidates"]
    assert memory["customer_facts"]["party_size"] == 2


def test_proactive_gate_blocks_silence_without_new_value():
    result = evaluate_proactive_eligibility({
        "module": "silence_touch",
        "journey": {"route_variant": "peach_9d_2027"},
    }, {"route_variant": "peach_9d_2027", "followup_candidates": []})
    assert result.eligible is False
    assert result.reason == "no_new_value"


def test_delivered_hotel_group_excludes_hotel_fact_candidate():
    memory = build_journey_memory({
        "route_variant": "peach_9d_2027", "customer_text": "住什麼飯店？",
        "journey": {"content_progress": {"hotel_reference": {
            "text_delivered": True, "history_unknown": False,
        }}},
    })
    assert "route.shared.hotel_reference" not in memory["followup_candidates"]


def test_asset_only_delivery_does_not_claim_hotel_text_was_delivered():
    memory = build_journey_memory({
        "route_variant": "peach_9d_2027", "customer_text": "住什麼飯店？",
        "journey": {"content_progress": {"hotel_reference": {
            "text_delivered": False, "history_unknown": False, "asset_keys": ["hotel_image"],
        }}},
    })
    assert memory["followup_candidates"][0] == "route.shared.hotel_reference"
    assert "route.shared.landmarks" in memory["followup_candidates"]


def test_proactive_gate_allows_silence_with_new_value():
    result = evaluate_proactive_eligibility({
        "module": "silence_touch",
        "journey": {"route_variant": "peach_9d_2027"},
        "lead_capture": {"status": "not_started"},
    }, {"route_variant": "peach_9d_2027", "followup_candidates": ["route.9.scope"]})
    assert result.eligible is True
    assert result.candidate_value_ids == ("route.9.scope",)


def test_silence_memory_uses_last_substantive_topic():
    memory = build_journey_memory({
        "route_variant": "peach_9d_2027",
        "module": "silence_touch",
        "customer_text": "我先跟家人討論",
        "context_messages": [
            {"role": "user", "content": "住什麼飯店？"},
            {"role": "assistant", "content": "我先說明住宿。"},
            {"role": "user", "content": "我先跟家人討論"},
        ],
        "journey": {"route_variant": "peach_9d_2027", "sent_content_groups": []},
    })
    assert memory["last_customer_topic"] == "hotel"
    assert memory["followup_candidates"][0] == "route.shared.hotel_reference"
    assert "route.shared.landmarks" in memory["followup_candidates"]


def test_silence_without_value_skips_before_model(monkeypatch):
    import app.reception_v2.runtime as runtime

    monkeypatch.setattr(runtime.settings, "deepseek_api_key", "")
    decision, logs, _, trace = runtime.run_v2_agent({
        "module": "silence_touch", "customer_text": "",
        "route_variant": "peach_9d_2027",
        "journey": {"route_variant": "peach_9d_2027", "sent_content_groups": [
            "route.9.scope", "route.shared.hotel_reference", "route.shared.vehicle_reference",
        ]},
    })
    assert decision.action == "no_action"
    assert decision.wakeup_action == "skip"
    assert logs == []
    assert trace["proactive_gate"]["reason"] == "no_new_value"


def test_decision_contract_explains_reply_and_handoff():
    import app.reception_v2.runtime as runtime

    decision = runtime.EvaluationDecision(
        action="handoff", branch="peach_9d", intent="contact",
        route_variant="peach_9d_2027", handoff_reason="customer_requested_human",
        safety_flags=[], slots={"party_size": 2},
        slot_evidence={"party_size": "我們兩位"}, journey_stage="handoff",
    )
    contract = runtime.build_decision_contract(
        {"module": "reply"}, decision, flow="lead_handoff",
        flow_reason="customer_requests_human",
    )
    assert contract["action"] == "handoff"
    assert contract["response"]["text"] == ""
    assert contract["response"]["suggestions"] == []
    assert contract["delivery"]["sections"] == []
    assert contract["delivery"]["material_keys"] == []
    assert contract["handoff"]["required"] is True
    assert contract["silence_plan"]["action"] == "skip"
    assert contract["memory_updates"][0]["evidence"] == "我們兩位"


def test_unbound_multi_constraint_lead_prefetches_route_shortlist_and_comparison():
    from app.reception_v2 import runtime

    results = runtime._prefetch_route_backend("compare routes for two people with a budget", "")
    assert [item["kind"] for item in results] == ["route_search", "route_comparison"]
    assert {item["route_variant"] for item in results[0]["data"]["routes"]} >= {
        "peach_9d_2027", "peach_11d_2027",
    }
    assert results[1]["data"]["route_ids"] == ["peach_11d_2027", "peach_9d_2027"]


def test_bound_route_does_not_run_unnecessary_route_prefetch():
    from app.reception_v2 import runtime

    assert runtime._prefetch_route_backend("compare routes", "peach_9d_2027") == []


def test_structured_route_comparison_uses_grounded_verification_without_second_model_call(monkeypatch):
    from app.reception_v2 import runtime

    monkeypatch.setattr(runtime, "call_reply_fact_verifier",
                        lambda *_args, **_kwargs: pytest.fail("structured route output is already grounded"))
    decision = runtime.EvaluationDecision(
        action="reply", branch="peach_9d", intent="other", route_variant="peach_9d_2027",
        reply="Recommend the 9-day route based on your requested pace and hotel.",
        evidence_refs=["route.9.scope"],
        presentations=[{"type": "route_comparison", "route_ids": ["peach_9d_2027", "peach_11d_2027"],
                        "criteria": ["duration", "hotel"]}],
    )
    audit, logs, digest = runtime._verify({
        "engine_version": "v2", "module": "reply", "customer_text": "compare routes",
    }, decision)
    assert audit.supported and audit.relevant
    assert logs[0]["node"] == "v2_structured_grounded_verification"
    assert digest


def test_v2_trace_contains_latency_breakdown(monkeypatch):
    import app.reception_v2.runtime as runtime

    replies = iter([_final(evidence_refs=[])])
    monkeypatch.setattr(runtime.settings, "deepseek_api_key", "test")
    monkeypatch.setattr(runtime, "_call", lambda _payload, _round: (
        next(replies), {"attempt": 1, "round": 0, "duration_ms": 7,
                        "status": "completed", "response_meta": {}}
    ))
    monkeypatch.setattr(runtime, "_verify", lambda _context, _decision: (None, [], ""))
    _, _, _, trace = runtime.run_v2_agent({
        "module": "reply", "customer_text": "price",
        "route_variant": "peach_9d_2027", "context_messages": [],
    })
    assert trace["model_request_ms"] == 7
    assert trace["verification_ms"] == 0
    assert trace["repair_ms"] == 0


def test_fact_tool_reads_approved_route_data():
    result = execute_tool("get_route_facts", {"route_variant": "peach_9d_2027", "topic": "price"}, SkillRegistry())
    facts = {item["id"]: item["text"] for item in result["facts"]}
    assert "route.9.price" in facts
    assert "9,980" in facts["route.9.price"]


def test_route_capabilities_are_route_specific():
    nine = execute_tool("get_route_capabilities", {"route_variant": "peach_9d_2027"}, SkillRegistry())
    eleven = execute_tool("get_route_capabilities", {"route_variant": "peach_11d_2027"}, SkillRegistry())
    assert nine["skill"] == "peach-9d-2027"
    assert eleven["skill"] == "peach-11d-2027"
    assert "route.9.scope" not in nine["followup_candidates"]
    assert "route.11.scope" not in eleven["followup_candidates"]
    assert "route.shared.hotel_reference" in nine["followup_candidates"]
    assert "route.shared.barkhor_culture" in eleven["followup_candidates"]
    assert "availability" in nine["human_check"]


@pytest.mark.parametrize("topic,expected", [
    ("9日多少錢？", "route.9.price"),
    ("車上有供氧嗎？", "service.medical_support"),
    ("住什麼飯店？", "route.shared.hotel_reference"),
])
def test_fact_tool_resolves_chinese_customer_topics(topic, expected):
    result = execute_tool("get_route_facts", {
        "route_variant": "peach_9d_2027", "topic": topic,
    }, SkillRegistry())
    assert expected in {item["id"] for item in result["facts"]}


def test_v2_agent_loads_skill_and_facts_before_answer(monkeypatch):
    import app.reception_v2.runtime as runtime

    replies = iter([
        {"tool_calls": [_tool_call("1", "load_skill", {"name": "peach-9d-2027"})]},
        {"tool_calls": [_tool_call("2", "get_route_facts", {"route_variant": "peach_9d_2027", "topic": "price"})]},
        _final(),
    ])

    def fake_call(_payload, round_index):
        return next(replies), {"attempt": round_index + 1, "duration_ms": 1, "input_tokens": 10, "output_tokens": 5, "status": "completed", "error_code": None, "response_meta": {}}

    monkeypatch.setattr(runtime.settings, "deepseek_api_key", "test")
    monkeypatch.setattr(runtime, "_call", fake_call)
    monkeypatch.setattr(runtime, "_verify", lambda _context, _decision: (None, [], ""))
    decision, logs, _digest, trace = runtime.run_v2_agent({"module": "reply", "customer_text": "9日多少錢？", "context_messages": []})
    assert decision.reply == "9日行程目前是人民幣9,980元／人。"
    assert trace["loaded_skills"] == ["peach-9d-2027"]
    assert "route.9.price" in trace["available_fact_ids"]
    assert len(logs) == 3


def test_invalid_optional_coverage_hint_is_discarded_without_regenerating_answer(monkeypatch):
    import app.reception_v2.runtime as runtime
    replies = iter([_final(covered_content_groups=['invented_group']), _final(covered_content_groups=['price_reference'])])
    monkeypatch.setattr(runtime.settings, 'deepseek_api_key', 'test')
    monkeypatch.setattr(runtime, '_call', lambda _payload, index: (next(replies), {
        'attempt': index + 1, 'duration_ms': 1, 'status': 'completed'}))
    monkeypatch.setattr(runtime, '_verify', lambda *args: (None, [], ''))
    decision, logs, _, _ = runtime.run_v2_agent({'module': 'reply', 'customer_text': '9日多少钱？',
                                               'route_variant': 'peach_9d_2027'})
    assert len(logs) == 1
    assert decision.covered_content_groups == ['price_reference']
    assert 'invented_group' not in decision.covered_content_groups


def test_model_handoff_reason_cannot_remain_a_reply_without_handoff(monkeypatch):
    import app.reception_v2.runtime as runtime
    monkeypatch.setattr(runtime.settings, 'deepseek_api_key', 'test')
    monkeypatch.setattr(runtime, '_call', lambda *_: (_final(handoff_reason='special_discount_requires_advisor'),
        {'attempt': 1, 'duration_ms': 1, 'status': 'completed'}))
    monkeypatch.setattr(runtime, '_verify', lambda *args: (None, [], ''))
    decision, _, _, _ = runtime.run_v2_agent({'module': 'reply', 'customer_text': '9日多人折扣多少钱？',
                                            'route_variant': 'peach_9d_2027'})
    assert decision.action == 'handoff'
    assert decision.handoff_reason == 'knowledge_confirmation_required'
    assert decision.journey_stage == 'handoff'
    assert decision.wakeup_action == 'skip'


def test_v2_agent_rejects_fact_reference_not_returned(monkeypatch):
    import app.reception_v2.runtime as runtime

    monkeypatch.setattr(runtime.settings, "deepseek_api_key", "test")
    monkeypatch.setattr(runtime, "_call", lambda _payload, round_index: (_final(evidence_refs=["route.11.price"]), {"attempt": 1, "duration_ms": 1, "status": "completed", "error_code": None, "response_meta": {}}))
    monkeypatch.setattr(runtime, "_verify", lambda _context, _decision: (None, [], ""))
    with pytest.raises(EvaluationCallError, match="v2_unknown_evidence_reference"):
        runtime.run_v2_agent({"module": "reply", "customer_text": "價格？", "context_messages": []})


def test_freeform_service_promise_is_sent_to_verifier(monkeypatch):
    import app.reception_v2.runtime as runtime
    from app.reply_fact_verification import FactVerification

    checked = []
    monkeypatch.setattr(runtime, "call_reply_fact_verifier", lambda context, plan, generated: (
        checked.append(generated.body) or FactVerification(True), [], "verified"
    ))
    decision = runtime.EvaluationDecision(
        action="reply", branch="unclassified", intent="other",
        reply="已經替您預訂好座位。",
    )
    verification, _, _ = runtime._verify({}, decision)
    assert verification is not None
    assert checked == ["已經替您預訂好座位。"]


def test_simple_considering_reply_needs_no_model_call(monkeypatch):
    import app.reception_v2.runtime as runtime

    monkeypatch.setattr(runtime.settings, "deepseek_api_key", "")
    decision, logs, _, trace = runtime.run_v2_agent({
        "module": "reply", "customer_text": "我先跟家人討論。",
        "route_variant": "peach_9d_2027",
    })
    assert decision.journey_stage == "considering"
    assert decision.route_variant == "peach_9d_2027"
    assert "家人" in decision.reply
    assert logs == []
    assert trace["fast_path"] == "considering_ack"
