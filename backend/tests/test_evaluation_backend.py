import json

import httpx
import pytest
from sqlalchemy import func, select

from app.business_knowledge import deterministic_branch
from app.chatwoot import ChatwootError, ReadOnlyChatwootClient
from app.deepseek_evaluation import EvaluationDecision, _decision_contract_error, request_payload
from app.decision_service import generate_decision
from app.evaluation_dataset import build_dataset
from app.evaluation_service import create_run
from app.models import ConversationState, EvaluationCase, EvaluationResult, InboxBinding, MessageEvent, OutboundMessage, User
from app.route_packages import ROUTES


def test_read_only_chatwoot_rejects_write_before_transport():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"payload": []})

    client = ReadOnlyChatwootClient("https://example.test", 1, "secret")
    client.client.close()
    client.client = httpx.Client(transport=httpx.MockTransport(handler), headers={"api_access_token": "secret"})
    assert client.list_inboxes() == {"payload": []}
    with pytest.raises(ChatwootError) as exc:
        client.create_text_message(10, "never send")
    assert exc.value.code == "chatwoot_write_blocked"
    with pytest.raises(ChatwootError) as exc:
        client.create_input_select_message(10, "choose", ["one", "two"])
    assert exc.value.code == "chatwoot_write_blocked"
    assert len(requests) == 1
    assert requests[0].method == "GET"
    client.close()


def test_quick_reply_text_is_decided_by_model_without_code_override():
    context = {
        "module": "reply",
        "customer_text": "桃花9日",
        "context_messages": [
            {"direction": "incoming", "content": "去年曾经问过9月出发"},
        ],
        "context_complete": True,
        "route_variant": "peach_9d_2027",
        "route_selection_source": "messenger_quick_reply",
        "memory": {},
        "available_materials": [],
    }

    def model_call(_packet):
        return EvaluationDecision(
            action="reply",
            branch="peach_9d",
            intent="route_intro",
            reply="桃花9日不上珠峰，我先介紹行程重點。",
            route_variant="peach_9d_2027",
            route_evidence="桃花9日",
        ), [], "hash"

    decision, _, _, _ = generate_decision(context, model_call=model_call)

    assert decision.action == "reply"
    assert decision.route_variant == "peach_9d_2027"
    assert decision.branch == "peach_9d"
    assert decision.handoff_reason != "departure_material_unverified"


@pytest.mark.parametrize("method", ["POST", "PATCH", "PUT", "DELETE", "OPTIONS"])
def test_read_only_http_hook_rejects_direct_write(method):
    requests = []
    client = ReadOnlyChatwootClient("https://example.test", 1, "secret")
    hooks = client.client.event_hooks
    client.client.close()
    client.client = httpx.Client(
        transport=httpx.MockTransport(lambda request: requests.append(request) or httpx.Response(200)),
        event_hooks=hooks,
    )
    try:
        with pytest.raises(ChatwootError, match="Read-only"):
            client.request(method, "conversations")
        with pytest.raises(ChatwootError, match="Read-only"):
            client.client.request(method, "https://example.test/direct")
        assert not requests
        client.client.head("https://example.test/health")
        assert [request.method for request in requests] == ["HEAD"]
    finally:
        client.close()


@pytest.mark.parametrize(("text", "branch"), [
    ("想了解桃花加珠峰11天", "peach_11d"),
    ("林芝桃花9日怎么走", "peach_9d"),
    ("我们六个人想自己包团", "private_group"),
    ("五月去西藏并上珠峰", "other_peak"),
    ("五月去西藏但不上珠峰", "other_no_peak"),
    ("想去云南玩", "other_destination"),
])
def test_six_deterministic_branches(text, branch):
    assert deterministic_branch(text) == branch


def test_model_contract_rejects_non_enum_intent():
    with pytest.raises(ValueError, match="deepseek_invalid_intent"):
        EvaluationDecision.parse({
            "action": "reply",
            "branch": "private_group",
            "intent": "詢問行程費用",
            "reply": "價格需要依資料回答。",
        })


def test_model_contract_sends_multiple_followups_back_to_model_for_repair():
    with pytest.raises(ValueError, match="deepseek_multiple_followup_questions"):
        EvaluationDecision.parse({
            "action": "reply",
            "branch": "peach_11d",
            "intent": "itinerary",
            "reply": "這是住宿參考。這間是您目前安排的酒店嗎？要再看房間圖片嗎？",
            "route_variant": "peach_11d_2027",
        })


