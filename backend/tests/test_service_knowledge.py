import hashlib
from copy import deepcopy

import pytest

from app.reply_generation import call_reply_generator
from app.reply_planning import build_reply_plan
from app.reply_understanding import CustomerUnderstanding, _parse as parse_understanding
from app.route_packages import JOURNEY_POLICY, ROUTES
from app.service_knowledge import (
    ROOT,
    SERVICE_FIXED_ANSWERS,
    SERVICE_KNOWLEDGE,
    ServiceKnowledgeError,
    _validate,
)


def _context(customer_text: str, *, route: str = "") -> dict:
    policy = deepcopy(JOURNEY_POLICY)
    policy["route_switch"]["allowed_routes"] = list(ROUTES)
    policy["operator_configuration"] = {
        "business_goal": "先回答客戶，再提供有用資訊",
        "custom_guidance": "",
        "lead_capture": {
            "enabled": True,
            "channels": ["LINE", "Email"],
            "require_supported_route": False,
            "require_party_size": False,
            "require_departure_window": False,
            "ask_after_answered_topics": 1,
        },
        "business_rules": [],
    }
    return {
        "module": "reply",
        "customer_text": customer_text,
        "context_messages": [],
        "route_variant": route,
        "memory": {},
        "journey": {"sent_content_groups": [], "customer_profile": {}},
        "lead_capture": {"status": "not_started"},
        "available_materials": [],
        "reception_policy": policy,
    }


def test_service_snapshots_match_reviewed_hashes():
    for source in SERVICE_KNOWLEDGE["sources"]:
        path = ROOT / source["snapshot_path"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == source["snapshot_sha256"]


def test_new_service_evidence_is_present_in_captured_website():
    sources = {s['id']: s for s in SERVICE_KNOWLEDGE['sources']}
    for fact in SERVICE_KNOWLEDGE['facts']:
        if not fact.get('source_quote'):
            continue
        source = sources[fact['source_ref'].split('#')[0]]
        text = (ROOT / source['snapshot_path']).read_text(encoding='utf-8')
        assert ''.join(fact['source_quote'].split()) in ''.join(text.split())


@pytest.mark.parametrize('route', ['', 'peach_9d_2027', 'peach_11d_2027'])
def test_doctor_paraphrase_receives_service_arrangements(route):
    plan = build_reply_plan(_context('如果不帶醫師，你們能提供哪些協助？', route=route), CustomerUnderstanding(
        intent='other', customer_questions=['medical_service'], confidence=0.99,
    ))
    assert {'service.medical_support', 'service.medical_preparedness', 'service.medical_logistics'} <= set(plan.allowed_fact_ids)
    assert plan.follow_up is None


def test_medication_includes_website_evidence_without_sales_question():
    plan = build_reply_plan(_context('提前吃紅景天有效嗎？還是要去開丹木斯', route='peach_9d_2027'), CustomerUnderstanding(
        intent='other', customer_questions=['medication'], confidence=0.99,
    ))
    assert {'service.medication', 'service.website_medication_precautions'} <= set(plan.allowed_fact_ids)
    assert plan.follow_up is None


def test_service_knowledge_rejects_snapshot_hash_drift():
    candidate = deepcopy(SERVICE_KNOWLEDGE)
    candidate["sources"][0]["snapshot_sha256"] = "0" * 64
    with pytest.raises(ServiceKnowledgeError, match="snapshot_hash_mismatch"):
        _validate(candidate)


@pytest.mark.parametrize(
    ("customer_text", "topic", "answer_id"),
    [
        ("你們有隨團醫師嗎？", "medical_service", "tour_doctor_service"),
        ("高原反應有哪些症狀？", "altitude_health", "altitude_sickness_symptoms"),
        ("高原反应有哪些症状？", "altitude_health", "altitude_sickness_symptoms"),
        ("到了西藏高反怎麼辦？", "altitude_health", "altitude_sickness_response"),
        ("到了西藏高反怎么办？", "altitude_health", "altitude_sickness_response"),
    ],
)
def test_reviewed_service_answers_are_sent_verbatim_without_route_or_model(
    monkeypatch, customer_text, topic, answer_id
):
    source = _context(customer_text)
    understanding = CustomerUnderstanding(
        intent="other",
        customer_questions=[topic],
        confidence=0.99,
    )
    plan = build_reply_plan(source, understanding)
    expected = next(item for item in SERVICE_FIXED_ANSWERS if item["id"] == answer_id)
    assert plan.fixed_answer_id == answer_id
    assert plan.fixed_answer_scope == "service"
    assert plan.follow_up is None
    assert plan.allowed_asset_ids == []

    def unexpected_model_call(**_kwargs):
        raise AssertionError("reviewed service answers must not call the generation model")

    monkeypatch.setattr("app.reply_generation.call_json_node", unexpected_model_call)
    generated, calls, source_id = call_reply_generator(source, plan)
    assert calls == []
    assert source_id == f"service-fixed-answer:{answer_id}"
    assert generated.body == expected["answer_text"]


def test_personal_medical_suitability_never_uses_general_website_answer():
    source = _context("我有心臟病，可以去西藏嗎？")
    understanding = CustomerUnderstanding(
        intent="other",
        semantic_signals=["personal_health_suitability"],
        customer_questions=["altitude_health"],
        confidence=0.99,
    )
    plan = build_reply_plan(source, understanding)
    assert plan.fixed_answer_id == ""
    assert "health_confirmation_required" in plan.safety_flags
    assert plan.follow_up is None


def test_empty_normalized_customer_text_never_matches_a_fixed_answer():
    source = _context("???")
    understanding = CustomerUnderstanding(
        intent="other",
        customer_questions=["medical_service"],
        confidence=0.1,
    )
    plan = build_reply_plan(source, understanding)
    assert plan.fixed_answer_id == ""


def test_understanding_recovers_service_health_topics_from_customer_words():
    base = {
        "intent": "other",
        "route_candidate": "",
        "route_resolution": "none",
        "route_evidence": "",
        "slot_updates": {},
        "semantic_signals": ["unresolved_direct_question"],
        "contact_candidates": [],
        "requested_contact_channel": "",
        "customer_questions": ["other"],
        "matched_rules": [],
        "confidence": 0.7,
    }
    doctor = parse_understanding(
        base,
        customer_text="你們有隨團醫師嗎？",
        allowed_routes=set(ROUTES),
        allowed_slots={"party_size", "departure_window"},
        allowed_rule_ids=set(),
    )
    assert doctor.customer_questions == ["medical_service"]
    assert "unresolved_direct_question" not in doctor.semantic_signals

    altitude = parse_understanding(
        base,
        customer_text="高原反應有哪些症狀？",
        allowed_routes=set(ROUTES),
        allowed_slots={"party_size", "departure_window"},
        allowed_rule_ids=set(),
    )
    assert altitude.customer_questions == ["altitude_health"]
