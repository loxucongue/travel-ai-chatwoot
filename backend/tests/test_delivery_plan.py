import pytest
from dataclasses import FrozenInstanceError, asdict

from app.delivery_plan import delivery_mode_for, ordered_delivery_parts
from app.delivery_plan import DELIVERY_PLAN_VERSION, DeliveryPart
from app.delivery_plan import expand_static_delivery_nodes
from app.sop_schedule import schedule_at
from copy import deepcopy


def test_static_group_expands_in_order_with_last_only_deferred_metadata():
    node = {
        "key": "intro", "content_group_key": "hotel_reference", "initial_delivery": True,
        "basis": "enrollment", "schedule_type": "relative", "delay_minutes": 5,
        "delivery_interval_seconds": 6, "skip_if_materials_provided": True,
        "deferred_follow_up": {"type": "contact", "question": "LINE?"},
        "messages": [
            {"key": "image", "content_type": "image", "media_id": 7},
            {"key": "body", "content_type": "text", "content": "Body"},
            {"key": "question", "content_type": "text", "content": "LINE?"},
        ],
    }
    original = deepcopy(node)
    parts = expand_static_delivery_nodes([node])
    assert node == original
    assert [part["messages"][0] for part in parts] == node["messages"]
    assert parts[0]["key"] == "intro"
    assert parts[0]["basis"] == "enrollment" and parts[0]["delay_minutes"] == 5
    assert all(part["content_group_key"] == "hotel_reference" for part in parts)
    assert all(part["initial_delivery"] and part["skip_if_materials_provided"] for part in parts)
    assert all("delivery_interval_seconds" not in part for part in parts)
    assert all("deferred_follow_up" not in part for part in parts[:-1])
    assert parts[-1]["deferred_follow_up"] == node["deferred_follow_up"]
    assert all(part["basis"] == "previous_node" and part["delay_minutes"] == 0
               and part["delay_seconds"] == 6 for part in parts[1:])
    assert expand_static_delivery_nodes(parts) == parts
    node["messages"][0]["media_id"] = 99
    node["deferred_follow_up"]["question"] = "changed"
    assert parts[0]["messages"][0]["media_id"] == 7
    assert parts[-1]["deferred_follow_up"]["question"] == "LINE?"


def test_static_keys_are_stable_bounded_and_do_not_collide_with_original_nodes():
    nodes = [{"key": "x" * 79 + suffix, "messages": [{"key": "a"}, {"key": "b"}]}
             for suffix in ("a", "b")]
    first = expand_static_delivery_nodes(nodes)
    assert first == expand_static_delivery_nodes(deepcopy(nodes))
    assert first[0]["key"] == nodes[0]["key"]
    assert all(len(first[index]["key"]) <= 80 for index in (1, 3))
    assert len({part["key"] for part in first}) == 4
    collision = {"key": first[1]["key"], "messages": [{"key": "only"}]}
    expanded = expand_static_delivery_nodes([*nodes, collision])
    assert len({part["key"] for part in expanded}) == 5
    assert expanded[-1] == collision


@pytest.mark.parametrize("interval,expected", [(None, 2), (0, 0), (7, 7)])
def test_static_chain_replaces_fixed_schedule_with_relative_interval(interval, expected):
    node = {"key": "fixed", "schedule_type": "fixed", "fixed_at": "2026-09-13T02:00:00+00:00",
            "messages": [{"key": "a"}, {"key": "b"}]}
    if interval is not None:
        node["delivery_interval_seconds"] = interval
    parts = expand_static_delivery_nodes([node])
    assert parts[0]["fixed_at"] == node["fixed_at"]
    assert parts[1]["schedule_type"] == "relative"
    assert "fixed_at" not in parts[1]
    assert parts[1]["delay_seconds"] == expected
    assert schedule_at(parts[1], customer_added_at=None, enrolled_at=node["fixed_at"]) is None
    from datetime import datetime, timedelta
    assert schedule_at(parts[1], customer_added_at=None, enrolled_at=node["fixed_at"],
                       previous_at=node["fixed_at"]) == (
        datetime.fromisoformat(node["fixed_at"]) + timedelta(seconds=expected)).isoformat()


