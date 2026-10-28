from copy import deepcopy
import json

import pytest

from app.automation_models import AutomationRun, AutomationSession
from app.reception_v3 import service, runtime
from app.reception_v3.lead_priority import task, outcome, validation_error
from app.reception_v3.runtime import Decision
from test_reception_v3 import create, step, bundle, delivered, answer


def history():
    return [
        {'id':'q1','direction':'incoming','content':'1位，明年八月份請問大概多少錢？','created_at':'2026-10-25T00:35:00+08:00'},
        {'id':'a1','direction':'outgoing','content':'西藏精華8天，團費人民幣¥9,980/人。','status':'sent','created_at':'2026-10-25T00:36:00+08:00'},
    ]


def configured():
    spec = bundle()
    spec['lead_capture'] = {'enabled':True,'next_day_invitation_enabled':True,
                            'consultation_turn_threshold':3,'channels':['LINE']}
    spec['silence'].update(daily_at='09:00',active_start='07:00',active_end='01:00',max_proactive_messages_per_day=1)
    return spec


def context():
    return {'event':'silence_due','now':'2026-10-26T01:00:00+00:00','skills':configured(),
            'route_variant':'nine','state':{},'profile':{'party_size':1},'messages':history()}


def invitation(**kwargs):
    return Decision(messages=[{'text':'方便留一下您的LINE嗎？行程資料也可以傳給您。',
                               'purpose':'contact_invitation'}],lead_review={},**kwargs).model_dump()


def test_actual_quote_qualifies_next_local_day_and_draft_does_not():
    c=context()
    assert outcome(invitation(),c)=='invite'
    for status in ('draft','cancelled','failed'):
        c['messages'][-1]['status']=status
        assert outcome(invitation(),c)=='ordinary'


@pytest.mark.parametrize('text',['無購物，違規賠償人民幣5000元。','機票人民幣6000元。','單房差人民幣1500元。'])
def test_compensation_and_extra_fees_are_not_quote_qualification(text):
    c=context();c['messages'][-1]['content']=text
    assert task(c)['quoted_message_ids']==[]


def test_day_boundary_uses_actual_reply_time_not_24_hours_or_server_date():
    c=context()
    c['messages'][0]['created_at']='2026-10-25T23:59:00+08:00'
    c['messages'][1]['created_at']='2026-10-26T00:01:00+08:00'
    assert outcome(invitation(),c)=='wait'
    assert validation_error(invitation(),c)
    c['now']='2026-10-27T01:00:00+00:00'
    assert outcome(invitation(),c)=='invite'


def test_three_question_evidence_threshold_no_duplicate_or_invented_ids():
    c=context();c['messages']=[]
    for index,text in enumerate(('酒店有供氧嗎？','每段車程多久？','包含機票嗎？'),1):
        c['messages'].append({'id':f'q{index}','direction':'incoming','content':text,'created_at':'2026-10-25T11:00:00+08:00'})
    d=invitation();d['lead_review']['question_message_ids']=['q1','q2']
    assert outcome(d,c)=='ordinary'
    d['lead_review']['question_message_ids'].append('q3')
    assert outcome(d,c)=='invite'
    c['skills']['lead_capture']['consultation_turn_threshold']=4
    assert outcome(d,c)=='ordinary'
    for invalid in (['q1','q1','q1'],['q1','q2','invented']):
        d['lead_review']['question_message_ids']=invalid
        assert validation_error(d,c)


@pytest.mark.parametrize('status,direction,text',[
    ('invited','outgoing','方便傳一下您的LINE QR Code嗎？'),
    ('declined','incoming','先在這裡問，不加LINE'),
    ('provided','incoming','我的LINE是visitor123'),
    ('deferred','incoming','下星期再聯絡我')])
