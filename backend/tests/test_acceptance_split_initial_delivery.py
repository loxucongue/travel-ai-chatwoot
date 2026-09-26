from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from scripts.run_advisor_playground_acceptance import _initial_delivery_result


def fixture(mode):
    spec = {"initial_delivery_interval_seconds": 2, "groups": {
        "greeting": {"text": "Hello", "assets": [], "delivery_mode": "text_only"},
        "value": {"text": "Reviewed value", "assets": ["a", "b"], "delivery_mode": mode},
    }, "sop": {"nodes": [{"content_group_key": key, "initial_delivery": True} for key in ("greeting", "value")]},
        "asset_bindings": {"a": {"media_id": 10}, "b": {"media_id": 11}}}
    text = {"content": "Reviewed value"}
    assets = [{"content": "", "media_id": 10}, {"content": "", "media_id": 11}]
    parts = {"text_only": [text], "assets_only": assets,
             "text_then_assets": [text, *assets], "assets_then_text": [*assets, text]}[mode]
    jobs = [SimpleNamespace(id=1, status="already_provided", payload={"initial_delivery": True, "content_group_key": "greeting"})]
    messages = [{"id": "ai", "content": "Hello", "run_id": 8, "source": "ai",
                 "content_attributes": {"delivery_item": {"group_key": "greeting"}}}]
    for index, part in enumerate(parts, 2):
        jobs.append(SimpleNamespace(id=index, status="simulated_delivered", payload={"initial_delivery": True, "content_group_key": "value"}))
        messages.append({**part, "id": str(index), "source": "sop", "group_id": index})
    jobs.append(SimpleNamespace(id=99, status="simulated_delivered", payload={"initial_delivery": True,
        "content_group_key": "question", "deferred_follow_up": {"question": "Guests?"}}))
    messages.append({"id": "question", "source": "sop", "group_id": 99, "content": "Guests?"})
    for index, item in enumerate(messages):
        item.update(direction="outgoing", status="simulated_delivered", timeline_sequence=index + 1,
                    created_at=(datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=2 * index)).isoformat())
    runs = [SimpleNamespace(id=8, module="reply", status="completed")]
    return spec, jobs, runs, messages


@pytest.mark.parametrize("mode", ["text_only", "assets_only", "text_then_assets", "assets_then_text"])
def test_split_jobs_validate_as_one_frozen_content_group(mode):
    spec, jobs, runs, messages = fixture(mode)
    before = deepcopy(spec)
    result = _initial_delivery_result(spec, jobs, runs, list(reversed(messages)))
    assert all(result["checks"].values()), result
    assert result["groups"] == ["value", "question"]
    assert result["preprovided_groups"] == ["greeting"]
    assert spec == before


@pytest.mark.parametrize("mutation", ["missing_image", "missing_text", "draft_text", "blocked_image",
                                       "wrong_image", "wrong_text", "no_messages", "no_snapshot"])
def test_split_groups_never_infer_delivery_from_completed_jobs(mutation):
    spec, jobs, runs, messages = fixture("assets_then_text")
    if mutation == "missing_image":
        messages = [item for item in messages if item.get("media_id") != 10]
    elif mutation == "missing_text":
        messages = [item for item in messages if item.get("content") != "Reviewed value"]
    elif mutation == "draft_text":
        next(item for item in messages if item.get("content") == "Reviewed value")["status"] = "draft"
    elif mutation == "blocked_image":
        next(item for item in messages if item.get("media_id") == 10)["status"] = "blocked"
    elif mutation == "wrong_image":
        next(item for item in messages if item.get("media_id") == 10)["media_id"] = 999
    elif mutation == "wrong_text":
        next(item for item in messages if item.get("content") == "Reviewed value")["content"] = "Arbitrary caption"
    elif mutation == "no_messages":
        messages = []
    else:
        spec = None
    assert not all(_initial_delivery_result(spec, jobs, runs, messages)["checks"].values())


@pytest.mark.parametrize("mutation,failed_check", [
    ("reverse_mode", "initial_delivery_mode_is_respected"),
    ("interleave_groups", "initial_delivery_order_matches_config"),
    ("early_question", "initial_question_is_last"),
    ("short_gap", "initial_delivery_gap_matches_route_config"),
])
def test_actual_receipt_order_and_timing_are_checked(mutation, failed_check):
    spec, jobs, runs, messages = fixture("assets_then_text")
    if mutation == "reverse_mode":
        messages[1]["timeline_sequence"], messages[3]["timeline_sequence"] = 4, 2
    elif mutation == "interleave_groups":
        messages[0]["timeline_sequence"], messages[2]["timeline_sequence"] = 3, 1
    elif mutation == "early_question":
        messages[-1]["timeline_sequence"] = 0
    else:
        messages[2]["created_at"] = messages[1]["created_at"]
    assert not _initial_delivery_result(spec, jobs, runs, messages)["checks"][failed_check]


