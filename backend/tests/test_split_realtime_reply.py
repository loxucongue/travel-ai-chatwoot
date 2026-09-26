from copy import deepcopy

from dataclasses import replace

import pytest

from app.deepseek_evaluation import EvaluationDecision
from app.decision_service import _validated_route_references, generate_decision
from app.reply_generation import (
    GeneratedReply,
    _parse as parse_generated_reply,
    deterministic_system_reply,
)
from app.reply_fact_verification import FactVerification
from app.reply_planning import FollowUp, build_reply_plan
from app.reception_policy_views import reception_policy_views
from app.reply_understanding import (
    ContactCandidate,
    CustomerUnderstanding,
    SlotUpdate,
    _parse as parse_understanding,
)
from app.realtime_reply_pipeline import run_realtime_reply_pipeline
from app.route_packages import JOURNEY_POLICY, ROUTES


def policy():
    value = deepcopy(JOURNEY_POLICY)
    value["route_switch"].update({
        "enabled": True,
        "allowed_routes": list(ROUTES),
        "outside_catalog_action": "recommend_supported_routes",
    })
    value["operator_configuration"] = {
        "business_goal": "先回答客户，再自然推进",
        "custom_guidance": "",
        "lead_capture": {
            "enabled": True,
            "channels": ["LINE", "微信"],
            "require_supported_route": True,
            "require_party_size": True,
            "require_departure_window": True,
            "ask_after_answered_topics": 2,
        },
        "business_rules": [],
    }
    return value


def context(**overrides):
    value = {
        "module": "reply",
        "customer_text": "想了解桃花9日",
        "context_messages": [],
        "route_variant": "",
        "memory": {},
        "journey": {"sent_content_groups": [], "customer_profile": {}},
        "lead_capture": {"status": "not_started"},
        "available_materials": [],
        "reception_policy": policy(),
    }
    value.update(overrides)
    return value


def test_understanding_contract_uses_runtime_route_allowlist():
    parsed = parse_understanding(
        {
            "intent": "route_intro",
            "route_candidate": "new_route_2028",
            "route_resolution": "confirmed",
            "route_evidence": "新线路",
            "slot_updates": {},
            "semantic_signals": [],
            "contact_candidates": [],
            "requested_contact_channel": "",
            "customer_questions": ["itinerary"],
            "matched_rule_ids": [],
            "confidence": 0.9,
        },
        customer_text="我想看新线路",
        allowed_routes={"new_route_2028"},
        allowed_slots={"party_size"},
        allowed_rule_ids=set(),
    )
    assert parsed.route_candidate == "new_route_2028"


def test_understanding_recovers_seasonal_weather_without_treating_it_as_live_weather():
    parsed = parse_understanding(
        {
            "intent": "other",
            "route_candidate": "",
            "route_resolution": "none",
            "route_evidence": "",
            "slot_updates": {},
            "semantic_signals": ["current_conditions", "unresolved_direct_question"],
            "contact_candidates": [],
            "requested_contact_channel": "",
            "customer_questions": ["other"],
            "matched_rules": [],
            "confidence": 0.1,
        },
        customer_text="那個時候會不會冷啊",
        allowed_routes=set(ROUTES),
        allowed_slots={"party_size", "departure_window"},
        allowed_rule_ids=set(),
    )
    assert "weather" in parsed.customer_questions
    assert "current_conditions" not in parsed.semantic_signals
    assert "unresolved_direct_question" not in parsed.semantic_signals


def test_understanding_keeps_current_weather_behind_live_confirmation():
    parsed = parse_understanding(
        {
            "intent": "other",
            "route_candidate": "",
            "route_resolution": "none",
            "route_evidence": "",
            "slot_updates": {},
            "semantic_signals": [],
            "contact_candidates": [],
            "requested_contact_channel": "",
            "customer_questions": ["other"],
            "matched_rules": [],
            "confidence": 0.4,
        },
        customer_text="現在林芝氣溫多少？",
        allowed_routes=set(ROUTES),
        allowed_slots={"party_size", "departure_window"},
        allowed_rule_ids=set(),
    )
    assert "weather" in parsed.customer_questions
    assert "current_conditions" in parsed.semantic_signals


@pytest.mark.parametrize("text,size", [("9位", 9), ("9 人同行", 9), ("12人。", 12)])
def test_understanding_recovers_unambiguous_party_size_short_answer(text, size):
    parsed = parse_understanding(
        {
            "intent": "other",
            "route_candidate": "",
            "route_resolution": "none",
            "route_evidence": "",
            "slot_updates": {},
            "semantic_signals": [],
            "contact_candidates": [],
            "requested_contact_channel": "",
            "customer_questions": ["other"],
            "matched_rule_ids": [],
            "confidence": 0.3,
        },
        customer_text=text,
        allowed_routes=set(ROUTES),
        allowed_slots={"party_size"},
        allowed_rule_ids=set(),
    )
    assert parsed.slot_updates["party_size"].value == size


@pytest.mark.parametrize("text", ["9月", "9天", "9岁", "第9日"])
def test_understanding_does_not_treat_other_numbers_as_party_size(text):
    parsed = parse_understanding(
        {
            "intent": "other",
            "route_candidate": "",
            "route_resolution": "none",
            "route_evidence": "",
            "slot_updates": {},
            "semantic_signals": [],
            "contact_candidates": [],
            "requested_contact_channel": "",
            "customer_questions": ["other"],
            "matched_rule_ids": [],
            "confidence": 0.3,
        },
        customer_text=text,
        allowed_routes=set(ROUTES),
        allowed_slots={"party_size"},
        allowed_rule_ids=set(),
    )
    assert "party_size" not in parsed.slot_updates


@pytest.mark.parametrize("text", ["不知道", "还不确定呀", "時間還沒定"])
def test_understanding_recovers_explicit_departure_uncertainty(text):
    parsed = parse_understanding(
        {
            "intent": "other",
            "route_candidate": "",
            "route_resolution": "none",
            "route_evidence": "",
            "slot_updates": {},
            "semantic_signals": [],
            "contact_candidates": [],
            "requested_contact_channel": "",
            "customer_questions": ["other"],
            "matched_rule_ids": [],
            "confidence": 0.4,
        },
        customer_text=text,
        allowed_routes=set(ROUTES),
        allowed_slots={"departure_window"},
        allowed_rule_ids=set(),
        expected_slot="departure_window",
    )
    assert parsed.slot_updates["departure_window"].value == "未确定"
    assert parsed.slot_updates["departure_window"].evidence_quote in text
    assert "departure_undecided" in parsed.semantic_signals


def test_understanding_does_not_recover_uncertainty_without_departure_question():
    parsed = parse_understanding(
        {
            "intent": "other",
            "route_candidate": "",
            "route_resolution": "none",
            "route_evidence": "",
            "slot_updates": {},
            "semantic_signals": [],
            "contact_candidates": [],
            "requested_contact_channel": "",
            "customer_questions": ["other"],
            "matched_rule_ids": [],
            "confidence": 0.4,
        },
        customer_text="还不确定",
        allowed_routes=set(ROUTES),
        allowed_slots={"departure_window"},
        allowed_rule_ids=set(),
    )
    assert "departure_window" not in parsed.slot_updates
    assert "departure_undecided" not in parsed.semantic_signals


def test_understanding_normalizes_question_taxonomy_intent_aliases():
    parsed = parse_understanding(
        {
            "intent": "route_comparison",
            "route_candidate": "",
            "route_resolution": "comparison",
            "route_evidence": "",
            "slot_updates": {},
            "semantic_signals": [],
            "contact_candidates": [],
            "requested_contact_channel": "",
            "customer_questions": ["route_comparison"],
            "matched_rule_ids": [],
            "confidence": 0.9,
        },
        customer_text="桃花9日和桃花加珠峰11日",
        allowed_routes=set(ROUTES),
        allowed_slots={"party_size"},
        allowed_rule_ids=set(),
    )
    assert parsed.intent == "route_intro"
    assert parsed.customer_questions == ["route_comparison"]


def test_understanding_recovers_configured_route_alias_and_explicit_stop():
    parsed = parse_understanding(
        {
            "intent": "other",
            "route_candidate": "",
            "route_resolution": "none",
            "route_evidence": "",
            "slot_updates": {},
            "semantic_signals": [],
            "contact_candidates": [],
            "requested_contact_channel": "",
            "customer_questions": ["other"],
            "matched_rule_ids": [],
            "confidence": 0.7,
        },
        customer_text="桃花9D先不考虑了，不要再联系",
        allowed_routes=set(ROUTES),
        allowed_slots={"party_size"},
        allowed_rule_ids=set(),
    )
    assert parsed.route_candidate == "peach_9d_2027"
    assert parsed.route_resolution == "confirmed"
    assert "explicit_stop" in parsed.semantic_signals


def test_understanding_recovers_english_everest_route_alias():
    parsed = parse_understanding(
        {
            "intent": "itinerary",
            "route_candidate": "",
            "route_resolution": "none",
            "route_evidence": "",
            "slot_updates": {},
            "semantic_signals": [],
            "contact_candidates": [],
            "requested_contact_channel": "",
            "customer_questions": ["vehicle"],
            "matched_rule_ids": [],
            "confidence": 0.8,
        },
        customer_text="11D Everest 車上oxygen設備怎樣？",
        allowed_routes=set(ROUTES),
        allowed_slots={"party_size"},
        allowed_rule_ids=set(),
    )
    assert parsed.route_candidate == "peach_11d_2027"
    assert parsed.route_resolution == "confirmed"