def test_legacy_history_blocks_reinvitation_without_new_delivery_marker(status,direction,text):
    c=context();c['messages'].append({'id':'old','direction':direction,'content':text,'status':'sent',
                                     'created_at':'2026-10-25T11:00:00+08:00'})
    d=invitation();d['lead_review'].update(contact_status=status,contact_message_id='old')
    assert outcome(d,c)=='ordinary'
    assert validation_error(d,c)
    d.update(action='wait',messages=[])
    assert validation_error(d,c) is None
    d['lead_review']['contact_message_id']='invented'
    assert validation_error(d,c)


@pytest.mark.parametrize('section,field,value',[
    ('state','human_followup',True),('state','handoff',True),('state','opt_out',True),
    ('profile','contact_capture_declined',True),('profile','contact_value','known@example.com'),
    ('profile','contact_status','self_reported_added'),('profile','contact_status','attachment_pending_verification')])
def test_existing_ownership_and_contact_state_exclude_priority(section,field,value):
    c=context();c[section][field]=value
    assert task(c) is None


def test_switch_and_scope_do_not_enable_other_routes_or_immediate_invites():
    c=context();c['skills']['lead_capture']['enabled']=False
    assert task(c) is None
    assert validation_error(invitation(),c)
    c=context();c['skills']['lead_capture']['next_day_invitation_enabled']=False
    assert task(c) is None
    c=context();c['event']='customer_message'
    assert task(c) is None
    c=context();c['route_variant']=''
    assert task(c) is None
    d=invitation();d['profile']={'contact_capture_declined':True}
    assert validation_error(d,context())


@pytest.mark.parametrize('change',[
    {'action':'wait','messages':[]},
    {'messages':[{'text':'看看星空','purpose':'reply'}]},
    {'messages':[{'text':'邀請','purpose':'contact_invitation','asset_keys':['star']}]},
    {'messages':[{'text':'邀請','purpose':'contact_invitation','material_rule_id':'star'}]},
    {'start_introduction':True}])
def test_priority_prevents_wash_or_reintroduction_in_invitation_turn(change):
    d=invitation();d.update(change)
    assert validation_error(d,context())


def setup(db,monkeypatch):
    row=create(db,monkeypatch)
    row.messages=history();row.virtual_now='2026-10-25T01:00:00+00:00'
    row.controls={**row.controls,'route_variant':'nine'}
    service.save(row,{'opening_started':True,'intake_status':'done','completed_introductions':['nine'],
                     'pending_event':'silence_due'})
    monkeypatch.setattr(service,'compile_skills',lambda db:deepcopy(configured()))
    return row


def test_multi_day_quote_wait_invite_restart_then_material_not_reinvite(session_factory,monkeypatch):
    events=[]
    def model(c):
        events.append(deepcopy(c))
        if task(c) and not task(c)['next_day']:
            return answer(action='wait',lead_review={})
        if task(c):
            return invitation(),[],'hash'
        return answer(messages=[{'text':'分享您關心的西藏風景。'}])
    monkeypatch.setattr(service,'run_agent',model)
    with session_factory() as db:
        row=setup(db,monkeypatch);db.commit()
        step(db,row)
        assert delivered(row)==[]
        assert service.state(row)['next_check_at'].startswith('2026-10-26')
        service.advance_next(row);service.tick(db,session_id=row.id,advance_clock=False)
        assert not task({**context(),'messages':row.messages}) is None  # queued is not sent
        step(db,row)
        assert len(delivered(row))==1 and 'LINE' in delivered(row)[0]
        assert not any(m.get('asset_key') for m in row.messages)
        assert service.state(row)['next_check_at'].startswith('2026-10-27')
        sid=row.id;db.commit()
    with session_factory() as db:
        row=db.get(AutomationSession,sid)
        service.advance_next(row);service.tick(db,session_id=row.id,advance_clock=False);step(db,row)
        assert delivered(row)==['方便留一下您的LINE嗎？行程資料也可以傳給您。','分享您關心的西藏風景。']
        assert task(events[-1]) is None