def test_silence_touch_contract_is_mandatory_and_stage_driven():
    no_action = EvaluationDecision.parse({
        "action": "no_action",
        "branch": "unclassified",
        "intent": "other",
        "journey_stage": "considering",
        "touch_goal": "soft_nurture",
        "touch_reason": "客户正在与家人讨论",
    })
    assert _decision_contract_error(no_action, {
        "module": "silence_touch",
        "customer_text": "我和家人商量一下",
    }) == "deepseek_silence_touch_must_send"

    assert _decision_contract_error(EvaluationDecision(
        "reply",
        "unclassified",
        "route_intro",
        reply="9日轻松一些，11日会去珠峰。您之后回复想看的线路即可。",
        reply_options=["桃花9日", "桃花+珠峰11日"],
        touch_goal="route_choice",
        touch_reason="客户尚未选择线路，轻量说明差异",
    ), {
        "module": "silence_touch",
        "context_messages": [],
        "customer_text": "你好，我想咨询旅行行程",
    }) == "deepseek_silence_touch_no_reply_options"

    mandatory_reply = EvaluationDecision.parse({
        "action": "reply",
        "branch": "peach_9d",
        "intent": "other",
        "reply": "您可以先把这份简要亮点转给家人参考，有问题再告诉我。",
        "route_variant": "peach_9d_2027",
        "journey_stage": "considering",
        "touch_goal": "soft_nurture",
        "touch_reason": "客户正在与家人讨论，适合低压力培育",
    })
    assert _decision_contract_error(mandatory_reply, {
        "module": "silence_touch",
        "customer_text": "我和家人商量一下",
    }) is None


def test_silence_touch_cannot_repeat_a_recent_ai_reply():
    repeated = EvaluationDecision.parse({
        "action": "reply",
        "branch": "peach_9d",
        "intent": "route_intro",
        "reply": "先给您看行程总览，请问预计几位同行？",
        "route_variant": "peach_9d_2027",
        "journey_stage": "needs_discovery",
        "touch_goal": "collect_need",
        "touch_reason": "补齐同行人数",
    })
    assert _decision_contract_error(repeated, {
        "module": "silence_touch",
        "customer_text": "桃花9日",
        "context_messages": [
            {"direction": "incoming", "content": "桃花9日"},
            {"direction": "outgoing", "content": "先给您看行程总览，请问预计几位同行？"},
        ],
    }) == "deepseek_silence_touch_duplicate_recent_reply"

    new_value = EvaluationDecision.parse({
        "action": "reply",
        "branch": "peach_9d",
        "intent": "itinerary",
        "reply": "这条线路会走林芝、拉萨、山南和日喀则，您也可以先看桃花亮点。",
        "route_variant": "peach_9d_2027",
        "journey_stage": "value_building",
        "touch_goal": "build_value",
        "touch_reason": "补充尚未提供的线路价值",
    })
    assert _decision_contract_error(new_value, {
        "module": "silence_touch",
        "customer_text": "桃花9日",
        "context_messages": [
            {"direction": "outgoing", "content": "先给您看行程总览，请问预计几位同行？"},
        ],
    }) is None


def test_silence_touch_cannot_reuse_only_already_covered_groups():
    repeated_group = EvaluationDecision.parse({
        "action": "reply",
        "branch": "peach_9d",
        "intent": "itinerary",
        "reply": "再给您补充一下刚才的行程总览。",
        "route_variant": "peach_9d_2027",
        "journey_stage": "value_building",
        "touch_goal": "build_value",
        "touch_reason": "补充线路价值",
        "covered_content_groups": ["itinerary_overview"],
    })
    assert _decision_contract_error(repeated_group, {
        "module": "silence_touch",
        "customer_text": "桃花9日",
        "journey": {"sent_content_groups": ["itinerary_overview", "entry_question"]},
    }) == "deepseek_silence_touch_reuses_covered_goal"