def test_understanding_prioritizes_a_unique_configured_route_keyword():
    parsed = parse_understanding(
        {
            "intent": "itinerary",
            "route_candidate": "",
            "route_resolution": "none",
            "route_evidence": "",
            "slot_updates": {},
            "semantic_signals": [],
            "contact_candidates": [],
            "requested_contact_channel": "",
            "customer_questions": ["hotel"],
            "matched_rule_ids": [],
            "confidence": 0.7,
        },
        customer_text="我比較想看看絨布旅館的住宿。",
        allowed_routes=set(ROUTES),
        allowed_slots={"party_size"},
        allowed_rule_ids=set(),
    )
    assert parsed.route_candidate == "peach_11d_2027"
    assert parsed.route_resolution == "candidate"
    assert "configured_route_keyword_prioritized" in parsed.validation_flags


def test_understanding_uses_the_more_specific_keyword_for_a_negative_preference():
    parsed = parse_understanding(
        {
            "intent": "route_intro",
            "route_candidate": "",
            "route_resolution": "none",
            "route_evidence": "",
            "slot_updates": {},
            "semantic_signals": [],
            "contact_candidates": [],
            "requested_contact_channel": "",
            "customer_questions": ["itinerary"],
            "matched_rule_ids": [],
            "confidence": 0.7,
        },
        customer_text="這次不去珠峰，想安排輕鬆一點。",
        allowed_routes=set(ROUTES),
        allowed_slots={"party_size"},
        allowed_rule_ids=set(),
    )
    assert parsed.route_candidate == "peach_9d_2027"


def test_understanding_does_not_force_a_route_for_shared_keywords(monkeypatch):
    monkeypatch.setitem(ROUTES["peach_9d_2027"], "match_keywords", ["桃花"])
    monkeypatch.setitem(ROUTES["peach_11d_2027"], "match_keywords", ["桃花"])
    parsed = parse_understanding(
        {
            "intent": "route_intro",
            "route_candidate": "",
            "route_resolution": "none",
            "route_evidence": "",
            "slot_updates": {},
            "semantic_signals": [],
            "contact_candidates": [],
            "requested_contact_channel": "",
            "customer_questions": ["itinerary"],
            "matched_rule_ids": [],
            "confidence": 0.7,
        },
        customer_text="想看桃花。",
        allowed_routes=set(ROUTES),
        allowed_slots={"party_size"},
        allowed_rule_ids=set(),
    )
    assert parsed.route_candidate == ""
    assert parsed.route_resolution == "none"


def test_understanding_recovers_implicit_everest_route_choice():
    parsed = parse_understanding(
        {
            "intent": "route_intro",
            "route_candidate": "peach_11d_2027",
            "route_resolution": "confirmed",
            "route_evidence": "有珠峰",
            "slot_updates": {},
            "semantic_signals": [],
            "contact_candidates": [],
            "requested_contact_channel": "",
            "customer_questions": ["other"],
            "matched_rule_ids": [],
            "confidence": 0.8,
        },
        customer_text="我要有珠峰的那条",
        allowed_routes=set(ROUTES),
        allowed_slots={"party_size"},
        allowed_rule_ids=set(),
    )
    assert parsed.route_candidate == "peach_11d_2027"
    assert parsed.route_resolution == "confirmed"


def test_understanding_recovers_party_and_price_question_types():
    solo = parse_understanding(
        {
            "intent": "departure",
            "route_candidate": "peach_9d_2027",
            "route_resolution": "confirmed",
            "route_evidence": "桃花9日",
            "slot_updates": {"party_size": {"value": 1, "evidence_quote": "一个人"}},
            "semantic_signals": [],
            "contact_candidates": [],
            "requested_contact_channel": "",
            "customer_questions": ["departure"],
            "matched_rule_ids": [],
            "confidence": 0.8,
        },
        customer_text="我一个人可以参加桃花9日吗？",
        allowed_routes=set(ROUTES),
        allowed_slots={"party_size"},
        allowed_rule_ids=set(),
    )
    assert solo.customer_questions == ["party_size"]
    solo_plan = build_reply_plan(context(customer_text="我一个人可以参加桃花9日吗？"), solo)
    assert solo_plan.allowed_content_group_keys == ["party_intro_solo"]

    accepted = parse_understanding(
        {
            "intent": "other",
            "route_candidate": "",
            "route_resolution": "none",
            "route_evidence": "",
            "slot_updates": {},
            "semantic_signals": ["booking_intent"],
            "contact_candidates": [],
            "requested_contact_channel": "",
            "customer_questions": ["other"],
            "matched_rule_ids": [],
            "confidence": 0.8,
        },
        customer_text="价格可以接受，怎么继续？",
        allowed_routes=set(ROUTES),
        allowed_slots={"party_size"},
        allowed_rule_ids=set(),
    )
    assert "price" in accepted.customer_questions


def test_understanding_maps_meal_arrangement_to_price_inclusions():
    parsed = parse_understanding(
        {
            "intent": "itinerary",
            "route_candidate": "peach_9d_2027",
            "route_resolution": "confirmed",
            "route_evidence": "桃花9日",
            "slot_updates": {},
            "semantic_signals": [],
            "contact_candidates": [],
            "requested_contact_channel": "",
            "customer_questions": ["itinerary"],
            "matched_rule_ids": [],
            "confidence": 0.8,
        },
        customer_text="桃花9日吃饭安排怎么样？",
        allowed_routes=set(ROUTES),
        allowed_slots={"party_size"},
        allowed_rule_ids=set(),
    )
    assert parsed.customer_questions == ["price"]
    assert "price_inclusion_question_recovered" in parsed.validation_flags


def test_understanding_removes_route_candidate_with_mismatched_evidence():
    parsed = parse_understanding(
        {
            "intent": "itinerary",
            "route_candidate": "peach_11d_2027",
            "route_resolution": "confirmed",
            "route_evidence": "云南12日线路",
            "slot_updates": {},
            "semantic_signals": [],
            "contact_candidates": [],
            "requested_contact_channel": "",
            "customer_questions": ["itinerary"],
            "matched_rule_ids": [],
            "confidence": 0.9,
        },
        customer_text="我想看云南12日线路",
        allowed_routes=set(ROUTES),
        allowed_slots={"party_size"},
        allowed_rule_ids=set(),
    )
    assert parsed.route_candidate == ""
    assert parsed.route_resolution == "none"
    assert "route_candidate_with_mismatched_evidence_removed" in parsed.validation_flags

    plan = build_reply_plan(context(route_variant="peach_9d_2027"), parsed)
    assert plan.route_variant == "peach_9d_2027"


def test_outside_catalog_system_reply_binds_configured_route_overviews():
    understanding = CustomerUnderstanding(
        intent="itinerary",
        route_resolution="outside_catalog",
        customer_questions=["itinerary"],
        confidence=0.9,
    )
    plan = build_reply_plan(context(customer_text="想看云南11日"), understanding)
    generated = deterministic_system_reply(plan)
    assert generated is not None
    assert generated.used_fact_ids == plan.allowed_fact_ids
    assert len(generated.used_fact_ids) == len(ROUTES)
    assert "skip_silence_enrollment" in plan.safety_flags


def test_exclusive_outside_catalog_does_not_recommend_or_enroll_silence():
    understanding = CustomerUnderstanding(
        intent="itinerary",
        route_resolution="outside_catalog",
        semantic_signals=["outside_catalog_exclusive"],
        customer_questions=["itinerary"],
        confidence=0.95,
    )
    plan = build_reply_plan(context(customer_text="我只想去云南，不考虑西藏线路"), understanding)
    assert plan.route_variant == ""
    assert plan.reply_options == []
    assert plan.follow_up is None
    assert plan.allowed_fact_ids == []
    assert "skip_silence_enrollment" in plan.safety_flags
    generated = deterministic_system_reply(plan)
    assert generated is not None
    assert "暫時沒有完整資料" in generated.reply
    assert generated.used_fact_ids == []


def test_unselected_budget_comparison_exposes_both_published_prices_to_generator():
    understanding = CustomerUnderstanding(
        intent="price",
        route_resolution="comparison",
        customer_questions=["route_comparison", "price"],
        confidence=0.95,
    )
    plan = build_reply_plan(context(customer_text="预算每人一万，两条线路怎么选？"), understanding)
    assert {"route.9.price", "route.11.price"} <= set(plan.allowed_fact_ids)
    assert deterministic_system_reply(plan) is None


def test_unresolved_route_choice_uses_configured_system_copy_without_generation():
    understanding = CustomerUnderstanding(
        intent="route_intro",
        route_resolution="none",
        customer_questions=["itinerary"],
        confidence=0.8,
    )
    plan = build_reply_plan(context(customer_text="想了解旅游行程"), understanding)
    generated = deterministic_system_reply(plan)
    assert generated is not None
    assert generated.follow_up is not None
    assert generated.follow_up.type == "route_choice"
    assert "沒有您所問行程" not in generated.body
    assert "已上線" not in generated.reply
    assert "目前可接待" not in generated.reply
    assert "9日不上珠峰，11日會到珠峰大本營" in generated.reply
    assert generated.follow_up.question == "您想先看看哪一條呢？"
    assert generated.used_fact_ids == plan.allowed_fact_ids


def test_initial_only_route_copy_is_sent_exactly_without_model_rewrite():
    understanding = CustomerUnderstanding(
        intent="itinerary",
        route_candidate="peach_9d_2027",
        route_resolution="confirmed",
        route_evidence="桃花9日",
        customer_questions=["itinerary"],
    )
    source = context(
        customer_text="桃花9日",
        context_messages=[{"direction": "outgoing", "content": "您好，想先看哪一條行程呢？"}],
    )
    plan = build_reply_plan(source, understanding)
    assert plan.allowed_content_group_keys == ["brand_positioning"]
    generated = deterministic_system_reply(plan)
    assert generated is not None
    assert generated.body == ROUTES["peach_9d_2027"]["groups"]["brand_positioning"]["text"]
    assert "4至10人" in generated.body
    assert "成團" not in generated.body


