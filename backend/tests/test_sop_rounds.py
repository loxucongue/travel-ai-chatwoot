from datetime import timedelta
import pytest
from sqlalchemy import select, func

from app.automation_models import AutomationSession, RehearsalEnrollment, RehearsalJob, TouchReservation
from app.automation_service import (apply_controls, trigger_sops, add_customer_message, enroll_rehearsal, sop_snapshot,
    advance_sops, reserve_touch, subject_key, dt, iso)
from app.models import (SopDefinition, ChatwootLabel, InboxBinding, ConversationState, Contact, User, MessageEvent,
    OutboundMessage, SopJob, HandoffTask, ChatwootConnection, WebhookEvent, utcnow)
from app.ops_api import sop_json
from app.sop_schedule import schedule_at

AT = '2026-08-26T02:00:00+00:00'


def node(key='a', **kw):
    return {'key':key, 'schedule_type':'relative','basis':'enrollment','delay_minutes':10,
            'messages':[{'key':'text','content_type':'text','content':'fixed test'}],**kw}


def session(db, **kw):
    row=AutomationSession(owner_id=1,mode='sop',virtual_now=AT,controls={'can_reply':True,'ai_enabled':True,'channel':'facebook','labels':[]},
        messages=[{'direction':'incoming','content':'hello','created_at':AT}],**kw)
    db.add(row);db.flush();return row


def version(db, **kw):
    sop=SopDefinition(tenant_id=1,created_by=1,name='rounds',status='running',nodes=[node()],**kw)
    db.add(sop);db.flush();return sop_snapshot(db,sop,1)


def test_terminal_round_never_auto_restarts_on_tag_toggle_generation_or_publish(session_factory):
    with session_factory() as db:
        s=session(db);v=version(db,trigger_type='label',trigger_labels=['start'])
        trigger_sops(db,s,added_labels={'start'})
        e=db.scalar(select(RehearsalEnrollment)); assert e.round_number==1
        s.virtual_now=iso(dt(AT)+timedelta(minutes=10));advance_sops(db,s);db.flush()
        assert e.status=='completed'
        s.generation+=5
        trigger_sops(db,s,added_labels={'start'})
        sop=db.get(SopDefinition,v.sop_id);sop.version+=1;sop_snapshot(db,sop,1)
        trigger_sops(db,s,added_labels={'start'})
        assert db.scalar(select(func.count()).select_from(RehearsalEnrollment))==1
        with pytest.raises(ValueError,match='sop_reenrollment_required'):enroll_rehearsal(db,s,v)
        e2=enroll_rehearsal(db,s,v,reenroll=True,request_key='explicit-round-two')
        assert e2.round_number==2 and e2.status=='active'
        assert enroll_rehearsal(db,s,v,reenroll=True,request_key='explicit-round-two').id==e2.id
        with pytest.raises(ValueError,match='sop_round_active'):enroll_rehearsal(db,s,v,reenroll=True,request_key='third')


def test_unrelated_or_unchanged_tags_keep_sop_but_exit_tag_cancels_all_remaining(session_factory):
    with session_factory() as db:
        s=session(db);v=version(db,exit_labels=['stop']);e=enroll_rehearsal(db,s,v)
        apply_controls(db,s,{'labels':['normal']});db.flush()
        assert e.status=='active'
        generation=s.generation
        apply_controls(db,s,{'labels':['normal']});assert s.generation==generation
        s.virtual_now=iso(dt(AT)+timedelta(minutes=10));advance_sops(db,s);db.flush()
        assert db.scalar(select(RehearsalJob)).status=='simulated_delivered'
        s2=session(db);e2=enroll_rehearsal(db,s2,v)
        apply_controls(db,s2,{'labels':['stop']});db.flush()
        assert e2.status=='cancelled'
        assert db.scalar(select(RehearsalJob).where(RehearsalJob.enrollment_id==e2.id)).reason=='exit_label'
        apply_controls(db,s2,{'labels':[]});assert e2.status=='cancelled'


def test_same_customer_other_session_and_strategy_version_cannot_duplicate(session_factory):
    with session_factory() as db:
        db.add(Contact(id=1,tenant_id=1,chatwoot_contact_id=123,name='test'))
        db.add(InboxBinding(id=1,tenant_id=1,chatwoot_inbox_id=101,name='test'));db.flush()
        db.add_all([ConversationState(id=i,tenant_id=1,inbox_binding_id=1,contact_id=1,chatwoot_conversation_id=i) for i in (1,2)]);db.flush()
        s=session(db,conversation_state_id=1,inbox_binding_id=1);s2=session(db,conversation_state_id=2,inbox_binding_id=1)
        v=version(db);e=enroll_rehearsal(db,s,v)
        assert enroll_rehearsal(db,s2,v).id==e.id
        sop=db.get(SopDefinition,v.sop_id);sop.version+=1;v2=sop_snapshot(db,sop,1)
        assert enroll_rehearsal(db,s2,v2).id==e.id


