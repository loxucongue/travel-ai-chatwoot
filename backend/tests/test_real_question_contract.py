from app.reply_understanding import _parse, CustomerUnderstanding
from app.web_knowledge import select_understood_web_facts
from app.reply_planning import build_reply_plan
from app.route_packages import JOURNEY_POLICY
from app.route_packages import ROUTES
from app.reply_generation import deterministic_system_reply
import pytest


def parse(value, text="自己要準備嗎", history=None):
    return _parse({"intent": "other", **value}, customer_text=text, allowed_routes=set(),
                  allowed_slots=set(), allowed_rule_ids=set(), history=history)


def test_reference_requires_matching_message_and_quote():
    detail = {"topic": "oxygen_service", "question": "氧氣瓶是否自備", "evidence_quote": "自己要準備嗎",
              "reference_message_id": "10", "reference_quote": "氧氣瓶"}
    assert parse({"question_details": [detail]}).question_details == []
    result = parse({"question_details": [detail]}, history=[{"id": 10, "content": "有氧氣瓶"}])
    assert len(result.question_details) == 1
    assert parse({"question_details": [detail]}, history=[{"id": 10, "content": "氧氣瓶", "private": True}]).question_details == []


def test_pause_requires_current_evidence_and_stops_plan():
    value = {"semantic_signals": ["pause_proactive"], "engagement_evidence": "暫時不用"}
    assert "pause_proactive" not in parse(value).semantic_signals
    understanding = parse(value, text="暫時不用")
    plan = build_reply_plan({"customer_text": "暫時不用", "reception_policy": JOURNEY_POLICY}, understanding)
    assert plan.action == "no_action" and plan.stop_automation
    assert not plan.allowed_asset_ids


def test_contact_cannot_retrieve_wechat_payment():
    contact = {"id": "web.1", "module_key": "official_contact", "text": "LINE 聯絡顧問"}
    payment = {"id": "web.2", "module_key": "travel_preparation", "text": "微信支付可以綁定信用卡"}
    understanding = CustomerUnderstanding(intent="contact", customer_questions=["contact"],
        semantic_signals=["asks_contact_channel"], requested_contact_channel="wechat")
    selected = select_understood_web_facts({"customer_text": "微信可以嗎", "global_knowledge_candidates": [contact, payment]}, understanding)
    assert selected["global_knowledge_facts"] == [contact]
    assert selected["knowledge_selection"]["rejected"][0]["fact_id"] == "web.2"


def test_oxygen_reference_does_not_retrieve_travel_preparation():
    understanding = CustomerUnderstanding(intent="other", customer_questions=["oxygen_service"],
        discussion_subject="下車氧氣瓶", question_details=[{"question": "下車氧氣瓶是否自備"}])
    selected = select_understood_web_facts({"customer_text": "自己要準備嗎", "global_knowledge_candidates": [
        {"id": "web.1", "module_key": "accommodation_oxygen_and_health", "text": "氧氣設備配置依路線確認"},
        {"id": "web.2", "module_key": "travel_preparation", "text": "自行準備網卡"},
    ]}, understanding)
    assert [f["id"] for f in selected["global_knowledge_facts"]] == ["web.1"]


@pytest.mark.parametrize("topic", ["weather", "hotel", "oxygen_service", "eligibility", "payment"])
def test_unselected_direct_question_is_not_replaced_by_route_opening(topic):
    understanding = CustomerUnderstanding(intent="other", customer_questions=[topic])
    plan = build_reply_plan({"customer_text": "具體問題", "reception_policy": JOURNEY_POLICY}, understanding)
    assert "direct_customer_question" in plan.safety_flags
    assert deterministic_system_reply(plan) is None
    assert not plan.allowed_asset_ids


def test_question_details_rejects_non_array():
    with pytest.raises(ValueError, match="question_details_must_be_array"):
        parse({"question_details": {"topic": "hotel"}})


