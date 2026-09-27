import pytest

from app.reply_fact_verification import FactVerification
from app.reply_generation import GeneratedFollowUp, GeneratedReply
from app.silence_planning import build_silence_plan
from app.silence_touch_pipeline import run_silence_touch_pipeline
from app.route_packages import ROUTES, route_catalog_context
from app.route_reply import (
    bind_new_route_snapshot, journey_context_from_values, make_route_snapshot,
    route_snapshot_from_values, update_content_progress_values, ROUTE_SNAPSHOTS_KEY,
)


def completed_initial_context(route="peach_11d_2027"):
    source = context(route=route, stage="value_building", profile={"party_size": 2, "departure_window": "3月底"})
    slots = bind_new_route_snapshot(route, source["memory"])
    sent = []
    for key, group in ROUTES[route]["groups"].items():
        if group.get("initial_delivery"):
            slots, sent, _ = update_content_progress_values(
                route, slots, sent, key, delivered_text=group["text"], asset_keys=group["assets"],
            )
    source["journey"] = journey_context_from_values(route, "value_building", slots, sent)
    return source


def test_consecutive_delivered_dynamic_topics_do_not_repeat_or_complete_fixed_copy():
    source = completed_initial_context()
    seen = []
    for group in ("landmarks", "zhaji"):
        plan = build_silence_plan(source)
        assert plan.reply_plan.allowed_content_group_keys == [group]
        assert group not in seen
        seen.append(group)
        journey = source["journey"]
        slots, sent, complete = update_content_progress_values(
            journey["route_variant"], journey["slots"], journey["sent_content_groups"],
            group, delivered_text=f"A delivered paraphrase about {group}.",
        )
        assert not complete
        source["journey"] = journey_context_from_values(journey["route_variant"], "value_building", slots, sent)
        assert group in source["journey"]["topic_covered_groups"]
        assert group not in source["journey"]["completed_content_groups"]
        source["touch_index"] += 1
    assert not set(build_silence_plan(source).reply_plan.allowed_content_group_keys) & set(seen)


@pytest.mark.parametrize("mandatory", ["advisor_greeting", "brand_positioning", "peach_highlights", "itinerary_overview", "hotel_reference"])
@pytest.mark.parametrize("partial", ["paraphrase", "exact_text_only", "assets_only"])
def test_topic_coverage_cannot_skip_mandatory_initial_group(mandatory, partial):
    source = completed_initial_context()
    route = source["journey"]["route_variant"]
    spec = route_snapshot_from_values(route, source["journey"]["slots"])
    spec["sequence"] = [mandatory, "landmarks", "zhaji"]
    # Require both dimensions even for normally text-only greeting groups.
    spec["groups"][mandatory]["initial_delivery"] = True
    spec["groups"][mandatory]["assets"] = ["required-initial-image"]
    spec["groups"][mandatory]["delivery_mode"] = "assets_then_text"
    slots = {ROUTE_SNAPSHOTS_KEY: {route: make_route_snapshot(route, spec)}}
    slots, sent, _ = update_content_progress_values(
        route, slots, [], "landmarks", delivered_text="Already discussed landmarks.",
    )
    slots, sent, complete = update_content_progress_values(
        route, slots, sent, mandatory,
        delivered_text=spec["groups"][mandatory]["text"] if partial == "exact_text_only" else
                       "Already discussed initial topic." if partial == "paraphrase" else None,
        asset_keys=["required-initial-image"] if partial == "assets_only" else [],
    )
    assert not complete
    source["journey"] = journey_context_from_values(route, "value_building", slots, sent)
    # Even a stale legacy marker must not override the explicit completion view.
    source["journey"]["sent_content_groups"] = [mandatory]
    with route_catalog_context(route, slots):
        assert build_silence_plan(source).reply_plan.allowed_content_group_keys == [mandatory]


