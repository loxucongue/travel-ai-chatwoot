import asyncio
import json
import time

import httpx
import pytest

from app import model_metering as meter, model_transport as transport
from app.config import settings


@pytest.fixture
def gateway(monkeypatch):
    transport.close_deepseek_transport()
    transport._primary_failures.clear()
    transport._fallback_failures.clear()
    monkeypatch.delenv('MODEL_TEST_COST_LEDGER', raising=False)
    monkeypatch.setattr(settings, 'gemini_fallback_enabled', True)
    monkeypatch.setattr(settings, 'google_gemini_base_url', 'https://fallback.invalid')
    monkeypatch.setattr(settings, 'gemini_api_key', 'fallback-test-secret')
    monkeypatch.setattr(settings, 'gemini_model', 'gemini-2.5-flash')
    monkeypatch.setattr(settings, 'deepseek_base_url', 'https://primary.invalid')
    monkeypatch.setattr(settings, 'deepseek_api_key', 'primary-test-secret')
    monkeypatch.setattr(settings, 'model_primary_cooldown_seconds', 60)
    original = httpx.AsyncClient
    requests = []
    handlers = {}
    async def handle(request):
        requests.append(request)
        result = handlers[request.url.host](request)
        return await result if hasattr(result, '__await__') else result
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: original(transport=httpx.MockTransport(handle)))
    records = []
    token = meter.calls.set(records)
    yield handlers, requests, records
    meter.calls.reset(token)
    transport.close_deepseek_transport()
    transport._primary_failures.clear()
    transport._fallback_failures.clear()


def success(request):
    return httpx.Response(200, json={'model': json.loads(request.content)['model'],
        'choices': [{'message': {'content': '{"ok":true}'}, 'finish_reason': 'stop'}],
        'usage': {'prompt_tokens': 10, 'completion_tokens': 5}})


def payload():
    return {'model': 'deepseek-v4-flash', 'messages': [{'role': 'user', 'content': 'hello'}], 'max_tokens': 100}


def test_primary_success_never_calls_backup(gateway):
    handlers, requests, records = gateway
    handlers['primary.invalid'] = success
    result = transport._post_with_deadline(payload(), 2)
    assert len(requests) == 1 and result.extensions['model_provider'] == 'deepseek'
    assert records[0]['status'] == 'completed'


def test_bailian_protocol_and_next_turn_returns_to_primary(gateway, monkeypatch):
    handlers, requests, records = gateway
    monkeypatch.setattr(settings, 'model_fallback_provider', 'bailian')
    monkeypatch.setattr(settings, 'google_gemini_base_url', 'https://fallback.invalid/compatible-mode/v1')
    monkeypatch.setattr(settings, 'gemini_model', 'deepseek-v4.1-flash')
    monkeypatch.setattr(settings, 'model_primary_cooldown_seconds', 0)
    handlers['primary.invalid'] = lambda r: httpx.Response(503)
    handlers['fallback.invalid'] = success
    request_payload = {**payload(), 'thinking': {'type': 'disabled'}, 'reasoning_effort': 'none',
                       'tools': [{'type': 'function', 'function': {'name': 'lookup', 'parameters': {'type': 'object'}}}]}
    result = transport._post_with_deadline(request_payload, 2)
    body = json.loads(requests[-1].content)
    assert result.extensions['model_provider'] == 'bailian'
    assert requests[-1].url.path == '/compatible-mode/v1/chat/completions'
    assert body['model'] == 'deepseek-v4.1-flash' and body['enable_thinking'] is False
    assert 'thinking' not in body and 'reasoning_effort' not in body
    assert body['tools'] == request_payload['tools']
    assert records[-1]['provider'] == 'bailian'
    # Same tool turn may continue with its successful fallback.
    transport._post_with_deadline(request_payload, 2)
    assert requests[-1].url.host == 'fallback.invalid'
    # A new customer turn has its own metering context and starts with primary.
    records.clear()
    handlers['primary.invalid'] = success
    result = transport._post_with_deadline(request_payload, 2)
    assert requests[-1].url.host == 'primary.invalid'
    assert result.extensions['model_provider'] == 'deepseek'


def test_bailian_preserves_requested_thinking_effort(monkeypatch):
    monkeypatch.setattr(settings, 'model_fallback_provider', 'bailian')
    original = {**payload(), 'thinking': {'type': 'enabled'}, 'reasoning_effort': 'low'}
    result = transport._gemini_payload(original)
    assert result['enable_thinking'] is True and result['reasoning_effort'] == 'low'
    assert result['max_tokens'] == original['max_tokens']


