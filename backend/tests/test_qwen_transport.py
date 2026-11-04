from copy import deepcopy
import httpx
import pytest
from app.config import settings
from app import model_transport as transport, model_metering
from test_model_fallback import response, http_error

@pytest.fixture(autouse=True)
def qwen(monkeypatch):
    monkeypatch.setattr(settings,'qwen_fallback_enabled',True)
    monkeypatch.setattr(settings,'qwen_api_key','qwen-test-key')
    monkeypatch.setattr(settings,'qwen_base_url','https://qwen.example/v1')
    monkeypatch.setattr(settings,'gemini_fallback_enabled',True)
    monkeypatch.delenv('MODEL_TEST_COST_LEDGER',raising=False)
    transport._primary_unavailable_until.clear()
    yield
    transport._primary_unavailable_until.clear()

@pytest.mark.parametrize('vision',[False,True])
def test_qwen_routes_images_directly_and_text_only_after_primary_failure(monkeypatch,vision):
    payload={'model':'deepseek-v4-flash','thinking':{'type':'disabled'},'reasoning_effort':'none',
      'max_tokens':1000,'tools':[{'type':'function','function':{'name':'load_skill'}}],
      'messages':[{'role':'assistant','content':None,'reasoning_content':'reasoning',
        'tool_calls':[{'id':'c1','type':'function','function':{'name':'load_skill','arguments':'{}'}}]},
        {'role':'tool','tool_call_id':'c1','content':'facts'},
        {'role':'user','content':[{'type':'text','text':'question'}]}]}
    if vision:payload['messages'][-1]['content'].append({'type':'image_url','image_url':{'url':'data:image/png;base64,test'}})
    original=deepcopy(payload);sent=[];records=[]
    def send(body,timeout,provider=None):
        sent.append((body,timeout,provider))
        if provider is None:raise http_error(503)
        return response()
    monkeypatch.setattr(transport,'_post_unmetered',send)
    token=model_metering.calls.set(records)
    try:result=transport._post_with_deadline(payload,45)
    finally:model_metering.calls.reset(token)
    assert payload==original and len(sent)==(1 if vision else 2)
    actual,timeout,provider=sent[-1]
    assert provider==('https://qwen.example/v1','qwen-test-key') and 0<timeout<=45
    assert actual['model']==('qwen3.5-flash' if vision else 'qwen-plus')
    assert actual['enable_thinking'] is False
    assert 'thinking' not in actual and 'reasoning_effort' not in actual
    assert 'reasoning_content' not in actual['messages'][0]
    assert actual['messages'][1:]==original['messages'][1:] and actual['tools']==original['tools']
    assert records[-1]['provider']==result.extensions['model_provider']==('qwen_vision' if vision else 'qwen_fallback')
    assert records[-1]['usage_known']

@pytest.mark.parametrize('status,count',[(401,1),(503,2)])
def test_qwen_does_not_retry_auth_errors_or_recurse_on_fallback_failure(monkeypatch,status,count):
    sent=[]
    def send(*args,**kwargs):sent.append(kwargs);raise http_error(status)
    monkeypatch.setattr(transport,'_post_unmetered',send)
    with pytest.raises(httpx.HTTPStatusError):transport._post_with_deadline({'model':'deepseek-v4-flash'},45)
    assert len(sent)==count

def test_qwen_remains_budget_guarded(tmp_path,monkeypatch):
    ledger=tmp_path/'cost.json';ledger.write_text('{"limit_cny":0.01,"calls":[]}')
    monkeypatch.setattr(model_metering,'_ledger_path',lambda:ledger)
    with pytest.raises(RuntimeError,match='model_test_budget_exhausted'):
        model_metering.begin_request({'model':'qwen3.5-flash','max_tokens':4000})