def test_unselected_supported_routes_do_not_send_route_images():
    understanding = CustomerUnderstanding(
        intent="route_intro",
        route_resolution="none",
        customer_questions=["itinerary"],
        confidence=0.9,
    )
    overview_assets = [
        ROUTES[route]["groups"]["itinerary_overview"]["assets"][0]
        for route in ROUTES
    ]
    source = context(available_materials=[
        {"key": key, "routes": [route]}
        for route, key in zip(ROUTES, overview_assets)
    ] + [{"key": "not-an-overview", "routes": list(ROUTES)}])
    plan = build_reply_plan(source, understanding)
    assert plan.allowed_asset_ids == []
    generated = deterministic_system_reply(plan)
    assert generated is not None
    assert generated.asset_ids == []
    assert "行程總覽圖" not in generated.reply


def test_parser_preserves_fixture_for_verifier_unselected_reply_cannot_claim_an_unsent_attachment():
    understanding = CustomerUnderstanding(
        intent="route_intro",
        route_resolution="none",
        customer_questions=["itinerary"],
        confidence=0.9,
    )
    plan = build_reply_plan(context(), understanding)
    generated = parse_generated_reply(
            {
                "body": "兩條路線的差別在後段，我先把完整行程資料發您參考。",
                "used_fact_ids": plan.allowed_fact_ids,
                "asset_ids": [],
            },
            plan=plan,
            max_characters=200,
            max_images=2,
        )
    assert generated.body == "兩條路線的差別在後段，我先把完整行程資料發您參考。"


def test_parser_preserves_soft_close_for_model_verification():
    understanding = CustomerUnderstanding(
        intent="route_intro",
        route_resolution="none",
        customer_questions=["itinerary"],
        confidence=0.9,
    )
    plan = build_reply_plan(context(), understanding)
    generated = parse_generated_reply(
        {
            "body": "兩條路線主要差在珠峰段。您先慢慢看，之後想到什麼再跟我說～",
            "used_fact_ids": plan.allowed_fact_ids,
            "asset_ids": [],
        },
        plan=plan,
        max_characters=200,
        max_images=2,
    )
    assert generated.body == "兩條路線主要差在珠峰段。您先慢慢看，之後想到什麼再跟我說～"
    assert generated.follow_up.question == "您想先看看哪一條呢？"


def test_parser_preserves_contact_benefit_for_model_verification():
    understanding = CustomerUnderstanding(
        intent="route_intro",
        route_resolution="none",
        customer_questions=["itinerary"],
        confidence=0.9,
    )
    plan = replace(
        build_reply_plan(context(), understanding),
        follow_up=FollowUp(
            "contact",
            "line_or_wechat",
            "方便留一個 LINE 或微信 給我嗎？我把完整行程資料整理給您，之後想到什麼都可以直接問我。",
        ),
    )
    generated = parse_generated_reply(
        {
            "body": "兩條路線主要差在珠峰段。我把完整行程資料整理給您，之後想到什麼都可以直接問我。",
            "used_fact_ids": plan.allowed_fact_ids,
            "asset_ids": [],
        },
        plan=plan,
        max_characters=200,
        max_images=2,
    )
    assert generated.body == "兩條路線主要差在珠峰段。我把完整行程資料整理給您，之後想到什麼都可以直接問我。"
    assert generated.follow_up.question == plan.follow_up.question


def test_final_validator_removes_all_unselected_route_images():
    overview_assets = [
        ROUTES[route]["groups"]["itinerary_overview"]["assets"][0]
        for route in ROUTES
    ]
    decision = EvaluationDecision(
        action="reply",
        branch="unclassified",
        intent="route_intro",
        reply="先看兩條行程總覽。",
        route_variant="",
        material_keys=[*overview_assets, "not-an-overview"],
        reply_options=[ROUTES[route]["selection_title"] for route in ROUTES],
    )
    _, materials, group, flags = _validated_route_references(
        decision,
        {"available_materials": [{"key": key} for key in [*overview_assets, "not-an-overview"]]},
    )
    assert materials == []
    assert group == ""
    assert "unavailable_material_reference_removed" in flags


def test_custom_handoff_rule_uses_specialist_waiting_transition():
    source = context(customer_text="请帮我转人工")
    source["reception_policy"]["operator_configuration"]["business_rules"] = [{
        "id": "operator_human",
        "name": "客户要求真人",
        "enabled": True,
        "condition": "客户明确要求真人顾问",
        "action": "handoff",
        "guidance": "安排顾问跟进",
        "system_key": None,
    }]
    understanding = CustomerUnderstanding(
        intent="other",
        matched_rule_ids=["operator_human"],
        matched_rule_evidence={"operator_human": "转人工"},
        customer_questions=["other"],
        confidence=0.95,
    )
    plan = build_reply_plan(source, understanding)
    generated = deterministic_system_reply(plan)
    assert generated is not None
    assert "稍等我一下" in generated.reply
    assert "專人旅遊顧問" in generated.reply


def test_custom_rule_without_current_message_evidence_is_not_executed():
    source = context(customer_text="我先看看行程")
    source["reception_policy"]["operator_configuration"]["business_rules"] = [{
        "id": "operator_human",
        "name": "客户要求真人",
        "enabled": True,
        "condition": "客户明确要求真人顾问",
        "action": "handoff",
        "guidance": "安排顾问跟进",
        "system_key": None,
    }]
    plan = build_reply_plan(
        source,
        CustomerUnderstanding(
            intent="itinerary",
            matched_rule_ids=["operator_human"],
            customer_questions=["itinerary"],
            confidence=0.8,
        ),
    )
    assert plan.action == "reply"


def test_understanding_keeps_only_business_rules_with_verbatim_evidence():
    parsed = parse_understanding(
        {
            "intent": "other",
            "route_candidate": "",
            "route_resolution": "none",
            "route_evidence": "",
            "slot_updates": {},
            "semantic_signals": [],
            "contact_candidates": [],
            "requested_contact_channel": "",
            "customer_questions": ["other"],
            "matched_rules": [
                {"rule_id": "operator_human", "evidence_quote": "转人工"},
                {"rule_id": "unknown", "evidence_quote": "转人工"},
            ],
            "confidence": 0.9,
        },
        customer_text="请帮我转人工",
        allowed_routes=set(ROUTES),
        allowed_slots={"party_size"},
        allowed_rule_ids={"operator_human"},
    )
    assert parsed.matched_rule_ids == ["operator_human"]
    assert parsed.matched_rule_evidence == {"operator_human": "转人工"}


def test_direct_identity_question_uses_truthful_code_owned_reply():
    parsed = parse_understanding(
        {
            "intent": "other",
            "route_candidate": "",
            "route_resolution": "none",
            "route_evidence": "",
            "slot_updates": {},
            "semantic_signals": [],
            "contact_candidates": [],
            "requested_contact_channel": "",
            "customer_questions": ["other"],
            "matched_rules": [],
            "confidence": 0.9,
        },
        customer_text="你是机器人吗？",
        allowed_routes=set(ROUTES),
        allowed_slots={"party_size"},
        allowed_rule_ids=set(),
    )
    assert "identity_disclosure_question" in parsed.semantic_signals
    plan = build_reply_plan(context(customer_text="你是机器人吗？"), parsed)
    generated = deterministic_system_reply(plan)
    assert generated is not None
    assert "線上旅遊顧問" in generated.reply
    assert "自動接待協助回覆" in generated.reply
    assert "由真人接待" in generated.reply
    assert generated.asset_ids == []
    assert generated.follow_up is None


@pytest.mark.parametrize("body", [
    "我是AI客服，先为您介绍。",
    "目前可接待的已上线线路有两条。",
    "页面展示的酒店仅供参考。",
])
def test_parser_preserves_fixture_for_verifier_reply_generator_rejects_internal_or_machine_language(body):
    understanding = CustomerUnderstanding(
        intent="route_intro",
        route_resolution="none",
        customer_questions=["itinerary"],
        confidence=0.7,
    )
    plan = build_reply_plan(context(customer_text="想了解旅游行程"), understanding)
    generated = parse_generated_reply(
            {"body": body, "used_fact_ids": [], "asset_ids": []},
            plan=plan,
            max_characters=200,
            max_images=2,
        )
    assert generated.body == body


def test_unverified_pasted_itinerary_does_not_inherit_historical_route():
    understanding = CustomerUnderstanding(
        intent="itinerary",
        route_resolution="none",
        customer_questions=["itinerary"],
        confidence=0.8,
    )
    plan = build_reply_plan(
        context(
            route_variant="peach_9d_2027",
            customer_text="Day1 昆明\nDay2 大理\nDay3 丽江\nDay4 返程",
        ),
        understanding,
    )
    assert plan.route_variant == ""
    assert plan.follow_up is not None and plan.follow_up.type == "route_choice"
    assert "unverified_pasted_itinerary_clears_current_route" in plan.safety_flags


def test_effective_policy_is_split_into_minimum_runtime_views():
    source = policy()
    source["reply_style"]["max_characters"] = 173
    source["reply_style"]["max_images_per_turn"] = 4
    views = reception_policy_views(source)
    assert views["runtime_policy"]["reply_limits"]["max_characters"] == 173
    assert views["runtime_policy"]["reply_limits"]["max_images_per_turn"] == 4
    assert "silence" not in views["prompt_policy"]
    assert "handoff" not in views["prompt_policy"]
    assert "business_rules" in views["decision_policy"]