def test_silence_request_highlights_the_mandatory_new_touch_contract():
    payload = request_payload({
        "module": "silence_touch",
        "customer_text": "桃花9日",
        "context_messages": [
            {"direction": "outgoing", "content": "刚才已经问过人数"},
        ],
        "journey": {"sent_content_groups": ["entry_question"]},
    })
    assert len(payload["messages"]) == 3
    assert "当前调用只处理客户沉默触达" in payload["messages"][1]["content"]
    user_context = json.loads(payload["messages"][-1]["content"])
    assert user_context["mandatory_silence_touch"]["must_send"] is True
    assert user_context["mandatory_silence_touch"]["recent_ai_messages_must_not_repeat"] == [
        "刚才已经问过人数"
    ]
    assert "先承接客户已知信息，再给新价值" in payload["messages"][1]["content"]
    assert "禁止默认使用‘看到了吗／满意吗／有需要吗’" in payload["messages"][1]["content"]
    assert "不得使用纯催问" in user_context["mandatory_silence_touch"]["instruction"]


def test_human_followup_style_is_included_for_silence_decisions():
    payload = request_payload({
        "module": "silence_touch",
        "customer_text": "我和家人商量一下",
        "context_messages": [
            {"direction": "incoming", "content": "我和家人商量一下"},
            {"direction": "outgoing", "content": "好的，您先讨论，有问题随时告诉我。"},
        ],
        "journey": {"stage": "considering", "sent_content_groups": ["itinerary_overview"]},
    })

    user_context = json.loads(payload["messages"][-1]["content"])
    style = user_context["reception_policy"]["human_followup_style"]
    assert style["message_formula"] == [
        "自然承接客户已知信息或当前阶段，不重复品牌自我介绍",
        "补充一个尚未提供、且与客户档案相关的具体价值",
        "最多提出一个容易回答的低压力问题",
    ]
    assert style["stage_patterns"]["considering"].startswith("承认客户需要比较")
    assert style["touch_progression"][-1]["touch_index"] == 6


def test_stage_profile_distinguishes_customer_facts_from_model_inference():
    decision = EvaluationDecision.parse({
        "action": "reply",
        "branch": "peach_9d",
        "intent": "other",
        "reply": "第一次进藏可以先了解手续和高原注意事项。",
        "route_variant": "peach_9d_2027",
        "journey_stage": "objection_handling",
        "profile_updates": {
            "first_time_tibet": {
                "value": True,
                "evidence_quote": "第一次去西藏",
                "confidence": 1,
                "reason": "客户明确说明",
            },
            "decision_status": {
                "value": "与家人讨论",
                "confidence": 0.86,
                "reason": "客户表示需要和家人商量",
            },
        },
    })
    assert _decision_contract_error(decision, {
        "customer_text": "第一次去西藏，我再和家人商量",
    }) is None


def test_model_contract_keeps_slot_updates_on_current_turn_and_unbound_options_consistent():
    stale_slot = EvaluationDecision.parse({
        "action": "reply",
        "branch": "peach_11d",
        "intent": "departure",
        "reply": "我先說明目前資訊。",
        "route_variant": "peach_11d_2027",
        "slots": {"party_size": "3-6人"},
        "slot_evidence": {"party_size": "3-6人"},
    })
    # Invalid provenance is sent back to the model before the reply can be
    # delivered; decision_service still retains the same defensive filter.
    assert _decision_contract_error(stale_slot, {"customer_text": "我們現在只有3人"}) == (
        "deepseek_slot_evidence_not_in_current_turn"
    )

    unresolved = EvaluationDecision.parse({
        "action": "reply",
        "branch": "peach_11d",
        "intent": "route_intro",
        "reply": "請選擇想了解的路線？",
        "route_variant": "peach_11d_2027",
        "reply_options": ["桃花9日", "桃花+珠峰11日"],
    })
    assert unresolved.reply_options == []
    assert _decision_contract_error(unresolved, {"customer_text": "兩條有什麼差別"}) is None


def test_model_contract_keeps_contacts_out_of_generic_memory_slots():
    with pytest.raises(ValueError, match="deepseek_invalid_memory_slot"):
        EvaluationDecision.parse({
            "action": "handoff",
            "branch": "peach_9d",
            "intent": "contact",
            "route_variant": "peach_9d_2027",
            "lead_action": "captured",
            "contact_values": {"wechat": "wx_test_2027"},
            "slots": {"contact_wechat": "wx_test_2027"},
            "slot_evidence": {"contact_wechat": "wx_test_2027"},
        })


