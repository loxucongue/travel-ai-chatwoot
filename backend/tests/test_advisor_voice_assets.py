from copy import deepcopy
from dataclasses import replace

import pytest

from app.reply_generation import _validate_no_duplicate_asset_narration

from app.advisor_voice import (
    ADVISOR_VOICE_VERSION,
    advisor_voice_contract,
    contact_follow_up_question,
    requested_contact_question,
    route_choice_question,
    slot_follow_up_question,
)


def test_repeated_silence_phrase_is_rejected_even_when_caption_uses_other_words():
    text = "行程裡也會去扎基寺，還能看看更貼近當地生活的寺院人文。這張是扎基寺，讓您先看看路線中這段貼近當地生活的寺院風景。"
    with pytest.raises(ValueError, match="reply_duplicate_asset_narration"):
        _validate_no_duplicate_asset_narration(text, ["zhaji"], {"zhaji": "這張是拉薩扎基寺外觀照片。"})
from app.asset_narratives import normalized_asset_narrative
from app.reply_fact_verification import call_reply_fact_verifier
from app.reply_generation import (
    REPLY_GENERATOR_REPAIR_PROMPT,
    GeneratedReply,
    _normalize_unapproved_social_proof,
    _system_prompt as reply_generator_system_prompt,
    _validate_customer_visible_body,
    call_reply_generator,
    deterministic_system_reply,
)
from app.reply_planning import ReplyPlan, build_reply_plan
from app.reply_understanding import CustomerUnderstanding, SlotUpdate
from app.route_packages import JOURNEY_POLICY, ROUTES
from app.silence_generation import (
    SILENCE_GENERATOR_REPAIR_PROMPT,
    _system_prompt as silence_generator_system_prompt,
    call_silence_generator,
)
from app.silence_planning import SilencePlan