@pytest.mark.parametrize("status", ["draft", "submitted", "submission_unknown", "failed", "sent", "delivered", "read"])
def test_only_confirmed_topic_receipts_suppress_dynamic_group(session_factory, status):
    from app.models import ConversationState, ConversationJourney, OutboundMessage
    from app.delivery_tracking import attach_delivery_item, record_delivery_progress
    source = completed_initial_context()
    initial = source["journey"]
    spec = route_snapshot_from_values(initial["route_variant"], initial["slots"])
    spec["sequence"] = ["landmarks", "zhaji"]
    slots = {ROUTE_SNAPSHOTS_KEY: {initial["route_variant"]: make_route_snapshot(initial["route_variant"], spec)}}
    with session_factory() as db:
        state = ConversationState(tenant_id=1, inbox_binding_id=1, chatwoot_conversation_id=1154)
        db.add(state)
        db.flush()
        journey = ConversationJourney(conversation_state_id=state.id, route_variant=initial["route_variant"],
                                      slots=slots, sent_groups=[])
        db.add(journey)
        db.flush()
        out = OutboundMessage(conversation_state_id=state.id, idempotency_key="test:landmarks",
                              content="Delivered landmarks paraphrase, not the approved full text.",
                              content_type="text", status=status)
        attach_delivery_item(db, out, journey, ["landmarks"], item_id="landmarks-text")
        db.add(out)
        db.flush()
        record_delivery_progress(db, out)
        source["journey"] = journey_context_from_values(journey.route_variant, "value_building", journey.slots, journey.sent_groups)
        with route_catalog_context(journey.route_variant, journey.slots):
            plan = build_silence_plan(source)
        expected = "zhaji" if status in {"sent", "delivered", "read"} else "landmarks"
        assert plan.reply_plan.allowed_content_group_keys == [expected]
        assert "landmarks" not in source["journey"]["completed_content_groups"]


@pytest.mark.parametrize("untrusted", ["citation_only", "projected_only", "unknown_history", "old_schema"])
def test_model_references_and_unverified_topic_flags_cannot_suppress_delivery(untrusted):
    source = completed_initial_context()
    source["covered_content_groups"] = ["landmarks"]
    if untrusted != "citation_only":
        source["journey"]["topic_covered_groups"] = ["landmarks"]
    if untrusted in {"unknown_history", "old_schema"}:
        source["journey"]["content_progress"]["landmarks"] = {
            "topic_covered": True, "history_unknown": untrusted == "unknown_history",
            "schema_version": 1 if untrusted == "old_schema" else 2,
        }
    assert build_silence_plan(source).reply_plan.allowed_content_group_keys == ["landmarks"]


@pytest.mark.parametrize("stage", ["value_building", "considering", "contact_requested"])
def test_delivered_optional_topics_are_not_reintroduced_by_final_touch_fallback(stage):
    source = completed_initial_context()
    journey = source["journey"]
    route, slots, sent = journey["route_variant"], journey["slots"], journey["sent_content_groups"]
    for group in ("landmarks", "zhaji", "contact_transition", "read_check"):
        if group in ROUTES[route]["groups"]:
            slots, sent, _ = update_content_progress_values(
                route, slots, sent, group, delivered_text=f"Delivered dynamic {group}.",
            )
    source["journey"] = journey_context_from_values(route, stage, slots, sent)
    source["touch_index"] = 6
    source["lead_capture"] = {"status": "asked"}
    plan = build_silence_plan(source)
    assert plan.reply_plan.allowed_content_group_keys == []
    assert plan.reply_plan.allowed_asset_ids == []
    assert plan.reply_plan.action == "no_action"


