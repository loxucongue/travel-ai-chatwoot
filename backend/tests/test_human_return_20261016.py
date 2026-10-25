from copy import deepcopy
from datetime import timedelta
import json
import httpx
import pytest
from sqlalchemy import select
from app.models import ConversationState, ConversationLabelSync, HandoffTask, MessageEvent, OutboundMessage, utcnow
from app.reception_v3 import live, service, runtime
from app.reception_v3 import human_followup as hf
from app import conversation_labels as labels
from app.runtime_settings import dt
from live_fixture import setup
from test_live_audit_fixes import session, event
from test_conversation_controls import FakeChatwoot
from test_reception_v3 import bundle


def prepared(factory, monkeypatch):
    setup(factory,monkeypatch)
    monkeypatch.setattr('app.reception_v3.skills.compile_skills',lambda db:bundle())
    with factory() as db:
        row=session(db)
        conv=db.get(ConversationState,1)
        live.handoff(db,row,'knowledge','需要确认')
        task=db.scalar(select(HandoffTask));task.status='claimed';task.assignee_user_id=1
        msg=MessageEvent(conversation_state_id=1,chatwoot_message_id=101,direction='outgoing',
            attribution='inferred_human',content='這個安排已經確認，您可以和家人討論看看。',created_at=utcnow())
        db.add(msg);db.commit()
        return task.id


def arm(factory,task_id):
    with factory() as db:
        row=live.session_for(db,1);conv=db.get(ConversationState,1);task=db.get(HandoffTask,task_id)
        hf.arm(db,conv,row,task,1);db.commit()
        return row.id


def activate(factory):
    with factory() as db:
        row = live.session_for(db, 1)
        row.virtual_now = row.controls['human_followup']['resume_at']
        hf.activate_due(db, db.get(ConversationState, 1), row)
        assert not hf.waiting(row) and hf.enabled(row)
        db.commit()


def sync(factory,monkeypatch,fake=None):
    fake=fake or FakeChatwoot()
    monkeypatch.setattr(labels,'client_for',lambda c:fake)
    assert labels.process_label_sync(factory)
    return fake


def test_handoff_labels_retry_preserve_external_and_no_customer_send(session_factory,monkeypatch):
    prepared(session_factory,monkeypatch)
    fake=FakeChatwoot();fake.current_labels=['ai','外部新增','客诉']
    def fail(*args):raise RuntimeError('offline')
    monkeypatch.setattr(fake,'set_conversation_labels',fail)
    sync(session_factory,monkeypatch,fake)
    with session_factory() as db:
        job=db.get(ConversationLabelSync,1)
        assert job.status=='pending' and job.attempts==1
        assert not live.permitted(db,db.get(ConversationState,1))
        assert not db.scalar(select(OutboundMessage.id))
        job.due_at=utcnow();db.commit()
    fake=FakeChatwoot();fake.current_labels=['ai','外部新增','客诉']
    sync(session_factory,monkeypatch,fake)
    assert fake.current_labels==['外部新增','客诉','人工接管']
    with session_factory() as db:
        assert db.get(ConversationLabelSync,1).status=='synced'
        assert not db.get(ConversationState,1).ai_label_present


def test_return_history_timer_labels_and_reply_permission(session_factory,monkeypatch):
    task=prepared(session_factory,monkeypatch);arm(session_factory,task)
    fake=sync(session_factory,monkeypatch)
    assert set(fake.current_labels)=={'人工接管','外部新增'}
    with session_factory() as db:
        row=live.session_for(db,1);v=service.state(row);conv=db.get(ConversationState,1)
        assert hf.waiting(row) and conv.effective_ai_state=='HUMAN_HANDOFF'
        assert any(m['content'].startswith('這個安排') for m in row.messages)
        assert not live.permitted(db,conv,purpose='followup')
        assert db.get(HandoffTask,task).status=='completed'
        assert (dt(v['next_check_at'])-dt(row.virtual_now)).total_seconds() >= 1750
        due=v['next_check_at']
        row.virtual_now=(dt(due)-timedelta(seconds=1)).isoformat()
        service.advance(row,wall_now=row.controls['simulation']['last_wall_at'])
        hf.activate_due(db,conv,row)
        assert service.state(row)['pending_event'] is None and hf.waiting(row)
        assert db.get(ConversationLabelSync,1).target=='human'
        row.virtual_now=due;service.advance(row,wall_now=row.controls['simulation']['last_wall_at'])
        assert service.state(row)['pending_event']=='silence_due'
        hf.activate_due(db,conv,row)
        assert not hf.waiting(row) and not live.permitted(db,conv,purpose='followup')
        assert db.get(ConversationLabelSync,1).target=='followup'
        db.commit()
    sync(session_factory,monkeypatch,fake)
    assert set(fake.current_labels)=={'ai','AI跟进','外部新增'}
    with session_factory() as db:
        conv=db.get(ConversationState,1)
        assert conv.effective_ai_state=='AI_FOLLOWUP'
        assert live.permitted(db,conv,purpose='followup')
        assert not live.permitted(db,conv)