def test_model_contract_requires_evidence_for_every_memory_slot():
    with pytest.raises(ValueError, match="deepseek_slot_evidence_mismatch"):
        EvaluationDecision.parse({
            "action": "reply",
            "branch": "peach_9d",
            "intent": "departure",
            "reply": "I have recorded the date.",
            "route_variant": "peach_9d_2027",
            "slots": {"departure_window": "March"},
            "slot_evidence": {},
        })


def test_contact_handoff_requires_an_actual_captured_contact_value():
    with pytest.raises(ValueError, match="deepseek_contact_handoff_requires_capture"):
        EvaluationDecision.parse({
            "action": "handoff",
            "branch": "peach_11d",
            "intent": "contact",
            "reply": "请提供您的微信 ID。",
            "route_variant": "peach_11d_2027",
            "lead_action": "ask",
        })


def test_lead_ask_requires_model_to_complete_the_contact_request_action():
    incomplete = EvaluationDecision.parse({
        "action": "reply",
        "branch": "peach_9d",
        "intent": "price",
        "reply": "我先说明住宿和参考价格。",
        "route_variant": "peach_9d_2027",
        "lead_action": "ask",
        "journey_stage": "contact_ready",
        "covered_content_groups": ["price_reference", "hotel_reference"],
    })
    assert _decision_contract_error(incomplete, {"customer_text": "住宿和价格怎样"}) == (
        "deepseek_lead_ask_contract_incomplete"
    )

    complete = EvaluationDecision.parse({
        "action": "reply",
        "branch": "peach_9d",
        "intent": "price",
        "reply": "我先说明住宿和参考价格。方便留下LINE吗？",
        "route_variant": "peach_9d_2027",
        "lead_action": "ask",
        "journey_stage": "contact_requested",
        "covered_content_groups": ["price_reference", "hotel_reference", "contact_request"],
    })
    assert _decision_contract_error(complete, {"customer_text": "住宿和价格怎样"}) is None


def test_model_reports_every_mainline_group_covered_by_one_reply():
    context = {
        "module": "reply",
        "customer_text": "桃花9日",
        "context_messages": [],
        "context_complete": True,
        "memory": {},
        "available_materials": [],
    }

    def model_call(_packet):
        return EvaluationDecision(
            action="reply",
            branch="peach_9d",
            intent="route_intro",
            reply="我先介紹9日行程，請問您預計幾位同行？",
            route_variant="peach_9d_2027",
            content_group_key="itinerary_overview",
            covered_content_groups=["itinerary_overview", "entry_question"],
        ), [], "hash"

    decision, _, _, trace = generate_decision(context, model_call=model_call)

    assert decision.covered_content_groups == ["itinerary_overview", "entry_question"]
    assert trace["covered_content_groups"] == ["itinerary_overview", "entry_question"]


def test_long_model_option_labels_are_filtered_by_configured_route_titles():
    context = {
        "module": "reply",
        "customer_text": "两条有什么区别",
        "context_messages": [],
        "context_complete": True,
        "memory": {},
        "available_materials": [],
    }

    def model_call(_packet):
        return EvaluationDecision.parse({
            "action": "reply",
            "branch": "unclassified",
            "intent": "route_intro",
            "reply": "请问您想了解哪一条？",
            "reply_options": [
                "桃花9日",
                "桃花加珠峰11日的完整介绍和参考行程安排",
            ],
        }), [], "hash"

    decision, _, _, _ = generate_decision(context, model_call=model_call)

    assert decision.reply_options == ["桃花9日"]


def test_model_route_option_objects_bind_to_configured_titles():
    decision = EvaluationDecision.parse({
        "action": "reply",
        "branch": "unclassified",
        "intent": "route_intro",
        "reply": "请问您想了解哪一条？",
        "reply_options": [
            {"key": "peach_9d", "name": "模型自定义名称"},
            {"key": "peach_11d_2027", "name": "另一个模型名称"},
        ],
    })

    assert decision.reply_options == ["桃花9日", "桃花+珠峰11日"]


def test_empty_optional_profile_update_does_not_discard_valid_reply():
    decision = EvaluationDecision.parse({
        "action": "reply",
        "branch": "peach_9d",
        "intent": "other",
        "reply": "我先为您说明高原旅行的注意事项。",
        "route_variant": "peach_9d_2027",
        "profile_updates": {"concerns": {"value": ""}},
    })

    assert decision.action == "reply"
    assert decision.profile_updates == {}


