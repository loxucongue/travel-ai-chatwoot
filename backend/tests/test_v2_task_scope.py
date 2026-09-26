import pytest
from app.reception_v2.task_scope import verify_task_scope


def test_task_proof_does_not_receive_draft_or_previous_handoff_opinion(monkeypatch):
    def model(**kw):
        assert 'proposed_body' not in kw['input_data']
        assert 'scope_check' not in kw['input_data']
        assert kw['input_data']['customer_message']=='小費怎麼付？'
        assert kw.get('reasoning_effort') == 'low'
        assert kw['max_tokens']==3000
        return kw['parser']({'checks':[{'index':0,'needed':False,'answer_complete_without_task':True,'reason':'No allocation question','required_task':'','has_unasked_details':False}]}),[],''
    monkeypatch.setattr('app.reception_v2.task_scope.call_json_node',model)
    checks,_,_=verify_task_scope({'customer_message':'小費怎麼付？','proposed_body':'I will check allocation',
        'scope_check':{'consultant_tasks':['check']}},['Check driver/guide allocation'])
    assert checks[0]['needed'] is False


@pytest.mark.parametrize('checks',[
    [],[{'index':1,'needed':True,'reason':'extra task'}],
    [{'index':0,'needed':'yes','reason':'not boolean'}],
    [{'index':0,'needed':True,'reason':''}],
    [{'index':False,'needed':True,'reason':'bool index'}],
])
def test_task_proof_cannot_add_tasks_or_omit_decisions(monkeypatch,checks):
    monkeypatch.setattr('app.reception_v2.task_scope.call_json_node',lambda **kw:kw['parser']({'checks':checks}))
    with pytest.raises(ValueError,match='v2_task_scope_checks_invalid'):
        verify_task_scope({},['one actual task'])


def test_history_refusal_does_not_become_a_new_event_error(monkeypatch):
    from app.reception_v2.reply_scope import verify_scope
    monkeypatch.setattr('app.reception_v2.reply_scope.call_json_node',lambda **kw:(
        {'requested_material_kinds':[],'event_errors':[],
         'contact_refusals':[{'quote':'不留LINE','scope':'LINE'}]},[],''))
    audit,_,_=verify_scope({'customer_message':'在哪集合？','event':'customer_message','v2_events':[],
        'recent_conversation':[{'role':'customer','content':'我不留LINE'}]})
    assert audit['event_errors']==[]


def test_unknown_refusal_quote_still_fails_closed(monkeypatch):
    from app.reception_v2.reply_scope import verify_scope
    monkeypatch.setattr('app.reception_v2.reply_scope.call_json_node',lambda **kw:(
        {'requested_material_kinds':[],'event_errors':[],
         'contact_refusals':[{'quote':'不留LINE','scope':'LINE'}]},[],''))
    with pytest.raises(ValueError,match='v2_scope_refusal_evidence_invalid'):
        verify_scope({'customer_message':'在哪集合？','event':'customer_message','v2_events':[]})


def test_rejected_task_is_blocked_until_reply_is_revised(monkeypatch):
    from app.reception_v2.reply_scope import verify_scope
    monkeypatch.setattr('app.reception_v2.reply_scope.call_json_node',lambda **kw:(
        {'requested_material_kinds':[],'event_errors':[],'contact_refusals':[],
         'consultant_tasks':['unasked allocation']},[],''))
    monkeypatch.setattr('app.reception_v2.task_scope.verify_task_scope',lambda *a:(
        [{'index':0,'needed':False,'reason':'Not requested'}],[],''))
    audit,_,_=verify_scope({'customer_message':'小費怎麼付？','event':'customer_message','v2_events':[]})
    assert audit['consultant_tasks']==[]
    assert audit['task_scope_errors'] and 'unasked allocation' in audit['task_scope_errors'][0]


@pytest.mark.parametrize('quote,expected',[
    ('當天餘位要請顧問核對。',True),
    ('模型猜出的另一句',False),
    ('',False),
])
def test_rejected_task_exposes_only_exact_draft_span_for_revision(monkeypatch,quote,expected):
    from app.reception_v2.reply_scope import verify_scope
    monkeypatch.setattr('app.reception_v2.reply_scope.call_json_node',lambda **kw:(
        {'requested_material_kinds':[],'event_errors':[],'contact_refusals':[],
         'consultant_tasks':['查餘位'],'consultant_task_checks':[
             {'needed':True,'task':'查餘位','draft_quote':quote}]},[],''))
    monkeypatch.setattr('app.reception_v2.task_scope.verify_task_scope',lambda *a:(
        [{'index':0,'needed':False,'reason':'Only supplied date'}],[],''))
    audit,_,_=verify_scope({'customer_message':'3月28日出發','event':'customer_message',
        'v2_events':[],'proposed_body':'已記下3月28日。當天餘位要請顧問核對。'})
    assert (quote in audit.get('unwanted_parts',[]))==expected


@pytest.mark.parametrize('reason',['lead_captured','explicit_human_request','requested_material_unavailable','customer_contact_outside_window'])
def test_required_service_handoff_is_not_reinterpreted_as_optional_knowledge_task(monkeypatch,reason):
    from app.reception_v2.reply_scope import verify_scope
    monkeypatch.setattr('app.reception_v2.reply_scope.call_json_node',lambda **kw:(
        {'requested_material_kinds':[],'event_errors':[],'contact_refusals':[],
         'consultant_tasks':['Existing required service action']},[],''))
    def unexpected(*a):
        raise AssertionError('Required execution action must not be judged as an optional fact check')
    monkeypatch.setattr('app.reception_v2.task_scope.verify_task_scope',unexpected)
    audit,_,_=verify_scope({'customer_message':'請顧問聯絡','event':'customer_message','v2_events':[],
        'planned_system_action':{'handoff_reason':reason}})
    assert audit['consultant_tasks']==['Existing required service action']