def test_draft_trigger_and_inbox_changes_do_not_modify_published_entry(session_factory):
    with session_factory() as db:
        s=session(db);v=version(db,trigger_type='label',trigger_labels=['A'])
        sop=db.get(SopDefinition,v.sop_id);sop.trigger_labels=['B'];sop.inbox_ids=[999];sop.version+=1
        trigger_sops(db,s,added_labels={'A'});db.flush()
        assert db.scalar(select(RehearsalEnrollment)) is not None
        s2=session(db);trigger_sops(db,s2,added_labels={'B'})
        assert not db.scalar(select(RehearsalEnrollment).where(RehearsalEnrollment.session_id==s2.id))


def test_first_customer_event_is_not_every_message(session_factory):
    with session_factory() as db:
        s=session(db);s.messages=[];v=version(db,trigger_type='first_message')
        add_customer_message(db,s,'first','1',queue_reply=False);db.flush()
        e=db.scalar(select(RehearsalEnrollment));assert e is not None
        add_customer_message(db,s,'second','2',queue_reply=False);db.flush()
        assert e.status=='cancelled'
        assert db.scalar(select(func.count()).select_from(RehearsalEnrollment))==1


def test_ten_twenty_minutes_and_previous_group_work_without_bypassing_other_strategies(session_factory):
    with session_factory() as db:
        s=session(db);v=version(db)
        sop=db.get(SopDefinition,v.sop_id);sop.nodes=[node(),node('b',delay_minutes=20),node('c',basis='previous_node',delay_minutes=10)];sop.version+=1
        v=sop_snapshot(db,sop,1);e=enroll_rehearsal(db,s,v)
        for minutes in (10,20,30):
            s.virtual_now=iso(dt(AT)+timedelta(minutes=minutes));advance_sops(db,s);db.flush()
        assert e.status=='completed'
        assert [x.status for x in db.scalars(select(RehearsalJob).where(RehearsalJob.enrollment_id==e.id))]==['simulated_delivered']*3
        assert not reserve_touch(db,subject_key(db,s),'wakeup',s.virtual_now)
        v2=version(db);enroll_rehearsal(db,s,v2);s.virtual_now=iso(dt(s.virtual_now)+timedelta(minutes=10));advance_sops(db,s);db.flush()
        assert db.scalar(select(RehearsalJob).order_by(RehearsalJob.id.desc())).reason=='contact_frequency_limit'
        assert db.scalar(select(OutboundMessage)) is None


def test_old_customer_can_use_enrollment_anchor_but_not_reset_channel_window(session_factory):
    assert schedule_at(node(),customer_added_at='2020-01-01T00:00:00+00:00',enrolled_at=AT)==iso(dt(AT)+timedelta(minutes=10))
    assert schedule_at(node(schedule_type='calendar_day',day_number=2,time_of_day='10:00'),customer_added_at='2020-01-01T00:00:00+00:00',enrolled_at=AT)=='2026-08-27T02:00:00+00:00'
    with session_factory() as db:
        s=session(db);s.messages=[{'direction':'incoming','created_at':'2020-01-01T00:00:00+00:00'}]
        v=version(db);enroll_rehearsal(db,s,v);s.virtual_now=iso(dt(AT)+timedelta(minutes=10));advance_sops(db,s);db.flush()
        assert db.scalar(select(RehearsalJob)).reason=='automatic_window_closed'


def test_manual_enrollment_checks_published_inbox_and_batch_is_atomic(authenticated,session_factory):
    client,csrf=authenticated;h={'X-CSRF-Token':csrf}
    with session_factory() as db:
        for i in (1,2):
            db.add(InboxBinding(id=i,tenant_id=1,chatwoot_inbox_id=100+i,name=str(i)))
        db.flush()
        for i in (1,2):db.add(ConversationState(id=i,tenant_id=1,inbox_binding_id=i,chatwoot_conversation_id=i))
        db.commit()
    sop=client.post('/v1/sops',json={'name':'scope','inbox_ids':[101],'nodes':[node()]},headers=h).json()
    assert client.post(f"/v1/sops/{sop['id']}/publish",headers=h).status_code==200
    assert client.post(f"/v1/sops/{sop['id']}/enroll",json={'conversation_ids':[1,2]},headers=h).status_code==422
    with session_factory() as db:assert db.scalar(select(RehearsalEnrollment)) is None
    assert client.get(f"/v1/sops/{sop['id']}/enrollment-candidates").json()['total']==1


