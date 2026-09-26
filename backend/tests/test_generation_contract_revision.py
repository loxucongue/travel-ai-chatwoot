import json

import httpx
import pytest

from app import model_gateway


@pytest.mark.parametrize("node", ["reply_generation", "silence_generation"])
def test_rejected_generation_is_repaired_as_data(monkeypatch, node):
    monkeypatch.setattr(model_gateway.settings, "deepseek_api_key", "test")
    requests = []

    def post(payload, timeout):
        requests.append(payload)
        body = "rejected" if len(requests) == 1 else "corrected"
        return httpx.Response(200, request=httpx.Request("POST", "https://example.test"), json={
            "choices": [{"message": {"content": json.dumps({"body": body})}, "finish_reason": "stop"}],
            "usage": {},
        })

    def parse(value):
        if value["body"] == "rejected":
            raise ValueError("specific_contract_error")
        return value

    monkeypatch.setattr(model_gateway, "_post_with_deadline", post)
    result, logs, _ = model_gateway.call_json_node(
        node=node, system_prompt="fixed contract", input_data={"reply_plan": {"action": "reply"}},
        parser=parse, max_tokens=100, repair_prompt="repair contract",
    )
    assert result["body"] == "corrected"
    assert len(requests) == 2
    assert [m["role"] for m in requests[1]["messages"]] == ["system", "user"]
    data = json.loads(requests[1]["messages"][1]["content"])
    assert data["reply_plan"] == {"action": "reply"}
    assert data["contract_revision"]["error"] == "specific_contract_error"
    assert json.loads(data["contract_revision"]["rejected_output"])["body"] == "rejected"
    assert logs[0]["error_code"] == "specific_contract_error"
