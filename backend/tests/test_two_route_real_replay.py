from types import SimpleNamespace
from copy import deepcopy

import pytest

from app.two_route_real_replay import (
    RealTurn,
    audit_route_packages,
    categories_for,
    infer_route_from_messages,
    mask_sensitive,
    _approved_reply,
    _explicit_current_route,
    _make_case,
    _requires_handoff,
    _testable_customer_question,
)
from app.route_packages import ROUTES


def test_dataset_route_inference_uses_latest_explicit_customer_choice():
    route, evidence = infer_route_from_messages([
        "我先看看桃花加珠峰11日",
        "後來想改成桃花9日，不上珠峰",
    ])
    assert route == "peach_9d_2027"
    assert "不上珠峰" in evidence

    route, _ = infer_route_from_messages(["這條總共11天"])
    assert route == "peach_11d_2027"

    route, _ = infer_route_from_messages(["預計3月9日出發"])
    assert route == ""


def test_dataset_categories_are_sampling_metadata_not_reply_rules():
    categories = categories_for("請問兩位出發的9日行程價格和酒店如何？")
    assert {"route_entry", "party_departure", "hotel_vehicle_media", "price_availability"} <= set(categories)


def test_personalised_discount_is_sampled_but_not_forced_to_handoff():
    assert "price_availability" in categories_for("成都自己訂房間兩位減多少")
    assert not _requires_handoff("台灣出發在哪報到，訂金多少", categories_for("台灣出發在哪報到，訂金多少"))
    assert _requires_handoff("我要投訴並找人工客服", categories_for("我要投訴並找人工客服"))


def test_attachment_placeholder_does_not_create_a_media_question():
    assert categories_for("[图片]\n感謝詳細回覆我和成員討論後做決定，謝謝") == []


def _turn(text: str, *, context: list[dict] | None = None, categories: list[str] | None = None) -> RealTurn:
    return RealTurn(
        case_key="case", conversation_state_id=1, conversation_id=1, contact_id=1,
        message_ids=[1], customer_text=text, context_messages=context or [], reference_answer="",
        categories=categories if categories is not None else categories_for(text),
        inferred_route="", route_evidence="", created_at="2026-08-30T00:00:00+00:00",
    )


def test_offline_route_score_only_labels_unambiguous_current_selection():
    assert _explicit_current_route("改看桃花9日，不上珠峰") == "peach_9d_2027"
    assert _explicit_current_route("我想了解桃花加珠峰11日") == "peach_11d_2027"
    assert _explicit_current_route("桃花9日和桃花加珠峰11日有什麼差別？") is None
    assert _explicit_current_route("請問費用") is None
    assert _explicit_current_route("我剛去過阿里，現在想看林芝長線") is None
    assert _explicit_current_route("其他時間，西藏自己包團，不上珠峰") is None


def test_punctuated_altitude_risk_and_closing_messages_are_classified_correctly():
    assert "safety_handoff" in categories_for("我擔心我的身體會有高，反")
    assert not _testable_customer_question(_turn("謝謝資料，我們會多比較參考一下。"))
    assert _testable_customer_question(_turn("我們會多比較，請問費用是多少？"))
    assert not _testable_customer_question(_turn("感謝！我發錯訊息了，有機會一定到西藏"))
    assert not _testable_customer_question(_turn("ok"))


def test_channel_ack_without_identifier_is_not_captured_or_reprompted():
    case = _make_case(
        _turn("已加你line喔"), "long_context_memory", "peach_9d_2027",
        context_mode="historical_route_binding",
    )
    assert case.expected_action == "no_action"
    assert case.expected_lead_action == "none"

    link_case = _make_case(
        _turn("https://line.me/ti/p/Uyfeasb797"), "lead_capture_and_contact", "peach_9d_2027",
        context_mode="historical_route_binding", contact_ready=True,
    )
    assert link_case.expected_action == "handoff"
    assert link_case.expected_lead_action == "captured"


def test_sensitive_values_are_masked_in_reports():
    value = mask_sensitive("Email abc.travel@example.com，LINE: trip_2027，電話 +886 912-345-678，https://line.me/ti/p/Uyfeasb797")
    assert "abc.travel" not in value
    assert "trip_2027" not in value
    assert "912-345" not in value
    assert "Uyfeasb797" not in value


def test_wechat_id_with_full_width_colon_is_masked():
    value = mask_sensitive("WeChat ID： CHANGMeiyann")
    assert "CHANGMeiyann" not in value