def test_large_group_handoff_is_decided_by_code():
    understanding = CustomerUnderstanding(
        intent="price",
        route_candidate="peach_11d_2027",
        route_resolution="confirmed",
        route_evidence="桃花加珠峰11日",
        slot_updates={"party_size": SlotUpdate(12, "12位")},
        customer_questions=["price", "hotel"],
        confidence=0.95,
    )
    plan = build_reply_plan(
        context(customer_text="桃花加珠峰11日，我们12位，价格和住宿怎样？"),
        understanding,
    )
    assert plan.action == "handoff"
    assert plan.handoff_reason == "large_group_custom_quote"
    assert plan.next_stage == "handoff"
    assert plan.lead_action == "none"
    assert plan.allowed_content_group_keys == ["hotel_reference"]
    assert plan.allowed_fact_ids
    assert not any("price" in fact_id for fact_id in plan.allowed_fact_ids)
    assert plan.follow_up is None
    generated = deterministic_system_reply(plan)
    assert generated is None


@pytest.mark.parametrize(
    ("party_size", "expected_action", "expected_range_flag"),
    [
        (7, "reply", False),
        (8, "handoff", False),
        ("1-2", "reply", False),
        ("6-10", "handoff", True),
    ],
)
def test_large_group_threshold_uses_effective_policy_for_exact_and_range_values(
    party_size, expected_action, expected_range_flag,
):
    source = context(customer_text=f"我们大概{party_size}位", route_variant="peach_11d_2027")
    source["reception_policy"]["handoff"]["large_group"]["minimum_party_size"] = 8
    plan = build_reply_plan(
        source,
        CustomerUnderstanding(
            intent="other",
            slot_updates={"party_size": SlotUpdate(party_size, str(party_size))},
            customer_questions=["other"],
            confidence=0.9,
        ),
    )
    assert plan.action == expected_action
    assert ("party_size_range_reaches_handoff_threshold" in plan.safety_flags) is expected_range_flag


def test_large_group_handoff_selects_unsent_hotel_assets_for_customer_waiting():
    route = "peach_11d_2027"
    hotel_assets = ROUTES[route]["groups"]["hotel_reference"]["assets"]
    route_assets = list(dict.fromkeys(
        key
        for group in ROUTES[route]["groups"].values()
        for key in group.get("assets", [])
    ))
    understanding = CustomerUnderstanding(
        intent="other",
        slot_updates={"party_size": SlotUpdate(9, "9位")},
        customer_questions=["other"],
        confidence=0.9,
    )
    source = context(
        customer_text="9位",
        route_variant=route,
        journey={"sent_content_groups": ["itinerary_overview"], "customer_profile": {}},
        available_materials=[{"key": key, "routes": [route]} for key in route_assets],
    )
    source["reception_policy"]["handoff"]["large_group"]["minimum_party_size"] = 8
    plan = build_reply_plan(source, understanding)
    assert plan.action == "handoff"
    assert plan.allowed_content_group_keys == ["hotel_reference"]
    assert plan.allowed_asset_ids == hotel_assets
    generated = parse_generated_reply(
        {
                "body": "9位屬於團體旅遊，請稍等一下，我這邊安排專人旅遊顧問接著服務。您可以先看看這組飯店客房與供氧設備照片。",
            "used_fact_ids": plan.allowed_fact_ids,
            "asset_ids": [],
        },
        plan=plan,
        max_characters=200,
        max_images=2,
    )
    assert generated.asset_ids == hotel_assets[:1]


def test_parser_preserves_fixture_for_verifier_sales_handoff_cannot_send_an_unexplained_image():
    route = "peach_11d_2027"
    hotel_assets = ROUTES[route]["groups"]["hotel_reference"]["assets"]
    source = context(
        customer_text="我们8位，想看桃花加珠峰11日",
        route_variant=route,
        available_materials=[{"key": key, "routes": [route]} for key in hotel_assets],
    )
    source["reception_policy"]["handoff"]["large_group"]["minimum_party_size"] = 8
    plan = build_reply_plan(
        source,
        CustomerUnderstanding(
            intent="other",
            slot_updates={"party_size": SlotUpdate(8, "8位")},
            customer_questions=["other"],
            confidence=0.9,
        ),
    )

    generated = parse_generated_reply(
            {
                "body": "收到，請稍等一下，我這邊安排專人旅遊顧問接著服務。",
                "used_fact_ids": plan.allowed_fact_ids,
                "asset_ids": [],
            },
            plan=plan,
            max_characters=200,
            max_images=2,
        )
    assert generated.body == "收到，請稍等一下，我這邊安排專人旅遊顧問接著服務。"


def test_parser_preserves_fixture_for_verifier_sales_handoff_missing_image_explanation_requires_repair():
    route = "peach_11d_2027"
    route_assets = list(dict.fromkeys(
        key
        for group in ROUTES[route]["groups"].values()
        for key in group.get("assets", [])
    ))
    source = context(
        customer_text="我要真人顾问",
        route_variant=route,
        available_materials=[{"key": key, "routes": [route]} for key in route_assets],
    )
    plan = build_reply_plan(
        source,
        CustomerUnderstanding(
            intent="other",
            route_candidate=route,
            route_resolution="confirmed",
            route_evidence="桃花加珠峰11日",
            semantic_signals=["explicit_human_request"],
            confidence=0.9,
        ),
    )
    assert plan.allowed_asset_ids
    selected_asset = plan.allowed_asset_ids[0]
    generated = parse_generated_reply(
        {
            "body": "收到，請稍等一下，我這邊安排專人旅遊顧問接著服務。",
            "used_fact_ids": plan.allowed_fact_ids,
            "asset_ids": [],
        },
        plan=plan,
        max_characters=200,
        max_images=2,
        approved_asset_captions={
            selected_asset: "我先把行程圖發您看～圖裡能看到每天路線和主要停留點，方便先了解整體走法。",
        },
    )
    assert generated.body == "收到，請稍等一下，我這邊安排專人旅遊顧問接著服務。"


def test_parser_preserves_fixture_for_verifier_reviewed_asset_caption_cannot_replace_customer_answer():
    route = "peach_11d_2027"
    asset = ROUTES[route]["groups"]["accommodation_summary"]["assets"][0]
    plan = build_reply_plan(
        context(
            customer_text="我想看看住宿",
            route_variant=route,
            journey={"sent_content_groups": ["hotel_reference"], "customer_profile": {}},
            available_materials=[{"key": asset, "routes": [route]}],
        ),
        CustomerUnderstanding(
            intent="hotel",
            customer_questions=["hotel"],
            confidence=0.9,
        ),
    )
    plan = replace(plan, allowed_asset_ids=[asset], follow_up=None)
    generated = parse_generated_reply(
        {"body": "住宿環境可以從實際畫面直接了解。", "used_fact_ids": plan.allowed_fact_ids, "asset_ids": []},
        plan=plan,
        max_characters=200,
        max_images=2,
        approved_asset_captions={
            asset: "再給您一張住宿環境～從這個角度能看到房間整體和活動空間，方便轉給同行者一起看。",
        },
    )
    assert generated.body == "住宿環境可以從實際畫面直接了解。"


@pytest.mark.parametrize(
    ("body", "caption"),
    [
        (
            "我先把希爾頓客房發您看～這張能直接看到房間空間、床鋪和休息區域。"
            "住宿這部分也幫您看一下～我先把希爾頓客房發您，可以直接看到房間空間、床鋪和休息區域。",
            "我先把希爾頓客房發您看～這張能直接看到房間空間、床鋪和休息區域，住宿環境不用只聽文字介紹。",
        ),
        (
            "這張是珠峰段的絨布旅館～房間配有獨立衛浴和供氧設備，您可以先看清楚珠峰當晚的實際住宿環境。"
            "珠峰段這晚安排入住絨布旅館，房間有獨立衛浴和供氧設備，可以再看看實際住宿環境。",
            "這張是珠峰段的絨布旅館～房間配有獨立衛浴和供氧設備，出發前先看清楚珠峰當晚的實際住宿環境。",
        ),
    ],
)
def test_parser_preserves_fixture_for_verifier_selected_asset_cannot_be_described_twice_in_one_reply(body, caption):
    route = "peach_11d_2027"
    asset = ROUTES[route]["groups"]["hotel_reference"]["assets"][0]
    plan = build_reply_plan(
        context(
            customer_text="我想看看住宿",
            route_variant=route,
            available_materials=[{"key": asset, "routes": [route]}],
        ),
        CustomerUnderstanding(intent="hotel", customer_questions=["hotel"], confidence=0.9),
    )
    plan = replace(plan, allowed_asset_ids=[asset], follow_up=None)
    generated = parse_generated_reply(
            {"body": body, "used_fact_ids": plan.allowed_fact_ids, "asset_ids": [asset]},
            plan=plan,
            max_characters=400,
            max_images=2,
            approved_asset_captions={asset: caption},
        )
    assert generated.body == body


def test_parser_preserves_fixture_for_verifier_overlapping_asset_copy_without_image_word_requires_repair():
    route = "peach_11d_2027"
    asset = ROUTES[route]["groups"]["rongbuk_reference"]["assets"][0]
    plan = build_reply_plan(
        context(
            customer_text="珠峰住宿我先看看",
            route_variant=route,
            available_materials=[{"key": asset, "routes": [route]}],
        ),
        CustomerUnderstanding(intent="hotel", customer_questions=["hotel"], confidence=0.9),
    )
    plan = replace(plan, allowed_asset_ids=[asset], follow_up=None)
    caption = "這張是珠峰段入住的絨布旅館，房間實況可以先看一下。"
    generated = parse_generated_reply(
        {
            "body": "珠峰段入住絨布旅館，房間實況可以先確認。",
            "used_fact_ids": plan.allowed_fact_ids,
            "asset_ids": [asset],
        },
        plan=plan,
        max_characters=200,
        max_images=2,
        approved_asset_captions={asset: caption},
    )
    assert generated.body == "珠峰段入住絨布旅館，房間實況可以先確認。"


