import json
import httpx
import pytest

from app.reply_context import fetch_customer_context
from app.history_sync import fetch_message_history, IncompleteHistoryError
from app.deepseek_evaluation import close_deepseek_transport, _post_with_deadline


class History:
    def __init__(self):
        self.calls = []

    def list_contact_conversations(self, _):
        return {"payload": [{"id": 26, "inbox_id": 12}, {"id": 27, "inbox_id": 12}, {"id": 28, "inbox_id": 99}]}

    def get_messages(self, cid, before=None):
        self.calls.append((cid, before))
        base = 0 if cid == 26 else 100
        messages = [{"id": i, "created_at": 1787700000 + i, "message_type": 0,
            "private": i == 3, "content": "old memory" if i == 1 else str(i)} for i in range(base + 1, base + 66)]
        return {"payload": [m for m in messages if before is None or m["id"] < before][-7:]}


def test_full_contact_history_paginates_short_pages_and_never_leaks_future_or_other_inbox():
    client = History()
    context, trace = fetch_customer_context(client, {"id": 27, "inbox_id": 12, "meta": {"sender": {"id": 5}}}, [150])
    assert context[0]["content"] == "old memory"
    assert len(context) == 64 + 49
    assert max(m["id"] for m in context) == 149
    assert all(cid != 28 for cid, _ in client.calls)
    assert 3 not in [m["id"] for m in context]
    assert trace["context_complete"] and trace["history_conversations"] == 2


def test_partial_history_never_claims_complete():
    with pytest.raises(IncompleteHistoryError, match="page_limit"):
        fetch_message_history(History(), 26, max_pages=1)
    class Broken(History):
        def get_messages(self, cid, before=None):
            return {"payload": [{"id": 3}]}
    with pytest.raises(IncompleteHistoryError, match="cursor"):
        fetch_message_history(Broken(), 26)


def test_unknown_history_timestamp_fails_closed():
    from app.reply_context import public_context
    with pytest.raises(IncompleteHistoryError, match="timestamp_unknown"):
        public_context([{"id": 1, "content": "unknown date"}], {"id": 2, "created_at": 1787700000})


@pytest.mark.parametrize("body", [{"unexpected": []}, {"payload": [{"content": "missing id"}]}])
def test_malformed_history_response_is_not_treated_as_empty_complete_page(body):
    class Broken:
        def get_messages(self, *_args, **_kwargs):
            return body
    with pytest.raises(IncompleteHistoryError):
        fetch_message_history(Broken(), 26)


def test_deepseek_stream_handles_keepalive_usage_and_reuses_transport(monkeypatch):
    from app import deepseek_evaluation as adapter
    close_deepseek_transport()
    monkeypatch.setattr(adapter.settings, "deepseek_api_key", "isolated-test-key")
    real_client = httpx.AsyncClient
    created = []
    content = ": keep-alive\n\n" + "\n\n".join("data: " + json.dumps(chunk) for chunk in [
        {"choices": [{"delta": {"content": '{"action":"reply"}'}, "finish_reason": None}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        {"choices": [], "usage": {"prompt_tokens": 20, "completion_tokens": 5}},
    ]) + "\n\ndata: [DONE]\n\n"
    def factory(**kwargs):
        created.append(1)
        return real_client(transport=httpx.MockTransport(lambda req: httpx.Response(200, headers={"content-type": "text/event-stream"}, text=content)), **kwargs)
    monkeypatch.setattr(httpx, "AsyncClient", factory)
    try:
        for _ in range(2):
            response = _post_with_deadline({"stream": True}, 2)
            assert response.json()["choices"][0]["message"]["content"] == '{"action":"reply"}'
            assert response.json()["usage"]["prompt_tokens"] == 20
            assert response.extensions["call_timing"]["first_token_ms"] is not None
        assert len(created) == 1
    finally:
        close_deepseek_transport()


def test_stream_reassembles_interleaved_tool_arguments_and_provider_identity(monkeypatch):
    close_deepseek_transport()
    real_client = httpx.AsyncClient
    chunks = [
        {'model':'provider-model','system_fingerprint':'revision-1','choices':[{'delta':{'tool_calls':[
            {'index':0,'id':'first','function':{'name':'get_route_facts','arguments':'{"topic":'}},
            {'index':1,'id':'second','function':{'name':'load_skill','arguments':'{"name":'}}]}}]},
        {'choices':[{'delta':{'tool_calls':[
            {'index':1,'function':{'arguments':'"route-selection"}'}},
            {'index':0,'function':{'arguments':'"price"}'}}]},'finish_reason':'tool_calls'}]},
        {'choices':[],'usage':{'prompt_tokens':50}},
    ]
    content = '\n\n'.join('data: '+json.dumps(c) for c in chunks)+'\n\ndata: [DONE]\n\n'
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kw:real_client(transport=httpx.MockTransport(
        lambda req:httpx.Response(200,headers={'content-type':'text/event-stream'},text=content)),**kw))
    try:
        body = _post_with_deadline({'stream':True},2).json()
        calls = body['choices'][0]['message']['tool_calls']
        assert [c['id'] for c in calls] == ['first','second']
        assert json.loads(calls[0]['function']['arguments']) == {'topic':'price'}
        assert json.loads(calls[1]['function']['arguments']) == {'name':'route-selection'}
        assert body['model'] == 'provider-model' and body['system_fingerprint'] == 'revision-1'
    finally:
        close_deepseek_transport()


def test_output_token_limit_is_not_misreported_as_retryable_network_error(monkeypatch):
    close_deepseek_transport()
    real_client=httpx.AsyncClient
    content='data: '+json.dumps({'choices':[{'delta':{},'finish_reason':'length'}]})+'\n\ndata: [DONE]\n\n'
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kw:real_client(transport=httpx.MockTransport(
        lambda req:httpx.Response(200,headers={'content-type':'text/event-stream'},text=content)),**kw))
    try:
        with pytest.raises(ValueError,match='deepseek_output_token_limit') as error:
            _post_with_deadline({'stream':True},2)
        assert error.value.call_timing['finish_reason']=='length'
    finally:
        close_deepseek_transport()


