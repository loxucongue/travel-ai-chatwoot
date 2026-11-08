import json
import httpx
import pytest
from app.reception_v3 import runtime
from app.model_transport import EvaluationCallError
from test_reception_v3_skills import context, final, tool


def test_protocol_retries_preserve_tools_and_return_original_text(monkeypatch):
    requests = []
    def post(payload, timeout):
        requests.append(payload)
        n = len(requests)
        if n in (2, 3):
            raise httpx.ReadError('invalid_model_response_format')
        message = ({'tool_calls': [tool('load_skill', {'name': 'peach-9d-2027'})]}
                   if n == 1 else final(messages=[{'text': '顧問回覆原稿，不由程式改寫。'}]))
        return httpx.Response(200, request=httpx.Request('POST', 'https://test.invalid'),
                              json={'choices': [{'message': message}]})
    monkeypatch.setattr(runtime, '_post_with_deadline', post)
    decision, logs, _ = runtime.run_agent(context('peach_9d_2027'))
    assert len(requests) == 4
    assert len([x for x in logs if x['status'] == 'protocol_retry']) == 2
    assert any(m['role'] == 'tool' for m in requests[-1]['messages'])
    assert requests[-1]['max_tokens'] == 8000
    assert decision['messages'][0]['text'] == '顧問回覆原稿，不由程式改寫。'


def test_protocol_retries_are_bounded(monkeypatch):
    calls = []
    def post(payload, timeout):
        calls.append(payload)
        raise httpx.ReadError('invalid_model_response_format')
    monkeypatch.setattr(runtime, '_post_with_deadline', post)
    with pytest.raises(EvaluationCallError, match='invalid_model_response_format'):
        runtime.run_agent(context())
    assert len(calls) == 3


def test_non_protocol_transport_error_not_misclassified(monkeypatch):
    calls = []
    def post(payload, timeout):
        calls.append(payload)
        raise httpx.ReadError('connection lost')
    monkeypatch.setattr(runtime, '_post_with_deadline', post)
    with pytest.raises(EvaluationCallError, match='connection lost'):
        runtime.run_agent(context())
    assert len(calls) == 1


@pytest.mark.parametrize('topics,visible', [(['transport'],True),(['price'],False),([],False)])
def test_single_topic_reference_is_selected_by_model_understanding(monkeypatch,topics,visible):
    from test_reception_v3_skills import model
    monkeypatch.setattr(runtime.settings,'reception_route_understanding_enabled',False)
    c=context('peach_9d_2027')
    c['event']='customer_message'
    c['messages']=[{'id':'customer-1','direction':'incoming','content':'本輪問題','content_type':'text'}]
    c['route_understanding']={'intent':'inquire','answer_topics':topics,'discussed_routes':['peach_9d_2027']}
    c['skills']['routes']['peach_9d_2027']['scripts'].append({'id':'test_transport','answer_text':'唯一短交通事實',
        'answer_scope':'single_topic','topics':['transport'],'asset_ids':[]})
    requests=model(monkeypatch,[final(messages=[{'text':'模型原答'}])])
    d,_,_=runtime.run_agent(c)
    assert ('唯一短交通事實' in requests[0]['messages'][-1]['content']) is visible
    assert d['messages'][0]['text']=='模型原答'