def test_model_route_option_label_objects_bind_to_configured_titles():
    decision = EvaluationDecision.parse({
        "action": "reply",
        "branch": "unclassified",
        "intent": "route_intro",
        "reply": "choose",
        "reply_options": [
            {"name": ROUTES["peach_9d_2027"]["selection_title"]},
            {"label": ROUTES["peach_11d_2027"]["selection_title"]},
        ],
    })

    assert decision.reply_options == [
        ROUTES["peach_9d_2027"]["selection_title"],
        ROUTES["peach_11d_2027"]["selection_title"],
    ]


def test_route_options_bind_both_route_overview_evidence():
    decision = EvaluationDecision.parse({
        "action": "reply",
        "branch": "unclassified",
        "intent": "route_intro",
        "reply": "choose",
        "reply_options": [
            ROUTES["peach_9d_2027"]["selection_title"],
            ROUTES["peach_11d_2027"]["selection_title"],
        ],
    })
    assert set(decision.evidence_refs) >= {"route.9.overview", "route.11.overview"}
    assert _decision_contract_error(decision, {"customer_text": "桃花"}) is None


def test_route_fact_mentions_bind_overview_evidence():
    decision = EvaluationDecision.parse({
        "action": "reply",
        "branch": "other_destination",
        "intent": "other",
        "reply": "目前可介紹桃花9日與桃花加珠峰11日。",
    })
    assert set(decision.evidence_refs) >= {"route.9.overview", "route.11.overview"}
    assert _decision_contract_error(decision, {"customer_text": "云南"}) is None


def test_operator_large_group_threshold_is_used_by_contract():
    decision = EvaluationDecision.parse({
        "action": "handoff",
        "branch": "peach_9d",
        "intent": "other",
        "reply": "已了解您們12位同行，接下來由顧問協助安排。",
        "route_variant": "peach_9d_2027",
        "handoff_reason": "large_group_custom_quote",
        "lead_action": "none",
        "slots": {"party_size": "12位"},
        "slot_evidence": {"party_size": "12位"},
    })
    policy = {
        "route_switch": {"allowed_routes": list(ROUTES)},
        "handoff": {"large_group": {
            "enabled": True,
            "minimum_party_size": 15,
            "reason": "large_group_custom_quote",
        }},
    }

    assert _decision_contract_error(decision, {
        "customer_text": "我们12位同行",
        "reception_policy": policy,
    }) == "deepseek_large_group_below_threshold"


def test_operator_disabled_route_is_rejected_by_contract():
    decision = EvaluationDecision.parse({
        "action": "reply",
        "branch": "peach_11d",
        "intent": "route_intro",
        "reply": "先為您介紹桃花加珠峰11日。",
        "route_variant": "peach_11d_2027",
        "evidence_refs": ["route.11.overview"],
    })
    policy = {
        "route_switch": {"allowed_routes": ["peach_9d_2027"]},
        "handoff": {"large_group": {
            "enabled": True,
            "minimum_party_size": 12,
            "reason": "large_group_custom_quote",
        }},
    }

    assert _decision_contract_error(decision, {
        "customer_text": "想了解11日",
        "reception_policy": policy,
    }) == "deepseek_disabled_route_selected"


def test_unresolved_route_cannot_collect_contact():
    decision = EvaluationDecision.parse({
        "action": "reply",
        "branch": "unclassified",
        "intent": "itinerary",
        "reply": "先為您介紹行程，方便留下LINE嗎？",
        "route_variant": "",
        "lead_action": "ask",
        "journey_stage": "contact_requested",
        "covered_content_groups": ["itinerary_overview", "contact_request"],
        "slots": {"party_size": "11个人"},
        "slot_evidence": {"party_size": "11个人"},
        "evidence_refs": ["route.9.overview"],
    })
    policy = {
        "route_switch": {"allowed_routes": list(ROUTES)},
        "handoff": {"large_group": {
            "enabled": True,
            "minimum_party_size": 12,
            "reason": "large_group_custom_quote",
        }},
        "operator_configuration": {"lead_capture": {
            "enabled": True,
            "require_supported_route": True,
            "require_party_size": True,
            "require_departure_window": True,
        }},
    }

    assert _decision_contract_error(decision, {
        "customer_text": "我们11个人想了解桃花9日行程",
        "reception_policy": policy,
    }) == "deepseek_unresolved_route_must_not_collect_contact"