@pytest.mark.parametrize('status', [403, 429, 503])
def test_bailian_failure_never_keeps_recovered_primary_bypassed(gateway, monkeypatch, status):
    handlers, requests, records = gateway
    monkeypatch.setattr(settings, 'model_fallback_provider', 'bailian')
    monkeypatch.setattr(settings, 'gemini_model', 'deepseek-v4.1-flash')
    handlers['primary.invalid'] = lambda r: httpx.Response(503)
    handlers['fallback.invalid'] = lambda r: httpx.Response(status)
    with pytest.raises(httpx.HTTPStatusError):
        transport._post_with_deadline(payload(), 2)
    assert [x['provider'] for x in records] == ['deepseek', 'bailian']
    handlers['primary.invalid'] = success
    result = transport._post_with_deadline(payload(), 2)
    assert result.extensions['model_provider'] == 'deepseek'
    assert [r.url.host for r in requests] == ['primary.invalid', 'fallback.invalid', 'primary.invalid']


@pytest.mark.parametrize('status', [401, 403, 408, 429, 500, 502, 503, 504])
def test_provider_failure_falls_back_and_accounts_both(gateway, status):
    handlers, requests, records = gateway
    handlers['primary.invalid'] = lambda r: httpx.Response(status)
    handlers['fallback.invalid'] = success
    result = transport._post_with_deadline(payload(), 2)
    assert [r.url.host for r in requests] == ['primary.invalid', 'fallback.invalid']
    assert requests[1].url.path == '/v1/chat/completions'
    assert requests[1].headers['authorization'] == 'Bearer fallback-test-secret'
    assert result.json()['model'] == 'gemini-2.5-flash'
    assert result.extensions['fallback_reason'] == 'http_' + str(status)
    assert [r['status'] for r in records] == ['failed', 'completed']
    assert records[0]['http_status'] == status and records[1]['cost_basis'] == 'gateway_estimate'
    assert 'test-secret' not in json.dumps(records)


@pytest.mark.parametrize('status', [400, 404, 413, 422])
def test_invalid_payload_is_not_hidden_by_fallback(gateway, status):
    handlers, requests, records = gateway
    handlers['primary.invalid'] = lambda r: httpx.Response(status)
    with pytest.raises(httpx.HTTPStatusError):
        transport._post_with_deadline(payload(), 2)
    assert len(requests) == len(records) == 1


@pytest.mark.parametrize('setting,value', [('gemini_fallback_enabled', False), ('gemini_api_key', '')])
def test_missing_or_disabled_fallback_preserves_primary_failure(gateway, monkeypatch, setting, value):
    monkeypatch.setattr(settings, setting, value)
    handlers, requests, _ = gateway
    handlers['primary.invalid'] = lambda r: httpx.Response(503)
    with pytest.raises(httpx.HTTPStatusError):
        transport._post_with_deadline(payload(), 2)
    assert len(requests) == 1


def test_timeout_reserves_fallback_time_without_extending_deadline(gateway):
    handlers, requests, _ = gateway
    async def slow(request):
        await asyncio.sleep(1)
        return success(request)
    handlers['primary.invalid'] = slow
    handlers['fallback.invalid'] = success
    start = time.monotonic()
    result = transport._post_with_deadline(payload(), .3)
    assert result.extensions['model_provider'] == 'gemini'
    assert time.monotonic() - start < .5
    assert len(requests) == 2


def test_both_providers_timeout_are_bounded_and_visible(gateway):
    handlers, requests, records = gateway
    async def slow(request):
        await asyncio.sleep(1)
        return success(request)
    handlers.update({'primary.invalid': slow, 'fallback.invalid': slow})
    start = time.monotonic()
    with pytest.raises(TimeoutError):
        transport._post_with_deadline(payload(), .3)
    assert time.monotonic() - start < .5
    assert len(requests) == 2 and all(r['status'] == 'failed' for r in records)


def test_partial_primary_stream_discarded_before_fallback(gateway):
    handlers, _, records = gateway
    handlers['primary.invalid'] = lambda r: httpx.Response(200, headers={'content-type': 'text/event-stream'},
        content='data: {"choices":[{"delta":{"content":"partial must not send"}}]}\n\n')
    handlers['fallback.invalid'] = success
    result = transport._post_with_deadline(payload(), 2)
    assert result.json()['choices'][0]['message']['content'] == '{"ok":true}'
    assert records[0]['error'] == 'ReadError'


def test_invalid_json_protocol_falls_back_without_executing_or_sending_it(gateway):
    handlers, _, records = gateway
    handlers['primary.invalid'] = lambda r: httpx.Response(200, json={
        'choices': [{'message': {'content': '{"ok":some_function()}'}, 'finish_reason': 'stop'}]})
    handlers['fallback.invalid'] = success
    result = transport._post_with_deadline({**payload(), 'response_format': {'type': 'json_object'}}, 2)
    assert result.json()['choices'][0]['message']['content'] == '{"ok":true}'
    assert records[0]['status'] == 'failed' and records[-1]['provider'] == 'gemini'