def test_parser_preserves_handoff_promise_for_model_verification():
    understanding = CustomerUnderstanding(
        intent="price",
        route_candidate="peach_11d_2027",
        route_resolution="confirmed",
        route_evidence="桃花加珠峰11日",
        slot_updates={"party_size": SlotUpdate(8, "8位")},
        customer_questions=["price"],
        confidence=0.95,
    )
    source = context(customer_text="桃花加珠峰11日，我们8位")
    source["reception_policy"]["handoff"]["large_group"]["minimum_party_size"] = 8
    plan = build_reply_plan(source, understanding)
    generated = parse_generated_reply(
        {"body": "8位沒問題，請稍等一下，我這邊安排專人旅遊顧問接著服務。", "used_fact_ids": [], "asset_ids": []},
        plan=plan, max_characters=200, max_images=2,
    )
    assert generated.body == "8位沒問題，請稍等一下，我這邊安排專人旅遊顧問接著服務。"
    assert "專人旅遊顧問" in generated.body


def test_first_three_route_turns_each_plan_a_fresh_visual_group():
    route = "peach_11d_2027"
    materials = [
        {"key": key, "routes": [route]}
        for group in ROUTES[route]["groups"].values()
        for key in group.get("assets", [])
    ]
    second_turn = build_reply_plan(
        context(
            customer_text="2位",
            route_variant=route,
            journey={
                "sent_content_groups": ["itinerary_overview", "party_question"],
                "customer_profile": {},
            },
            available_materials=materials,
        ),
        CustomerUnderstanding(
            intent="other",
            slot_updates={"party_size": SlotUpdate(2, "2位")},
            customer_questions=["other"],
            confidence=0.9,
        ),
    )
    assert second_turn.allowed_content_group_keys == ["party_intro_small", "peach_highlights"]
    assert second_turn.allowed_asset_ids
    assert second_turn.follow_up and second_turn.follow_up.field == "departure_window"

    third_turn = build_reply_plan(
        context(
            customer_text="还不确定呀",
            route_variant=route,
            journey={
                "sent_content_groups": [
                    "itinerary_overview",
                    "party_question",
                    "peach_highlights",
                    "departure_question",
                    "rongbuk_reference",
                ],
                "customer_profile": {"party_size": 2},
            },
            available_materials=materials,
        ),
        CustomerUnderstanding(
            intent="other",
            slot_updates={"departure_window": SlotUpdate("未确定", "还不确定呀")},
            semantic_signals=["departure_undecided"],
            customer_questions=["other"],
            confidence=0.9,
        ),
    )
    assert third_turn.allowed_content_group_keys == ["hotel_reference"]
    assert third_turn.allowed_asset_ids
    assert third_turn.follow_up and third_turn.follow_up.type == "contact"
    assert "完整行程" in third_turn.follow_up.question
    assert "什麼時候出發" not in third_turn.follow_up.question
    assert third_turn.next_stage == "contact_requested"


def test_customer_considering_with_family_gets_value_without_requirement_question():
    route = "peach_11d_2027"
    materials = [
        {"key": key, "routes": [route]}
        for group in ROUTES[route]["groups"].values()
        for key in group.get("assets", [])
    ]
    plan = build_reply_plan(
        context(
            customer_text="我先跟家人讨论一下，你先发重点给我。",
            available_materials=materials,
        ),
        CustomerUnderstanding(
            intent="route_intro",
            route_candidate=route,
            route_resolution="confirmed",
            route_evidence="桃花加珠峰11日",
            semantic_signals=["considering"],
            customer_questions=["other"],
            confidence=0.95,
        ),
    )
    assert plan.next_stage == "considering"
    assert plan.follow_up is None
    assert plan.allowed_content_group_keys == ["advisor_greeting"]
    assert plan.allowed_asset_ids == []


def test_reply_generator_binds_planned_visual_when_model_omits_asset():
    route = "peach_11d_2027"
    asset = ROUTES[route]["groups"]["rongbuk_reference"]["assets"][0]
    plan = build_reply_plan(
        context(
            customer_text="2位",
            route_variant=route,
            journey={
                "sent_content_groups": ["itinerary_overview", "party_question"],
                "customer_profile": {},
            },
            available_materials=[{"key": asset, "routes": [route]}],
        ),
        CustomerUnderstanding(
            intent="other",
            slot_updates={"party_size": SlotUpdate(2, "2位")},
            customer_questions=["other"],
            confidence=0.9,
        ),
    )
    generated = parse_generated_reply(
        {
                "body": "了解，我先按兩位同行記錄；我把絨布旅館照片發您看，房間有獨立衛浴和供氧設備。",
            "used_fact_ids": plan.allowed_fact_ids,
            "asset_ids": [],
        },
        plan=plan,
        max_characters=200,
        max_images=2,
    )
    assert generated.asset_ids == [asset]


@pytest.mark.parametrize("reason", ["refund", "contract_dispute", "complaint", "attachment_requires_vision"])
def test_risk_handoff_never_adds_sales_content(reason):
    understanding = CustomerUnderstanding(
        intent="complaint",
        route_candidate="peach_11d_2027",
        route_resolution="confirmed",
        route_evidence="桃花加珠峰11日",
        semantic_signals=[reason],
        customer_questions=["hotel"],
        confidence=0.95,
    )
    plan = build_reply_plan(context(customer_text="需要人工处理"), understanding)
    assert plan.action == "handoff"
    assert plan.allowed_fact_ids == []
    assert plan.allowed_asset_ids == []
    generated = deterministic_system_reply(plan)
    assert generated is not None
    assert generated.asset_ids == []


def test_contact_candidate_requires_deterministic_format_validation():
    understanding = CustomerUnderstanding(
        intent="contact",
        route_candidate="peach_9d_2027",
        route_resolution="confirmed",
        route_evidence="桃花9日",
        contact_candidates=[ContactCandidate("line", "LINE", "LINE")],
        customer_questions=["contact"],
        confidence=0.9,
    )
    plan = build_reply_plan(context(customer_text="桃花9日，可以用 LINE 联系吗？"), understanding)
    assert plan.action == "reply"
    assert plan.contact_values == {}
    assert plan.lead_action != "captured"


def test_valid_contact_is_deterministically_captured_and_handed_off():
    understanding = CustomerUnderstanding(
        intent="contact",
        route_candidate="peach_9d_2027",
        route_resolution="confirmed",
        route_evidence="桃花9日",
        contact_candidates=[ContactCandidate("line", "travel_2027", "travel_2027")],
        customer_questions=["contact"],
        confidence=0.9,
    )
    plan = build_reply_plan(context(customer_text="桃花9日，我的 LINE 是 travel_2027"), understanding)
    assert plan.action == "handoff"
    assert plan.handoff_reason == "lead_captured"
    assert plan.lead_action == "captured"
    assert plan.contact_values == {"line": "travel_2027"}
    generated = parse_generated_reply(
        {
            "body": "收到您的聯絡方式，我這邊安排專人旅遊顧問接著服務桃花9日行程。",
            "used_fact_ids": plan.allowed_fact_ids,
            "asset_ids": [],
        },
        plan=plan,
        max_characters=200,
        max_images=2,
    )
    assert generated.body == "收到您的聯絡方式，我這邊安排專人旅遊顧問接著服務桃花9日行程。"
    assert "專人旅遊顧問" in generated.reply
    assert generated.reply.count("專人旅遊顧問") == 1


def test_parser_preserves_contact_body_without_deleting_it():
    route = "peach_9d_2027"
    plan = replace(
        build_reply_plan(
            context(customer_text="詳細資料怎麼拿", route_variant=route),
            CustomerUnderstanding(intent="contact", customer_questions=["contact"], confidence=0.9),
        ),
        follow_up=FollowUp(
            "contact",
            "line",
            "如果您想收完整行程，可以把 LINE 留給我，我整理好再傳給您。",
        ),
    )
    generated = parse_generated_reply(
        {
            "body": "完整行程可以先整理給您，之後有問題也能直接聯絡。",
            "used_fact_ids": plan.allowed_fact_ids,
            "asset_ids": [],
        },
        plan=plan,
        max_characters=200,
        max_images=2,
    )
    assert generated.body == "完整行程可以先整理給您，之後有問題也能直接聯絡。"
    assert generated.follow_up.question == plan.follow_up.question
    assert "行程圖" not in generated.reply
    assert generated.asset_ids == []


def test_explicit_labeled_contact_is_recovered_when_model_omits_candidate():
    parsed = parse_understanding(
        {
            "intent": "contact",
            "route_candidate": "",
            "route_resolution": "none",
            "route_evidence": "",
            "slot_updates": {},
            "semantic_signals": ["asks_contact_channel"],
            "contact_candidates": [],
            "requested_contact_channel": "line",
            "customer_questions": ["contact"],
            "matched_rule_ids": [],
            "confidence": 0.9,
        },
        customer_text="Line: travel_2027",
        allowed_routes=set(ROUTES),
        allowed_slots={"party_size"},
        allowed_rule_ids=set(),
    )
    assert [(item.channel, item.value) for item in parsed.contact_candidates] == [
        ("line", "travel_2027")
    ]
    assert "explicit_contact_syntax_recovered" in parsed.validation_flags


def test_contact_channel_question_does_not_trigger_route_or_slot_question():
    understanding = CustomerUnderstanding(
        intent="contact",
        semantic_signals=["asks_contact_channel"],
        requested_contact_channel="wechat",
        customer_questions=["contact"],
        confidence=0.9,
    )
    plan = build_reply_plan(context(customer_text="可以用微信联系吗？"), understanding)
    assert plan.action == "reply"
    assert plan.lead_action == "ask"
    assert plan.next_stage == "contact_requested"
    assert plan.follow_up.field == "wechat"
    assert plan.reply_options == []
    assert plan.allowed_fact_ids == []
    generated = deterministic_system_reply(plan)
    assert generated.follow_up.field == "wechat"
    assert generated.reply.count("？") == 1