def test_dynamic_single_and_legacy_nodes_remain_unchanged():
    nodes = [
        {"key": "dynamic", "journey_trigger": "silence_mainline",
         "messages": [{"key": "a"}, {"key": "b"}], "delivery_interval_seconds": 9},
        {"key": "single", "messages": [{"key": "a"}], "deferred_follow_up": {"type": "contact"}},
        {"key": "legacy", "content": "body"},
        {"key": "empty", "messages": []},
    ]
    assert expand_static_delivery_nodes(nodes) == nodes
    assert expand_static_delivery_nodes([]) == []


def test_following_node_schedules_after_the_final_expanded_message():
    nodes = [
        {"key": "intro", "basis": "enrollment", "schedule_type": "relative",
         "messages": [{"key": "a"}, {"key": "b"}, {"key": "c"}]},
        {"key": "dynamic", "journey_trigger": "silence_mainline", "basis": "previous_node",
         "schedule_type": "relative", "delay_seconds": 10},
    ]
    parts = expand_static_delivery_nodes(nodes)
    assert parts[-1] == nodes[-1]
    previous = None
    times = []
    for part in parts:
        previous = schedule_at(part, customer_added_at=None,
                               enrolled_at="2026-09-13T02:00:00+00:00", previous_at=previous)
        times.append(previous)
    assert times == [f"2026-09-13T02:00:{second:02d}+00:00" for second in (0, 2, 4, 14)]


@pytest.mark.parametrize("interval", [-1, 1.5, float("inf"), "invalid"])
def test_static_group_rejects_invalid_intervals(interval):
    with pytest.raises(ValueError, match="delivery_interval_invalid"):
        expand_static_delivery_nodes([{"key": "bad", "delivery_interval_seconds": interval,
                                      "messages": [{"key": "a"}, {"key": "b"}]}])


@pytest.mark.parametrize("mode", [
    "text_only", "assets_only", "text_then_assets", "assets_then_text",
])
def test_follow_up_is_always_a_separate_final_text(mode):
    legacy = ordered_delivery_parts("body", [{"key": "a"}], mode)
    parts = ordered_delivery_parts(
        "body", [{"key": "a"}], mode, plan_id="silence:17",
        content_group_key="hotel_reference", interval_seconds=2,
        follow_up_question="  Which month?  ", follow_up_type="slot",
        follow_up_field="departure_window", follow_up_group_key="departure_question",
    )
    assert [p.kind for p in parts] == [p.kind for p in legacy] + ["text"]
    assert parts[-1].content == "Which month?"
    assert sum(p.is_follow_up for p in parts) == 1
    assert sum(p.content.count("?") for p in parts if p.kind == "text") == 1
    assert parts[-1].is_follow_up
    assert parts[-1].follow_up_type == "slot"
    assert parts[-1].follow_up_field == "departure_window"
    assert parts[-1].content_group_key == "departure_question"
    assert all(p.content_group_key == "hotel_reference" for p in parts[:-1])
    assert [p.interval_seconds for p in parts] == [0] + [2] * (len(parts) - 1)
    assert all(p.plan_version == DELIVERY_PLAN_VERSION for p in parts)


def test_ids_are_frozen_repeatable_scoped_and_unique():
    def build(**kwargs):
        return ordered_delivery_parts("body", [{"key": "a"}, {"key": "a"}],
                                      "text_then_assets", **kwargs)
    parts = build(plan_id="job:1")
    assert parts == build(plan_id="job:1")
    assert len({p.part_id for p in parts}) == 3
    assert parts[0].part_id != build(plan_id="job:2")[0].part_id
    assert parts[0].part_id != build(plan_id="job:1", interval_seconds=2)[0].part_id
    with pytest.raises(FrozenInstanceError):
        parts[0].part_id = "changed"
    assert DeliveryPart(**asdict(parts[0])) == parts[0]


@pytest.mark.parametrize("field,value", [
    ("plan_version", "other"), ("content_group_key", "other"),
    ("interval_seconds", 10), ("is_follow_up", True),
])
def test_delivery_metadata_is_immutable(field, value):
    part = ordered_delivery_parts("body", [], "text_only")[0]
    with pytest.raises(FrozenInstanceError):
        setattr(part, field, value)


