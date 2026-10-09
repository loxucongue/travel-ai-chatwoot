from copy import deepcopy

import pytest
from sqlalchemy import select

from app.automation_models import AutomationRun, AutomationSession
from app.reception_config import IntakeSettings, SilenceSettings
from app.reception_v3 import service, live
from app.models import ConversationState, MessageEvent, OutboundMessage
from live_fixture import setup
from test_reception_v3 import bundle, answer, delivered, step


def config():
    spec = bundle()
    spec['intake'] = IntakeSettings().model_dump()
    spec['silence'] = {**SilenceSettings().model_dump(), 'intervals_minutes':[1,4,10,45,120,180], 'active_start':'00:00', 'active_end':'00:00'}
    spec['routes']['eleven'] = {**deepcopy(spec['routes']['nine']), 'name':'十一日'}
    return spec


def create(db, monkeypatch, entry='九日', model=None):
    monkeypatch.setattr(service, 'compile_skills', lambda db: config())
    monkeypatch.setattr(service, 'active_web_facts', lambda *a, **k: ([], 'test'))
    monkeypatch.setattr(service, 'candidate_materials', lambda *a: [])
    monkeypatch.setattr(service, 'run_agent', model or (lambda c: answer(action='wait')))
    row = AutomationSession(owner_id=1, engine_version='v3', mode='journey', environment='playground',
        messages=[], memory={}, controls={}, virtual_now='2026-10-07T01:00:00+00:00')
    db.add(row); db.flush()
    service.start(db, row, entry); db.commit()
    return row


def test_intake_actual_delivery_anchors_sixty_seconds_and_restarts(session_factory, monkeypatch):
    with session_factory() as db:
        row=create(db,monkeypatch)
        assert not service.state(row).get('intake_deadline')
        assert [m['content_type'] for m in row.messages if m.get('status')=='draft']==['input_select']
        step(db,row,10)
        assert delivered(row)==[config()['intake']['question']]
        deadline=service.state(row)['intake_deadline']
        assert deadline==service.later(row.virtual_now,60)
        row_id=row.id
    with session_factory() as db:
        row=db.get(AutomationSession,row_id)
        step(db,row,59)
        assert service.state(row)['intake_status']=='waiting'
        step(db,row,1)
        assert service.state(row)['delivery_kind']=='introduction'
        assert not service.state(row).get('intake_deadline')
        run=db.scalar(select(AutomationRun))
        assert run.trace['model_http_request_count']==0
        for _ in range(4):step(db,row,2)
        assert delivered(row)[1:]==['配置开场一','配置开场二','完整行程图','住宿原文']


@pytest.mark.parametrize('reply,profile',[('4～6位',{'party_size_range':'4～6位'}),('先介紹吧',{}),('在哪裡集合？',{})])
def test_any_ordinary_answer_starts_intro_and_keeps_question(session_factory,monkeypatch,reply,profile):
    seen=[]
    def model(context):
        seen.append(context)
        return answer(profile=profile) if context['event']=='customer_message' else answer(messages=[{'text':'林芝集合。'}])
    with session_factory() as db:
        row=create(db,monkeypatch,model=model)
        step(db,row,0)
        service.add_message(db,row,reply,'party');db.commit()
        step(db,row,0)
        assert service.state(row)['delivery_kind']=='introduction'
        assert 'party' in service.state(row)['buffered_questions']
        assert not service.state(row)['intake_deadline']
        for _ in range(4):step(db,row,2)
        assert seen[-1]['event']=='introduction_completed'
        assert any(m['content']==reply for m in seen[-1]['buffered_questions'])
        if profile:assert row.memory==profile and 'party_size' not in row.memory


def test_first_message_known_party_skips_intake(session_factory,monkeypatch):
    with session_factory() as db:
        row=create(db,monkeypatch,entry='九日，四位',model=lambda c:answer(route_variant='nine',profile={'party_size':4},start_introduction=True))
        step(db,row,0)
        assert service.state(row)['delivery_kind']=='introduction'
        assert not any(m.get('content_type')=='input_select' for m in row.messages)