@pytest.mark.parametrize("module", ["silence_touch", "wakeup"])
@pytest.mark.parametrize("body", ["Body without a question.", ""])
def test_silence_decision_matches_realtime_split_contract(module, body):
    from app.realtime_reply_pipeline import _compatibility_decision
    from app.silence_touch_pipeline import _decision

    silence_plan = build_silence_plan(context())
    for follow_up in (None, GeneratedFollowUp("contact", "line", "LINE?")):
        generated = GeneratedReply(body, follow_up, [], [])
        decision = _decision(silence_plan, generated, legacy_wakeup=module == "wakeup")
        realtime = _compatibility_decision(silence_plan.reply_plan, generated)
        for field in ("reply", "reply_body", "follow_up_type", "follow_up_field", "follow_up_question"):
            assert getattr(decision, field) == getattr(realtime, field)
        assert decision.wakeup_action == ("generate" if module == "wakeup" else None)


def context(
    *,
    route: str = "",
    stage: str = "route_selection",
    touch_index: int = 1,
    profile: dict | None = None,
    sent: list[str] | None = None,
    lead_status: str = "not_started",
) -> dict:
    return {
        "module": "silence_touch",
        "customer_text": "我想了解西藏桃花行程",
        "context_messages": [
            {"direction": "incoming", "content": "我想了解西藏桃花行程"},
            {"direction": "outgoing", "content": "目前有桃花9日和桃花加珠峰11日。"},
        ],
        "route_variant": route,
        "memory": profile or {},
        "journey": {
            "route_variant": route,
            "stage": stage,
            "customer_profile": profile or {},
            "sent_content_groups": sent or [],
        },
        "lead_capture": {"status": lead_status},
        "touch_index": touch_index,
        "available_materials": [],
        "reception_policy": {
            "reply_style": {
                "max_characters": 200,
                "max_messages_per_turn": 3,
                "max_images_per_turn": 2,
            },
            "route_switch": {
                "allowed_routes": ["peach_9d_2027", "peach_11d_2027"],
            },
            "handoff": {
                "large_group": {
                    "enabled": True,
                    "minimum_party_size": 12,
                    "reason": "large_group_custom_quote",
                },
            },
            "silence_journey": {"wakeup_after_minutes": [3, 5, 10, 30, 60]},
            "operator_configuration": {
                "lead_capture": {
                    "enabled": True,
                    "channels": ["LINE"],
                    "require_party_size": True,
                    "require_departure_window": True,
                    "ask_after_answered_topics": 2,
                },
                "business_rules": [],
            },
        },
    }


def test_unselected_route_gets_one_value_touch_then_stops_repeating():
    first = build_silence_plan(context(touch_index=1))
    assert first.reply_plan.action == "reply"
    assert first.touch_goal == "request_contact"
    assert first.reply_plan.reply_options == []
    assert first.reply_plan.follow_up.type == "contact"
    assert "完整行程" in first.reply_plan.follow_up.question
    assert first.reply_plan.lead_action == "ask"
    assert set(first.reply_plan.allowed_fact_ids) == {"route.9.overview", "route.11.overview"}

    second = build_silence_plan(context(touch_index=2))
    assert second.reply_plan.action == "no_action"
    assert second.skip_reason == "silence_unresolved_route_no_new_value"


def test_known_profile_is_not_returned_as_new_slots_or_reasked():
    plan = build_silence_plan(context(
        route="peach_9d_2027",
        stage="needs_discovery",
        profile={"party_size": "2位", "departure_window": "明年3月底"},
        sent=["itinerary_overview"],
    ))
    assert plan.reply_plan.action == "reply"
    assert plan.reply_plan.slots == {}
    assert plan.reply_plan.follow_up is None
    assert plan.reply_plan.allowed_content_group_keys == ["peach_highlights"]


def test_undecided_departure_can_request_contact_after_new_silence_value():
    plan = build_silence_plan(context(
        route="peach_11d_2027",
        stage="value_building",
        profile={"party_size": 2, "departure_window": "未确定"},
        sent=["itinerary_overview"],
    ))
    assert plan.touch_goal == "request_contact"
    assert plan.reply_plan.follow_up is not None
    assert plan.reply_plan.follow_up.type == "contact"
    assert "完整行程" in plan.reply_plan.follow_up.question
    assert "時間還沒確定" in plan.reply_plan.follow_up.question
    assert plan.reply_plan.lead_action == "ask"