def test_customer_returns_to_human_and_cancels_drafts(session_factory,monkeypatch):
    task=prepared(session_factory,monkeypatch);arm(session_factory,task);sync(session_factory,monkeypatch)
    with session_factory() as db:
        row=live.session_for(db,1);generation=row.generation
        row.messages=[*row.messages,{'id':'draft','status':'draft'}];db.commit()
        event(db,'message_created',['ai','AI跟进'],30,102,'incoming')
        assert not hf.enabled(row) and row.controls['human']
        assert row.generation>generation
        assert row.messages[-1].get('status')!='draft'
        assert service.state(row)['next_check_at'] is None
        tasks=db.scalars(select(HandoffTask).order_by(HandoffTask.id)).all()
        assert len(tasks)==2 and tasks[-1].reason_code=='followup_customer_replied'
        assert db.get(ConversationLabelSync,1).target=='human'
        assert not db.scalar(select(OutboundMessage.id))


def test_advisor_resets_timer_duplicate_does_not(session_factory,monkeypatch):
    task=prepared(session_factory,monkeypatch);arm(session_factory,task);sync(session_factory,monkeypatch)
    with session_factory() as db:
        row=live.session_for(db,1);generation=row.generation
        event(db,'message_created',['ai','AI跟进'],31,102,'outgoing')
        assert hf.enabled(row) and row.generation>generation
        deadline=service.state(row)['next_check_at'];generation=row.generation
        live.record_human_reply(db,db.get(ConversationState,1),db.scalar(select(MessageEvent).where(MessageEvent.chatwoot_message_id==102)))
        assert service.state(row)['next_check_at']==deadline and row.generation==generation


@pytest.mark.parametrize('kind',['optout','complaint','unknown','unanswered','window','other_task'])
def test_return_blockers(session_factory,monkeypatch,kind):
    task_id=prepared(session_factory,monkeypatch)
    with session_factory() as db:
        row=live.session_for(db,1);conv=db.get(ConversationState,1)
        if kind=='optout':service.save(row,{**service.state(row),'opt_out':True})
        if kind=='complaint':conv.labels=['ai','客诉']
        if kind=='unknown':db.add(OutboundMessage(conversation_state_id=1,idempotency_key='u',source_type='ai',status='unknown',content='test'))
        if kind=='unanswered':db.add(MessageEvent(conversation_state_id=1,chatwoot_message_id=102,direction='incoming',content='还有问题'))
        if kind=='window':conv.can_reply=False
        if kind=='other_task':db.add(HandoffTask(conversation_state_id=1,status='claimed',reason_code='other'))
        db.flush()
        with pytest.raises(ValueError):hf.arm(db,conv,row,db.get(HandoffTask,task_id),1)


@pytest.mark.parametrize('now,last,expected',[
    ('2026-10-16T12:00:00+08:00','2026-10-16T12:00:00+08:00','2026-10-16T12:30:00+08:00'),
    ('2026-10-16T12:00:00+08:00','2026-10-16T11:00:00+08:00','2026-10-16T12:30:00+08:00'),
    ('2026-10-16T00:45:00+08:00','2026-10-16T00:45:00+08:00','2026-10-16T07:00:00+08:00')])
def test_schedule_quiet_and_no_burst(session_factory,monkeypatch,now,last,expected):
    prepared(session_factory,monkeypatch)
    with session_factory() as db:
        row=live.session_for(db,1);conv=db.get(ConversationState,1);conv.last_customer_message_at=now
        hf.schedule(row,conv,now,last)
        assert dt(service.state(row)['next_check_at'])==dt(expected)