def test_failed_invitation_not_consumed_and_switch_cancels_queued_contact(session_factory,monkeypatch):
    with session_factory() as db:
        row=setup(db,monkeypatch);row.virtual_now=context()['now'];db.commit()
        monkeypatch.setattr(service,'run_agent',lambda c:(invitation(),[],'hash'))
        step(db,row)
        queued=next(m for m in row.messages if m.get('status')=='draft')
        assert queued['contact_invitation'] and queued['next_day_contact_invitation']
        c={**context(),'messages':[{**queued,'status':'failed'},*history()]}
        assert task(c) is not None
        disabled=configured();disabled['lead_capture']['next_day_invitation_enabled']=False
        monkeypatch.setattr(service,'compile_skills',lambda db:disabled)
        step(db,row)
        assert not delivered(row)
        assert next(m for m in row.messages if m['id']==queued['id'])['status']=='cancelled'


def test_finite_same_day_slots_move_to_next_day_without_exhaustion(session_factory,monkeypatch):
    with session_factory() as db:
        row=setup(db,monkeypatch)
        config=configured();config['silence'].update(daily_at=None,intervals_minutes=[60,120,180],active_start='09:00',active_end='21:00')
        monkeypatch.setattr(service,'compile_skills',lambda db:deepcopy(config))
        monkeypatch.setattr(service,'run_agent',lambda c:answer(action='wait',lead_review={}))
        db.commit();step(db,row)
        assert service.state(row).get('silence_step',0)==0
        assert service.state(row)['next_check_at']=='2026-10-26T09:00:00+08:00'
        monkeypatch.setattr(service,'run_agent',lambda c:(invitation(),[],'hash'))
        service.advance_next(row);service.tick(db,session_id=row.id,advance_clock=False);step(db,row)
        assert len(delivered(row))==1
        assert service.state(row)['silence_step']==1
        assert service.state(row)['next_check_at']=='2026-10-26T11:00:00+08:00'


def test_model_protocol_repairs_priority_without_sending_first_wash(monkeypatch):
    from test_reception_v3_skills import context as runtime_context, model, final
    c=runtime_context('peach_9d_2027');c.update(event='silence_due',now=context()['now'],messages=history(),profile={},state={})
    c['skills']['lead_capture']=configured()['lead_capture']
    requests=model(monkeypatch,[final(messages=[{'text':'看看风景'}]),
        {'content':json.dumps(invitation(),ensure_ascii=False)}])
    monkeypatch.setattr('app.reception_v3.repetition.review',lambda d,*a:(d,[],None))
    result,logs,_=runtime.run_agent(c)
    assert result['messages'][0]['purpose']=='contact_invitation'
    assert len(requests)==2 and logs[0]['status']=='lead_priority_rejected'
    assert 'lead_priority_task' in json.dumps(requests[0],ensure_ascii=False)


def test_config_save_roundtrip_and_invalid_threshold(authenticated):
    client,csrf=authenticated
    url='/v1/automation/reception-config'
    original=client.get(url).json()['config']
    for enabled in (True,False):
        response=client.patch(url,json={'lead_capture':{'next_day_invitation_enabled':enabled,'consultation_turn_threshold':3}},headers={'X-CSRF-Token':csrf})
        assert response.status_code==200,response.text
        config=client.get(url).json()['config']
        assert config['lead_capture']['next_day_invitation_enabled']==enabled
        assert config['silence']==original['silence']
        assert config['routing']==original['routing']
    assert client.patch(url,json={'lead_capture':{'consultation_turn_threshold':0}},headers={'X-CSRF-Token':csrf}).status_code in (400,422)


def test_release_configuration_preserves_master_switch_and_customer_controls():
    from scripts.configure_lead_priority_20261028 import update_config
    c=configured();c['lead_capture']['enabled']=False
    result=update_config(c)
    assert not result['lead_capture']['enabled']
    assert result['silence']==c['silence']
    assert result==update_config(result)