def test_undecided_departure_advances_mainline_instead_of_asking_another_slot():
    plan = build_silence_plan(context(
        route="peach_11d_2027",
        stage="needs_discovery",
        profile={"departure_window": "未确定"},
        sent=["itinerary_overview"],
    ))
    assert plan.touch_goal == "build_value"
    assert plan.reply_plan.allowed_content_group_keys == ["rongbuk_upgrade"]
    assert plan.reply_plan.follow_up is None
    assert "不重複追問" in plan.touch_reason


def test_missing_profile_gets_one_structured_follow_up():
    plan = build_silence_plan(context(
        route="peach_9d_2027",
        stage="needs_discovery",
        profile={"departure_window": "明年3月底"},
        sent=["itinerary_overview"],
    ))
    assert plan.touch_goal == "collect_need"
    assert plan.reply_plan.follow_up.type == "slot"
    assert plan.reply_plan.follow_up.field == "party_size"


def test_recent_unanswered_slot_question_is_not_repeated_or_replaced():
    plan = build_silence_plan(context(
        route="peach_9d_2027",
        stage="needs_discovery",
        profile={},
        sent=["itinerary_overview", "party_question"],
    ))
    assert plan.touch_goal == "build_value"
    assert plan.reply_plan.follow_up is None
    assert plan.reply_plan.allowed_content_group_keys == ["peach_highlights"]
    assert "不重複或疊加問題" in plan.touch_reason


def test_objection_touch_uses_only_safety_fact_and_no_unplanned_question_group():
    plan = build_silence_plan(context(
        route="peach_9d_2027",
        stage="objection_handling",
        touch_index=3,
        profile={
            "party_size": "2位",
            "departure_window": "明年3月底",
            "permit_awareness": "不太清楚",
        },
        sent=["itinerary_overview"],
    ))
    assert plan.reply_plan.action == "reply"
    assert plan.reply_plan.follow_up is None
    assert plan.reply_plan.allowed_content_group_keys == []
    assert plan.reply_plan.allowed_fact_ids == ["service.safety"]
    assert "requirements_confirmation_required" in plan.reply_plan.safety_flags


def test_requirement_objection_uses_code_copy_without_generation():
    generation_calls = []

    def generation(_context, _plan):
        generation_calls.append(True)
        raise AssertionError("high-risk requirement boundary must be code-rendered")

    decision, logs, _digest, trace = run_silence_touch_pipeline(
        context(
            route="peach_9d_2027",
            stage="objection_handling",
            touch_index=3,
            profile={
                "party_size": "2位",
                "departure_window": "明年3月底",
                "permit_awareness": "不太清楚",
            },
        ),
        generation_node=generation,
    )
    assert decision.action == "reply"
    assert decision.reply == "這部分會依每位旅客的情況不同，需要再請顧問幫您確認一下，我先不隨便下結論喔。"
    assert decision.evidence_refs == ["service.safety"]
    assert generation_calls == []
    assert logs == []
    assert trace["response_source"] == "deterministic_system_copy"


def test_large_group_is_code_owned_handoff():
    plan = build_silence_plan(context(
        route="peach_11d_2027",
        stage="value_building",
        profile={"party_size": 12, "departure_window": "明年3月底"},
    ))
    assert plan.reply_plan.action == "handoff"
    assert plan.reply_plan.handoff_reason == "large_group_custom_quote"


def test_small_party_range_does_not_concatenate_into_large_group():
    plan = build_silence_plan(
        context(
            route="peach_9d_2027",
            profile={"party_size": "1-2", "departure_window": "明年3月底"},
        )
    )
    assert plan.reply_plan.action != "handoff"