def _policy() -> dict:
    value = deepcopy(JOURNEY_POLICY)
    value["route_switch"]["allowed_routes"] = list(ROUTES)
    value["operator_configuration"] = {
        "business_goal": "answer first and provide useful value",
        "tone_guidance": "Use a softer Taiwan advisor rhythm without repeated greetings.",
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
    return value


def _asset(key: str, route: str) -> dict:
    return {
        "key": key,
        "name": "hotel room",
        "topic": "hotel",
        "routes": [route],
        "what_it_shows": "a twin room and in-room oxygen equipment",
        "feature_points": ["actual room layout", "oxygen equipment"],
        "customer_value": "lets the customer inspect the accommodation setup",
        "recommended_caption": "I will send the room photos first.",
        "avoid_claims": ["guarantees no altitude sickness"],
    }


def _context(route: str, asset: dict) -> dict:
    return {
        "module": "reply",
        "customer_text": "I would like to see the hotel.",
        "context_messages": [],
        "route_variant": route,
        "memory": {},
        "journey": {"sent_content_groups": [], "customer_profile": {}},
        "lead_capture": {"status": "not_started"},
        "available_materials": [asset],
        "reception_policy": _policy(),
    }


def _plan(route: str, asset_key: str) -> ReplyPlan:
    return ReplyPlan(
        action="reply",
        intent="hotel",
        route_variant=route,
        branch=ROUTES[route]["branch"],
        next_stage="value_building",
        reply_goal="explain the hotel and its customer value",
        follow_up=None,
        allowed_fact_ids=["route.shared.hotel_reference"],
        allowed_content_group_keys=["hotel_reference"],
        allowed_asset_ids=[asset_key],
        reply_options=[],
        slots={},
        slot_evidence={},
        missing_slots=[],
        handoff_reason=None,
        lead_action="none",
        contact_values={},
        route_evidence="",
        confidence=0.9,
        safety_flags=[],
    )


def test_shared_advisor_voice_only_describes_customer_visible_copy():
    assert ADVISOR_VOICE_VERSION == "china2go-taiwan-advisor-voice-v15"
    realtime = advisor_voice_contract(silence=False)
    silence = advisor_voice_contract(silence=True)
    assert "AI" in realtime
    assert "看了嗎" in silence
    assert "不要重新問候" in silence
    assert "what_it_shows" in realtime
    assert "人數和日期不是話術上的硬性前提" not in realtime
    assert "程式提供的唯一下一步" in realtime
    assert "台灣自然繁體中文" in realtime
    assert "無論輸入簡體或繁體" in realtime
    assert "25至80" in realtime
    assert "不要連續使用" in realtime
    assert "不要每輪固定以『收到』" in realtime
    assert "您先慢慢看" in silence
    assert "『9日行程』或『11日行程』" in realtime


def test_undecided_date_contact_invitation_has_no_duplicate_generated_preface():
    from app.reply_planning import FollowUp
    plan = replace(_plan('peach_9d_2027', ''),
                   slots={'departure_window': '未确定'}, allowed_fact_ids=[], allowed_asset_ids=[],
                   follow_up=FollowUp('contact', 'line', '時間還沒確定也沒關係。方便留個 LINE 嗎？'))
    result = deterministic_system_reply(plan)
    assert result is not None
    assert result.body == ''
    assert result.reply == plan.follow_up.question
    assert deterministic_system_reply(replace(plan, allowed_fact_ids=['route.9.price'])) is None


def test_customer_visible_model_prompts_are_fully_taiwan_traditional():
    prompts = {
        "reply_system": reply_generator_system_prompt(200, 2),
        "reply_repair": REPLY_GENERATOR_REPAIR_PROMPT,
        "silence_system": silence_generator_system_prompt(200, 2),
        "silence_repair": SILENCE_GENERATOR_REPAIR_PROMPT,
    }
    simplified_or_mainland_terms = {
        "客户", "回复", "节点", "业务", "线路", "阶段", "事实", "代码",
        "当前", "允许", "发送", "图片", "文件", "选择", "推断", "保证",
        "结构", "修复", "删除", "错误", "补充", "状态", "输出", "字段",
        "长度", "字符", "车辆", "设施", "记录", "联系", "酒店", "消息",
        "專項顧問", "線路", "聯繫方式", "對接", "匹配方案", "出行方案",
        "小夥伴", "當前可接待",
    }
    for name, prompt in prompts.items():
        assert "台灣自然繁體中文" in prompt, name
        assert "全程使用『您』" in prompt, name
        assert not sorted(term for term in simplified_or_mainland_terms if term in prompt), name
    assert "客戶回覆生成節點" in prompts["reply_system"]
    assert "客戶沉默跟進文案生成節點" in prompts["silence_system"]
    assert "程式會在正文後附上" in prompts["reply_repair"]
    assert "程式會在正文後附上" in prompts["silence_repair"]


def test_code_owned_followups_use_same_advisor_voice():
    assert route_choice_question() == "您想先看看哪一條呢？"
    assert slot_follow_up_question("party_size") == "這次大概會有幾位一起來呢？"
    assert "還沒決定也沒關係" in slot_follow_up_question("departure_window")
    assert requested_contact_question("LINE") == "可以把您的LINE帳號或連結留給我嗎？"
    undecided = contact_follow_up_question("LINE 或 Email", departure_undecided=True)
    assert "時間還沒確定也沒關係" in undecided
    assert "完整行程" in undecided
    assert "LINE 或 Email" in undecided
    reminder = contact_follow_up_question("LINE 或 Email", reminder=True)
    assert reminder != undecided
    assert reminder == "如果您想收完整行程，留一個 LINE 或 Email 給我就可以了，我整理好再傳給您。"
    standard = contact_follow_up_question("LINE 或微信")
    assert len(standard) <= 35
    assert standard.count("？") == 1
    assert "完整行程" in standard and "LINE" in standard


def test_customer_copy_rejects_unapproved_social_proof():
    with pytest.raises(ValueError, match="reply_unapproved_social_proof"):
        _validate_customer_visible_body("住宿是很多客人最關心的，我先替您介紹。")


def test_taiwan_wording_allows_hotel_proper_name_but_rejects_generic_mainland_term():
    _validate_customer_visible_body("您問的是利源酒店，我先核對這個住宿點的資料。")
    with pytest.raises(ValueError, match="reply_must_use_taiwan_service_terms"):
        _validate_customer_visible_body("這趟會入住酒店，酒店房間照片也會一起提供。")


def test_unapproved_hotel_social_proof_is_reduced_to_specific_content():
    value = _normalize_unapproved_social_proof(
        "住宿這部分通常家人會比較關心，我把客房照片發您看。"
    )
    assert value == "住宿這部分，我把客房照片發您看。"


def test_asset_narrative_normalization_deduplicates_reviewed_lists():
    result = normalized_asset_narrative({
        "what_it_shows": " room ",
        "feature_points": ["bed", "bed", ""],
        "customer_value": " compare rooms ",
        "recommended_caption": " see this ",
        "avoid_claims": ["guaranteed", "guaranteed"],
    })
    assert result == {
        "what_it_shows": "room",
        "feature_points": ["bed"],
        "customer_value": "compare rooms",
        "recommended_caption": "see this",
        "avoid_claims": ["guaranteed"],
    }


def test_reply_generator_receives_full_reviewed_asset_narrative(monkeypatch):
    route = "peach_11d_2027"
    key = ROUTES[route]["groups"]["hotel_reference"]["assets"][0]
    source = _context(route, _asset(key, route))
    captured = {}

    def fake_call_json_node(**kwargs):
        captured.update(kwargs)
        return "ok", [], "digest"

    monkeypatch.setattr("app.reply_generation.call_json_node", fake_call_json_node)
    assert call_reply_generator(source, _plan(route, key))[0] == "ok"
    assert "客戶回覆生成節點" in captured["system_prompt"]
    assert "台灣自然繁體中文" in captured["system_prompt"]
    assert "客户回复生成节点" not in captured["system_prompt"]
    assert '親切柔和' in captured['input_data']['operator_preferences']['tone_description']
    allowed = captured["input_data"]["allowed_assets"]
    assert allowed[0]["what_it_shows"] == "a twin room and in-room oxygen equipment"
    assert allowed[0]["customer_value"] == "lets the customer inspect the accommodation setup"
    assert allowed[0]["avoid_claims"] == ["guarantees no altitude sickness"]
    assert captured["input_data"]["operator_preferences"]["tone_guidance"] == (
        "Use a softer Taiwan advisor rhythm without repeated greetings."
    )


def test_handoff_copy_is_deterministic_and_does_not_call_the_model(monkeypatch):
    route = "peach_9d_2027"
    key = ROUTES[route]["groups"]["itinerary_overview"]["assets"][0]
    source = _context(route, _asset(key, route))
    source["available_materials"][0]["recommended_caption"] = "我先把9日行程圖發您看～每天的路線都整理在裡面。"
    source["customer_text"] = "我想看桃花9日，請安排真人顧問。"
    plan = build_reply_plan(
        source,
        CustomerUnderstanding(
            intent="itinerary",
            route_candidate=route,
            route_resolution="confirmed",
            route_evidence="桃花9日",
            semantic_signals=["explicit_human_request"],
            confidence=0.99,
        ),
    )

    def unexpected_model_call(**kwargs):
        raise AssertionError("handoff copy must not call the model")

    monkeypatch.setattr("app.reply_generation.call_json_node", unexpected_model_call)
    generated, calls, digest = call_reply_generator(source, plan)
    assert calls == []
    assert digest == "deterministic-handoff-v1"
    assert generated.body.startswith("可以，我現在幫您轉給專人旅遊顧問")
    assert generated.body.count("專人旅遊顧問") == 1
    assert generated.asset_ids == [key]


def test_reviewed_route_answer_is_emitted_verbatim_without_a_generation_call(monkeypatch):
    route = "peach_9d_2027"
    source = _context(route, {})
    source["available_materials"] = []
    source["customer_text"] = "桃花9日一個人多少錢？"
    plan = build_reply_plan(
        source,
        CustomerUnderstanding(
            intent="price",
            route_candidate=route,
            route_resolution="confirmed",
            route_evidence="桃花9日",
            customer_questions=["price"],
            confidence=0.98,
        ),
    )
    expected = next(item for item in ROUTES[route]["fixed_answers"] if item["id"] == "price")
    assert plan.fixed_answer_id == "price"

    def unexpected_model_call(**kwargs):
        raise AssertionError("a reviewed fixed answer must not call the generation model")

    monkeypatch.setattr("app.reply_generation.call_json_node", unexpected_model_call)
    generated, calls, source_id = call_reply_generator(source, plan)
    assert calls == []
    assert source_id == "route-fixed-answer:price"
    assert generated.body == expected["answer_text"]
    assert generated.follow_up is None


def test_fixed_answer_does_not_force_match_ambiguous_or_multi_topic_questions():
    route = "peach_11d_2027"
    source = _context(route, {})
    source["available_materials"] = []
    source["customer_text"] = "住宿和價格可以一起介紹嗎？"
    plan = build_reply_plan(
        source,
        CustomerUnderstanding(
            intent="price",
            route_candidate=route,
            route_resolution="confirmed",
            route_evidence="桃花加珠峰11日",
            customer_questions=["price", "hotel"],
            confidence=0.95,
        ),
    )
    assert plan.fixed_answer_id == ""


def test_fixed_answer_negative_example_defers_to_controlled_availability_reply():
    route = "peach_11d_2027"
    source = _context(route, {})
    source["available_materials"] = []
    source["customer_text"] = "今天還有位子嗎？"
    plan = build_reply_plan(
        source,
        CustomerUnderstanding(
            intent="price",
            route_candidate=route,
            route_resolution="confirmed",
            route_evidence="桃花加珠峰11日",
            customer_questions=["price"],
            confidence=0.95,
        ),
    )
    assert plan.fixed_answer_id == ""


def test_fixed_answer_does_not_guess_between_answers_that_share_a_topic():
    route = "peach_11d_2027"
    source = _context(route, {})
    source["available_materials"] = []
    source["customer_text"] = "可以再說一些這趟的特色嗎？"
    plan = build_reply_plan(
        source,
        CustomerUnderstanding(
            intent="route_intro",
            route_candidate=route,
            route_resolution="confirmed",
            route_evidence="這趟",
            customer_questions=["highlights"],
            confidence=0.99,
        ),
    )
    assert plan.fixed_answer_id == ""


def test_silence_generator_receives_same_reviewed_asset_narrative(monkeypatch):
    route = "peach_11d_2027"
    key = ROUTES[route]["groups"]["hotel_reference"]["assets"][0]
    source = _context(route, _asset(key, route))
    source["module"] = "silence_touch"
    captured = {}

    def fake_call_json_node(**kwargs):
        captured.update(kwargs)
        return "ok", [], "digest"

    monkeypatch.setattr("app.silence_generation.call_json_node", fake_call_json_node)
    silence_plan = SilencePlan(
        reply_plan=_plan(route, key),
        touch_goal="build_value",
        touch_reason="fresh hotel value",
    )
    assert call_silence_generator(source, silence_plan)[0] == "ok"
    assert "客戶沉默跟進文案生成節點" in captured["system_prompt"]
    assert "台灣自然繁體中文" in captured["system_prompt"]
    assert "客户沉默跟进文案生成节点" not in captured["system_prompt"]
    assert '親切柔和' in captured['input_data']['operator_preferences']['tone_description']
    assert captured["input_data"]["allowed_assets"][0]["recommended_caption"]
    assert captured["input_data"]["operator_preferences"]["tone_guidance"] == (
        "Use a softer Taiwan advisor rhythm without repeated greetings."
    )


def test_unselected_silence_copy_is_deterministic(monkeypatch):
    source = {
        "module": "silence_touch",
        "customer_text": "我想了解西藏桃花行程。",
        "context_messages": [],
        "route_variant": "",
        "memory": {},
        "journey": {"sent_content_groups": [], "customer_profile": {}},
        "lead_capture": {"status": "not_started"},
        "available_materials": [],
        "reception_policy": _policy(),
    }
    plan = build_reply_plan(
        source,
        CustomerUnderstanding(
            intent="route_intro",
            route_resolution="none",
            customer_questions=["itinerary"],
            confidence=0.9,
        ),
    )

    def unexpected_model_call(**kwargs):
        raise AssertionError("unselected silence copy must not call the model")

    monkeypatch.setattr("app.silence_generation.call_json_node", unexpected_model_call)
    generated, calls, digest = call_silence_generator(
        source,
        SilencePlan(plan, "route_choice", "customer has not selected a route"),
    )
    assert calls == []
    assert digest == "deterministic-unselected-silence-v1"
    assert "11日會再到珠峰大本營" in generated.body
    assert generated.asset_ids == []


def test_fact_verifier_receives_reviewed_claims_and_prohibited_claims(monkeypatch):
    route = "peach_11d_2027"
    key = ROUTES[route]["groups"]["hotel_reference"]["assets"][0]
    source = _context(route, _asset(key, route))
    captured = {}

    def fake_call_json_node(**kwargs):
        captured.update(kwargs)
        return "ok", [], "digest"

    monkeypatch.setattr("app.reply_fact_verification.call_json_node", fake_call_json_node)
    generated = GeneratedReply(
        body="hotel introduction",
        follow_up=None,
        used_fact_ids=["route.shared.hotel_reference"],
        asset_ids=[key],
    )
    assert call_reply_fact_verifier(source, _plan(route, key), generated)[0] == "ok"
    claim = captured["input_data"]["allowed_asset_claims"][0]
    assert claim["feature_points"] == ["actual room layout", "oxygen equipment"]
    assert claim["avoid_claims"] == ["guarantees no altitude sickness"]


def test_effective_eight_person_threshold_handles_seven_and_eight_in_code():
    route = "peach_11d_2027"
    source = _context(route, _asset("unused", route))
    source["reception_policy"]["handoff"]["large_group"]["minimum_party_size"] = 8

    def plan_for(size: int):
        return build_reply_plan(
            {**source, "customer_text": f"We have {size} people."},
            CustomerUnderstanding(
                intent="other",
                slot_updates={"party_size": SlotUpdate(size, str(size))},
                customer_questions=["other"],
                confidence=0.9,
            ),
        )

    assert plan_for(7).action != "handoff"
    assert plan_for(8).action == "handoff"
    assert plan_for(8).handoff_reason == "large_group_custom_quote"