@pytest.fixture
def original_input(session_factory, monkeypatch):
    import hashlib
    from app import two_route_real_replay as replay
    from app.models import ConversationState, MessageEvent
    monkeypatch.setattr(replay.settings, "live_sop_enabled", False)
    with session_factory() as db:
        state = ConversationState(tenant_id=1, inbox_binding_id=1, chatwoot_conversation_id=21)
        db.add(state)
        db.flush()
        for mid, text in [(101, "WeChat ID: synthetic_fixture_only"), (102, "Thank you")]:
            db.add(MessageEvent(conversation_state_id=state.id, chatwoot_message_id=mid,
                direction="incoming", content=text, created_at=f"2026-09-13T00:00:{mid - 100:02d}+00:00"))
        db.commit()
        raw = "WeChat ID: synthetic_fixture_only\nThank you"
        case = _make_case(_turn(replay.mask_sensitive(raw)), "unit", "peach_9d_2027",
                          context_mode="historical_route_binding")
        case.source_case_key = hashlib.sha256(b"21:101,102").hexdigest()[:24]
        case.conversation_id = 21
        yield db, case, raw, state


def test_restore_original_input_is_select_only_and_memory_only(original_input):
    import hashlib
    import json
    from sqlalchemy import event
    from app import two_route_real_replay as replay
    db, case, raw, state = original_input
    before = deepcopy(case)
    statements = []
    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    event.listen(db.bind, "before_cursor_execute", capture)
    try:
        restored, provenance = replay.restore_masked_case_input(db, case)
    finally:
        event.remove(db.bind, "before_cursor_execute", capture)
    assert restored.customer_text == raw and case == before
    assert restored.context_messages == case.context_messages
    assert restored.expected_action == case.expected_action
    assert all(query.lstrip().upper().startswith("SELECT") for query in statements)
    assert not db.new and not db.dirty
    assert provenance["input_sha256"] == hashlib.sha256(raw.encode()).hexdigest()
    assert provenance["chatwoot_message_ids"] == [101, 102]
    assert "synthetic_fixture_only" not in json.dumps(provenance)


@pytest.mark.parametrize("problem", ["missing", "wrong_mask", "ambiguous", "masked_db"])
def test_restore_never_guesses_missing_or_changed_source(original_input, problem):
    from sqlalchemy import select
    from app import two_route_real_replay as replay
    from app.models import ConversationState, MessageEvent
    db, case, raw, state = original_input
    if problem == "missing":
        case.source_case_key = "missing"
    elif problem == "wrong_mask":
        case.customer_text += " different frozen question"
    elif problem == "masked_db":
        row = db.scalar(select(MessageEvent).where(MessageEvent.chatwoot_message_id == 101))
        row.content = replay.mask_sensitive(row.content)
        db.commit()
    else:
        second = ConversationState(tenant_id=2, inbox_binding_id=2, chatwoot_conversation_id=21)
        db.add(second)
        db.flush()
        for mid, text in [(101, "WeChat ID: synthetic_fixture_only"), (102, "Thank you")]:
            db.add(MessageEvent(conversation_state_id=second.id, chatwoot_message_id=mid,
                direction="incoming", content=text, created_at=f"2026-09-13T00:00:{mid - 100:02d}+00:00"))
        db.commit()
    with pytest.raises(ValueError, match="replay_original_input_"):
        replay.restore_masked_case_input(db, case)


def test_restore_rejects_live_runtime(original_input, monkeypatch):
    from app import two_route_real_replay as replay
    db, case, *_ = original_input
    monkeypatch.setattr(replay.settings, "live_sop_enabled", True)
    with pytest.raises((RuntimeError, ValueError), match="evaluation_read_only_mode|live_sop_requires_live_outbound"):
        replay.restore_masked_case_input(db, case)


@pytest.mark.parametrize("explicit", [False, True])
def test_run_case_passes_isolated_policy_and_preserves_two_argument_api(monkeypatch, explicit):
    from app import two_route_real_replay as replay
    from app.deepseek_evaluation import EvaluationCallError
    case = _make_case(_turn("Question?"), "unit", "peach_9d_2027", context_mode="historical_route_binding")
    policy = {"reply_style": {"max_characters": 123}, "operator": ["private policy"]}
    expected = deepcopy(policy if explicit else replay.JOURNEY_POLICY)
    seen = []
    def generate(context):
        seen.append(deepcopy(context["reception_policy"]))
        context["reception_policy"].clear()
        raise EvaluationCallError("test", [], "a" * 64)
    monkeypatch.setattr(replay, "generate_decision", generate)
    monkeypatch.setattr("app.release_provenance.source_fingerprint", lambda: "source")
    result = replay.run_case(case, [], reception_policy=policy) if explicit else replay.run_case(case, [])
    assert seen == [expected]
    assert result["reception_policy_fingerprint"] == replay.reception_policy_fingerprint(expected)
    assert policy["reply_style"]["max_characters"] == 123
    assert "private policy" not in str(result)