def test_publish_rejects_empty_unknown_and_contradictory_tags(authenticated,session_factory):
    client,csrf=authenticated;h={'X-CSRF-Token':csrf}
    with session_factory() as db:
        db.add(ChatwootLabel(tenant_id=1,chatwoot_label_id=1,title='start'));db.commit()
    for entry,exit in (([],[]),(['unknown'],[]),(['start'],['start'])):
        sop=client.post('/v1/sops',json={'name':'invalid','trigger_type':'label','trigger_labels':entry,'exit_labels':exit,'nodes':[node()]},headers=h).json()
        assert client.post(f"/v1/sops/{sop['id']}/publish",headers=h).status_code==422


def test_rehearsal_metrics_count_real_rehearsal_tables(session_factory):
    with session_factory() as db:
        s=session(db);v=version(db);enroll_rehearsal(db,s,v);s.virtual_now=iso(dt(AT)+timedelta(minutes=10));advance_sops(db,s);db.flush()
        metrics=sop_json(db,db.get(SopDefinition,v.sop_id))
        assert (metrics['rehearsal_enrolled'],metrics['rehearsal_sent'],metrics['sent'])==(1,1,0)


def test_chatwoot_label_creates_realtime_rehearsal_and_never_outbound(session_factory):
    from app.webhook_ingest import ingest_payload
    from app.automation_shadow import observe_webhook, advance_shadow_sops
    with session_factory() as db:
        connection=ChatwootConnection(tenant_id=1,account_id=180474,base_url='https://example.test',connection_key='test')
        db.add(connection);db.add(InboxBinding(id=1,tenant_id=1,chatwoot_inbox_id=128859,name='FB',channel_type='Channel::FacebookPage'));db.flush()
        db.add(ConversationState(id=1,tenant_id=1,inbox_binding_id=1,chatwoot_conversation_id=26,can_reply=True));db.flush()
        db.add(MessageEvent(conversation_state_id=1,chatwoot_message_id=1,direction='incoming',content='old customer recent reply',created_at=utcnow()))
        v=version(db,trigger_type='label',trigger_labels=['start']);sop=db.get(SopDefinition,v.sop_id);sop.nodes=[node(delay_minutes=0)];sop.version+=1;sop_snapshot(db,sop,1);db.commit()
        event={'event':'conversation_updated','account':{'id':180474},'id':26,'inbox_id':128859,'labels':['start','ai'],'can_reply':True}
        ingest_payload(db,connection,event);assert observe_webhook(db)
        e=db.scalar(select(RehearsalEnrollment));assert e is not None
        s=db.get(AutomationSession,e.session_id)
        assert s.environment=='shadow' and s.controls['observed_labels']==['start','ai']
        event['labels']=['start','ai','normal'];ingest_payload(db,connection,event);observe_webhook(db)
        assert e.status=='active'
        advance_shadow_sops(db)
        assert db.scalar(select(OutboundMessage)) is None and db.scalar(select(SopJob)) is None and db.scalar(select(HandoffTask)) is None