def test_outside_catalog_boundary_respects_no_recommendation_policy():
    understanding = CustomerUnderstanding(
        intent="other",
        route_resolution="outside_catalog",
        customer_questions=["itinerary"],
        confidence=0.9,
    )
    custom_context = context(customer_text="云南12日怎么走？")
    custom_context["reception_policy"]["route_switch"]["outside_catalog_action"] = "explain_boundary_only"
    plan = build_reply_plan(custom_context, understanding)
    assert plan.route_variant == ""
    assert plan.reply_options == []
    assert plan.follow_up is None
    assert plan.allowed_fact_ids == []


def test_reply_generator_uses_one_structured_followup_and_code_caps_assets():
    understanding = CustomerUnderstanding(
        intent="itinerary",
        route_candidate="peach_9d_2027",
        route_resolution="confirmed",
        route_evidence="桃花9日",
        customer_questions=["itinerary"],
        confidence=0.7,
    )
    plan = build_reply_plan(
        context(
            route_variant="peach_9d_2027",
            journey={"sent_content_groups": ["advisor_greeting", "brand_positioning"]},
            available_materials=[{
                "key": "routes12-9d-itinerary",
                "routes": ["peach_9d_2027"],
            }],
        ),
        understanding,
    )
    generated = parse_generated_reply(
        {
            "body": "我把9日行程圖發您看；行程從林芝較低海拔開始，不走珠峰。",
            "follow_up": {
                "type": "slot",
                "field": "party_size",
                "question": "请问预计几位同行？",
            },
            "used_fact_ids": ["route.9.overview"],
            "asset_ids": ["routes12-9d-itinerary", "unknown", "routes12-9d-itinerary"],
        },
        plan=plan,
        max_characters=200,
        max_images=1,
    )
    assert generated.follow_up.field == "party_size"
    assert generated.asset_ids == ["routes12-9d-itinerary"]
    assert generated.reply.count("？") == 1


def test_parser_preserves_hidden_question_for_model_verification():
    understanding = CustomerUnderstanding(
        intent="itinerary",
        route_candidate="peach_9d_2027",
        route_resolution="confirmed",
        route_evidence="桃花9日",
        customer_questions=["itinerary"],
    )
    plan = build_reply_plan(context(), understanding)
    generated = parse_generated_reply(
        {
            "body": "9日线路不上珠峰。您几位同行？",
            "follow_up": {
                "type": "slot",
                "field": "party_size",
                "question": "请问预计几位同行？",
            },
            "used_fact_ids": ["route.9.overview"],
            "asset_ids": [],
        },
        plan=plan,
        max_characters=200,
        max_images=2,
    )
    assert generated.body == "9日线路不上珠峰。您几位同行？"
    assert generated.follow_up.question == plan.follow_up.question


def test_parser_preserves_newline_question_for_model_verification():
    understanding = CustomerUnderstanding(
        intent="itinerary",
        route_candidate="peach_9d_2027",
        route_resolution="confirmed",
        route_evidence="桃花9日",
        customer_questions=["itinerary"],
    )
    plan = build_reply_plan(context(), understanding)
    generated = parse_generated_reply(
        {
            "body": "9日线路不上珠峰。\n您预计几位同行？方便的话",
            "follow_up": {
                "type": "slot",
                "field": "party_size",
                "question": "请问预计几位同行？",
            },
            "used_fact_ids": ["route.9.overview"],
            "asset_ids": [],
        },
        plan=plan,
        max_characters=200,
        max_images=2,
    )
    assert generated.body == "9日线路不上珠峰。\n您预计几位同行？方便的话"
    assert generated.follow_up.question == plan.follow_up.question


def test_parser_preserves_unpunctuated_request_for_model_verification():
    understanding = CustomerUnderstanding(
        intent="price",
        route_candidate="peach_11d_2027",
        route_resolution="confirmed",
        route_evidence="桃花加珠峰11日",
        customer_questions=["price"],
    )
    rich = context(
        memory={"party_size": {"value": 2}, "departure_window": {"value": "3月底"}},
        journey={
            "sent_content_groups": ["itinerary_overview", "hotel_reference", "price_reference"],
            "customer_profile": {"party_size": 2, "departure_window": "3月底"},
        },
    )
    plan = build_reply_plan(rich, understanding)
    assert plan.follow_up and plan.follow_up.type == "contact"
    generated = parse_generated_reply(
        {
            "body": "珠峰段入住絨布旅館。方便的話，可以加您LINE，我把住宿照片發給您參考。",
            "follow_up": {
                "type": "contact",
                "field": plan.follow_up.field,
                "question": "方便提供您的LINE吗？",
            },
            "used_fact_ids": ["route.11.price"],
            "asset_ids": [],
        },
        plan=plan,
        max_characters=200,
        max_images=2,
    )
    assert generated.body == "珠峰段入住絨布旅館。方便的話，可以加您LINE，我把住宿照片發給您參考。"
    assert generated.follow_up.question == plan.follow_up.question


def test_parser_preserves_unplanned_question_for_model_verification():
    understanding = CustomerUnderstanding(
        intent="other",
        route_candidate="peach_9d_2027",
        route_resolution="confirmed",
        route_evidence="桃花9日",
        customer_questions=[],
    )
    plan = replace(
        build_reply_plan(context(), understanding),
        follow_up=None,
        allowed_fact_ids=["service.requirements"],
    )
    generated = parse_generated_reply(
        {
            "body": "入藏證件需要按客戶情況確認。您更關注手續還是行程？您只需把人數告訴我。我可以先說明現有資料。",
            "follow_up": None,
            "used_fact_ids": ["service.requirements"],
            "asset_ids": [],
        },
        plan=plan,
        max_characters=200,
        max_images=2,
    )
    assert generated.body == "入藏證件需要按客戶情況確認。您更關注手續還是行程？您只需把人數告訴我。我可以先說明現有資料。"
    assert generated.follow_up is None


@pytest.mark.parametrize("claim", [
        "房間配有獨立衛浴和供氧設備，環境更安心。",
    "房間配有獨立衛浴和供氧設備，家人可以先放心。",
    "房間配有獨立衛浴和供氧設備，心裡會踏實些。",
    "出發前先確認住宿配置會更安心。",
    "這條路線從林芝低海拔入藏，適合第一次進藏。",
    "2位很適合我們的精緻小團，走起來輕鬆。",
    "這樣的人數很適合我們小團，走起來也輕鬆。",
])
def test_parser_preserves_fixture_for_verifier_reply_generator_rejects_instead_of_deleting_unsupported_facility_benefit(claim):
    understanding = CustomerUnderstanding(
        intent="hotel",
        route_candidate="peach_11d_2027",
        route_resolution="confirmed",
        route_evidence="桃花加珠峰11日",
        customer_questions=["hotel"],
    )
    plan = replace(build_reply_plan(context(), understanding), follow_up=None)
    generated = parse_generated_reply(
            {"body": claim, "used_fact_ids": ["route.11.rongbuk_hotel"], "asset_ids": []},
            plan=plan, max_characters=200, max_images=2,
        )
    assert generated.body == claim


def test_parser_preserves_fixture_for_verifier_reply_generator_rejects_obvious_simplified_chinese_mixing():
    understanding = CustomerUnderstanding(
        intent="itinerary",
        route_candidate="peach_9d_2027",
        route_resolution="confirmed",
        route_evidence="桃花9日",
        customer_questions=["itinerary"],
    )
    plan = replace(build_reply_plan(context(), understanding), follow_up=None)
    generated = parse_generated_reply(
            {
                "body": "我先把9日行程图发您看，这条线路每天怎么走都标在里面。",
                "used_fact_ids": ["route.9.overview"],
                "asset_ids": [],
            },
            plan=plan,
            max_characters=200,
            max_images=2,
        )
    assert generated.body == "我先把9日行程图发您看，这条线路每天怎么走都标在里面。"


def test_parser_preserves_fixture_for_verifier_reply_generator_does_not_inject_caption_from_metadata():
    understanding = CustomerUnderstanding(
        intent="hotel",
        route_candidate="peach_11d_2027",
        route_resolution="confirmed",
        route_evidence="桃花加珠峰11日",
        customer_questions=["hotel"],
    )
    asset = ROUTES["peach_11d_2027"]["groups"]["hotel_reference"]["assets"][0]
    plan = replace(
        build_reply_plan(context(), understanding),
        follow_up=None,
        allowed_asset_ids=[asset],
    )
    generated = parse_generated_reply(
            {
                "body": "住宿條件我這邊已經幫您整理好了。",
                "used_fact_ids": plan.allowed_fact_ids,
                "asset_ids": [],
            },
            plan=plan,
            max_characters=200,
            max_images=2,
            approved_asset_captions={
                asset: "我先把酒店照片发您看，这里可以看到房间。",
            },
        )
    assert generated.body == "住宿條件我這邊已經幫您整理好了。"


def test_reply_generator_rejects_long_body_instead_of_dropping_answer():
    understanding = CustomerUnderstanding(
        intent="itinerary",
        route_candidate="peach_9d_2027",
        route_resolution="confirmed",
        route_evidence="桃花9日",
        customer_questions=["itinerary"],
    )
    plan = build_reply_plan(context(), understanding)
    error = pytest.raises(ValueError, parse_generated_reply,
        {
            "body": "第一段說明。" + "第二段很長的說明。" * 30,
            "used_fact_ids": ["route.9.overview"],
            "asset_ids": [],
        },
        plan=plan,
        max_characters=80,
        max_images=2,
    )
    assert str(error.value).startswith("reply_too_long_after_code_limit:body=")
    assert ",budget=" in str(error.value)


