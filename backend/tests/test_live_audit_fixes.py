from copy import deepcopy
from datetime import timedelta
from sqlalchemy import select
import respx

from app import live_reply, notification_dispatch
from app.models import ConversationState, WebhookEvent, MessageEvent, HandoffTask, Notification, NotificationDelivery, utcnow
from app.automation_models import AutomationSession, AutomationRun
from app.reception_v3 import service, live
from app.operations import ensure_handoff
from app.runtime_settings import dt, iso
from live_fixture import setup
from test_reception_v3 import bundle, answer, create
from test_runtime_reliability import notification_setup


def session(db, status='running'):
    row=AutomationSession(owner_id=1,environment='live',engine_version='v3',conversation_state_id=1,
        virtual_now=utcnow(),messages=[],memory={},controls={'simulation':{'status':status,'last_wall_at':utcnow()},
        'v3':{'pending_event':'customer_message','opening_started':False}})
    db.add(row);db.commit();return row


def event(db, kind, labels, revision, mid=None, direction=None):
    conv={'id':26,'inbox_id':128859,'labels':labels,'can_reply':True,'updated_at':revision,
          'meta':{'sender':{'id':55,'name':'Test'}}}
    payload={'event':kind,**conv} if kind.startswith('conversation') else {
        'event':kind,'id':mid,'conversation':conv,'message_type':direction,'content':'hello',
        'sender':{'id':9,'type':'user'},'created_at':utcnow(),'private':False}
    row=WebhookEvent(connection_id=1,event=kind,account_id=180474,resource_id=str(mid or 26),
        idempotency_key=f'{kind}:{revision}:{mid}',payload=payload,received_at=utcnow())
    db.add(row);db.flush();live_reply.mirror_event(db,row)


def test_late_creation_and_message_snapshots_do_not_cancel_live_intake(session_factory,monkeypatch):
    setup(session_factory,monkeypatch)
    with session_factory() as db:
        row=session(db)
        event(db,'conversation_created',[],1)
        event(db,'message_updated',[],2,101,'incoming')
        event(db,'conversation_updated',['ai'],3)
        assert row.controls['simulation']['status']=='running'
        assert service.state(row)['pending_event']=='customer_message'
        assert db.get(ConversationState,1).effective_ai_state=='AI_ACTIVE'
        event(db,'conversation_updated',[],2)
        assert db.get(ConversationState,1).labels==['ai']
        event(db,'conversation_updated',[],4)
        assert row.controls['simulation']['status']=='stopped'


def test_human_reply_claims_task_stops_ai_and_suppresses_stale_reminder(session_factory,monkeypatch):
    notification_setup(session_factory,monkeypatch)
    with session_factory() as db:
        row=session(db)
        task=db.scalar(select(HandoffTask));task.sla_due_at=iso(dt(utcnow())-timedelta(seconds=1));db.commit()
    assert notification_dispatch.process_handoff_overdue(session_factory)
    with session_factory() as db:
        event(db,'message_created',['ai'],1,105,'outgoing')
        task=db.scalar(select(HandoffTask))
        assert task.status=='claimed' and task.chatwoot_assignee_id==9 and task.sla_due_at is None
        assert db.get(ConversationState,1).effective_ai_state=='HUMAN_HANDOFF'
        event(db,'conversation_updated',['ai'],2)
        assert db.get(ConversationState,1).effective_ai_state=='HUMAN_HANDOFF'
        first=db.scalar(select(NotificationDelivery).order_by(NotificationDelivery.id));first.status='delivered';db.commit()
    with respx.mock():
        assert notification_dispatch.process_notification_delivery(session_factory)
    with session_factory() as db:
        assert db.scalars(select(NotificationDelivery).order_by(NotificationDelivery.id)).all()[-1].status=='cancelled'
    assert not notification_dispatch.process_handoff_overdue(session_factory)