def test_contact_channel_reply_preserves_current_route():
    decision = EvaluationDecision.parse({
        "action": "reply",
        "branch": "unclassified",
        "intent": "contact",
        "reply": "可以，請直接發送您的LINE ID。",
        "route_variant": "",
        "lead_action": "ask",
        "journey_stage": "contact_requested",
    })
    assert _decision_contract_error(decision, {
        "customer_text": "可以用LINE联系吗？",
        "route_variant": "peach_9d_2027",
    }) == "deepseek_contact_reply_lost_current_route"


def test_mature_configured_journey_requires_model_owned_contact_ask():
    decision = EvaluationDecision.parse({
        "action": "reply",
        "branch": "peach_9d",
        "intent": "price",
        "reply": "住宿與參考價格已為您整理。",
        "route_variant": "peach_9d_2027",
        "slots": {"party_size": "2位", "departure_window": "明年3月底"},
        "slot_evidence": {"party_size": "2位", "departure_window": "明年3月底"},
        "covered_content_groups": ["price_reference", "hotel_reference"],
        "evidence_refs": ["route.9.price", "route.shared.hotel_reference"],
        "lead_action": "none",
    })
    policy = {
        "route_switch": {"allowed_routes": list(ROUTES)},
        "handoff": {"large_group": {
            "enabled": True,
            "minimum_party_size": 12,
            "reason": "large_group_custom_quote",
        }},
        "operator_configuration": {"lead_capture": {
            "enabled": True,
            "require_supported_route": True,
            "require_party_size": True,
            "require_departure_window": True,
            "ask_after_answered_topics": 2,
        }},
    }
    assert _decision_contract_error(decision, {
        "customer_text": "我们2位，明年3月底出发，住宿和价格怎样？",
        "route_variant": "peach_9d_2027",
        "reception_policy": policy,
    }) == "deepseek_lead_should_ask"


def test_visual_answer_requires_model_to_select_an_available_matching_material():
    base = {
        "action": "reply",
        "branch": "peach_11d",
        "intent": "other",
        "reply": "住宿会参考页面展示的客房与供氧设备，实际安排以团期确认为准。",
        "route_variant": "peach_11d_2027",
        "content_group_key": "hotel_reference",
        "covered_content_groups": ["hotel_reference"],
        "evidence_refs": ["route.shared.hotel_reference"],
    }
    case = {
        "customer_text": "住宿怎样？",
        "route_variant": "peach_11d_2027",
        "journey": {"sent_content_groups": ["itinerary_overview"]},
        "available_materials": [{
            "key": "routes12-hilton-room",
            "routes": ["peach_11d_2027"],
            "content_group_key": "hotel_reference",
        }],
    }

    missing = EvaluationDecision.parse({**base, "material_keys": []})
    assert _decision_contract_error(missing, case) == "deepseek_visual_material_required"

    selected = EvaluationDecision.parse({**base, "material_keys": ["routes12-hilton-room"]})
    assert _decision_contract_error(selected, case) is None


def test_material_from_secondary_covered_group_survives_transport_validation():
    context = {
        "customer_text": "住宿和价格怎样？",
        "context_messages": [],
        "context_complete": True,
        "memory": {},
        "available_materials": [{
            "key": "routes12-hilton-room",
            "routes": ["peach_11d_2027"],
            "content_group_key": "hotel_reference",
        }],
    }

    def model_call(_packet):
        return EvaluationDecision.parse({
            "action": "reply",
            "branch": "peach_11d",
            "intent": "price",
            "reply": "参考价格与住宿资料如下。",
            "route_variant": "peach_11d_2027",
            "content_group_key": "price_reference",
            "covered_content_groups": ["price_reference", "hotel_reference"],
            "material_keys": ["routes12-hilton-room"],
            "evidence_refs": ["route.11.price", "route.shared.hotel_reference"],
        }), [], "hash"

    decision, _, _, _ = generate_decision(context, model_call=model_call)

    assert decision.material_keys == ["routes12-hilton-room"]