def test_contact_reminder_without_new_value_stops_even_at_final_touch():
    sent = [
        "itinerary_overview", "peach_highlights", "hotel_reference", "vehicle_reference",
        "accommodation_summary", "landmarks", "zhaji", "read_check", "contact_request",
        "vehicle_oxygen", "no_shopping",
    ]
    early = build_silence_plan(context(
        route="peach_9d_2027",
        stage="contact_requested",
        touch_index=2,
        profile={"party_size": 2, "departure_window": "明年3月底"},
        sent=sent,
        lead_status="asked",
    ))
    assert early.reply_plan.action == "no_action"

    final = build_silence_plan(context(
        route="peach_9d_2027",
        stage="contact_requested",
        touch_index=6,
        profile={"party_size": 2, "departure_window": "明年3月底"},
        sent=sent,
        lead_status="asked",
    ))
    assert final.reply_plan.action == "no_action"
    assert final.skip_reason == "silence_no_relevant_content"
    assert final.reply_plan.follow_up is None
    assert final.reply_plan.allowed_content_group_keys == []
    assert final.reply_plan.allowed_asset_ids == []


def test_completed_mainline_does_not_repeat_final_text_nurture():
    plan = build_silence_plan(context(
        route="peach_9d_2027",
        stage="needs_discovery",
        touch_index=6,
        profile={},
        sent=[
            "itinerary_overview", "peach_highlights", "hotel_reference",
            "vehicle_reference", "accommodation_summary", "landmarks", "zhaji",
            "read_check", "party_question", "vehicle_oxygen", "no_shopping",
        ],
    ))
    assert plan.reply_plan.action == "no_action"
    assert plan.reply_plan.allowed_content_group_keys == []
    assert plan.reply_plan.allowed_asset_ids == []
    assert plan.reply_plan.follow_up is None


def test_proactive_hotel_mainline_uses_room_and_oxygen_images():
    source = context(
        route="peach_11d_2027",
        stage="value_building",
        touch_index=3,
        profile={"party_size": 2, "departure_window": "明年3月底"},
        sent=["itinerary_overview", "rongbuk_reference", "rongbuk_upgrade", "peach_highlights", "peach_culture"],
    )
    source["available_materials"] = [
        {"key": "routes12-hilton-room", "routes": ["peach_11d_2027"]},
        {"key": "routes12-hilton-oxygen", "routes": ["peach_11d_2027"]},
    ]
    plan = build_silence_plan(source)
    assert plan.reply_plan.allowed_content_group_keys == ["hotel_reference"]
    assert plan.reply_plan.allowed_asset_ids == [
        "routes12-hilton-room", "routes12-hilton-oxygen",
    ]


def test_partial_landmarks_resume_describes_only_remaining_asset():
    source = context(
        route="peach_11d_2027",
        stage="value_building",
        touch_index=2,
        profile={"party_size": 2, "departure_window": "3月底"},
        sent=[key for key, group in ROUTES["peach_11d_2027"]["groups"].items() if group.get("initial_delivery")],
    )
    source["journey"]["content_progress"] = {
        "landmarks": {"text_delivered": True, "asset_keys": ["routes12-potala"]},
    }
    source["journey"]["sent_asset_keys"] = ["routes12-potala"]
    source["available_materials"] = [
        {"key": "routes12-potala", "routes": ["peach_11d_2027"]},
        {"key": "routes12-barkhor", "routes": ["peach_11d_2027"]},
    ]
    plan = build_silence_plan(source)
    assert plan.reply_plan.allowed_content_group_keys == ["landmarks"]
    assert plan.reply_plan.allowed_asset_ids == ["routes12-barkhor"]
    assert "resume_partial_content_group" in plan.reply_plan.safety_flags
    assert "不要重述" in plan.reply_plan.reply_goal


