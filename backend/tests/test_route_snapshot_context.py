import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier

import pytest

from app.decision_knowledge import FACTS, evidence_packet
from app.decision_service import generate_decision
from app.deepseek_evaluation import EvaluationDecision
from app.route_packages import ROUTES, route_catalog_context, reload_route_packages
from app.route_reply import (
    CONTENT_PROGRESS_KEY, ROUTE_SNAPSHOTS_KEY, append_deferred_initial_follow_up,
    bind_new_route_snapshot, deferred_initial_follow_up, group_content_from_values,
    frozen_sop_nodes, make_route_snapshot, playbook_prompt, prepare_route_reply_values, route_snapshot_from_values,
)


ROUTE = "peach_9d_2027"
OTHER = "peach_11d_2027"


def slots_for(label):
    spec = deepcopy(ROUTES[ROUTE])
    spec["name"] = label
    spec["groups"]["hotel_reference"]["text"] = label
    return {ROUTE_SNAPSHOTS_KEY: {ROUTE: make_route_snapshot(ROUTE, spec)}}


def model_decision():
    return EvaluationDecision("reply", "peach_9d", "other", route_variant=ROUTE, reply="test")


def test_thread_contexts_are_isolated_and_return_copies():
    original = ROUTES[ROUTE]["name"]
    barrier = Barrier(2)

    def run(label):
        with route_catalog_context(ROUTE, slots_for(label)):
            barrier.wait(timeout=10)
            assert ROUTES[ROUTE]["name"] == label
            copy = ROUTES[ROUTE]
            copy["name"] = "mutated"
            assert ROUTES[ROUTE]["name"] == label
            barrier.wait(timeout=10)
        return ROUTES[ROUTE]["name"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(run, "a"), pool.submit(run, "b")
        assert a.result(timeout=15) == b.result(timeout=15) == original


def test_async_tasks_and_nested_exception_restore_context():
    original = ROUTES[ROUTE]["name"]

    async def run(label):
        with route_catalog_context(ROUTE, slots_for(label)):
            await asyncio.sleep(0)
            assert ROUTES[ROUTE]["name"] == label
            with pytest.raises(RuntimeError):
                with route_catalog_context(ROUTE, slots_for("nested")):
                    raise RuntimeError("test")
            assert ROUTES[ROUTE]["name"] == label

    async def main():
        await asyncio.gather(run("a"), run("b"))

    asyncio.run(main())
    assert ROUTES[ROUTE]["name"] == original


def test_reload_writes_backing_registry_without_changing_bound_view():
    old = slots_for("historical")
    current = ROUTES[ROUTE]["name"]
    with route_catalog_context(ROUTE, old):
        reload_route_packages()
        assert ROUTES[ROUTE]["name"] == "historical"
    assert ROUTES[ROUTE]["name"] == current


def test_other_routes_remain_pinned_during_publish(monkeypatch):
    current = deepcopy(ROUTES[OTHER])
    with route_catalog_context(ROUTE, slots_for("old")):
        changed = deepcopy(current)
        changed["name"] = "published"
        monkeypatch.setitem(ROUTES, OTHER, changed)
        assert ROUTES[OTHER]["name"] == current["name"]
    assert ROUTES[OTHER]["name"] == "published"


def test_generate_binds_assets_before_model_and_persists_exact_selected_spec(monkeypatch):
    asset = ROUTES[ROUTE]["groups"]["hotel_reference"]["assets"][0]
    materials = [{"key": asset, "media_hash": "old-hash", "media_id": 123, "content_type": "image"}]
    before = deepcopy(ROUTES[ROUTE])

    def model(packet):
        assert ROUTES[ROUTE]["asset_hashes"] == {asset: "old-hash"}
        assert packet["route_playbook"][ROUTE]["name"] == before["name"]
        materials[0]["media_hash"] = "changed-after-model-start"
        changed = deepcopy(before)
        changed["name"] = "new-publication"
        monkeypatch.setitem(ROUTES, ROUTE, changed)
        decision = model_decision()
        decision.bound_route_snapshot = {"forged": True}
        return decision, [], "test"

    decision, *_ = generate_decision({"available_materials": materials}, model)
    _, progress = prepare_route_reply_values(decision)
    pinned = route_snapshot_from_values(ROUTE, progress["slots"])
    assert pinned["name"] == before["name"]
    assert pinned["asset_hashes"] == {asset: "old-hash"}
    assert pinned["asset_bindings"][asset] == {
        "asset_key": asset, "media_hash": "old-hash", "media_id": 123, "content_type": "image",
    }


def test_old_snapshot_facts_and_bindings_used_through_entire_decision():
    slots = slots_for("old")
    spec = route_snapshot_from_values(ROUTE, slots)
    fact = next(item for item in spec["knowledge_facts"] if item["id"].endswith(".overview"))
    fact["text"] = "historical-fact"
    spec["asset_hashes"] = {"old-asset": "old-hash"}
    spec["asset_bindings"] = {"old-asset": {"media_hash": "old-hash"}}
    slots[ROUTE_SNAPSHOTS_KEY][ROUTE] = make_route_snapshot(ROUTE, spec)

    def model(packet):
        assert {item["id"]: item["text"] for item in packet["knowledge"]["facts"]}[fact["id"]] == "historical-fact"
        assert {item["id"]: item["text"] for item in FACTS}[fact["id"]] == "historical-fact"
        from app.reply_generation import _facts
        assert _facts([fact["id"]])[0]["text"] == "historical-fact"
        assert ROUTES[ROUTE]["asset_hashes"] == {"old-asset": "old-hash"}
        return model_decision(), [], "test"

    decision, *_ = generate_decision({
        "journey": {"route_variant": ROUTE, "slots": slots},
        "available_materials": [{"key": "old-asset", "media_hash": "new-hash"}],
    }, model)
    assert decision.bound_route_snapshot == slots[ROUTE_SNAPSHOTS_KEY][ROUTE]


@pytest.mark.parametrize("slots", [{}, {ROUTE_SNAPSHOTS_KEY: {ROUTE: {"schema_version": 1}}}])
def test_invalid_history_blocks_before_model(slots):
    calls = []
    with pytest.raises(ValueError, match="route_snapshot_unverifiable"):
        generate_decision({"journey": {"route_variant": ROUTE, "slots": slots}}, lambda packet: calls.append(packet))
    assert calls == []


def test_creation_helper_cannot_replace_unknown_or_existing_progress():
    for slots in (
        {ROUTE_SNAPSHOTS_KEY: {ROUTE: {"history_unknown": True}}},
        {CONTENT_PROGRESS_KEY: {ROUTE: {}}},
        slots_for("existing"),
    ):
        with pytest.raises(ValueError, match="route_snapshot_already_initialized"):
            bind_new_route_snapshot(ROUTE, slots)
    slots = bind_new_route_snapshot(ROUTE, {"party_size": 2})
    assert route_snapshot_from_values(ROUTE, slots)
    assert slots["party_size"] == 2


def test_deferred_helpers_and_content_do_not_read_latest_catalog(monkeypatch):
    slots = bind_new_route_snapshot(ROUTE, {})
    expected = route_snapshot_from_values(ROUTE, slots)
    monkeypatch.delitem(ROUTES, ROUTE)
    decision = {"action": "reply", "route_variant": ROUTE, "follow_up_question": "Question?"}
    assert deferred_initial_follow_up(decision, [], slots)["question"] == "Question?"
    assert deferred_initial_follow_up(decision, [], {}) is None
    assert deferred_initial_follow_up(decision, ["hotel_reference"], slots) is None
    content = group_content_from_values(ROUTE, slots, "hotel_reference")
    assert content == expected["groups"]["hotel_reference"]
    content["assets"].clear()
    assert group_content_from_values(ROUTE, slots, "hotel_reference")["assets"]
    assert playbook_prompt(ROUTE, slots)[ROUTE]["name"] == expected["name"]
    nodes = [{"initial_delivery": True}]
    result = append_deferred_initial_follow_up(nodes, ROUTE, {"question": "Question?"}, slots)
    assert result[-1]["delay_seconds"] == expected["initial_delivery_interval_seconds"]
    assert append_deferred_initial_follow_up(nodes, ROUTE, {"question": "Question?"}, {}) == nodes


def test_verified_snapshot_with_unknown_receipts_still_blocks_model():
    with pytest.raises(ValueError, match="route_history_unverifiable"):
        generate_decision({"journey": {
            "route_variant": ROUTE, "slots": slots_for("old"),
            "sent_content_groups": ["hotel_reference"],
        }}, lambda _: pytest.fail("model must not run"))


def test_conflicting_shared_fact_versions_fail_closed():
    slots = slots_for("old")
    spec = route_snapshot_from_values(ROUTE, slots)
    shared = next(fact for fact in spec["knowledge_facts"] if fact["id"].startswith("route.shared."))
    shared["text"] = "conflicting historical shared fact"
    slots[ROUTE_SNAPSHOTS_KEY][ROUTE] = make_route_snapshot(ROUTE, spec)
    with pytest.raises(RuntimeError, match="route_fact_conflict"):
        generate_decision({"journey": {"route_variant": ROUTE, "slots": slots}},
                          lambda _: pytest.fail("model must not run"))


def test_prepare_rejects_decision_snapshot_different_from_existing_binding():
    decision = model_decision()
    decision.bound_route_snapshot = slots_for("new")[ROUTE_SNAPSHOTS_KEY][ROUTE]
    with pytest.raises(ValueError, match="decision_route_snapshot_mismatch"):
        prepare_route_reply_values(decision, current_route=ROUTE, slots=slots_for("old"))


def test_real_planner_and_deterministic_generator_use_old_greeting(monkeypatch):
    from app.realtime_reply_pipeline import run_realtime_reply_pipeline
    from app.reply_understanding import CustomerUnderstanding
    from app.route_packages import JOURNEY_POLICY

    slots = slots_for("old")
    spec = route_snapshot_from_values(ROUTE, slots)
    spec["groups"]["advisor_greeting"]["text"] = "Historical greeting."
    slots[ROUTE_SNAPSHOTS_KEY][ROUTE] = make_route_snapshot(ROUTE, spec)
    understanding = CustomerUnderstanding("route_intro", route_candidate=ROUTE, route_resolution="confirmed")

    def pipeline(packet):
        return run_realtime_reply_pipeline(
            packet, understanding_node=lambda _: (understanding, [], "understanding"),
            generation_node=lambda *_: pytest.fail("greeting should be deterministic"),
        )

    monkeypatch.setattr("app.decision_service.run_realtime_reply_pipeline", pipeline)
    decision, *_ = generate_decision({
        "journey": {"route_variant": ROUTE, "slots": slots, "sent_content_groups": []},
        "route_variant": ROUTE, "reception_policy": deepcopy(JOURNEY_POLICY),
    })
    assert decision.reply == "Historical greeting."


def sop_spec():
    spec = deepcopy(ROUTES[ROUTE])
    keys = sorted({key for group in spec["groups"].values() for key in group["assets"]})
    spec["asset_bindings"] = {
        key: {"asset_key": key, "media_id": index, "media_hash": f"hash-{index}", "content_type": "image"}
        for index, key in enumerate(keys, 1)
    }
    spec["asset_hashes"] = {key: value["media_hash"] for key, value in spec["asset_bindings"].items()}
    return spec


def test_frozen_sop_nodes_preserve_old_order_copy_and_pin_candidates(monkeypatch):
    spec = sop_spec()
    original = deepcopy(spec)
    monkeypatch.delitem(ROUTES, ROUTE)
    nodes = frozen_sop_nodes(spec)
    assert [node["key"] for node in nodes] == [node["key"] for node in original["sop"]["nodes"]]
    checked_media = 0
    checked_candidates = 0
    for source, frozen in zip(original["sop"]["nodes"], nodes):
        pairs = [(source, frozen), *zip(source.get("content_group_candidates", []), frozen.get("content_group_candidates", []))]
        checked_candidates += len(frozen.get("content_group_candidates", []))
        for source_node, frozen_node in pairs:
            for old, item in zip(source_node["messages"], frozen_node["messages"]):
                assert old["content"] == item["content"]
                if item["content_type"] != "text":
                    checked_media += 1
                    for key, value in spec["asset_bindings"][item["asset_key"]].items():
                        assert item[key] == value
    assert checked_media and checked_candidates
    assert spec == original
    nodes[0]["messages"].clear()
    assert spec == original


def test_frozen_sop_nodes_missing_candidate_binding_blocks_whole_enrollment():
    spec = sop_spec()
    spec["sop"]["nodes"] = [node for node in spec["sop"]["nodes"] if node.get("content_group_candidates")]
    spec["asset_bindings"] = {}
    with pytest.raises(ValueError, match="route_snapshot_asset_binding_missing"):
        frozen_sop_nodes(spec)


@pytest.mark.parametrize("field,value", [("media_hash", "wrong"), ("media_id", 9999), ("content_type", "video")])
def test_frozen_sop_nodes_rejects_conflicting_frozen_media(field, value):
    spec = sop_spec()
    item = next(item for node in spec["sop"]["nodes"] for item in node["messages"] if item["content_type"] != "text")
    item[field] = value
    with pytest.raises(ValueError, match="route_snapshot_asset_binding_mismatch"):
        frozen_sop_nodes(spec)


def test_frozen_sop_nodes_no_snapshot_cannot_fall_back():
    with pytest.raises(ValueError, match="route_snapshot_sop_invalid"):
        frozen_sop_nodes(None)


@pytest.mark.parametrize("extra", [{}, {"journey": None}, {"journey": {}}])
def test_route_only_hint_does_not_require_historical_snapshot(extra):
    calls = []

    def model(packet):
        calls.append(packet)
        assert packet["route_variant"] == ROUTE
        return model_decision(), [], "test"

    decision, *_ = generate_decision({"route_variant": ROUTE, **extra}, model)
    assert len(calls) == 1
    assert decision.bound_route_snapshot["route_variant"] == ROUTE


@pytest.mark.parametrize("extra", [
    {"journey": {"route_variant": ROUTE}},
    {"journey": {"route_variant": ROUTE, "slots": {}}},
    {"journey": {"sent_content_groups": ["hotel_reference"]}},
    {"slots": {}},
])
def test_durable_route_is_not_downgraded_to_selection_hint(extra):
    with pytest.raises(ValueError, match="route_snapshot_unverifiable"):
        generate_decision({"route_variant": ROUTE, **extra},
                          lambda _: pytest.fail("model must not run"))
