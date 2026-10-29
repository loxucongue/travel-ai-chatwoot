import json
import time
import httpx
import pytest
from app.config import settings
from app.reception_v3 import repetition
from app.reception_v3.runtime import Decision


def draft(**extra):
    return Decision(messages=[{'text':'三月底在出發區間內。您們可以自己一團。想看珠峰嗎？'}],**extra).model_dump()


def test_removes_only_selected_complete_clauses_and_preserves_state():
    d=draft(profile={'party_size':4})
    result=repetition.trim(d,repetition.clauses_for(d),[1])
    assert result['messages'][0]['text']=='三月底在出發區間內。想看珠峰嗎？'
    assert result['profile']==d['profile'] and result['route_variant']==d['route_variant']
    assert d['messages'][0]['text']=='三月底在出發區間內。您們可以自己一團。想看珠峰嗎？'


def test_removing_old_invitation_preserves_complete_acknowledgment():
    d=Decision(messages=[{'text':'原來是排假的關係，那這次先不勉強。有別的假期再說就好。'}]).model_dump()
    result=repetition.trim(d,repetition.clauses_for(d),[1])
    assert result['messages'][0]['text']=='原來是排假的關係，那這次先不勉強。'


def test_price_clause_cannot_leave_a_hanging_topic_or_drop_its_condition():
    d=Decision(messages=[{'text':'六月可以看這條線。費用的話，先確認人數，再按人數核價。'}]).model_dump()
    clauses=repetition.clauses_for(d)
    assert len(clauses)==2
    assert repetition.trim(d,clauses,[1])['messages'][0]['text']=='六月可以看這條線。'
    d=Decision(messages=[{'text':'如果未達人數，就不能保證發團。'}]).model_dump()
    assert len(repetition.clauses_for(d))==1


@pytest.mark.parametrize('ids', [[-1],[99],[True],['1'],[0,0],None])
def test_rejects_invalid_removals(ids):
    with pytest.raises((ValueError,TypeError)):
        repetition.trim(draft(),repetition.clauses_for(draft()),ids)


def test_cannot_rewrite_templates_images_or_erase_route_offer():
    d=Decision(messages=[{'script_id':'price_shared_5'},
        {'text':'新照片','material_rule_id':'photo'}, {'text':'行程圖','asset_keys':['map']}]).model_dump()
    assert repetition.clauses_for(d)==[]
    offer=draft(route_choice='recommend')
    with pytest.raises(ValueError,match='erase_route_offer'):
        repetition.trim(offer,repetition.clauses_for(offer),[0,1,2])
    assert repetition.trim(draft(),repetition.clauses_for(draft()),[0,1,2])['action']=='wait'


@pytest.mark.parametrize('ids,expected', [([1],'三月底在出發區間內。想看珠峰嗎？'),
    ([0,1,2], '三月底在出發區間內。您們可以自己一團。想看珠峰嗎？'),
    ([], '三月底在出發區間內。您們可以自己一團。想看珠峰嗎？')])
def test_review_real_payload_and_trimming(monkeypatch,ids,expected):
    monkeypatch.setattr(settings,'reception_repeat_review_enabled',True)
    requests=[]
    def post(payload,timeout):
        requests.append(payload)
        return httpx.Response(200,request=httpx.Request('POST','https://test.invalid'),json={
            'choices':[{'message':{'content':json.dumps({'remove_clause_ids':ids})}}],
            'usage':{'prompt_tokens':100,'completion_tokens':10}})
    monkeypatch.setattr(repetition,'_post_with_deadline',post)
    context={'event':'customer_message','messages':[
        {'direction':'outgoing','content':'可以自己一團。','status':'sent'},
        {'direction':'outgoing','content':'UNSENT','status':'draft'},
        {'direction':'incoming','content':'三月底出發'}]}
    result,logs,digest=repetition.review(draft(),context,time.monotonic()+20)
    assert result['messages'][0]['text']==expected
    assert logs[0]['status']=='completed' and digest
    assert 'UNSENT' not in requests[0]['messages'][-1]['content']


def test_review_failure_retains_answer_and_is_auditable(monkeypatch):
    monkeypatch.setattr(settings,'reception_repeat_review_enabled',True)
    def fail(*args):raise TimeoutError('test_timeout')
    monkeypatch.setattr(repetition,'_post_with_deadline',fail)
    d=draft()
    result,logs,_=repetition.review(d,{'messages':[
        {'direction':'outgoing','content':'之前說過','status':'sent'}]},time.monotonic()+20)
    assert result==d and logs[0]['status']=='failed'
