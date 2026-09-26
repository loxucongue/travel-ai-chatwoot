from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from scripts import run_advisor_playground_acceptance as acceptance


def run(action="reply", status="completed", reason="", **trace):
    return SimpleNamespace(id=2, module="silence_touch", status=status,
                           decision={"action": action},
                           trace={"skip_reason": reason, **trace})


def message(**overrides):
    return {"run_id": 2, "direction": "outgoing", "content": "New route detail",
            "status": "simulated_delivered", **overrides}


def scenario(expectation="positive_value"):
    return {"key": "test", "silence_touches": 1, "silence_expectation": expectation}


@pytest.mark.parametrize("runs,messages", [
    ([], []), ([run()], []), ([run()], [message(content="  ")]),
    ([run()], [message(status="draft")]),
    ([run()], [message(direction="incoming")]),
    ([run()], [message(run_id=1)]),
    ([run(status="failed")], [message()]),
])
def test_required_positive_rejects_missing_delivery(runs, messages):
    assert not acceptance._silence_result(scenario(), runs, messages)["passed"]


@pytest.mark.parametrize("output", [message(), message(content="", media_id=9)])
def test_required_positive_accepts_actual_delivery(output):
    result = acceptance._silence_result(scenario(), [run()], [output])
    assert result["passed"]
    assert result["counts"]["positive_value"] == 1
    assert result["delivered_messages"] == 1


def test_expected_no_action_needs_reason_and_no_output():
    row = run("no_action", reason="silence_no_relevant_content")
    assert acceptance._silence_result(scenario("no_action"), [row], [])["passed"]
    assert not acceptance._silence_result(scenario(), [row], [])["passed"]
    assert not acceptance._silence_result(scenario("no_action"), [row], [message()])["passed"]
    assert not acceptance._silence_result(scenario("no_action"), [run("no_action")], [])["passed"]


@pytest.mark.parametrize("trace", [
    {"reason": "fact_verification_failed_no_action"},
    {"fact_verification_passed": False},
    {"reason": "silence_content_without_facts"},
])
def test_verification_blocked_is_not_expected_no_action(trace):
    result = acceptance._silence_result(scenario("value_or_no_action"), [run("no_action", **trace)], [])
    assert not result["passed"]
    assert result["counts"]["verification_blocked"] == 1
    assert result["counts"]["expected_no_action"] == 0


def test_mixed_touches_do_not_all_have_to_send():
    first = run()
    second = run("no_action", reason="silence_no_relevant_content")
    second.id = 3
    spec = {**scenario("value_or_no_action"), "silence_touches": 2}
    result = acceptance._silence_result(spec, [first, second], [message()])
    assert result["passed"]
    assert result["observed_touches"] == 2
    assert result["counts"]["expected_no_action"] == 1
    assert not acceptance._silence_result(spec, [first], [message()])["passed"]


def test_every_silence_scenario_has_explicit_expectation():
    for spec in acceptance.SCENARIOS:
        if spec.get("silence_touches"):
            assert spec["key"] in acceptance.SILENCE_EXPECTATIONS
    assert acceptance.SILENCE_EXPECTATIONS["unselected_route"] == "positive_value"


def test_session_empty_silence_fails_and_report_exposes_breakdown(monkeypatch):
    spec = next(item for item in acceptance.SCENARIOS if item["key"] == "unselected_route")
    db = MagicMock()
    db.get.return_value = SimpleNamespace(messages=[], controls={})
    reply = SimpleNamespace(id=1, module="reply", status="completed",
                            decision={"action": "reply", "route_variant": ""}, trace={})
    db.scalars.return_value.all.side_effect = [[reply], []]
    factory = MagicMock()
    factory.return_value.__enter__.return_value = db
    monkeypatch.setattr(acceptance, "SessionLocal", factory)
    result = acceptance._session_result(1, spec)
    assert not result["passed"]
    assert not result["checks"]["silence_expected_outcome"]
    counts = acceptance._report_counts([result])
    assert counts["failed_sessions"] == 1
    assert counts["checks_failed"] >= 1
    report = {"sessions": [result], "effective_large_group_threshold": 8,
              "passed_sessions": 0, "total_sessions": 1, "outbound_before": 0, "outbound_after": 0}
    markdown = acceptance._markdown(report)
    assert "touches=0/1" in markdown
    assert "silence_expected_outcome" in markdown


def mock_silence_wait(monkeypatch, status, *, active=False, messages=None):
    db = MagicMock()
    db.get.side_effect = [SimpleNamespace(messages=messages or []), SimpleNamespace(status=status)]
    factory = MagicMock()
    factory.return_value.__enter__.return_value = db
    monkeypatch.setattr(acceptance, "SessionLocal", factory)
    driver = MagicMock()
    monkeypatch.setattr(acceptance, "_drive_once", driver)
    monkeypatch.setattr(acceptance, "_active_run", lambda *args: active)
    # One polling iteration, then the deadline; no real sleeps or worker calls.
    monkeypatch.setattr(acceptance.time, "time", MagicMock(side_effect=[0, 0, 2]))
    return driver


@pytest.mark.parametrize("status", ["skipped", "verification_blocked", "blocked"])
def test_wait_silence_terminal_returns_for_finished_unsent_jobs(monkeypatch, status):
    driver = mock_silence_wait(monkeypatch, status)
    acceptance._wait_silence_terminal(1, 2, drive_worker=True, timeout=1)
    driver.assert_called_once_with(True, 1)


@pytest.mark.parametrize("status", ["skipped", "verification_blocked", "blocked"])
@pytest.mark.parametrize("pending", ["active_run", "draft"])
def test_wait_silence_terminal_still_waits_for_pending_work(monkeypatch, status, pending):
    mock_silence_wait(monkeypatch, status, active=pending == "active_run",
                      messages=[message(status="draft")] if pending == "draft" else [])
    with pytest.raises(TimeoutError, match="playground_silence_timeout:1:2"):
        acceptance._wait_silence_terminal(1, 2, drive_worker=True, timeout=1)


@pytest.mark.parametrize("status", ["skipped", "verification_blocked", "blocked"])
def test_terminal_unsent_job_does_not_pass_positive_contract(monkeypatch, status):
    if status == "skipped":
        row = run("no_action", reason="silence_no_relevant_content")
        messages = []
    elif status == "verification_blocked":
        row = run("no_action", reason="fact_verification_failed_no_action", fact_verification_passed=False)
        messages = []
    else:
        row = run()
        messages = [message(status="blocked")]
    mock_silence_wait(monkeypatch, status, messages=messages)
    acceptance._wait_silence_terminal(1, 2, drive_worker=True, timeout=1)
    result = acceptance._silence_result(scenario(), [row], messages)
    assert not result["passed"]
    assert result["counts"]["positive_value"] == 0
    assert result["delivered_messages"] == 0
    optional = acceptance._silence_result(scenario("value_or_no_action"), [row], messages)
    assert optional["passed"] is (status == "skipped")
    if status == "verification_blocked":
        assert result["counts"]["verification_blocked"] == 1
