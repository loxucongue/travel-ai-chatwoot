import pytest

from app.route_reply import append_deferred_initial_follow_up, deferred_initial_follow_up
from app.silence_planning import _has_unanswered_slot_question


def test_direct_answer_still_asks_missing_people_after_intro():
    result = deferred_initial_follow_up({
        "action": "reply", "route_variant": "peach_9d_2027",
        "missing_slots": ["party_size", "departure_window"],
    }, [])
    assert result["field"] == "party_size"


def test_intro_question_respects_known_fields_and_health_boundary():
    decision = {"action": "reply", "route_variant": "peach_9d_2027", "missing_slots": ["departure_window"]}
    assert deferred_initial_follow_up(decision, [])["field"] == "departure_window"
    assert deferred_initial_follow_up({**decision, "missing_slots": []}, []) is None
    assert deferred_initial_follow_up({**decision, "safety_flags": ["skip_silence_enrollment"]}, []) is None
    assert deferred_initial_follow_up({**decision, "safety_flags": ["deferred_follow_up_prohibited"]}, []) is None


def test_executor_never_invents_question_when_planner_explicitly_prohibits_it():
    decision = {
        "action": "reply",
        "route_variant": "peach_9d_2027",
        "missing_slots": ["party_size", "departure_window"],
        "follow_up_question": "",
        "safety_flags": ["deferred_follow_up_prohibited"],
    }
    assert deferred_initial_follow_up(decision, []) is None


@pytest.mark.parametrize("route", ["peach_9d_2027", "peach_11d_2027"])
@pytest.mark.parametrize("field,group", [
    ("party_size", "party_question"),
    ("departure_window", "departure_question"),
])
def test_deferred_question_carries_delivery_ledger_group(route, field, group):
    nodes = append_deferred_initial_follow_up(
        [{"initial_delivery": True}], route,
        {"type": "slot", "field": field, "question": "Question?"},
    )
    assert nodes[-1]["content_group_key"] == group
    assert not _has_unanswered_slot_question(route, set(), {})
    assert _has_unanswered_slot_question(route, {group}, {})