def test_route_intro_with_missing_date_does_not_ask_contact_early():
    decision = EvaluationDecision.parse({
        "action": "reply",
        "branch": "peach_9d",
        "intent": "route_intro",
        "reply": "先為您介紹9日行程，方便留下LINE嗎？",
        "route_variant": "peach_9d_2027",
        "slots": {"party_size": "11个人"},
        "slot_evidence": {"party_size": "11个人"},
        "covered_content_groups": ["itinerary_overview", "contact_request"],
        "evidence_refs": ["route.9.overview"],
        "lead_action": "ask",
        "journey_stage": "contact_requested",
    })
    policy = {
        "route_switch": {"allowed_routes": list(ROUTES)},
        "handoff": {"large_group": {
            "enabled": True,
            "minimum_party_size": 12,
            "reason": "large_group_custom_quote",
        }},
        "operator_configuration": {"lead_capture": {
            "enabled": True,
            "require_supported_route": True,
            "require_party_size": True,
            "require_departure_window": True,
            "ask_after_answered_topics": 2,
        }},
    }
    assert _decision_contract_error(decision, {
        "customer_text": "我们11个人想了解桃花9日行程",
        "reception_policy": policy,
    }) == "deepseek_lead_too_early"


def test_raw_chat_policy_limits_reply_length_and_material_count():
    common = {
        "action": "reply", "branch": "unclassified", "intent": "other",
        "route_variant": "", "covered_content_groups": [], "reply_options": [],
    }
    with pytest.raises(ValueError, match="deepseek_reply_too_long"):
        EvaluationDecision.parse({**common, "reply": "旅" * 201})
    with pytest.raises(ValueError, match="deepseek_too_many_materials"):
        EvaluationDecision.parse({
            **common, "reply": "为您提供资料。", "material_keys": ["a", "b", "c"]
        })


def test_dataset_excludes_future_human_answer(session_factory):
    with session_factory() as db:
        user = db.scalar(select(User))
        inbox = InboxBinding(tenant_id=1, chatwoot_inbox_id=128859, name="Facebook", channel_type="Channel::FacebookPage")
        db.add(inbox)
        db.flush()
        conversation = ConversationState(tenant_id=1, inbox_binding_id=inbox.id, chatwoot_conversation_id=26)
        db.add(conversation)
        db.flush()
        db.add_all([
            MessageEvent(conversation_state_id=conversation.id, chatwoot_message_id=1, direction="outgoing", content="欢迎", created_at="2026-01-01T00:00:00+00:00"),
            MessageEvent(conversation_state_id=conversation.id, chatwoot_message_id=2, direction="incoming", content="想了解林芝桃花9日", created_at="2026-01-01T00:01:00+00:00"),
            MessageEvent(conversation_state_id=conversation.id, chatwoot_message_id=3, direction="incoming", content="两个人", created_at="2026-01-01T00:01:01+00:00"),
            MessageEvent(conversation_state_id=conversation.id, chatwoot_message_id=4, direction="outgoing", content="这是历史人工答案", created_at="2026-01-01T00:02:00+00:00"),
        ])
        db.flush()
        dataset = build_dataset(db, user, "test", 128859)
        db.commit()
        case = db.scalar(select(EvaluationCase).where(EvaluationCase.dataset_id == dataset.id))
        assert dataset.case_count == 1
        assert case.target_message_ids == [2, 3]
        assert case.reference_answer == "这是历史人工答案"
        assert all(item["content"] != "这是历史人工答案" for item in case.context_messages)


def test_create_run_is_idempotent_and_never_creates_outbound(session_factory):
    with session_factory() as db:
        user = db.scalar(select(User))
        inbox = InboxBinding(tenant_id=1, chatwoot_inbox_id=128859, name="Facebook", channel_type="Channel::FacebookPage")
        db.add(inbox)
        db.flush()
        conversation = ConversationState(tenant_id=1, inbox_binding_id=inbox.id, chatwoot_conversation_id=27)
        db.add(conversation)
        db.flush()
        db.add(MessageEvent(conversation_state_id=conversation.id, chatwoot_message_id=10, direction="incoming", content="桃花11日", created_at="2026-01-01T00:00:00+00:00"))
        db.flush()
        dataset = build_dataset(db, user, "test", 128859)
        first = create_run(db, dataset, user)
        second = create_run(db, dataset, user)
        db.commit()
        assert first.id == second.id
        assert db.scalar(select(func.count(EvaluationResult.id))) == 1
        assert db.scalar(select(func.count(OutboundMessage.id))) == 0