def test_optout_during_intake_then_question_does_not_restart_intro(session_factory,monkeypatch):
    with session_factory() as db:
        row=create(db,monkeypatch,model=lambda c:answer(opt_out=True,interrupt=True))
        step(db,row,0)
        service.add_message(db,row,'不要再聯絡','stop');db.commit();step(db,row,0)
        assert not service.state(row).get('intake_answered')
        service.add_message(db,row,'集合在哪？','question');db.commit()
        monkeypatch.setattr(service,'run_agent',lambda c:answer(messages=[{'text':'林芝'}]))
        step(db,row,0);step(db,row,0)
        assert delivered(row)[-1]=='林芝'
        assert not service.state(row).get('next_check_at')
        assert '完整行程图' not in delivered(row)


@pytest.mark.parametrize('decision',[{'action':'handoff'},{'opt_out':True,'interrupt':True},{'route_variant':'eleven','interrupt':True,'start_introduction':True}])
def test_urgent_reply_preempts_intake(session_factory,monkeypatch,decision):
    with session_factory() as db:
        row=create(db,monkeypatch,model=lambda c:answer(**decision))
        step(db,row,0)
        service.add_message(db,row,'urgent','urgent');db.commit();step(db,row,0)
        assert not service.state(row)['intake_deadline']
        if decision.get('route_variant'):assert row.controls['route_variant']=='eleven'
        else:assert service.state(row).get('delivery_kind')!='introduction'


def test_six_hour_timeline_survives_skip_stop_flag_and_delivery_time(session_factory,monkeypatch):
    seen=[]
    def model(c):
        seen.append(c)
        if c['event']=='silence_due' and len(seen)%2==0:
            return answer(messages=[{'text':f'新內容{len(seen)}'}],stop_followup=True,next_check_minutes=999)
        return answer(action='wait',stop_followup=True)
    with session_factory() as db:
        row=create(db,monkeypatch,entry='先看看',model=model)
        # No route: configured textual opening, then start the followup clock at final delivery.
        step(db,row,0);step(db,row,0);step(db,row,2)
        anchor=service.state(row)['followup_anchor']
        for minute in [1,5,15,60,180,360]:
            assert service.state(row)['next_check_at']==service.later(anchor,minute*60)
            service.advance_next(row);db.commit();step(db,row,0)
            timing=seen[-1]['followup_schedule']
            assert minute <= timing['elapsed_since_reply_minutes'] < minute+0.2
            assert timing['minutes_since_last_customer'] >= minute
            if any(m.get('status')=='draft' for m in row.messages):step(db,row,7)
        assert len([c for c in seen if c['event']=='silence_due'])==6
        assert not service.state(row)['next_check_at']
        step(db,row,3600)
        assert len([c for c in seen if c['event']=='silence_due'])==6


def test_late_worker_skips_missed_slots_and_expired_window(session_factory,monkeypatch):
    with session_factory() as db:
        row=create(db,monkeypatch,entry='先看看')
        step(db,row,0);step(db,row,0);step(db,row,2)
        anchor=service.state(row)['followup_anchor']
        step(db,row,20*60)
        assert service.state(row)['next_check_at']==service.later(anchor,60*60)
        before=db.scalar(select(AutomationRun).order_by(AutomationRun.id.desc())).id
        step(db,row,7*3600)
        assert not service.state(row)['next_check_at']
        assert db.scalar(select(AutomationRun).order_by(AutomationRun.id.desc())).id==before


def test_quiet_hours_defer_only_within_the_same_six_hour_window(session_factory,monkeypatch):
    calls=[]
    with session_factory() as db:
        row=create(db,monkeypatch,entry='先看看',model=lambda c:(calls.append(c) or answer(action='wait')))
        step(db,row,0);step(db,row,0);step(db,row,2)
        spec=config();spec['silence'].update(active_start='10:00',active_end='21:00')
        monkeypatch.setattr(service,'compile_skills',lambda db:spec)
        before=len(calls)
        service.advance_next(row);db.commit();step(db,row,0)
        assert len(calls)==before
        assert service.date(service.state(row)['next_check_at']).isoformat().startswith('2026-10-07T10:00:00')
        service.advance_next(row);db.commit();step(db,row,0)
        assert len(calls)==before+1
        assert service.date(service.state(row)['next_check_at']) <= service.date(service.state(row)['followup_until'])