def test_fenced_json_is_valid_and_does_not_trigger_backup(gateway):
    handlers, requests, _ = gateway
    handlers['primary.invalid'] = lambda r: httpx.Response(200, json={
        'choices': [{'message': {'content': '```json\n{"ok":true}\n```'}, 'finish_reason': 'stop'}]})
    transport._post_with_deadline({**payload(), 'response_format': {'type': 'json_object'}}, 2)
    assert len(requests) == 1


@pytest.mark.parametrize('fallback_enabled', [False, True])
def test_last_provider_invalid_draft_reaches_existing_protocol_repair(gateway, monkeypatch, fallback_enabled):
    handlers, requests, records = gateway
    monkeypatch.setattr(settings, 'gemini_fallback_enabled', fallback_enabled)
    invalid = lambda r: httpx.Response(200, json={
        'choices': [{'message': {'content': 'needs JSON repair'}, 'finish_reason': 'stop'}]})
    handlers['primary.invalid'] = (lambda r: httpx.Response(503)) if fallback_enabled else invalid
    handlers['fallback.invalid'] = invalid
    result = transport._post_with_deadline({**payload(), 'response_format': {'type': 'json_object'}}, 2)
    assert result.json()['choices'][0]['message']['content'] == 'needs JSON repair'
    assert records[-1]['provider'] == ('gemini' if fallback_enabled else 'deepseek')
    assert len(requests) == (2 if fallback_enabled else 1)


def test_tool_ids_and_results_survive_switch_without_mutating_primary_payload(gateway):
    handlers, requests, _ = gateway
    handlers['primary.invalid'] = lambda r: httpx.Response(503)
    handlers['fallback.invalid'] = success
    original = payload()
    original.update(thinking={'type': 'enabled'}, reasoning_effort='low',
        tools=[{'type': 'function', 'function': {'name': 'get_price', 'parameters': {'type': 'object'}}}],
        tool_choice='auto', response_format={'type': 'json_object'})
    original['messages'] += [{'role': 'assistant', 'reasoning_content': 'hidden', 'content': None,
        'tool_calls': [{'id': 'call-1', 'type': 'function', 'function': {'name': 'get_price', 'arguments': '{}'}}]},
        {'role': 'tool', 'tool_call_id': 'call-1', 'content': '{"price":10780}'}]
    saved = json.dumps(original)
    transport._post_with_deadline(original, 2)
    forwarded = json.loads(requests[1].content)
    assert json.dumps(original) == saved
    assert 'thinking' not in forwarded and forwarded['reasoning_effort'] == 'low'
    assert 'reasoning_content' not in forwarded['messages'][1]
    assert forwarded['messages'][2] == original['messages'][2]
    assert forwarded['tools'] == original['tools']
    assert forwarded['temperature'] == settings.gemini_temperature


def test_cooldown_and_turn_stickiness_then_primary_recovery(gateway, monkeypatch):
    handlers, requests, records = gateway
    handlers['primary.invalid'] = lambda r: httpx.Response(503)
    handlers['fallback.invalid'] = success
    transport._post_with_deadline(payload(), 2)
    records.clear()  # New customer turn during outage cooldown.
    assert transport._post_with_deadline(payload(), 2).extensions['fallback_reason'] == 'primary_cooldown'
    transport._primary_failures.clear()  # Cooldown expired during tool continuation.
    assert transport._post_with_deadline(payload(), 2).extensions['fallback_reason'] == 'turn_continuation'
    records.clear()  # Next turn probes primary again.
    handlers['primary.invalid'] = success
    assert transport._post_with_deadline(payload(), 2).extensions['model_provider'] == 'deepseek'
    assert [r.url.host for r in requests] == ['primary.invalid', 'fallback.invalid', 'fallback.invalid', 'fallback.invalid', 'primary.invalid']


def test_fallback_tool_stream_and_metering(gateway):
    handlers, _, records = gateway
    handlers['primary.invalid'] = lambda r: httpx.Response(503)
    chunks = [{'model': 'gemini-2.5-flash', 'choices': [{'delta': {'tool_calls': [{'index': 0, 'id': 'gemini-tool',
        'function': {'name': 'load_skill', 'arguments': '{"name":"peach-9d-2027"}'}}]}, 'finish_reason': 'tool_calls'}]},
        {'choices': [], 'usage': {'prompt_tokens': 100, 'completion_tokens': 20}}]
    handlers['fallback.invalid'] = lambda r: httpx.Response(200, headers={'content-type': 'text/event-stream'},
        content=''.join('data: '+json.dumps(c)+'\n\n' for c in chunks)+'data: [DONE]\n\n')
    result = transport._post_with_deadline(payload(), 2)
    assert result.json()['choices'][0]['message']['tool_calls'][0]['id'] == 'gemini-tool'
    assert records[-1]['input_tokens'] == 100 and records[-1]['output_tokens'] == 20


