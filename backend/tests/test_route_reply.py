from sqlalchemy import select

from app.deepseek_evaluation import EvaluationDecision
from app.decision_service import generate_decision
from app.models import Contact, ConversationJourney, ConversationState, InboxBinding
from app.lead_capture import bind_lead_request
from app.route_reply import mark_group_delivered, prepare_route_reply, prepare_route_reply_values
from app.route_reply import (
    ROUTE_SNAPSHOTS_KEY, CONTENT_PROGRESS_KEY, content_progress_from_values,
    group_requirements_from_values, journey_context_from_values,
    route_snapshot_from_values, update_content_progress_values,
)
from app.route_packages import ROUTES
from copy import deepcopy
from types import SimpleNamespace


def snapshot_slots(route="peach_9d_2027"):
    _, progress = prepare_route_reply_values(SimpleNamespace(route_variant=route))
    return progress["slots"]


def test_snapshot_survives_catalog_changes_and_return_value_mutation(monkeypatch):
    route = "peach_9d_2027"
    slots = snapshot_slots(route)
    original = route_snapshot_from_values(route, slots)
    changed = deepcopy(ROUTES[route])
    changed["groups"]["hotel_reference"]["assets"].append("new-asset")
    changed["sequence"] = ["new-group"]
    monkeypatch.setitem(ROUTES, route, changed)
    assert route_snapshot_from_values(route, slots) == original
    assert "new-asset" not in group_requirements_from_values(route, slots, "hotel_reference")[1]
    original["groups"].clear()
    assert route_snapshot_from_values(route, slots)["groups"]
    monkeypatch.delitem(ROUTES, route)
    assert journey_context_from_values(route, slots=slots)["next_content_group"]


def test_exact_text_and_assets_required_topics_cannot_complete():
    route, group = "peach_9d_2027", "hotel_reference"
    slots = snapshot_slots(route)
    spec = route_snapshot_from_values(route, slots)["groups"][group]
    slots, sent, complete = update_content_progress_values(
        route, slots, [], group, text_delivered=True, complete=True,
        delivered_text=spec["text"] + " extra",
    )
    progress = content_progress_from_values(route, slots, sent)[group]
    assert progress["topic_covered"] and not progress["text_delivered"]
    assert progress["asset_keys"] == []
    assert not complete and not sent
    slots, sent, complete = update_content_progress_values(
        route, slots, sent, group, delivered_text=spec["text"],
    )
    assert not complete
    slots, sent, complete = update_content_progress_values(
        route, slots, sent, group, asset_keys=spec["assets"],
    )
    assert complete and sent == [group]
    assert journey_context_from_values(route, slots=slots, sent_groups=sent)["completed_content_groups"] == [group]


def test_legacy_history_pauses_without_fabricating_assets():
    route, group = "peach_9d_2027", "hotel_reference"
    _, prepared = prepare_route_reply_values(
        SimpleNamespace(route_variant=route), current_route=route, sent_groups=[group],
    )
    assert route_snapshot_from_values(route, prepared["slots"]) is None
    context = journey_context_from_values(route, slots=prepared["slots"], sent_groups=[group])
    assert context["history_unknown"] and context["automatic_delivery_paused"]
    assert context["sent_asset_keys"] == []
    assert context["next_content_group"] is None
    assert context["completed_content_groups"] == []
    slots, sent, complete = update_content_progress_values(route, {}, [group], group, complete=True)
    assert not complete and sent == [group]
    assert content_progress_from_values(route, slots, sent)[group]["asset_keys"] == []


def test_legacy_partial_receipts_remain_explicit_but_unknown():
    route, group = "peach_9d_2027", "hotel_reference"
    slots = snapshot_slots(route)
    slots[CONTENT_PROGRESS_KEY] = {route: {group: {"text_delivered": True, "asset_keys": ["old"]}}}
    progress = content_progress_from_values(route, slots, [group])[group]
    assert progress["asset_keys"] == ["old"]
    assert not progress["text_delivered"] and progress["history_unknown"]


def test_reserved_metadata_cannot_be_overwritten_by_decision():
    route = "peach_9d_2027"
    slots = snapshot_slots(route)
    original = deepcopy(slots)
    decision = SimpleNamespace(route_variant=route, slots={ROUTE_SNAPSHOTS_KEY: {}},
                               profile_updates={CONTENT_PROGRESS_KEY: {"value": {"fake": True}}})
    _, prepared = prepare_route_reply_values(decision, current_route=route, slots=slots)
    assert prepared["slots"] == original
    assert slots == original


def test_corrupt_snapshot_is_not_rebound_and_pauses():
    route = "peach_9d_2027"
    slots = snapshot_slots(route)
    slots[ROUTE_SNAPSHOTS_KEY][route]["spec"]["sequence"] = []
    _, prepared = prepare_route_reply_values(SimpleNamespace(route_variant=route), slots=slots)
    assert route_snapshot_from_values(route, prepared["slots"]) is None
    assert journey_context_from_values(route, slots=prepared["slots"])["automatic_delivery_paused"]


def test_switching_back_does_not_rebind_unknown_history():
    old, new = "peach_9d_2027", "peach_11d_2027"
    _, switched = prepare_route_reply_values(
        SimpleNamespace(route_variant=new), current_route=old, sent_groups=["hotel_reference"],
    )
    assert route_snapshot_from_values(new, switched["slots"])
    _, returned = prepare_route_reply_values(
        SimpleNamespace(route_variant=old), current_route=new, slots=switched["slots"],
    )
    assert route_snapshot_from_values(old, returned["slots"]) is None
    assert journey_context_from_values(old, slots=returned["slots"])["automatic_delivery_paused"]


