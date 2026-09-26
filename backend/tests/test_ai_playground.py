from app.deepseek_evaluation import EvaluationDecision
from app.models import OutboundMessage


def test_playground_returns_diagnostics_without_outbound(authenticated, session_factory, monkeypatch):
    client, csrf = authenticated

    def fake_call(_case):
        return EvaluationDecision(
            action="reply",
            branch="peach_11d",
            route_variant="peach_11d_2027",
            route_evidence="想了解11天桃花加珠峰",
            intent="itinerary",
            reply="可以，這條路線會包含林芝桃花與珠峰。",
            slots={"party_size": 2},
            missing_slots=["departure_window"],
            evidence_refs=["route.11.days"],
            confidence=0.92,
        ), [{"attempt": 1, "input_tokens": 120, "output_tokens": 60}], "digest", {"prompt_version": "test-split"}

    monkeypatch.setattr("app.decision_service.generate_decision", fake_call)
    with session_factory() as db:
        before = db.query(OutboundMessage).count()

    response = client.post(
        "/v1/evaluation/playground/reply",
        headers={"X-CSRF-Token": csrf},
        json={
            "scenario": "peach_11d",
            "customer_message": "兩個人想去，行程怎麼安排？",
            "messages": [{"role": "customer", "content": "想了解11天桃花加珠峰"}],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["outbound"] is False
    assert body["branch"] == "peach_11d"
    assert body["branch_name"] == "桃花加珠峰 11 日"
    assert body["action"] == "reply"
    assert body["input_tokens"] == 120
    with session_factory() as db:
        assert db.query(OutboundMessage).count() == before


def test_playground_preserves_model_business_decision(authenticated, monkeypatch):
    client, csrf = authenticated

    def fake_call(_case):
        return EvaluationDecision(
            action="reply", branch="peach_9d", intent="price",
            reply="價格需要進一步確認。", confidence=0.8,
        ), [{"attempt": 1}], "digest", {"prompt_version": "test-split"}

    monkeypatch.setattr("app.decision_service.generate_decision", fake_call)
    response = client.post(
        "/v1/evaluation/playground/reply",
        headers={"X-CSRF-Token": csrf},
        json={"scenario": "peach_9d", "customer_message": "這個行程多少錢？", "messages": []},
    )

    assert response.status_code == 200
    assert response.json()["action"] == "reply"
    assert response.json()["reply"] == "價格需要進一步確認。"
    assert response.json()["handoff_reason"] is None


def test_playground_compare_runs_isolated_v1_and_v2(authenticated, monkeypatch):
    client, csrf = authenticated
    seen = []

    def fake_call(case):
        engine = case["engine_version"]
        seen.append(engine)
        return EvaluationDecision(
            action="reply", branch="peach_9d", intent="price",
            reply=f"{engine} reply", confidence=0.8,
        ), [{"attempt": 1}], "digest", {
            "prompt_version": f"{engine}-prompt", "engine_version": engine,
            "engine_release_id": f"{engine}-release",
        }

    monkeypatch.setattr("app.decision_service.generate_decision", fake_call)
    response = client.post(
        "/v1/evaluation/playground/compare",
        headers={"X-CSRF-Token": csrf},
        json={"scenario": "peach_9d", "customer_message": "价格多少？", "messages": []},
    )

    assert response.status_code == 200
    body = response.json()
    assert seen == ["v1", "v2"]
    assert body["v1"]["reply"] == "v1 reply"
    assert body["v2"]["reply"] == "v2 reply"
    assert body["outbound"] is False