def test_transport_deadline_cancels_slow_response_without_late_result(monkeypatch):
    import asyncio
    import time
    from app import deepseek_evaluation as adapter
    close_deepseek_transport()
    monkeypatch.setattr(adapter.settings,'deepseek_api_key','isolated-test-key')
    real_client=httpx.AsyncClient
    completed=[]
    async def slow(request):
        await asyncio.sleep(0.3)
        completed.append(True)
        return httpx.Response(200,json={'choices':[]})
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kw:real_client(transport=httpx.MockTransport(slow),**kw))
    started=time.monotonic()
    try:
        with pytest.raises(TimeoutError): _post_with_deadline({},0.03)
        assert time.monotonic()-started < 1
        assert completed == []
    finally:
        close_deepseek_transport()


def test_schema_repair_names_invalid_route_field_without_retrying_valid_results(monkeypatch):
    from app import deepseek_evaluation as adapter
    monkeypatch.setattr(adapter.settings, "deepseek_api_key", "isolated-test-key")
    payloads = []
    def respond(payload, _timeout):
        payloads.append(payload)
        decision = {"action": "handoff", "branch": "peach_11d" if len(payloads) == 1 else "unclassified", "intent": "other",
                    "route_variant": "peach_11d" if len(payloads) == 1 else ""}
        return httpx.Response(200, request=httpx.Request("POST", "https://example.invalid"), json={
            "choices": [{"message": {"content": json.dumps(decision)}, "finish_reason": "stop"}]})
    monkeypatch.setattr(adapter, "_post_with_deadline", respond)
    decision, logs, _ = adapter.call_deepseek({"context_messages": [], "customer_text": "isolated question"})
    assert decision.action == "handoff" and decision.route_variant == ""
    assert len(payloads) == 2
    assert "deepseek_invalid_route_variant" in payloads[1]["messages"][-1]["content"]
    assert "peach_11d_2027" in payloads[1]["messages"][-1]["content"]
    assert logs[0]["error_code"] == "deepseek_invalid_route_variant"


def test_playground_queues_all_history_not_last_thirty(session_factory):
    from sqlalchemy import select
    from app.automation_models import AutomationSession, AutomationRun
    from app.automation_service import queue_passive
    from app.models import utcnow
    with session_factory() as db:
        history = [{"direction": "outgoing", "content": str(i), "created_at": utcnow()} for i in range(80)]
        session = AutomationSession(owner_id=1, virtual_now=utcnow(), due_at=utcnow(),
            controls={"can_reply": True, "ai_enabled": True, "channel": "facebook"},
            messages=history + [{"direction": "incoming", "content": "next", "created_at": utcnow()}])
        db.add(session)
        db.commit()
        assert queue_passive(db)
        run = db.scalar(select(AutomationRun))
        assert len(run.input_snapshot["context_messages"]) == 80


def test_local_history_session_retains_over_100_messages(session_factory):
    from app.automation_api import build_session, SessionCreate
    from app.models import InboxBinding, ConversationState, MessageEvent, User, utcnow
    with session_factory() as db:
        db.add(InboxBinding(id=1, tenant_id=1, chatwoot_inbox_id=128859, name="test"))
        db.flush()
        db.add(ConversationState(id=1, tenant_id=1, inbox_binding_id=1, chatwoot_conversation_id=27))
        db.flush()
        db.add_all([MessageEvent(conversation_state_id=1, chatwoot_message_id=i, direction="outgoing", content=str(i), created_at="2026-01-01T00:00:00+00:00") for i in range(1, 131)])
        db.flush()
        session = build_session(SessionCreate(conversation_id=1, virtual_now=utcnow()), db.get(User, 1), db)
        assert len(session.messages) == 130
        assert session.controls["history_complete"] is False  # A local mirror alone is not proof of cloud completeness.