def state(db):
    db.add(InboxBinding(id=1, tenant_id=1, chatwoot_inbox_id=128859, name="Facebook", ai_enabled=True))
    db.add(Contact(id=1, tenant_id=1, chatwoot_contact_id=55, name="Tester"))
    db.flush()
    row = ConversationState(
        id=1,
        tenant_id=1,
        inbox_binding_id=1,
        contact_id=1,
        chatwoot_conversation_id=26,
    )
    db.add(row)
    db.flush()
    return row


def test_route_reply_persists_model_copy_and_progress_without_reinterpretation(session_factory):
    with session_factory() as db:
        row = state(db)
        decision = EvaluationDecision(
            "reply",
            "peach_9d",
            "route_intro",
            reply="模型按上下文生成的最终回复",
            route_variant="peach_9d_2027",
            content_group_key="itinerary_overview",
            material_keys=["routes12-9d-itinerary"],
            journey_stage="introducing",
        )
        decision, journey = prepare_route_reply(db, row, decision)

        assert decision.reply == "模型按上下文生成的最终回复"
        assert decision.content_group_key == "itinerary_overview"
        assert decision.material_keys == ["routes12-9d-itinerary"]
        assert journey.route_variant == "peach_9d_2027"
        assert journey.stage == "value_building"

        mark_group_delivered(journey, decision.content_group_key)
        db.commit()
        saved = db.scalar(select(ConversationJourney))
        assert saved.sent_groups == []
        assert saved.slots["_content_progress"][saved.route_variant]["itinerary_overview"]["topic_covered"]


def test_lead_request_timing_is_model_owned_but_request_is_durable_once():
    decision = EvaluationDecision(
        "reply",
        "peach_9d",
        "contact",
        reply="方便的話，可以留下 LINE 嗎？",
        route_variant="peach_9d_2027",
        lead_action="ask",
    )
    decision, asked = bind_lead_request(
        decision,
        route_variant="peach_9d_2027",
        journey_stage="introducing",
        capture_status="not_started",
    )
    assert asked is True
    assert decision.reply == "方便的話，可以留下 LINE 嗎？"

    decision, asked = bind_lead_request(
        decision,
        route_variant="peach_9d_2027",
        journey_stage="contact_requested",
        capture_status="asked",
    )
    assert asked is False
    assert decision.lead_action == "none"


def test_route_switch_resets_only_delivery_progress(session_factory):
    with session_factory() as db:
        row = state(db)
        first = EvaluationDecision(
            "reply",
            "peach_9d",
            "route_intro",
            reply="9日介紹",
            route_variant="peach_9d_2027",
            content_group_key="itinerary_overview",
        )
        first, journey = prepare_route_reply(db, row, first)
        mark_group_delivered(journey, first.content_group_key)

        switched = EvaluationDecision(
            "reply",
            "peach_11d",
            "itinerary",
            reply="11日絨布寺住宿說明",
            route_variant="peach_11d_2027",
            content_group_key="rongbuk_reference",
            material_keys=["routes12-rongbuk-room"],
        )
        switched, journey = prepare_route_reply(db, row, switched)

        assert journey.sent_groups == []
        assert switched.reply == "11日絨布寺住宿說明"
        assert switched.material_keys == ["routes12-rongbuk-room"]


def test_silence_result_without_route_preserves_confirmed_journey_route():
    decision = EvaluationDecision(
        "reply",
        "unclassified",
        "other",
        reply="继续介绍尚未覆盖的线路内容",
        route_variant="",
        content_group_key="hotel_reference",
    )

    decision, progress = prepare_route_reply_values(
        decision,
        current_route="peach_11d_2027",
        preserve_current_route=True,
        stage="value_building",
        sent_groups=["itinerary_overview"],
    )

    assert decision.route_variant == "peach_11d_2027"
    assert progress["route_variant"] == "peach_11d_2027"
    assert progress["sent_content_groups"] == ["itinerary_overview"]


def test_decision_service_does_not_override_model_business_decision():
    model_decision = EvaluationDecision(
        "reply",
        "peach_9d",
        "price",
        reply="頁面參考價與即時餘位是兩件事，我先說明可確認的部分。",
        route_variant="peach_9d_2027",
        content_group_key="price_reference",
        evidence_refs=["route.9.price"],
    )

    def model(_):
        return model_decision, [], "hash"

    decision, *_ = generate_decision(
        {
            "customer_text": "現在還有餘位嗎？兩個人多少錢？",
            "context_messages": [],
            "route_variant": "peach_9d_2027",
            "slots": snapshot_slots(),
            "available_materials": [],
        },
        model,
    )

    assert decision.action == "reply"
    assert decision.reply == model_decision.reply
    assert decision.content_group_key == "price_reference"


def test_decision_service_filters_only_invalid_references_not_reply_text():
    model_decision = EvaluationDecision(
        "reply",
        "peach_9d",
        "itinerary",
        reply="這是模型最終話術",
        route_variant="peach_9d_2027",
        content_group_key="rongbuk_reference",
        material_keys=["unknown-image"],
        evidence_refs=["unknown.fact"],
    )

    decision, *_ = generate_decision(
        {
            "customer_text": "想看行程",
            "context_messages": [],
            "available_materials": [{"key": "unknown-image"}],
        },
        lambda _: (model_decision, [], "hash"),
    )

    assert decision.reply == "這是模型最終話術"
    assert decision.content_group_key == ""
    assert decision.material_keys == []
    assert decision.evidence_refs == []
    assert "content_group_reference_rejected" in decision.safety_flags