def test_unselected_hotel_question_gets_hotel_facts_not_route_overviews():
    plan = build_reply_plan({"customer_text": "住宿如何", "reception_policy": JOURNEY_POLICY},
                            CustomerUnderstanding(intent="other", customer_questions=["hotel"]))
    expected = {fact for route in ROUTES.values()
                for fact in route["groups"].get("hotel_reference", {}).get("evidence", [])}
    assert expected and expected.intersection(plan.allowed_fact_ids)
    assert not any(fact.endswith(".overview") for fact in plan.allowed_fact_ids)


@pytest.mark.parametrize("direction,private,valid", [("incoming", False, True), ("outgoing", False, False), ("incoming", True, False)])
def test_historical_selection_requires_public_customer_evidence(direction, private, valid):
    value = {"intent": "other", "historical_route_choice": {
        "route_variant": "peach_11d_2027", "message_id": "10", "evidence_quote": "我要桃花加珠峰11日"}}
    history = [{"id": 10, "direction": direction, "private": private, "content": "我要桃花加珠峰11日"}]
    result = _parse(value, customer_text="下車怎麼辦", allowed_routes=set(ROUTES),
                    allowed_slots=set(), allowed_rule_ids=set(), history=history)
    assert bool(result.historical_route_choice) == valid
    plan = build_reply_plan({"reception_policy": JOURNEY_POLICY}, result)
    assert (plan.route_variant == "peach_11d_2027") == valid
    assert result.slot_updates == {}


def test_confirmation_is_recorded_without_sending_product_claim():
    from app.realtime_reply_pipeline import run_realtime_reply_pipeline
    from app.reply_generation import GeneratedReply
    from app.reply_fact_verification import FactVerification
    decision, _, _, trace = run_realtime_reply_pipeline(
        {"customer_text": "有優惠嗎", "reception_policy": JOURNEY_POLICY},
        understanding_node=lambda _: (CustomerUnderstanding(intent="price", customer_questions=["price"]), [], "u"),
        generation_node=lambda *_: (GeneratedReply("優惠需要核對。", None, [], []), [], "g"),
        verification_node=lambda *_: (FactVerification(True, [], True, [], ["兩人同行優惠"]), [], "v"),
    )
    assert decision.action == "handoff"
    assert decision.handoff_reason == "knowledge_confirmation_required"
    assert trace["confirmation_questions"] == ["兩人同行優惠"]
    assert not decision.material_keys


def test_multiple_answers_share_sender_and_question_is_last():
    from app.delivery_plan import ordered_delivery_parts
    parts = ordered_delivery_parts("住宿安排。\n\n用車安排。", [], "text_then_assets",
        text_segments=["住宿安排。", "用車安排。"], follow_up_question="幾位呢？", interval_seconds=2)
    assert [part.content for part in parts] == ["住宿安排。", "用車安排。", "幾位呢？"]
    assert len({part.part_id for part in parts}) == 3
    assert [part.interval_seconds for part in parts] == [0, 2, 2]
    assert parts[-1].is_follow_up


def test_delivery_segments_cannot_change_verified_body():
    from app.delivery_plan import ordered_delivery_parts
    with pytest.raises(ValueError, match="delivery_text_segments_mismatch"):
        ordered_delivery_parts("已核驗正文", [], "text_only", text_segments=["另外編造的正文"])


def test_contact_collection_is_authorized_action_not_invented_official_account(monkeypatch):
    from app import reply_fact_verification as verifier
    from app.reply_generation import GeneratedReply
    from app.reply_planning import FollowUp
    captured = {}
    def node(**kwargs):
        captured.update(kwargs)
        return verifier.FactVerification(True), [], "verified"
    monkeypatch.setattr(verifier, "call_json_node", node)
    plan = build_reply_plan({"reception_policy": JOURNEY_POLICY}, CustomerUnderstanding(intent="other"))
    from dataclasses import replace
    plan = replace(plan, follow_up=FollowUp("contact", "wechat", "可以留下微信嗎？"))
    verifier.call_reply_fact_verifier({}, plan, GeneratedReply("可以留下您的微信。", plan.follow_up, [], []))
    assert captured["input_data"]["planned_system_action"]["contact_collection_channel"] == "wechat"
    assert "不支持編造公司的微信帳號" in captured["system_prompt"]


