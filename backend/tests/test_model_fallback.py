from copy import deepcopy
import json

import httpx
import pytest

from app import model_metering
from app import model_transport as transport
from app.config import settings


@pytest.fixture(autouse=True)
def fallback(monkeypatch):
    monkeypatch.setattr(settings, 'gemini_fallback_enabled', True)
    monkeypatch.setattr(settings, 'google_gemini_base_url', 'https://backup.example')
    monkeypatch.setattr(settings, 'gemini_api_key', 'backup-secret')
    monkeypatch.setattr(settings, 'gemini_model', 'gemini-2.5-flash')
    monkeypatch.setattr(settings, 'gemini_fallback_primary_timeout_seconds', 15)
    monkeypatch.setattr(settings, 'gemini_fallback_cooldown_seconds', 60)
    monkeypatch.delenv('MODEL_TEST_COST_LEDGER', raising=False)
    transport._primary_unavailable_until.clear()
    yield
    transport._primary_unavailable_until.clear()


def response():
    return httpx.Response(200, request=httpx.Request('POST', 'https://test.example'),
        json={'choices':[{'message':{'content':'{"ok":true}'}, 'finish_reason':'stop'}],
              'usage':{'prompt_tokens':100, 'completion_tokens':10}})


def http_error(status):
    return httpx.HTTPStatusError('upstream error', request=httpx.Request('POST', 'https://primary.example'),
        response=httpx.Response(status))


@pytest.mark.parametrize('error', [http_error(s) for s in (429, 500, 502, 503, 504)] +
    [TimeoutError(), httpx.ConnectError('unavailable'), httpx.ReadError('incomplete_model_stream')])
def test_transient_failure_switches_with_same_tool_history_and_separate_metering(monkeypatch, error):
    payload={'model':'deepseek-v4-flash', 'thinking':{'type':'enabled'}, 'reasoning_effort':'low',
        'stream':True, 'max_tokens':4000, 'tools':[{'type':'function','function':{'name':'load_skill'}}],
        'messages':[{'role':'assistant','content':None,'reasoning_content':'private reasoning',
            'tool_calls':[{'id':'call1','type':'function','function':{'name':'load_skill','arguments':'{}'}}]},
            {'role':'tool','tool_call_id':'call1','content':'route facts'}]}
    original=deepcopy(payload);sent=[]
    def send(body, timeout, provider=None):
        sent.append((body,timeout,provider))
        if provider is None:raise error
        return response()
    monkeypatch.setattr(transport,'_post_unmetered',send)
    records=[];token=model_metering.calls.set(records)
    try:result=transport._post_with_deadline(payload,45)
    finally:model_metering.calls.reset(token)
    assert payload==original
    assert sent[0][1]==15 and 0<sent[1][1]<=45
    assert sent[1][2]==('https://backup.example/v1','backup-secret')
    backup=sent[1][0]
    assert backup['model']=='gemini-2.5-flash' and 'thinking' not in backup
    assert 'reasoning_content' not in backup['messages'][0]
    assert backup['messages'][0]['tool_calls']==original['messages'][0]['tool_calls']
    assert backup['messages'][1]==original['messages'][1] and backup['tools']==original['tools']
    assert [(r['provider'],r['status']) for r in records]==[('deepseek','failed'),('gemini_fallback','completed')]
    assert records[1]['model']=='gemini-2.5-flash' and records[1]['usage_known']
    assert result.extensions['model_provider']=='gemini_fallback'


@pytest.mark.parametrize('status',[400,401,402,403,404,413,422])
def test_configuration_and_input_errors_do_not_retry(monkeypatch,status):
    sent=[]
    def send(*args,**kwargs):sent.append(1);raise http_error(status)
    monkeypatch.setattr(transport,'_post_unmetered',send)
    with pytest.raises(httpx.HTTPStatusError):transport._post_with_deadline({'model':'deepseek-v4-flash'},20)
    assert len(sent)==1 and not transport._primary_unavailable_until


def test_cooldown_skips_failed_primary_then_recovers_and_preserves_overall_deadline(monkeypatch):
    clock=[100.0];sent=[]
    monkeypatch.setattr(transport.time,'monotonic',lambda:clock[0])
    def send(payload,timeout,provider=None):
        sent.append((provider,timeout))
        if len(sent)==1:
            clock[0]+=15
            raise TimeoutError()
        return response()
    monkeypatch.setattr(transport,'_post_unmetered',send)
    payload={'model':'deepseek-v4-flash','max_tokens':4000}
    transport._post_with_deadline(payload,45)
    assert sent[1][1]==30
    transport._post_with_deadline(payload,20)
    assert len(sent)==3 and sent[2][0] is not None and sent[2][1]==20
    clock[0]+=61
    transport._post_with_deadline(payload,20)
    assert sent[-1]==(None,10) and not transport._primary_unavailable_until


def test_fallback_failure_is_not_retried_recursively(monkeypatch):
    sent=[]
    def send(*args,**kwargs):sent.append(1);raise http_error(503)
    monkeypatch.setattr(transport,'_post_unmetered',send)
    with pytest.raises(httpx.HTTPStatusError):transport._post_with_deadline({'model':'deepseek-v4-flash'},20)
    assert len(sent)==2


def test_disabled_fallback_keeps_original_primary_timeout(monkeypatch):
    monkeypatch.setattr(settings,'gemini_fallback_enabled',False)
    sent=[]
    def send(payload,timeout):sent.append(timeout);return response()
    monkeypatch.setattr(transport,'_post_unmetered',send)
    transport._post_with_deadline({'model':'deepseek-v4-flash'},45)
    assert sent==[45]


def test_backup_auth_and_url_never_use_primary_key(monkeypatch):
    transport.close_deepseek_transport();requests=[]
    provider=('https://backup.example/v1','backup-secret')
    def handler(request):requests.append(request);return response()
    transport._transport.client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    transport._transport.identity=provider
    try:transport._post_unmetered({'model':'gemini-2.5-flash'},5,provider=provider)
    finally:transport.close_deepseek_transport()
    assert str(requests[0].url)=='https://backup.example/v1/chat/completions'
    assert requests[0].headers['authorization']=='Bearer backup-secret'


def test_gemini_calls_still_respect_test_budget(tmp_path,monkeypatch):
    path=tmp_path/'cost.json'
    path.write_text(json.dumps({'limit_cny':.01,'calls':[]}))
    monkeypatch.setattr(model_metering,'_ledger_path',lambda:path)
    with pytest.raises(RuntimeError,match='model_test_budget_exhausted'):
        model_metering.begin_request({'model':'gemini-2.5-flash','max_tokens':4000})