def direct_answer_fixture(group="value", *, priority_assets=False):
    spec, jobs, runs, messages = fixture("assets_then_text")
    # Unlike an intro-only turn, the greeting remains in the fixed SOP.
    messages[0].update(source="sop", group_id=1)
    messages[0].pop("run_id")
    jobs[0].status = "simulated_delivered"
    direct = []
    if priority_assets:
        for media_id in (10, 11):
            item = next(item for item in messages if item.get("media_id") == media_id)
            messages.remove(item)
            direct.append({**item, "id": f"priority-{media_id}", "source": "ai", "run_id": 8,
                           "content_attributes": {"delivery_item": {"group_key": group}}})
            direct[-1].pop("group_id")
    direct.append({"id": "answer", "source": "ai", "run_id": 8, "content": "Answer to the current question.",
                   "content_attributes": {"delivery_item": {"group_key": group}},
                   "direction": "outgoing", "status": "simulated_delivered"})
    all_messages = direct + messages
    for index, item in enumerate(all_messages):
        seconds = 2 * index
        item.update(timeline_sequence=index + 1,
                    created_at=(datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=seconds)).isoformat())
    return spec, jobs, runs, all_messages


@pytest.mark.parametrize("group", ["value", "price_reference", "spring_weather", "tips_reference", "route_scope"])
def test_direct_answer_does_not_become_fixed_mainline_copy_or_order(group):
    args = direct_answer_fixture(group)
    result = _initial_delivery_result(*args)
    assert all(result["checks"].values()), result
    assert result["groups"] == ["greeting", "value", "question"]
    assert [item["message_id"] for item in result["direct_answer_messages"]] == ["answer"]


def test_priority_images_are_receipts_not_a_requirement_to_repeat_them():
    result = _initial_delivery_result(*direct_answer_fixture(priority_assets=True))
    assert all(result["checks"].values()), result
    value = next(row for row in result["group_results"] if row["content_group_key"] == "value")
    assert value["prior_receipt_ids"] == ["priority-10", "priority-11"]
    assert value["actual_fixed_parts"] == ["text"]
    assert value["message_count"] == 3


@pytest.mark.parametrize("mutation", ["missing_text", "missing_image", "blocked_image", "wrong_hash",
                                       "wrong_copy", "duplicate_text", "duplicate_image", "wrong_order", "short_gap", "early_question",
                                       "skipped_assets_six_second_gap", "zero_stage_boundary"])
def test_direct_answer_never_excuses_incomplete_or_invalid_fixed_delivery(mutation):
    spec, jobs, runs, messages = direct_answer_fixture(priority_assets=True)
    reviewed = next(item for item in messages if item.get("content") == "Reviewed value")
    greeting = next(item for item in messages if item.get("content") == "Hello")
    priority = next(item for item in messages if item.get("media_id") == 10)
    if mutation == "missing_text":
        messages.remove(reviewed)
    elif mutation == "missing_image":
        messages.remove(priority)
    elif mutation == "blocked_image":
        priority["status"] = "blocked"
    elif mutation == "wrong_hash":
        priority["media_hash"] = "not-the-snapshot-hash"
    elif mutation == "wrong_copy":
        reviewed["content"] = "Answer to the current question."
    elif mutation == "duplicate_text":
        messages.append({**reviewed, "id": "repeated-caption"})
    elif mutation == "duplicate_image":
        messages.append({**priority, "id": "repeated-image", "source": "sop", "group_id": 2,
                         "timeline_sequence": reviewed["timeline_sequence"]})
    elif mutation == "wrong_order":
        greeting["timeline_sequence"], reviewed["timeline_sequence"] = reviewed["timeline_sequence"], greeting["timeline_sequence"]
    elif mutation == "short_gap":
        reviewed["created_at"] = greeting["created_at"]
    elif mutation == "skipped_assets_six_second_gap":
        reviewed["created_at"] = (datetime.fromisoformat(greeting["created_at"]) + timedelta(seconds=6)).isoformat()
    elif mutation == "zero_stage_boundary":
        greeting["created_at"] = next(item for item in messages if item["id"] == "answer")["created_at"]
    else:
        messages[-1]["timeline_sequence"] = greeting["timeline_sequence"] - 1
    result = _initial_delivery_result(spec, jobs, runs, messages)
    assert not all(result["checks"].values())
    if mutation in {"skipped_assets_six_second_gap", "zero_stage_boundary"}:
        assert not result["checks"]["initial_delivery_gap_matches_route_config"]
        expected_bad_gap = 6 if mutation == "skipped_assets_six_second_gap" else 0
        assert any(item["actual_seconds"] == expected_bad_gap for item in result["visible_delivery_gaps"])