def test_contact_handoff_prevents_new_conversation_model_or_auto_activation(session_factory,monkeypatch):
    setup(session_factory,monkeypatch)
    with session_factory() as db:
        session(db)
        ensure_handoff(db,db.get(ConversationState,1),'custom','12人定制')
        other=ConversationState(tenant_id=1,inbox_binding_id=1,contact_id=1,chatwoot_conversation_id=27,
            labels=['ai'],ai_mode='enabled',ai_label_present=True,can_reply=True)
        db.add(other);db.commit()
        assert live.human_owned(db,other) and not live.permitted(db,other)
        live.accept(db,other,db.get(MessageEvent,1))
        assert live.session_for(db,other.id) is None


def test_label_refresh_and_contact_updates_preserve_human_ownership_without_task(session_factory,monkeypatch):
    from app.api import sync_local_labels
    setup(session_factory,monkeypatch)
    with session_factory() as db:
        row=session(db)
        event(db,'message_created',['ai'],1,106,'outgoing')
        conv=db.get(ConversationState,1)
        assert not db.scalar(select(HandoffTask.id))
        sync_local_labels(db,conv,['ai'])
        assert conv.effective_ai_state=='HUMAN_HANDOFF'
        payload={'event':'contact_updated','id':55,'name':'Test','labels':[]}
        update=WebhookEvent(connection_id=1,event='contact_updated',account_id=180474,
            resource_id='55',idempotency_key='contact:55',payload=payload,received_at=utcnow())
        db.add(update);db.flush();live_reply.mirror_event(db,update)
        assert conv.effective_ai_state=='HUMAN_HANDOFF'
        assert not live.permitted(db,conv)


def test_planned_handoff_can_send_final_message_before_task_exists(session_factory,monkeypatch):
    setup(session_factory,monkeypatch)
    with session_factory() as db:
        row=session(db);row.controls={**row.controls,'human':True,'v3':{'handoff':True}}
        assert live.permitted(db,db.get(ConversationState,1))
        ensure_handoff(db,db.get(ConversationState,1),'test')
        assert not live.permitted(db,db.get(ConversationState,1))


def test_six_hour_silence_continues_after_wait_then_sends_at_three_hours(session_factory,monkeypatch):
    checkpoints=[]
    with session_factory() as db:
        row=create(db,monkeypatch)
        spec=bundle();spec['silence'].update(intervals_minutes=[1,4,10,45,120,180],active_start='00:00',active_end='00:00')
        monkeypatch.setattr(service,'compile_skills',lambda db:deepcopy(spec))
        row.messages=[{'id':'customer','direction':'incoming','content':'我先和家人商量住宿','created_at':row.virtual_now},
            {'id':'reply','direction':'outgoing','status':'simulated_delivered','content':'好的','created_at':row.virtual_now}]
        value={'silence_step':0,'followup_intervals':spec['silence']['intervals_minutes']}
        service.schedule_followup(value,spec,row.virtual_now,reset=True);service.save(row,value);db.commit()
        def model(ctx):
            minute=ctx['followup_schedule']['elapsed_since_reply_minutes'];checkpoints.append(minute)
            return answer(action='reply' if minute==180 else 'wait',stop_followup=True,
                messages=[{'text':'住宿這部分，家人還有想了解的地方嗎？'}] if minute==180 else [])
        monkeypatch.setattr(service,'run_agent',model)
        for minute in [1,5,15,60,180,360]:
            service.advance_next(row);db.commit();service.tick(db,session_id=row.id,advance_clock=False)
            for m in list(row.messages):
                if m.get('status')=='draft':service.complete_delivery(row,m['id'],'simulated_delivered')
            db.commit()
        assert checkpoints==[1,5,15,60,180,360]
        assert sum(m.get('content')=='住宿這部分，家人還有想了解的地方嗎？' for m in row.messages)==1
        assert service.state(row)['next_check_at'] is None
        assert not service.state(row).get('pending_event')