def test_split_pipeline_never_calls_generation_for_no_action():
    calls = []

    def generation(_context, _plan):
        calls.append("generation")
        raise AssertionError("generation must not run")

    decision, logs, _digest, trace = run_silence_touch_pipeline(
        context(touch_index=2),
        generation_node=generation,
    )
    assert decision.action == "no_action"
    assert calls == []
    assert decision.reply_body == ""
    assert decision.follow_up_question == ""
    assert decision.follow_up_type == ""
    assert decision.follow_up_field == ""
    assert logs == []
    assert trace["pipeline"] == "split_silence_touch"


def test_split_pipeline_uses_code_plan_and_fact_checked_generation():
    seen = {}

    def generation(_context, silence_plan):
        seen["plan"] = silence_plan
        generated = GeneratedReply(
            body="9日路线不上珠峰，11日路线包含珠峰段。",
            follow_up=GeneratedFollowUp("contact", "line", "您可以先留 LINE，我把完整行程资料发给您，方便吗？"),
            used_fact_ids=["route.9.overview", "route.11.overview"],
            asset_ids=[],
        )
        return generated, [{"node": "silence_generation", "status": "completed"}], "generation"

    def verification(_context, plan, generated):
        assert plan.action == "reply"
        assert generated.used_fact_ids == plan.allowed_fact_ids
        return FactVerification(True), [{"node": "reply_fact_verification", "status": "completed"}], "verify"

    decision, logs, _digest, trace = run_silence_touch_pipeline(
        context(touch_index=1),
        generation_node=generation,
        verification_node=verification,
    )
    assert decision.action == "reply"
    assert decision.touch_goal == "request_contact"
    assert decision.lead_action == "ask"
    assert decision.reply_options == []
    assert len(logs) == 2
    assert decision.reply_body == decision.reply.split(" ", 1)[0]
    assert decision.follow_up_type == "contact"
    assert decision.follow_up_field == "line"
    assert decision.reply == f"{decision.reply_body} {decision.follow_up_question}"
    from app.delivery_plan import ordered_delivery_parts
    parts = ordered_delivery_parts(
        decision.reply_body, [{"key": "image"}], "text_then_assets",
        follow_up_question=decision.follow_up_question,
        follow_up_type=decision.follow_up_type,
        follow_up_field=decision.follow_up_field,
    )
    assert [p.kind for p in parts] == ["text", "media", "text"]
    assert parts[-1].is_follow_up
    assert parts[0].content == decision.reply_body
    assert parts[-1].content == decision.follow_up_question
    assert trace["fact_verification_passed"] is True
    assert seen["plan"].reply_plan.next_stage == "contact_requested"


def test_split_pipeline_turns_twice_rejected_silence_copy_into_no_action():
    calls = []

    def generation(_context, silence_plan):
        calls.append(silence_plan)
        return GeneratedReply(
            body="整條路線都已完整安排，您可以放心。",
            follow_up=None,
            used_fact_ids=list(silence_plan.reply_plan.allowed_fact_ids),
            asset_ids=[],
        ), [{"node": "silence_generation", "status": "completed"}], f"g{len(calls)}"

    decision, logs, _digest, trace = run_silence_touch_pipeline(
        context(
            route="peach_11d_2027",
            stage="value_building",
            touch_index=2,
            sent=[key for key, group in ROUTES["peach_11d_2027"]["groups"].items() if group.get("initial_delivery")],
        ),
        generation_node=generation,
        verification_node=lambda *_args: (
            FactVerification(False, ["整條路線都已完整安排"]),
            [{"node": "reply_fact_verification", "status": "completed"}],
            "v",
        ),
    )

    assert len(calls) == 2
    assert decision.action == "no_action"
    assert decision.reply is None
    assert decision.reply_body == ""
    assert decision.follow_up_question == ""
    assert decision.follow_up_type == ""
    assert decision.follow_up_field == ""
    assert "silence_verification_failed_no_action" in decision.safety_flags
    assert trace["skip_reason"] == "fact_verification_failed_no_action"
    assert trace["fact_verification_passed"] is False
    assert len(logs) == 4