def test_realtime_confirmation_preserves_answerable_questions():
    from app.realtime_reply_pipeline import run_realtime_reply_pipeline
    from app.reply_generation import GeneratedReply
    from app.reply_fact_verification import FactVerification
    captured = {}
    def generate(context, plan):
        captured.update(context=context, plan=plan)
        return GeneratedReply("費用包含住宿；道路現況需要核對。", None, [], []), [], "g"
    result, _, _, trace = run_realtime_reply_pipeline(
        {"customer_text": "住宿含在費用嗎？目前道路通嗎？", "route_variant": "peach_11d_2027", "reception_policy": JOURNEY_POLICY},
        understanding_node=lambda _: (CustomerUnderstanding(intent="price", customer_questions=["price"], semantic_signals=["current_conditions"]), [], "u"),
        generation_node=generate,
        verification_node=lambda *_: (FactVerification(True, [], True, [], ["道路現況"]), [], "v"),
    )
    assert captured["plan"].action == "reply"
    assert captured["context"]["reply_reserved_characters"] > 0
    assert "service.current_conditions" in captured["plan"].allowed_fact_ids
    assert "route.11.price" in captured["plan"].allowed_fact_ids
    assert result.action == "handoff"
    assert result.reply.startswith("費用包含住宿")
    assert trace["confirmation_questions"] == ["道路現況"]


def test_profile_update_does_not_erase_compound_question_facts():
    from types import SimpleNamespace
    understanding = CustomerUnderstanding(
        intent="price", customer_questions=["price", "itinerary", "highlights"],
        semantic_signals=["details_provided"],
        slot_updates={"party_size": SimpleNamespace(value=2, evidence_quote="二人")},
    )
    plan = build_reply_plan({
        "route_variant": "peach_11d_2027", "reception_policy": JOURNEY_POLICY,
        "journey": {"sent_content_groups": list(ROUTES["peach_11d_2027"]["groups"])},
    }, understanding)
    assert "route.11.price" in plan.allowed_fact_ids
    assert any(key.endswith((".overview", ".days")) for key in plan.allowed_fact_ids)


@pytest.mark.parametrize("signal", ["current_conditions", "customization_request"])
def test_specialist_question_keeps_other_requested_facts(signal):
    plan = build_reply_plan({"route_variant": "peach_11d_2027", "reception_policy": JOURNEY_POLICY},
        CustomerUnderstanding(intent="price", customer_questions=["price"], semantic_signals=[signal]))
    assert "route.11.price" in plan.allowed_fact_ids


def test_transport_question_can_use_bound_itinerary_transfer_evidence():
    plan = build_reply_plan({"route_variant": "peach_11d_2027", "reception_policy": JOURNEY_POLICY},
        CustomerUnderstanding(intent="price", customer_questions=["price", "transport"]))
    assert "route.11.price" in plan.allowed_fact_ids
    assert "route.11.days" in plan.allowed_fact_ids


@pytest.mark.parametrize("body", [
    "機票可以提供航班建議，也可以協助代訂。",
    "機票可以提供航班建議，也可以協助代訂。請您提供出發日期。",
    "可以留下您的微信。",
    "路況需要依出發日期核對，請您提供日期。",
    "酒店的衛生間與客房照片可以讓您查看。",
])
def test_parser_preserves_body_for_semantic_model_verification(body):
    from dataclasses import replace
    from app.reply_generation import _parse as parse_reply
    plan = build_reply_plan({"customer_text": "你好", "reception_policy": JOURNEY_POLICY},
                            CustomerUnderstanding(intent="other"))
    plan = replace(plan, follow_up=None, allowed_asset_ids=[])
    result = parse_reply({"body": body, "used_fact_ids": [], "asset_ids": []},
                         plan=plan, max_characters=200, max_images=2)
    assert result.body == body