def test_reply_generator_allows_nonfactual_acknowledgement_without_fact_reference():
    understanding = CustomerUnderstanding(
        intent="other",
        route_candidate="peach_9d_2027",
        route_resolution="confirmed",
        route_evidence="桃花9日",
        slot_updates={"party_size": SlotUpdate(2, "二個人")},
        customer_questions=["other"],
    )
    plan = build_reply_plan(
        context(customer_text="大概一個人或二個人"),
        understanding,
    )
    assert plan.allowed_fact_ids
    generated = parse_generated_reply(
        {
            "body": "了解，我先按一至兩位同行記錄。",
            "used_fact_ids": [],
            "asset_ids": [],
        },
        plan=replace(plan, follow_up=None),
        max_characters=200,
        max_images=2,
    )
    assert generated.used_fact_ids == []


def test_availability_question_is_code_planned_as_direct_answer_without_followup():
    understanding = CustomerUnderstanding(
        intent="departure",
        customer_questions=["availability"],
        confidence=0.9,
    )
    plan = build_reply_plan(
        context(
            customer_text="如果該日期只有我們三人報名就不會出團是嗎",
            route_variant="peach_11d_2027",
        ),
        understanding,
    )
    assert plan.route_variant == "peach_11d_2027"
    assert plan.follow_up is None
    assert "service.availability" in plan.allowed_fact_ids
    assert "availability_confirmation_required" in plan.safety_flags
    assert "不要承諾一定成團" in plan.reply_goal
    generated = deterministic_system_reply(plan)
    assert generated is not None
    assert generated.used_fact_ids == ["service.availability"]
    assert "依出發日再確認" in generated.reply
    assert "不會" not in generated.reply


def test_personal_health_suitability_is_code_owned_and_not_inferred_from_facilities():
    route = "peach_11d_2027"
    available_materials = [
        {"key": key, "routes": [route]}
        for group in ROUTES[route]["groups"].values()
        for key in group.get("assets", [])
    ]
    understanding = CustomerUnderstanding(
        intent="other",
        semantic_signals=["has_objection", "personal_health_suitability"],
        customer_questions=["altitude"],
        confidence=0.95,
    )
    plan = build_reply_plan(
        context(
            customer_text="上合歡山會高反，適合去西藏嗎",
            route_variant=route,
            available_materials=available_materials,
        ),
        understanding,
    )
    assert plan.follow_up is None
    assert plan.allowed_fact_ids == ["service.safety"]
    assert "health_confirmation_required" in plan.safety_flags
    assert "skip_silence_enrollment" in plan.safety_flags
    generated = deterministic_system_reply(plan)
    assert generated is not None
    assert "醫師" in generated.reply and "評估" in generated.reply
    assert "一次高反經驗" not in generated.reply
    assert "適合" not in generated.reply.replace("是否適合", "")


def test_personal_health_question_still_answers_requested_hotel_facts_and_allows_image():
    route = "peach_11d_2027"
    hotel_assets = ROUTES[route]["groups"]["hotel_reference"]["assets"]
    understanding = CustomerUnderstanding(
        intent="itinerary",
        semantic_signals=["has_objection", "personal_health_suitability"],
        customer_questions=["hotel", "altitude"],
        confidence=0.95,
    )
    plan = build_reply_plan(
        context(
            customer_text="带65岁的父母去，担心高反，酒店条件怎么样？",
            route_variant=route,
            available_materials=[{"key": key, "routes": [route]} for key in hotel_assets],
        ),
        understanding,
    )
    assert plan.follow_up is None
    assert "service.safety" in plan.allowed_fact_ids
    assert any("hotel" in fact_id for fact_id in plan.allowed_fact_ids)
    assert plan.allowed_content_group_keys == ["hotel_reference"]
    assert plan.allowed_asset_ids
    assert "skip_silence_enrollment" in plan.safety_flags
    assert deterministic_system_reply(plan) is None


def test_current_conditions_boundary_keeps_requested_contact_channel_followup():
    understanding = CustomerUnderstanding(
        intent="contact",
        semantic_signals=["has_objection", "current_conditions", "asks_contact_channel"],
        requested_contact_channel="wechat",
        customer_questions=["other", "contact"],
        confidence=0.9,
    )
    plan = build_reply_plan(
        context(
            customer_text="最近西藏的情況會影響景點嗎？可以用微信聯繫嗎？",
            route_variant="peach_11d_2027",
        ),
        understanding,
    )
    assert plan.follow_up is not None and plan.follow_up.field == "wechat"
    assert plan.allowed_fact_ids == ["service.current_conditions"]
    assert "current_conditions_confirmation_required" in plan.safety_flags
    generated = deterministic_system_reply(plan)
    assert generated is not None
    assert "等出發日期確定一些時" in generated.reply
    assert generated.reply.count("可以") == 1
    assert "把您的微信帳號或連結留給我" in generated.reply
    assert generated.reply.count("？") == 1


def test_medical_guarantee_answers_facility_without_making_safety_promise():
    understanding = CustomerUnderstanding(
        intent="itinerary",
        semantic_signals=["medical_guarantee_request"],
        customer_questions=["vehicle", "altitude"],
    )
    plan = build_reply_plan(
        context(
            customer_text="桃花9日车上有供氧设备吗？老人坐车安全吗？",
            route_variant="peach_9d_2027",
        ),
        understanding,
    )
    assert "route.shared.vehicle_reference" in plan.allowed_fact_ids
    assert "service.safety" in plan.allowed_fact_ids
    generated = deterministic_system_reply(plan)
    assert generated is not None
    assert "9座VIP航空座椅車" in generated.reply
    assert "不能保證" in generated.reply


def test_customization_request_uses_code_boundary_without_inventing_product_limits():
    understanding = CustomerUnderstanding(
        intent="other",
        semantic_signals=["customization_request"],
        customer_questions=["other"],
        confidence=0.9,
    )
    plan = build_reply_plan(
        context(
            customer_text="我偏愛自助行，不愛跟團，酒店可能選便宜一點的",
            route_variant="peach_11d_2027",
            memory={"party_size": 2, "departure_window": "3月底"},
            journey={
                "sent_content_groups": ["itinerary_overview", "hotel_reference"],
                "customer_profile": {"party_size": 2, "departure_window": "3月底"},
            },
        ),
        understanding,
    )
    assert plan.allowed_content_group_keys == []
    assert plan.allowed_fact_ids == ["service.customization"]
    generated = deterministic_system_reply(plan)
    assert generated is not None
    assert "另外規劃" in generated.reply
    assert "無法調整" not in generated.reply


def test_pipeline_assembles_legacy_decision_from_code_plan():
    understanding = CustomerUnderstanding(
        intent="itinerary",
        route_candidate="peach_9d_2027",
        route_resolution="confirmed",
        route_evidence="桃花9日",
        customer_questions=["itinerary"],
        confidence=0.88,
    )

    def understand(_context):
        return understanding, [{"node": "customer_understanding", "status": "completed"}], "u"

    def generate(_context, plan):
        assert plan.action == "reply"
        return GeneratedReply(
            body="9日线路从林芝较低海拔开始，不走珠峰。",
            follow_up=None,
            used_fact_ids=["route.9.overview"],
            asset_ids=[],
        ), [{"node": "reply_generation", "status": "completed"}], "g"

    rich_context = context(
        route_variant="peach_9d_2027",
        memory={
            "party_size": {"value": 2, "quote": "2位"},
            "departure_window": {"value": "3月底", "quote": "3月底"},
        },
        journey={
            "sent_content_groups": ["advisor_greeting", "brand_positioning"],
            "customer_profile": {"party_size": 2, "departure_window": "3月底"},
        },
    )
    decision, logs, digest, trace = run_realtime_reply_pipeline(
        rich_context,
        understanding_node=understand,
        generation_node=generate,
        verification_node=lambda _context, _plan, _generated: (
            FactVerification(True),
            [{"node": "reply_fact_verification", "status": "completed"}],
            "v",
        ),
    )
    assert decision.action == "reply"
    assert decision.route_variant == "peach_9d_2027"
    assert decision.journey_stage == "value_building"
    # Compatibility output identifies the touched group; the item-level
    # execution ledger keeps it incomplete until its reviewed image arrives.
    assert decision.content_group_key == "itinerary_overview"
    assert decision.covered_content_groups == ["itinerary_overview"]
    assert len(logs) == 3 and digest
    assert trace["pipeline"] == "split_realtime_reply"


def test_pipeline_records_single_planned_group_when_generator_omits_hidden_fact_ids():
    understanding = CustomerUnderstanding(
        intent="departure",
        route_candidate="peach_11d_2027",
        route_resolution="confirmed",
        route_evidence="桃花加珠峰11日",
        slot_updates={"party_size": SlotUpdate(1, "一个人")},
        customer_questions=["party_size"],
        confidence=0.9,
    )

    decision, *_ = run_realtime_reply_pipeline(
        context(customer_text="我一个人可以参加桃花加珠峰11日吗？"),
        understanding_node=lambda _context: (understanding, [], "u"),
        generation_node=lambda _context, _plan: (
            GeneratedReply("一個人也可以先了解，我先按單人安排幫您整理。", None, [], []),
            [],
            "g",
        ),
        verification_node=lambda _context, _plan, _generated: (
            FactVerification(True),
            [],
            "v",
        ),
    )
    assert decision.content_group_key == "party_intro_solo"
    assert decision.covered_content_groups == ["party_intro_solo"]


