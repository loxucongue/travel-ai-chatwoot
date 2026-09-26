from app.config import settings
from app.customer_journey_dataset import dataset_summary, run_dataset_cases, selected_cases
from app.deepseek_evaluation import EvaluationDecision
from app.route_reply import ROUTE_SNAPSHOTS_KEY, route_snapshot_from_values


def test_customer_journey_dataset_loads_generated_cases():
    summary = dataset_summary("v2026-09-03")

    assert summary["case_count"] >= 100
    assert summary["by_suite"]["answer"] >= 10
    assert summary["by_suite"]["journey"] >= 5
    assert summary["by_suite"]["shadow"] >= 200
    assert summary["sop_silence_minutes"] == [1, 3, 5, 10, 30, 60]
    assert any(item["scenario"] == "price_hotel" for item in summary["cases"])


def test_selected_cases_can_filter_core_suites():
    cases = selected_cases("v2026-09-03", None, ["answer", "journey"])

    assert cases
    assert all(case["suite"] in {"answer", "journey"} for case in cases)
    assert not any(case["suite"] == "shadow" for case in cases)


def test_dataset_runner_requires_ai_label(session_factory):
    case = selected_cases("v2026-09-03", ["answer_no_ai_label_full_auto"], None)[0]
    with session_factory() as db:
        result = run_dataset_cases(db, [case], model_call=lambda _payload: (
            EvaluationDecision(
                action="reply",
                branch="peach_9d",
                intent="route_intro",
                reply="桃花9日行程以林芝桃花与小团体验为主。",
                route_variant="peach_9d_2027",
                journey_stage="value_building",
            ),
            [{"status": "completed"}],
            "hash",
        ))

    assert result["outbound"] is False
    assert result["summary"] == {"total": 1, "passed": 0, "failed": 1}
    item = result["results"][0]
    assert item["blocked_reason"] == "missing_ai_label"


def test_dataset_runner_scores_reply_and_sop_preview(session_factory):
    case = {
        "case_id": "unit_price_hotel",
        "suite": "answer",
        "scenario": "price_hotel",
        "customer_type": "unit",
        "title": "unit",
        "messages": [{"role": "customer", "content": "我们2位，明年3月底出发，住宿和价格怎样？"}],
        "initial_route_variant": "peach_9d_2027",
        "initial_memory": {},
        "initial_sent_groups": [],
        "lead_capture": {"status": "not_started", "request_count": 0, "captured_kinds": []},
        "controls": {"ai_label": True, "can_reply": True, "human": False},
        "expected": {
            "action": "reply",
            "route_variant": "peach_9d_2027",
            "slots": {"party_size": "2位", "departure_window": "明年3月底"},
            "must_answer_topics": ["price", "hotel", "contact"],
            "lead_action": "ask",
            "will_enroll_sop": True,
            "next_sop_behavior": "stage_driven_mandatory_touch",
        },
    }

    def model_call(_payload):
        assert route_snapshot_from_values("peach_9d_2027", _payload["journey"]["slots"])
        assert not _payload["journey"]["automatic_delivery_paused"]
        return (
            EvaluationDecision(
                action="reply",
                branch="peach_9d",
                intent="price",
                reply="住宿以当地酒店为主，价格依资料说明。方便留下LINE让顾问核对吗？",
                route_variant="peach_9d_2027",
                slots={"party_size": "2位", "departure_window": "明年3月底"},
                slot_evidence={"party_size": "2位", "departure_window": "明年3月底"},
                content_group_key="price_reference",
                covered_content_groups=["price_reference", "hotel_reference", "contact_request"],
                lead_action="ask",
                journey_stage="contact_requested",
            ),
            [{"status": "completed"}],
            "hash",
        )

    with session_factory() as db:
        result = run_dataset_cases(db, [case], model_call=model_call)

    assert result["summary"] == {"total": 1, "passed": 1, "failed": 0}
    item = result["results"][0]
    assert item["outbound"] is False
    assert item["sop_preview"]["will_enroll"] is True
    assert item["sop_preview"]["timeline_minutes"] == [1, 3, 5, 10, 30, 60]
    assert item["lead_action"] == "ask"


def test_customer_journey_dataset_api_rejects_outbound(authenticated):
    client, csrf = authenticated

    summary = client.get("/v1/evaluation/customer-journey-dataset")
    assert summary.status_code == 200
    assert summary.json()["case_count"] >= 100

    response = client.post(
        "/v1/evaluation/customer-journey-dataset/run",
        headers={"X-CSRF-Token": csrf},
        json={
            "dataset_version": "v2026-09-03",
            "case_ids": ["answer_route_9d_intro"],
            "outbound": True,
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "dataset_run_must_be_read_only"


def test_customer_journey_dataset_api_runs_selected_case_read_only(authenticated, monkeypatch):
    client, csrf = authenticated

    def fake_model(_payload):
        return (
            EvaluationDecision(
                action="reply",
                branch="peach_9d",
                intent="route_intro",
                reply="我先提供桃花9日行程参考，请问预计几位同行？",
                route_variant="peach_9d_2027",
                content_group_key="itinerary_overview",
                covered_content_groups=["itinerary_overview", "entry_question"],
            ),
            [{"status": "completed"}],
            "hash",
            {"prompt_version": "test-split", "outbound": False},
        )

    monkeypatch.setattr("app.customer_journey_dataset.generate_decision", fake_model)

    response = client.post(
        "/v1/evaluation/customer-journey-dataset/run",
        headers={"X-CSRF-Token": csrf},
        json={
            "dataset_version": "v2026-09-03",
            "case_ids": ["answer_route_9d_intro"],
            "outbound": False,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["outbound"] is False
    assert body["summary"] == {"total": 1, "passed": 1, "failed": 0}
    assert body["results"][0]["sop_preview"]["timeline_minutes"] == [1, 3, 5, 10, 30, 60]


def test_evaluation_fixture_assumptions_are_explicit_and_do_not_mutate_memory():
    from app.customer_journey_dataset import _evaluation_journey_slots
    from app.route_reply import journey_context_from_values

    route = "peach_9d_2027"
    memory = {"party_size": "2"}
    slots = _evaluation_journey_slots(route, memory, ["hotel_reference"], [])
    assert memory == {"party_size": "2"}
    assert slots["_evaluation_fixture"] == {"synthetic": True, "assumed_sent_groups": ["hotel_reference"]}
    context = journey_context_from_values(route, slots=slots, sent_groups=["hotel_reference"])
    assert not context["automatic_delivery_paused"]
    assert context["completed_content_groups"] == ["hotel_reference"]


def test_evaluation_binder_does_not_repair_unknown_customer_snapshot():
    import pytest
    from app.customer_journey_dataset import _evaluation_journey_slots

    route = "peach_9d_2027"
    with pytest.raises(ValueError, match="route_snapshot_already_initialized"):
        _evaluation_journey_slots(route, {ROUTE_SNAPSHOTS_KEY: {route: {"history_unknown": True}}}, [], [])