def test_semantic_verifier_owns_unplanned_request_correction():
    from app.reply_fact_verification import _system_prompt
    prompt = _system_prompt()
    assert "沒有問號的請求也屬於追問" in prompt
    assert "是服務陳述" in prompt
    assert "交回原生成節點重寫，不直接刪改正文" in prompt


def test_contract_violation_cannot_pass_with_supported_facts():
    from app.reply_fact_verification import _parse
    result = _parse({"supported": True, "unsupported_claims": [], "relevant": True,
                     "unanswered_questions": [], "contract_violations": ["正文重複留資"]})
    assert result.supported is True
    assert result.relevant is False
    assert result.contract_violations == result.unanswered_questions == ["正文重複留資"]


def test_verifier_never_merges_legal_followup_into_proposed_body(monkeypatch):
    from dataclasses import replace
    from app.reply_planning import FollowUp
    from app.reply_generation import GeneratedReply, GeneratedFollowUp
    from app.reply_fact_verification import call_reply_fact_verifier, FactVerification
    plan = build_reply_plan({"customer_text": "你好", "reception_policy": JOURNEY_POLICY},
                            CustomerUnderstanding(intent="other"))
    plan = replace(plan, follow_up=FollowUp("contact", "line", "方便留個LINE嗎？"))
    generated = GeneratedReply("可以幫您整理行程。", GeneratedFollowUp("contact", "line", plan.follow_up.question), [], [])
    def node(**kwargs):
        data = kwargs["input_data"]
        assert "proposed_reply" not in data
        assert data["proposed_body"] == generated.body
        assert data["planned_follow_up"] == plan.follow_up.question
        return FactVerification(True), [], "verified"
    monkeypatch.setattr("app.reply_fact_verification.call_json_node", node)
    assert call_reply_fact_verifier({}, plan, generated)[0].relevant


@pytest.mark.parametrize("bad_body", [
    "房間配有供氧設備，一定不會高反。",
    "房間配有供氧設備。請您告訴我人數和日期。",
    "我把照片傳給您看。",
    "目前可接待的已上线线路有两条。",
])
@pytest.mark.parametrize("rewrite_passes", [True, False])
def test_model_contract_rejection_rewrites_or_blocks_without_editing(bad_body, rewrite_passes):
    from app.reply_generation import GeneratedReply
    from app.reply_fact_verification import FactVerification
    from app.realtime_reply_pipeline import run_realtime_reply_pipeline
    source = {"customer_text": "桃花11日的飯店設備如何？",
              "route_variant": "peach_11d_2027", "reception_policy": JOURNEY_POLICY,
              "journey": {"sent_content_groups": ["advisor_greeting", "brand_positioning"]}}
    understanding = CustomerUnderstanding(intent="hotel", customer_questions=["hotel"],
        route_candidate="peach_11d_2027", route_resolution="confirmed", route_evidence="桃花11日")
    calls = []
    checked = []
    good_body = "房間配有供氧設備。"
    def generate(context, plan):
        calls.append(context.get("reply_generation_feedback"))
        return GeneratedReply(good_body if len(calls) == 2 and rewrite_passes else bad_body,
                              None, [], []), [], "generation"
    def verify(context, plan, generated):
        checked.append(generated.body)
        accepted = generated.body == good_body
        return FactVerification(True, [], accepted, [] if accepted else ["表達合同不符"]), [], "check"
    decision, _, _, trace = run_realtime_reply_pipeline(source,
        understanding_node=lambda _: (understanding, [], "understanding"),
        generation_node=generate, verification_node=verify)
    assert checked[0] == bad_body
    assert len(calls) == 2
    assert calls[1]["unanswered_questions"] == ["表達合同不符"]
    assert bad_body not in decision.reply
    if rewrite_passes:
        assert decision.reply == good_body
    else:
        assert decision.action == "handoff"
        assert decision.handoff_reason == "knowledge_verification_required"
        assert trace["question_coverage_passed"] is False