def test_daily_limit_does_not_send_or_extend_followup_into_tomorrow(session_factory,monkeypatch):
    calls=[]
    with session_factory() as db:
        row=create(db,monkeypatch,entry='先看看',model=lambda c:(calls.append(c) or answer(action='wait')))
        step(db,row,0);step(db,row,0);step(db,row,2)
        spec=config();spec['silence']['max_proactive_messages_per_day']=1
        monkeypatch.setattr(service,'compile_skills',lambda db:spec)
        row.messages=[*row.messages,{'id':'previous-touch','direction':'outgoing','content':'已發的相關資料',
                      'status':'simulated_delivered','proactive':True,'run_id':900,'created_at':row.virtual_now}]
        db.commit();before=len(calls)
        service.advance_next(row);db.commit();step(db,row,0)
        assert len(calls)==before
        assert not service.state(row)['next_check_at']


def test_appointment_and_new_message_reset(session_factory,monkeypatch):
    with session_factory() as db:
        row=create(db,monkeypatch,entry='三小時後聯絡',model=lambda c:answer(messages=[{'text':'好'}],next_check_minutes=180))
        for _ in range(4):step(db,row,2)
        assert service.state(row)['next_check_at']==service.later(row.virtual_now,180*60)
        service.add_message(db,row,'不要再聯絡','stop');db.commit()
        monkeypatch.setattr(service,'run_agent',lambda c:answer(opt_out=True,interrupt=True))
        step(db,row,0)
        assert not service.state(row)['next_check_at']
        service.add_message(db,row,'集合在哪','new');db.commit()
        monkeypatch.setattr(service,'run_agent',lambda c:answer(messages=[{'text':'林芝'}]))
        step(db,row,0);step(db,row,0)
        assert '林芝' in delivered(row) and not service.state(row)['next_check_at']


def test_live_input_select_is_persisted_and_callback_before_response(session_factory,monkeypatch):
    setup(session_factory,monkeypatch)
    monkeypatch.setattr(service,'compile_skills',lambda db:config())
    class Client:
        sent=[]
        def get_conversation(self,cid):return {'can_reply':True}
        def get_conversation_labels(self,cid):return {'payload':['ai']}
        def create_input_select_message(self,cid,text,options):
            self.sent.append((text,options))
            with session_factory() as callback:
                callback.add(MessageEvent(conversation_state_id=1,chatwoot_message_id=333,direction='outgoing',content=text,status='sent',content_type='input_select'))
                callback.commit()
            return {'id':333}
        def close(self):pass
    client=Client();monkeypatch.setattr(live,'client_for',lambda c:client)
    with session_factory() as db:
        message=db.get(MessageEvent,1);message.content='九日';db.commit()
        live.accept(db,db.get(ConversationState,1),message);db.commit()
        row=live.session_for(db,1)
        live.deliver_one(db,row)
        assert client.sent==[(config()['intake']['question'],config()['intake']['options'])]
        out=db.scalar(select(OutboundMessage))
        assert out.status=='sent' and len(out.content_attributes['items'])==5
        assert service.state(row)['intake_deadline']==service.later(row.virtual_now,60)
        live.deliver_one(db,row)
        assert len(client.sent)==1


def test_configuration_controls_intake_and_rejects_long_window(authenticated):
    client,csrf=authenticated
    response=client.patch('/v1/automation/reception-config',headers={'X-CSRF-Token':csrf},json={'intake':{'question':'幾位呢？','wait_seconds':45,'options':['一位','兩位']},'silence':{'intervals_minutes':[1,4,10,45,120,180]}})
    assert response.status_code==200,response.text
    assert response.json()['config']['intake']['wait_seconds']==45
    response=client.patch('/v1/automation/reception-config',headers={'X-CSRF-Token':csrf},json={'silence':{'intervals_minutes':[361]}})
    assert response.status_code==422


def test_migration_preserves_actual_greeting_assets_and_live_switches():
    from app.reception_config import default_reception_configuration
    from scripts.update_intake_followup import updated
    before=default_reception_configuration()
    before['reply']['opening_message']='Amber原文'
    before['reply']['opening_messages']=['Amber原文']
    before['reply']['opening_items']=[{'key':'greeting','content_type':'text','content':'Amber原文','media_id':None,'media_hash':''}]
    before['silence'].update(enabled=False,live_enabled=False,active_end='00:00',max_proactive_messages_per_day=8,intervals_minutes=[1,3,5])
    after=updated(before)
    assert after['reply']==before['reply']
    assert after['silence']=={**before['silence'],'intervals_minutes':[360]}
    assert updated(after)==after