def test_gemini_budget_is_supported_and_labelled(gateway, tmp_path, monkeypatch):
    path = tmp_path/'ledger.json'
    path.write_text(json.dumps({'limit_cny': 5, 'calls': []}))
    monkeypatch.setattr(meter, '_ledger_path', lambda: path)
    handle = meter.begin_request({'model': 'gemini-2.5-flash', 'max_tokens': 100}, provider='gemini')
    meter.end_request(handle, {'usage': {'prompt_tokens': 100, 'completion_tokens': 20}})
    saved = json.loads(path.read_text())['calls'][0]
    assert saved['usage_known'] and saved['cost_basis'] == 'gateway_estimate'
    assert saved['charged_or_reserved_cny'] == pytest.approx(.0011)


def test_slow_healthy_primary_is_not_cut_off_at_half_the_budget(gateway):
    handlers, requests, records = gateway
    async def healthy_but_slow(request):
        await asyncio.sleep(.24)
        return success(request)
    handlers['primary.invalid'] = healthy_but_slow
    handlers['fallback.invalid'] = lambda request: httpx.Response(503)
    result = transport._post_with_deadline(payload(), .4)
    assert result.extensions['model_provider'] == 'deepseek'
    assert len(requests) == len(records) == 1


@pytest.mark.parametrize('status', [403, 429, 503])
def test_failed_backup_does_not_poison_next_primary_attempt(gateway, status):
    handlers, requests, records = gateway
    handlers['primary.invalid'] = lambda request: httpx.Response(503)
    handlers['fallback.invalid'] = lambda request: httpx.Response(status)
    with pytest.raises(httpx.HTTPStatusError):
        transport._post_with_deadline(payload(), 2)
    records.clear()
    handlers['primary.invalid'] = success
    assert transport._post_with_deadline(payload(), 2).extensions['model_provider'] == 'deepseek'
    assert [r.url.host for r in requests] == ['primary.invalid', 'fallback.invalid', 'primary.invalid']


def test_failed_sticky_backup_returns_to_primary_in_same_turn(gateway):
    handlers, requests, records = gateway
    handlers['primary.invalid'] = lambda request: httpx.Response(503)
    handlers['fallback.invalid'] = success
    transport._post_with_deadline(payload(), 2)
    handlers['primary.invalid'] = success
    handlers['fallback.invalid'] = lambda request: httpx.Response(403)
    result = transport._post_with_deadline(payload(), 2)
    assert result.extensions['model_provider'] == 'deepseek'
    assert result.extensions['fallback_reason'] == 'fallback_unavailable'
    assert transport._post_with_deadline(payload(), 2).extensions['model_provider'] == 'deepseek'
    assert [r.url.host for r in requests] == ['primary.invalid', 'fallback.invalid', 'fallback.invalid', 'primary.invalid', 'primary.invalid']
    assert records[-2]['status'] == records[-1]['status'] == 'completed'


def test_backup_cooldown_gives_primary_full_budget_and_can_recover(gateway):
    handlers, requests, records = gateway
    handlers['primary.invalid'] = lambda request: httpx.Response(503)
    handlers['fallback.invalid'] = lambda request: httpx.Response(403)
    with pytest.raises(httpx.HTTPStatusError):
        transport._post_with_deadline(payload(), .4)
    records.clear()
    async def slow_primary(request):
        await asyncio.sleep(.3)
        return success(request)
    handlers['primary.invalid'] = slow_primary
    assert transport._post_with_deadline(payload(), .4).extensions['model_provider'] == 'deepseek'
    assert len(requests) == 3
    transport._fallback_failures.clear()  # Backup cooldown expires.
    records.clear()
    handlers['primary.invalid'] = lambda request: httpx.Response(503)
    handlers['fallback.invalid'] = success
    assert transport._post_with_deadline(payload(), .4).extensions['model_provider'] == 'gemini'


def test_sticky_backup_and_primary_failure_still_share_one_deadline(gateway):
    handlers, requests, records = gateway
    handlers['primary.invalid'] = lambda request: httpx.Response(503)
    handlers['fallback.invalid'] = success
    transport._post_with_deadline(payload(), 2)
    handlers['fallback.invalid'] = lambda request: httpx.Response(403)
    async def slow_primary(request):
        await asyncio.sleep(1)
        return success(request)
    handlers['primary.invalid'] = slow_primary
    start = time.monotonic()
    with pytest.raises(TimeoutError):
        transport._post_with_deadline(payload(), .2)
    assert time.monotonic() - start < .4
    assert [r.url.host for r in requests][-2:] == ['fallback.invalid','primary.invalid']