def test_active_round_and_request_id_survive_separate_database_sessions(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.db import Base
    from app.models import Tenant
    engine=create_engine(f"sqlite:///{tmp_path / 'concurrent.db'}",connect_args={'check_same_thread':False,'timeout':10})
    Base.metadata.create_all(engine)
    factory=sessionmaker(engine,expire_on_commit=False)
    with factory() as db:
        db.add(Tenant(id=1,name='test'));db.add(User(id=1,email='local@example.com',password_hash='unused',display_name='QA',role='admin'));db.flush()
        s=session(db);v=version(db);sid,vid=s.id,v.id;db.commit()
    barrier=Barrier(2)
    def enroll_once():
        from app.automation_models import SopVersion
        with factory() as db:
            s=db.get(AutomationSession,sid);v=db.get(SopVersion,vid);barrier.wait()
            e=enroll_rehearsal(db,s,v,source='label');db.commit();return e.id
    with ThreadPoolExecutor(2) as pool:
        ids=list(pool.map(lambda _:enroll_once(),range(2)))
    assert ids[0]==ids[1]
    with factory() as db:
        e=db.get(RehearsalEnrollment,ids[0]);e.status='completed';db.commit()
        from app.automation_models import SopVersion
        e2=enroll_rehearsal(db,db.get(AutomationSession,sid),db.get(SopVersion,vid),reenroll=True,request_key='restart-safe');db.commit();second_id=e2.id
    with factory() as db:
        repeated=enroll_rehearsal(db,db.get(AutomationSession,sid),db.get(SopVersion,vid),reenroll=True,request_key='restart-safe')
        assert repeated.id==second_id
        assert db.scalar(select(func.count()).select_from(RehearsalEnrollment))==2
        db.commit()
    engine.dispose()


def test_contact_mapping_and_real_handoff_are_hard_blocks(session_factory):
    from app.models import AppSetting
    from app.automation_service import control_block
    from app.automation_shadow import mirror_controls
    with session_factory() as db:
        db.add(AppSetting(key='label_mappings',value={'contact_block_labels':['do-not-call']}));db.flush()
        s=session(db);s.controls={**s.controls,'contact_labels':['do-not-call']}
        assert control_block(s,db)=='human_or_contact_block'
        db.add(InboxBinding(id=1,tenant_id=1,chatwoot_inbox_id=123,name='test'));db.flush()
        state=ConversationState(id=1,tenant_id=1,inbox_binding_id=1,chatwoot_conversation_id=26)
        db.add(state);db.flush()
        db.add(HandoffTask(conversation_state_id=1,reason_code='manual',status='pending'));db.flush()
        from app.models import Tenant
        assert mirror_controls(state,db.get(Tenant,1),db)['human'] is True


def test_shadow_sessions_cannot_have_clock_or_messages_forged(authenticated,session_factory):
    client,csrf=authenticated
    with session_factory() as db:
        s=session(db);s.environment='shadow';sid=s.id;db.commit()
    for suffix,payload in [('advance',{'minutes':10,'generation':1}),('messages',{'content':'fake','client_key':'fake'}),('confirm',{'message_id':'fake'}),('reset',{}),('sop',{'version_id':1})]:
        assert client.post(f'/v1/playground/sessions/{sid}/{suffix}',json=payload,headers={'X-CSRF-Token':csrf}).status_code==403
    assert client.get('/v1/playground/sessions').json()['items']==[]


def test_legacy_sender_never_schedules_new_grouped_sop(session_factory):
    from app.worker_main import schedule_sop_enrollment
    with session_factory() as db:
        v=version(db)
        assert not schedule_sop_enrollment(db,db.get(SopDefinition,v.sop_id),None)
        assert db.scalar(select(SopJob)) is None


def test_old_incoming_webhook_is_context_only_and_cannot_end_new_round(session_factory):
    from app.webhook_ingest import ingest_payload
    from app.automation_shadow import observe_webhook
    with session_factory() as db:
        connection=ChatwootConnection(tenant_id=1,account_id=180474,connection_key='stale-test')
        db.add(connection);db.add(InboxBinding(id=1,tenant_id=1,chatwoot_inbox_id=128859,name='FB'));db.flush()
        db.add(ConversationState(id=1,tenant_id=1,inbox_binding_id=1,chatwoot_conversation_id=26,can_reply=True,ai_mode='enabled',ai_label_present=True,labels=['ai']));db.flush()
        s=session(db,inbox_binding_id=1,conversation_state_id=1,environment='shadow');v=version(db);e=enroll_rehearsal(db,s,v);db.commit()
        event={'event':'message_created','account':{'id':180474},'id':800,'message_type':'incoming','private':False,'content':'backlog',
               'created_at':iso(dt(utcnow())-timedelta(hours=1)),'conversation':{'id':26,'inbox_id':128859,'can_reply':False,'labels':['AI关闭']}}
        ingest_payload(db,connection,event);assert observe_webhook(db)
        assert e.status=='active'
        assert any(x['content']=='backlog' for x in s.messages)


@pytest.mark.parametrize("live", [False, True])
def test_reenrollment_api_retry_does_not_rewind_session(authenticated,session_factory,monkeypatch,live):
    if live:
        from app.config import settings
        monkeypatch.setattr(settings,"app_profile","live_reply")
        monkeypatch.setattr(settings,"outbound_mode","live")
        monkeypatch.setattr(settings,"chatwoot_write_enabled",True)
    client,csrf=authenticated;h={'X-CSRF-Token':csrf}
    with session_factory() as db:
        db.add(InboxBinding(id=1,tenant_id=1,chatwoot_inbox_id=128859,name='test'));db.flush()
        db.add(ConversationState(id=1,tenant_id=1,inbox_binding_id=1,chatwoot_conversation_id=26));db.flush()
        v=version(db);s=session(db,conversation_state_id=1,inbox_binding_id=1,environment='shadow')
        e=enroll_rehearsal(db,s,v);e.status='completed';sid,sopid=s.id,v.sop_id;db.commit()
    url=f'/v1/sops/{sopid}/enroll';payload={'conversation_ids':[26],'environment':'shadow','reenroll':True,'request_key':'same-request'}
    first=client.post(url,json=payload,headers=h);assert first.status_code==200
    assert first.json()["outbound"] is False
    with session_factory() as db:
        s=db.get(AutomationSession,sid);s.messages=[{'content':'must-preserve','created_at':AT}];s.virtual_now=AT;db.commit()
    assert client.post(url,json=payload,headers=h).json()==first.json()
    with session_factory() as db:
        s=db.get(AutomationSession,sid);assert s.messages[0]['content']=='must-preserve' and s.virtual_now==AT