def test_recovered_contact_is_redacted_everywhere_in_success_and_error_results(monkeypatch):
    import json
    from app import two_route_real_replay as replay
    case = _make_case(_turn("WeChat ID: synthetic_fixture_only"), "unit", "peach_9d_2027",
                      context_mode="historical_route_binding")
    token = "synthetic_fixture_only"
    payload = {"model_decision": {"contact_values": {"wechat": token},
                                 "reply": token, "slot_evidence": {"wechat": token}},
               "final_decision": {"slots": {"wechat": token}},
               "model_calls": [{"error": token}], "request_hash": "1234567890abcdef" * 4}
    monkeypatch.setattr(replay, "_run_case", lambda *args, **kwargs: deepcopy(payload))
    monkeypatch.setattr("app.release_provenance.source_fingerprint", lambda: "source")
    result = replay.run_case(case, [])
    assert token not in json.dumps(result)
    assert result["request_hash"] == payload["request_hash"]


def test_two_route_package_rules_are_complete():
    audit = audit_route_packages()
    assert audit["passed"] is True
    for route_id in ("peach_9d_2027", "peach_11d_2027"):
        checks = audit["routes"][route_id]["checks"]
        assert checks["six_silence_interval_touches"] is True
        assert checks["raw_analysis_policy_bound"] is True
        assert checks["all_nodes_offer_reviewed_sequence"] is True
        assert checks["large_group_handoff_configured"] is True


def test_customer_reply_can_be_natural_but_must_not_make_unsupported_claims():
    approved = ROUTES["peach_9d_2027"]["groups"]["itinerary_overview"]["text"]
    assert _approved_reply(SimpleNamespace(action="reply", reply=approved), "peach_9d_2027")
    assert _approved_reply(SimpleNamespace(
        action="reply", reply="我先回答您的問題，再為您整理行程重點。", evidence_refs=[]
    ), "peach_9d_2027")
    assert not _approved_reply(SimpleNamespace(
        action="reply", reply="9月出發沒問題，而且保證有位。", evidence_refs=[]
    ), "peach_9d_2027")

    acknowledgement = SimpleNamespace(
        action="reply", reply="了解，人數確認後再告訴我即可。", content_group_key="",
        intent="other", material_keys=[],
    )
    assert _approved_reply(acknowledgement, "peach_9d_2027")


def test_health_disclaimer_is_not_scored_as_a_health_guarantee():
    assert _approved_reply(SimpleNamespace(action="reply", reply="有供氧不代表一定不會高反。"), "peach_9d_2027")
    assert not _approved_reply(SimpleNamespace(action="reply", reply="有供氧，一定不會高反。"), "peach_9d_2027")
    assert not _approved_reply(SimpleNamespace(action="reply", reply="一般不代表一定不會高反，但我們一定不會高反。"), "peach_9d_2027")


@pytest.mark.parametrize("route", ["peach_9d_2027", "peach_11d_2027"])
def test_replay_initial_progress_is_explicit_simulation_not_historical_evidence(monkeypatch, route):
    from app import two_route_real_replay as replay
    from app.decision_service import generate_decision
    from app.deepseek_evaluation import EvaluationDecision
    from app.route_reply import route_snapshot_from_values

    case = _make_case(_turn("Question?"), "unit", route, context_mode="historical_route_binding")
    case.slots = {"party_size": 2}
    case.sent_groups = ["hotel_reference"]
    original = deepcopy(case)
    calls = []

    def model(packet):
        calls.append(packet)
        journey = packet["journey"]
        spec = route_snapshot_from_values(route, journey["slots"])
        progress = journey["content_progress"]["hotel_reference"]
        assert spec and not journey["automatic_delivery_paused"]
        assert progress["text_delivered"]
        assert progress["asset_keys"] == spec["groups"]["hotel_reference"]["assets"]
        assert journey["slots"]["_evaluation_fixture"]["synthetic"]
        return EvaluationDecision("reply", ROUTES[route]["branch"], "other", reply="Answer.", route_variant=route), [], "test"

    monkeypatch.setattr(replay, "generate_decision", lambda context: generate_decision(context, model))
    result = replay._run_case(case, [])
    assert len(calls) == 1
    assert case == original
    assert result["status"] != "infrastructure_failed"
    assert result["trace"]["simulated_initial_progress"] == {
        "simulated": True, "real_delivery_evidence": False, "route_variant": route,
        "assumed_sent_groups": ["hotel_reference"],
        "basis": "reviewed_snapshot_full_text_and_configured_asset_keys",
    }