def test_new_confirmation_keeps_verified_answer_via_text_rewrite():
    from app.realtime_reply_pipeline import run_realtime_reply_pipeline
    from app.reply_generation import GeneratedReply
    from app.reply_fact_verification import FactVerification
    calls = []
    def generate(context, plan):
        calls.append(plan)
        if len(calls) == 1:
            return GeneratedReply("房間照片給您看；特殊房型需核對。", None, [], ["photo"]), [], "g1"
        assert not plan.allowed_asset_ids and context["reply_reserved_characters"] > 0
        return GeneratedReply("已公布住宿含供氧設備，特殊房型需核對。", None, [], []), [], "g2"
    decision, _, _, trace = run_realtime_reply_pipeline(
        {"route_variant": "peach_11d_2027", "customer_text": "特殊房型？", "reception_policy": JOURNEY_POLICY},
        understanding_node=lambda _: (CustomerUnderstanding(intent="other", customer_questions=["hotel"]), [], "u"),
        generation_node=generate,
        verification_node=lambda *_: (FactVerification(True, [], True, [], ["特殊房型"]), [], "v"),
    )
    assert len(calls) == 2
    assert decision.action == "handoff" and "已公布住宿含供氧設備" in decision.reply
    assert not decision.material_keys and trace["confirmation_questions"] == ["特殊房型"]


def test_oxygen_contract_requires_scope_and_excludes_usage_instructions():
    from app.reply_generation import _system_prompt as generator
    from app.reply_fact_verification import _system_prompt as verifier
    assert "5000" in generator(200, 2) and "5000" in verifier()
    assert "吸氧時機、頻率、流量" in verifier()


def test_health_disclaimer_is_not_a_guarantee():
    from types import SimpleNamespace
    from app.two_route_real_replay import _approved_reply
    assert _approved_reply(SimpleNamespace(action="reply", reply="有供氧不能代表一定不會高反。"), "peach_11d_2027")
    assert not _approved_reply(SimpleNamespace(action="reply", reply="有供氧一定不會高反。"), "peach_11d_2027")


@pytest.mark.parametrize("error,assets", [
    ("reply_too_long_after_code_limit:body=230,budget=200", ["test-image"]),
    ("reply_too_long_after_code_limit:body=230,budget=200", []),
    ("reply_duplicate_asset_narration", ["test-image"]),
])
def test_content_contract_failure_retries_text_before_handoff(monkeypatch, error, assets):
    from dataclasses import replace
    from app import realtime_reply_pipeline as pipeline
    from app.reply_generation import GeneratedReply
    from app.reply_fact_verification import FactVerification
    from app.deepseek_evaluation import EvaluationCallError
    plan = build_reply_plan({"route_variant": "peach_11d_2027", "reception_policy": JOURNEY_POLICY}, CustomerUnderstanding(intent="price", customer_questions=["price"]))
    monkeypatch.setattr(pipeline, "build_reply_plan", lambda *_: replace(plan, action="reply", allowed_asset_ids=assets, allowed_content_group_keys=["hotel_reference"], fixed_answer_id=""))
    calls=[]
    def generate(context, current):
        calls.append(current)
        if len(calls)==1:
            raise EvaluationCallError(error, [], "bad")
        return GeneratedReply("費用已含住宿。", None, [], []), [], "good"
    decision, _, _, _ = pipeline.run_realtime_reply_pipeline(
        {"customer_text": "費用包括住宿嗎", "reception_policy": JOURNEY_POLICY},
        understanding_node=lambda _: (CustomerUnderstanding(intent="price"), [], "u"),
        generation_node=generate,
        verification_node=lambda *_: (FactVerification(True), [], "v"),
    )
    assert len(calls)==2 and not calls[-1].allowed_asset_ids
    assert decision.action=="reply" and decision.reply=="費用已含住宿。"