def test_daily_cap_and_channel_window_not_extended(session_factory,monkeypatch):
    prepared(session_factory,monkeypatch)
    with session_factory() as db:
        row=live.session_for(db,1);conv=db.get(ConversationState,1)
        now='2026-10-16T12:00:00+08:00';conv.last_customer_message_at=now
        row.messages=[{'proactive':True,'status':'submitted','created_at':now}]
        hf.schedule(row,conv,now,now)
        assert dt(service.state(row)['next_check_at'])==dt('2026-10-17T09:00:00+08:00')
        conv.last_customer_message_at='2026-10-09T13:00:00+08:00'
        hf.schedule(row,conv,now,now)
        assert service.state(row)['next_check_at'] is None


def test_followup_model_is_bounded_and_cannot_quote(monkeypatch):
    requests=[]
    def post(payload,timeout):
        requests.append(payload)
        return httpx.Response(200,request=httpx.Request('POST','https://model.invalid'),json={
            'choices':[{'message':{'content':json.dumps({'action':'reply','messages':[{'text':'您和家人討論得如何呢？'}]})}}]})
    monkeypatch.setattr(runtime,'_post_with_deadline',post)
    decision,logs,digest=runtime.run_agent({'state':{'human_followup':True},'messages':[]})
    assert decision['messages'][0]['text']=='您和家人討論得如何呢？'
    assert requests[0]['max_tokens']==800 and requests[0]['thinking']['type']=='disabled'
    assert 'tools' not in requests[0] and logs[0]['node']=='advisor_followup'


def test_label_intent_changed_during_http_reconciles_new_owner(session_factory,monkeypatch):
    task=prepared(session_factory,monkeypatch);arm(session_factory,task);activate(session_factory)
    fake=FakeChatwoot();original=fake.set_conversation_labels
    def change(cid,new_labels):
        result=original(cid,new_labels)
        with session_factory() as db:
            live.handoff(db,live.session_for(db,1),'requested','客户找真人');db.commit()
        return result
    monkeypatch.setattr(fake,'set_conversation_labels',change)
    sync(session_factory,monkeypatch,fake)
    with session_factory() as db:
        job=db.get(ConversationLabelSync,1)
        assert job.target=='human' and job.status=='pending'
        assert not live.permitted(db,db.get(ConversationState,1),purpose='followup')


def test_endpoint_explicit_return_and_duplicate(authenticated,session_factory,monkeypatch):
    client,csrf=authenticated;task_id=prepared(session_factory,monkeypatch)
    with session_factory() as db:version=db.get(HandoffTask,task_id).version
    path=f'/v1/handoffs/{task_id}/return-followup'
    result=client.post(path,headers={'X-CSRF-Token':csrf},json={'version':version})
    assert result.status_code==200,result.text
    assert result.json()['followup_enabled'] and result.json()['label_sync_status']=='pending'
    again=client.post(path,headers={'X-CSRF-Token':csrf},json={'version':result.json()['version']})
    assert again.status_code==409
    with session_factory() as db:
        assert not db.scalar(select(OutboundMessage.id))
        assert hf.enabled(live.session_for(db,1))


def test_pending_task_cannot_return(authenticated,session_factory,monkeypatch):
    client,csrf=authenticated;task_id=prepared(session_factory,monkeypatch)
    with session_factory() as db:
        task=db.get(HandoffTask,task_id);task.status='pending';version=task.version;db.commit()
    result=client.post(f'/v1/handoffs/{task_id}/return-followup',headers={'X-CSRF-Token':csrf},json={'version':version})
    assert result.status_code==409