def test_pipeline_sends_active_fixed_answer_verbatim_without_fact_model():
    route = "peach_9d_2027"
    understanding = CustomerUnderstanding(
        intent="price",
        route_candidate=route,
        route_resolution="confirmed",
        route_evidence="桃花9日",
        customer_questions=["price"],
        confidence=0.98,
    )
    expected = next(item for item in ROUTES[route]["fixed_answers"] if item["id"] == "price")

    def generate(_context, plan):
        assert plan.fixed_answer_id == "price"
        return GeneratedReply(
            expected["answer_text"],
            None,
            plan.allowed_fact_ids,
            plan.allowed_asset_ids,
        ), [], "route-fixed-answer:price"

    def unexpected_verification(*_args):
        raise AssertionError("an active fixed answer must not call a fact model")

    decision, logs, _, trace = run_realtime_reply_pipeline(
        context(customer_text="桃花9日一個人多少錢？"),
        understanding_node=lambda _context: (understanding, [], "u"),
        generation_node=generate,
        verification_node=unexpected_verification,
    )
    assert decision.reply == expected["answer_text"]
    assert logs == []
    assert trace["response_source"] == "route_fixed_answer"
    assert trace["generation_rounds"] == 0
    assert trace["fact_verification_passed"] is None
    assert trace["fact_verification_mode"] == "reviewed_copy"


def test_seasonal_weather_question_uses_reviewed_website_answer_without_slot_follow_up():
    route = "peach_11d_2027"
    understanding = CustomerUnderstanding(
        intent="other",
        customer_questions=["other"],
        confidence=0.1,
    )
    expected = next(item for item in ROUTES[route]["fixed_answers"] if item["id"] == "spring_weather")

    plan = build_reply_plan(
        context(
            customer_text="那個時候會不會冷啊",
            route_variant=route,
            memory={"departure_window": "3月底"},
            journey={
                "stage": "value_building",
                "sent_content_groups": ["itinerary_overview"],
                "customer_profile": {"departure_window": "3月底"},
            },
        ),
        understanding,
    )
    assert plan.fixed_answer_id == "spring_weather"
    assert plan.allowed_content_group_keys == ["spring_weather"]
    assert plan.follow_up is None

    decision, _, _, trace = run_realtime_reply_pipeline(
        context(
            customer_text="那個時候會不會冷啊",
            route_variant=route,
            memory={"departure_window": "3月底"},
            journey={
                "stage": "value_building",
                "sent_content_groups": ["itinerary_overview"],
                "customer_profile": {"departure_window": "3月底"},
            },
        ),
        understanding_node=lambda _context: (understanding, [], "u"),
        verification_node=lambda *_args: (_ for _ in ()).throw(
            AssertionError("reviewed fixed answers must not call the fact model")
        ),
    )
    assert decision.reply == expected["answer_text"]
    assert decision.content_group_key == "spring_weather"
    assert decision.material_keys == []
    assert "幾位" not in decision.reply and "什麼時候" not in decision.reply
    assert trace["response_source"] == "route_fixed_answer"


def test_unresolved_direct_question_does_not_advance_route_mainline_or_ask_missing_slots():
    understanding = CustomerUnderstanding(
        intent="other",
        semantic_signals=["unresolved_direct_question"],
        customer_questions=["other"],
        confidence=0.1,
    )
    plan = build_reply_plan(
        context(
            customer_text="那一段會怎麼樣啊",
            route_variant="peach_11d_2027",
            journey={
                "stage": "value_building",
                "sent_content_groups": ["itinerary_overview"],
                "customer_profile": {},
            },
        ),
        understanding,
    )
    assert plan.allowed_content_group_keys == []
    assert plan.allowed_fact_ids == []
    assert plan.follow_up is not None and plan.follow_up.type == "clarification"
    assert plan.follow_up.field == "topic"
    generated = deterministic_system_reply(plan)
    assert generated is not None
    assert "珠峰" not in generated.reply and "住宿" not in generated.reply
    assert "幾位" not in generated.reply and "什麼時候" not in generated.reply


def test_customer_stop_is_code_owned_and_skips_reply_generation():
    understanding = CustomerUnderstanding(
        intent="other",
        semantic_signals=["explicit_stop"],
        customer_questions=["other"],
    )
    called = False

    def understand(_context):
        return understanding, [], "u"

    def generate(_context, _plan):
        nonlocal called
        called = True
        raise AssertionError("generation must not run")

    decision, *_ = run_realtime_reply_pipeline(
        context(customer_text="不要再联系我"),
        understanding_node=understand,
        generation_node=generate,
    )
    assert decision.action == "no_action"
    assert "stop_automation" in decision.safety_flags
    assert called is False


def test_pipeline_rewrites_only_customer_copy_after_fact_verification_failure():
    understanding = CustomerUnderstanding(
        intent="itinerary",
        route_candidate="peach_9d_2027",
        route_resolution="confirmed",
        route_evidence="桃花9日",
        customer_questions=["itinerary"],
    )
    generation_calls = []
    verification_calls = 0

    def understand(_context):
        return understanding, [], "u"

    def generate(node_context, _plan):
        generation_calls.append(node_context.get("reply_generation_feedback"))
        if len(generation_calls) == 2:
            assert _plan.allowed_asset_ids == []
            assert _plan.allowed_content_group_keys == []
            assert _plan.follow_up is None
            assert _plan.lead_action == "none"
        body = "不受支持的事实" if len(generation_calls) == 1 else "9日线路不走珠峰。"
        return GeneratedReply(body, None, ["route.9.overview"], []), [], f"g{len(generation_calls)}"

    def verify(_context, _plan, _generated):
        nonlocal verification_calls
        verification_calls += 1
        return (
            FactVerification(
                supported=verification_calls == 2,
                unsupported_claims=[] if verification_calls == 2 else ["不受支持的事实"],
            ),
            [],
            f"v{verification_calls}",
        )

    rich_context = context(
        memory={"party_size": {"value": 2}, "departure_window": {"value": "3月底"}},
        journey={"sent_content_groups": ["advisor_greeting", "brand_positioning"], "customer_profile": {"party_size": 2, "departure_window": "3月底"}},
    )
    decision, _, _, trace = run_realtime_reply_pipeline(
        rich_context,
        understanding_node=understand,
        generation_node=generate,
        verification_node=verify,
    )
    assert decision.reply == "9日线路不走珠峰。"
    assert generation_calls[0] is None
    assert generation_calls[1]["unsupported_claims"] == ["不受支持的事实"]
    assert trace["response_source"] == "model_rewrite"
    assert trace["fact_verification_passed"] is True
    assert decision.covered_content_groups == []
    assert "skip_silence_enrollment" not in decision.safety_flags


def test_pipeline_falls_back_to_verified_text_when_asset_caption_contract_keeps_failing():
    from app.deepseek_evaluation import EvaluationCallError

    asset = ROUTES["peach_11d_2027"]["groups"]["hotel_reference"]["assets"][0]
    understanding = CustomerUnderstanding(
        intent="hotel",
        route_candidate="peach_11d_2027",
        route_resolution="confirmed",
        route_evidence="桃花加珠峰11日",
        customer_questions=["hotel"],
    )
    generation_calls = []

    def generate(node_context, plan):
        generation_calls.append(list(plan.allowed_asset_ids))
        if len(generation_calls) == 1:
            assert plan.allowed_asset_ids
            raise EvaluationCallError(
                "selected_assets_not_described",
                [{"node": "reply_generation", "status": "invalid_json"}],
                "caption-failed",
            )
        assert plan.allowed_asset_ids == []
        assert node_context["reply_generation_feedback"]["instruction"]
        return GeneratedReply(
            "這段會安排國際品牌希爾頓飯店，房內有供氧設備。",
            None,
            ["route.shared.hotel_reference"],
            [],
        ), [{"node": "reply_generation", "status": "completed"}], "text-only"

    decision, _, _, trace = run_realtime_reply_pipeline(
        context(
            customer_text="想了解住宿",
            route_variant="peach_11d_2027",
            journey={
                "sent_content_groups": ["advisor_greeting", "brand_positioning", "itinerary_overview"],
                "customer_profile": {},
            },
            available_materials=[{"key": asset, "routes": ["peach_11d_2027"]}],
        ),
        understanding_node=lambda _context: (understanding, [], "u"),
        generation_node=generate,
        verification_node=lambda *_args: (FactVerification(True), [], "v"),
    )

    assert len(generation_calls) == 2
    assert decision.material_keys == []
    assert "希爾頓" in decision.reply
    assert "asset_caption_failed_text_only" in decision.safety_flags
    assert trace["fact_verification_passed"] is True


def test_pipeline_blocks_claims_rejected_after_the_single_model_rewrite():
    understanding = CustomerUnderstanding(
        intent="itinerary",
        route_candidate="peach_9d_2027",
        route_resolution="confirmed",
        route_evidence="桃花9日",
        customer_questions=["itinerary"],
    )

    def understand(_context):
        return understanding, [], "u"

    def generate(_context, _plan):
        return GeneratedReply(
            "9日行程不上珠峰，很適合您。",
            None,
            ["route.9.overview"],
            [],
        ), [], "g"

    def verify(_context, _plan, _generated):
        return FactVerification(False, ["很適合您"]), [], "v"

    decision, _, _, trace = run_realtime_reply_pipeline(
        context(journey={"sent_content_groups": ["advisor_greeting", "brand_positioning"]}), understanding_node=understand,
        generation_node=generate, verification_node=verify,
    )
    assert decision.action == "handoff"
    assert decision.handoff_reason == "knowledge_verification_required"
    assert "很適合您" not in decision.reply
    assert decision.material_keys == []
    assert trace["generation_rounds"] == 2
    assert trace["fact_verification_passed"] is False


def test_generate_decision_routes_uninjected_reply_to_split_pipeline(monkeypatch):
    expected = EvaluationDecision("no_action", "unclassified", "other")

    def split(_packet):
        return expected, [], "split-hash", {"pipeline": "split_realtime_reply"}

    monkeypatch.setattr("app.decision_service.run_realtime_reply_pipeline", split)
    decision, _, digest, trace = generate_decision(context())
    assert decision is expected
    assert digest == "split-hash"
    assert trace["pipeline"] == "split_realtime_reply"