def test_material_snapshot_is_isolated_from_external_nested_mutation():
    material = {"key": "a", "metadata": {"captions": ["original"]}}
    materials = [material]
    part = ordered_delivery_parts("", materials, "assets_only")[0]
    part_id = part.part_id
    material["key"] = "changed"
    material["metadata"]["captions"].append("changed")
    materials.clear()
    assert part.material == {"key": "a", "metadata": {"captions": ["original"]}}
    assert part.part_id == part_id
    assert DeliveryPart(**asdict(part)) == part


def test_material_snapshots_are_independent_between_parts_and_plans():
    material = {"key": "a", "metadata": {"captions": ["original"]}}
    parts = ordered_delivery_parts("", [material, material], "assets_only")
    other = ordered_delivery_parts("", [material], "assets_only")[0]
    assert isinstance(parts[0].material, dict)
    assert parts[0].material is not material
    parts[0].material["metadata"]["captions"].append("local change")
    assert material["metadata"]["captions"] == ["original"]
    assert parts[1].material == material
    assert other.material == material


@pytest.mark.parametrize("mode", ["assets_only", "text_only", "assets_then_text", "text_then_assets"])
def test_follow_up_only_and_empty_plans(mode):
    assert ordered_delivery_parts("  ", [], mode, follow_up_question="  ") == []
    parts = ordered_delivery_parts("", [], mode, follow_up_question="Question?", interval_seconds=3)
    assert len(parts) == 1
    assert parts[0].is_follow_up and parts[0].interval_seconds == 0


@pytest.mark.parametrize("interval", [-1, float("nan"), float("inf")])
def test_invalid_intervals_are_rejected(interval):
    with pytest.raises(ValueError, match="delivery_interval_invalid"):
        ordered_delivery_parts("body", [], "text_only", interval_seconds=interval)


@pytest.mark.parametrize("route_variant", ["peach_9d_2027", "snapshot_only"])
def test_bound_route_spec_overrides_current_route(route_variant):
    spec = {"groups": {"hotel_reference": {
        "assets": ["routes12-hilton-room"], "delivery_mode": "assets_only",
    }}}
    assert delivery_mode_for(
        route_variant, ["hotel_reference"], ["routes12-hilton-room"],
        route_spec=spec,
    ) == "assets_only"
    assert spec["groups"]["hotel_reference"]["delivery_mode"] == "assets_only"


@pytest.mark.parametrize("spec", [{}, {"groups": {}}, {"groups": {
    "hotel_reference": {"assets": ["snapshot-only-asset"], "delivery_mode": "assets_only"},
}}])
def test_bound_route_spec_never_falls_back_to_current_groups(spec):
    assert delivery_mode_for(
        "peach_9d_2027", ["hotel_reference"], ["routes12-hilton-room"],
        route_spec=spec,
    ) == "text_then_assets"


def test_explicit_none_route_spec_preserves_current_route_lookup():
    assert delivery_mode_for(
        "peach_9d_2027", ["hotel_reference"], ["routes12-hilton-room"],
        route_spec=None,
    ) == "assets_then_text"


def test_route_delivery_mode_is_resolved_once_from_reviewed_group():
    assert delivery_mode_for(
        'peach_9d_2027', ['hotel_reference'],
        ['routes12-hilton-room', 'routes12-hilton-oxygen'],
    ) == 'assets_then_text'
    assert delivery_mode_for('', [], []) == 'text_then_assets'


@pytest.mark.parametrize(('mode', 'expected'), [
    ('text_only', ['text']),
    ('assets_only', ['media', 'media']),
    ('text_then_assets', ['text', 'media', 'media']),
    ('assets_then_text', ['media', 'media', 'text']),
])
def test_delivery_order_contract(mode, expected):
    parts = ordered_delivery_parts('文字', [{'key': 'a'}, {'key': 'b'}], mode)
    assert [part.kind for part in parts] == expected


def test_unknown_delivery_mode_is_rejected():
    with pytest.raises(ValueError, match='delivery_mode_invalid'):
        ordered_delivery_parts('文字', [], 'unknown')