def test_followup_delivery_race_cannot_send(session_factory,monkeypatch):
    task=prepared(session_factory,monkeypatch);arm(session_factory,task);activate(session_factory);sync(session_factory,monkeypatch)
    fake=FakeChatwoot();fake.current_labels=['ai','AI跟进'];original=fake.get_conversation
    def incoming(cid):
        with session_factory() as db:
            hf.customer_replied(db,db.get(ConversationState,1),live.session_for(db,1));db.commit()
        return original(cid)
    monkeypatch.setattr(fake,'get_conversation',incoming)
    monkeypatch.setattr(live,'client_for',lambda c:fake)
    with session_factory() as db:
        row=live.session_for(db,1);v=service.state(row)
        service.append_plan(row,v,[{'content':'您和家人討論得如何呢？','content_type':'text','proactive':True}],'reply',2,999)
        service.save(row,v);db.commit()
        live.deliver_one(db,row)
        assert not db.scalar(select(OutboundMessage.id))
        assert not hf.enabled(row)


def test_manual_handoff_tag_revokes_followup_permission(session_factory,monkeypatch):
    task=prepared(session_factory,monkeypatch);arm(session_factory,task);activate(session_factory);sync(session_factory,monkeypatch)
    with session_factory() as db:
        event(db,'conversation_updated',['ai','AI跟进','人工接管'],50)
        row=live.session_for(db,1)
        assert not hf.enabled(row) and row.controls['human']
        event(db,'message_created',['人工接管'],51,102,'outgoing')
        assert not hf.enabled(row) and row.controls['simulation']['status']=='stopped'


def test_private_note_does_not_restart_timer(session_factory,monkeypatch):
    from app.live_reply import mirror_event
    from app.models import WebhookEvent
    task=prepared(session_factory,monkeypatch);arm(session_factory,task);sync(session_factory,monkeypatch)
    with session_factory() as db:
        row=live.session_for(db,1);before=deepcopy(row.controls)
        e=WebhookEvent(connection_id=1,event='message_created',account_id=180474,resource_id='103',idempotency_key='note',received_at=utcnow(),
            payload={'event':'message_created','id':103,'message_type':'outgoing','private':True,'content':'内部跟进说明',
                'created_at':utcnow(),'sender':{'id':9,'type':'user'},'conversation':{'id':26,'inbox_id':128859}})
        db.add(e);db.flush();mirror_event(db,e)
        assert service.state(row)['next_check_at']==before['v3']['next_check_at']
        assert not any(m.get('content')=='内部跟进说明' for m in row.messages)


def test_confirmed_control_revision_ignores_late_human_label_webhook(session_factory,monkeypatch):
    task=prepared(session_factory,monkeypatch);arm(session_factory,task);activate(session_factory)
    fake=FakeChatwoot()
    monkeypatch.setattr(fake,'get_conversation',lambda cid:{'id':26,'updated_at':100,'can_reply':True})
    sync(session_factory,monkeypatch,fake)
    with session_factory() as db:
        assert db.get(ConversationState,1).chatwoot_control_updated_at==100
        event(db,'conversation_updated',['人工接管'],99)
        row=live.session_for(db,1)
        assert hf.enabled(row) and live.permitted(db,db.get(ConversationState,1),purpose='followup')
        event(db,'conversation_updated',['人工接管'],101)
        assert not hf.enabled(row)


def test_followup_completion_does_not_restart_introduction(session_factory,monkeypatch):
    from app.automation_models import AutomationRun
    task=prepared(session_factory,monkeypatch);arm(session_factory,task);activate(session_factory);sync(session_factory,monkeypatch)
    with session_factory() as db:
        row=live.session_for(db,1)
        row.virtual_now=service.state(row)['next_check_at']
        run=AutomationRun(session_id=row.id,module='reception_v3',generation=row.generation,status='processing',idempotency_key='advisor-followup')
        db.add(run);db.flush()
        rules=bundle();rules['silence']={'daily_at':'09:00','intervals_minutes':[]}
        decision=runtime.Decision(action='reply',messages=[runtime.Message(text='您和家人討論得如何呢？')]).model_dump()
        service.apply_decision(db,row,run,rules,decision)
        drafts=[m for m in row.messages if m.get('status')=='draft']
        assert len(drafts)==1 and drafts[0]['proactive'] and drafts[0]['content_type']=='text'
        service.complete_delivery(row,drafts[0]['id'],'submitted')
        value=service.state(row)
        assert value['pending_event'] is None and value['daily_last_touch_at']==row.virtual_now
        if value['next_check_at']:
            assert dt(value['next_check_at']) > dt(row.virtual_now)
        assert not any(m.get('status')=='draft' for m in row.messages)
