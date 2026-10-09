from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import sessionmaker

from app.advisor_assignment import AssignmentConfig, Rule, emit, incoming, process_assignment, choose_rule
from app.db import Base
from app.models import (AdvisorAssignment, AdvisorRotation, ConversationState, ChatwootAgent, HandoffTask,
                        Notification, NotificationDelivery, Tenant, InboxBinding, MessageEvent, utcnow)
from app.operations import save_setting, ensure_handoff
from app.config import settings
from live_fixture import setup


def configure(db, rules=None, advisors=None):
    for aid in (11,12):
        if not db.scalar(select(ChatwootAgent).where(ChatwootAgent.chatwoot_agent_id==aid)):
            db.add(ChatwootAgent(tenant_id=1,chatwoot_agent_id=aid,name=f'顾问{aid}',inbox_ids=[128859]))
    value = {'enabled':True,'advisors':advisors or [{'agent_id':11,'accepting':True},{'agent_id':12,'accepting':True}],
             'rules':rules or [Rule(id='leads',name='留资分配',event='handoff.contact',strategy='round_robin',agent_ids=[11,12]).model_dump()]}
    save_setting(db,'advisor_assignment',value)
    db.flush()


def test_rotation_unique_event_and_existing_owner(session_factory,monkeypatch):
    setup(session_factory,monkeypatch)
    with session_factory() as db:
        configure(db)
        c=db.get(ConversationState,1)
        first=emit(db,c,['handoff.contact'],'first')
        assert first.agent_id==11
        assert emit(db,c,['handoff.contact'],'first').id==first.id
        second=emit(db,c,['handoff.contact'],'second')
        assert second.agent_id==11  # Reservation persists before network sync.
        c.assignee_id=12
        assert emit(db,c,['handoff.contact'],'third').agent_id==12
        assert db.get(AdvisorRotation,'1:leads').position==1
        assert db.scalar(select(func.count()).select_from(HandoffTask))==1


def test_stopped_pool_no_random_fallback_and_retry_target_stays(session_factory,monkeypatch):
    setup(session_factory,monkeypatch)
    with session_factory() as db:
        configure(db,advisors=[{'agent_id':11,'accepting':False},{'agent_id':12,'accepting':False}])
        r=emit(db,db.get(ConversationState,1),['handoff.contact'],'empty')
        assert r.status=='unassigned' and r.agent_id is None
        assert db.scalar(select(Notification).where(Notification.event_type=='assignment.unassigned'))


def test_priority_route_numeric_conditions(session_factory,monkeypatch):
    setup(session_factory,monkeypatch)
    with session_factory() as db:
        c=db.get(ConversationState,1)
        rules=[Rule(id='general',name='新消息',event='customer.message',agent_ids=[11],action='assign'),
               Rule(id='days',name='长行程',event='customer.profile',minimum_days=12,priority=10,agent_ids=[12],routes=['nine'])]
        cfg=AssignmentConfig(enabled=True,rules=rules)
        assert choose_rule(cfg,['customer.message','customer.profile'],c,{'route_variant':'nine','trip_days':15}).id=='days'
        assert choose_rule(cfg,['customer.profile'],c,{'route_variant':'nine','trip_days':'15'}) is None
        assert choose_rule(cfg,['customer.profile'],c,{'route_variant':'eleven','trip_days':15}) is None
        assert choose_rule(cfg,['customer.profile'],c,{'route_variant':'nine','trip_days':9}) is None
        people=AssignmentConfig(rules=[Rule(id='people',name='七人以上',event='customer.profile',minimum_people=7,agent_ids=[11])])
        assert choose_rule(people,['customer.profile'],c,{'party_size_range':'7～10位','party_size_min':7})
        assert choose_rule(people,['customer.profile'],c,{'party_size':4,'party_size_min':7}) is None


def test_handoff_typed_event_single_notification_after_sync(session_factory,monkeypatch):
    setup(session_factory,monkeypatch)
    monkeypatch.setattr(settings,'live_reply_inbox_id',128859)
    with session_factory() as db:
        configure(db)
        save_setting(db,'notification_settings',{'enabled':True,'channel':'chatwoot','agent_id':999,'bot_id':7,'event_types':['handoff.created']})
        task=ensure_handoff(db,db.get(ConversationState,1),'contact','客户留下Email')
        assert task.status=='pending'
        assert not db.scalar(select(Notification).where(Notification.event_type=='handoff.created'))
        db.commit()
    class Client:
        owner=None
        calls=0
        def get_conversation(self,_): return {'id':26,'inbox_id':128859,'meta':{'assignee':{'id':self.owner} if self.owner else {}}}
        def list_inbox_agents(self,_): return [{'id':11,'name':'A'},{'id':12,'name':'B'}]
        def assign_conversation(self,cid,aid): self.owner=aid;self.calls+=1
        def close(self): pass
    fake=Client()
    monkeypatch.setattr('app.chatwoot_service.client_for',lambda _:fake)
    assert process_assignment(session_factory)
    assert not process_assignment(session_factory)
    with session_factory() as db:
        r=db.scalar(select(AdvisorAssignment))
        assert r.status=='synced' and fake.calls==1
        n=db.get(Notification,r.notification_id)
        assert n.target_agent_id==11
        assert db.scalar(select(func.count()).select_from(NotificationDelivery))==1
        t=db.scalar(select(HandoffTask))
        assert t.status=='pending' and t.claimed_at is None and t.chatwoot_assignee_id==11


def test_network_timeout_reconciles_same_target_and_respects_manual_owner(session_factory,monkeypatch):
    setup(session_factory,monkeypatch)
    monkeypatch.setattr(settings,'live_reply_inbox_id',128859)
    with session_factory() as db:
        configure(db,rules=[Rule(id='first',name='首次分配',event='customer.first_message',agent_ids=[11],action='assign').model_dump()])
        r=emit(db,db.get(ConversationState,1),['customer.first_message'],'first')
        rid=r.id
        db.commit()
    class Client:
        owner=None
        count=0
        def get_conversation(self,_): return {'id':26,'inbox_id':128859,'meta':{'assignee':{'id':self.owner} if self.owner else {}}}
        def list_inbox_agents(self,_): return [{'id':11},{'id':12}]
        def assign_conversation(self,_,aid): self.owner=aid;self.count+=1;raise TimeoutError('timeout')
        def close(self): pass
    fake=Client()
    monkeypatch.setattr('app.chatwoot_service.client_for',lambda _:fake)
    process_assignment(session_factory)
    with session_factory() as db:
        r=db.get(AdvisorAssignment,rid)
        assert r.agent_id==11 and r.status=='retry'
        r.available_at=utcnow();db.commit()
    fake.owner=12 # Manual reassignment wins before retry.
    process_assignment(session_factory)
    with session_factory() as db:
        r=db.get(AdvisorAssignment,rid)
        assert r.status=='synced' and r.agent_id==12 and fake.count==1
        assert not db.scalar(select(HandoffTask)) # Assignment alone did not stop AI.


def test_incoming_ad_then_actual_response_is_not_two_first_customers(session_factory,monkeypatch):
    setup(session_factory,monkeypatch)
    with session_factory() as db:
        configure(db,rules=[Rule(id='first',name='首条',event='customer.first_message',agent_ids=[11],action='assign').model_dump(),
                            Rule(id='response',name='回应',event='customer.first_response',priority=10,agent_ids=[12],action='notify').model_dump()])
        c=db.get(ConversationState,1);m=db.get(MessageEvent,1)
        assert incoming(db,c,m).event_type=='customer.first_message'
        db.add(MessageEvent(conversation_state_id=1,chatwoot_message_id=101,direction='outgoing',content='幾位？',created_at=utcnow()))
        m2=MessageEvent(conversation_state_id=1,chatwoot_message_id=102,direction='incoming',content='4位',created_at=utcnow())
        db.add(m2);db.flush()
        assert incoming(db,c,m2).event_type=='customer.first_response'
        assert incoming(db,c,m2).event_type=='customer.first_response'
        db.commit()


def test_concurrent_rotation_and_duplicate_webhook(tmp_path):
    engine=create_engine('sqlite:///'+str(tmp_path/'routing.db'),connect_args={'timeout':30})
    Base.metadata.create_all(engine)
    factory=sessionmaker(engine,expire_on_commit=False)
    with factory() as db:
        db.add(Tenant(id=1,name='test'));db.flush()
        db.add(InboxBinding(id=1,tenant_id=1,chatwoot_inbox_id=128859,name='test',channel_type='test'));db.flush()
        for i in range(8):
            db.add(ConversationState(id=i+1,tenant_id=1,inbox_binding_id=1,chatwoot_conversation_id=26+i))
        db.flush()
        configure(db,rules=[Rule(id='rr',name='均分',event='customer.message',strategy='round_robin',agent_ids=[11,12],action='assign').model_dump()]);db.commit()
    def send(i):
        with factory() as db:
            r=emit(db,db.get(ConversationState,i+1),['customer.message'],f'msg:{i}')
            aid=r.agent_id;db.commit();return aid
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(send,list(range(8))*2))
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(AdvisorAssignment))==8
        assert db.scalar(select(func.count()).select_from(AdvisorAssignment).where(AdvisorAssignment.agent_id==11))==4
        assert db.get(AdvisorRotation,'1:rr').position==8
    engine.dispose()


def test_configuration_api_validates_agents_and_round_robin(authenticated,session_factory):
    client,csrf=authenticated
    with session_factory() as db:
        db.add(ChatwootAgent(tenant_id=1,chatwoot_agent_id=11,name='顾问',inbox_ids=[]));db.commit()
    value={'enabled':True,'advisors':[{'agent_id':11,'accepting':True}],
           'rules':[Rule(id='one',name='转人工',event='handoff.created',agent_ids=[11]).model_dump()]}
    assert client.put('/v1/advisor-assignment',json=value,headers={'X-CSRF-Token':csrf}).status_code==200
    assert client.get('/v1/advisor-assignment').json()['config']['enabled'] is True
    value['rules'][0]['agent_ids']=[999]
    assert client.put('/v1/advisor-assignment',json=value,headers={'X-CSRF-Token':csrf}).status_code==422
    assert client.get('/v1/advisor-assignment/records').json()['items']==[]


@pytest.mark.parametrize('field,minimum,actual', [('party_size',7,8),('trip_days',12,15),('party_size_range',7,'7～10位')])
def test_profile_rule_stops_introduction_before_any_delivery(session_factory,monkeypatch,field,minimum,actual):
    from app.automation_models import AutomationRun
    from app.reception_v3 import service
    from app.reception_v3.runtime import Decision
    from test_reception_v3 import create,bundle
    setup(session_factory,monkeypatch)
    with session_factory() as db:
        conditions={'minimum_people' if field.startswith('party_size') else 'minimum_days':minimum}
        configure(db,rules=[Rule(id='threshold',name='人数天数交接',event='customer.profile',agent_ids=[11],**conditions).model_dump()])
        row=create(db,monkeypatch)
        row.environment='live';row.conversation_state_id=1;row.inbox_binding_id=1
        run=AutomationRun(session_id=row.id,module='reply',generation=row.generation,idempotency_key='profile-test',status='running',input_snapshot={'event':'customer_message'})
        db.add(run);db.flush()
        profile={field:actual}
        if field=='party_size_range':
            row.memory={'party_size':4}
            profile['party_size_min']=7
        decision=Decision(route_variant='nine',profile=profile,start_introduction=True).model_dump()
        service.apply_decision(db,row,run,bundle(),decision)
        assert row.memory[field]==actual
        if field=='party_size_range':
            assert 'party_size' not in row.memory
        assert row.controls['human'] and row.controls['simulation']['status']=='stopped'
        assert not any(m.get('status')=='draft' for m in row.messages)
        assert db.get(ConversationState,1).effective_ai_state=='HUMAN_HANDOFF'
        assert db.scalar(select(HandoffTask)).status=='pending'
        assert db.scalar(select(AdvisorAssignment)).agent_id==11


def test_overdue_assignment_is_cancelled_after_advisor_accepts(session_factory,monkeypatch):
    setup(session_factory,monkeypatch)
    with session_factory() as db:
        configure(db,rules=[Rule(id='late',name='超时提醒',event='handoff.overdue',agent_ids=[11],action='notify').model_dump()])
        c=db.get(ConversationState,1)
        task=ensure_handoff(db,c,'knowledge',dispatch=False)
        task.sla_due_at=(datetime.now(timezone.utc)-timedelta(minutes=5)).isoformat()
        emit(db,c,['handoff.overdue'],f'overdue:{task.id}')
        task.status='claimed';task.claimed_at=utcnow();db.commit()
    monkeypatch.setattr('app.chatwoot_service.client_for',lambda _:pytest.fail('No request after claim'))
    assert process_assignment(session_factory)
    with session_factory() as db:
        assert db.scalar(select(AdvisorAssignment)).status=='cancelled'


def test_notification_uses_routed_advisor_and_private_bot_message(session_factory,monkeypatch):
    from app.notification_dispatch import process_notification_delivery
    from app.operations import create_notification
    setup(session_factory,monkeypatch)
    monkeypatch.setattr(settings,'live_reply_inbox_id',128859)
    with session_factory() as db:
        save_setting(db,'notification_settings',{'enabled':True,'channel':'chatwoot','agent_id':999,'bot_id':7})
        note=create_notification(db,'assignment.routed','留资分配','客户留下联系方式',26)
        note.target_agent_id=12
        db.add(NotificationDelivery(notification_id=note.id));db.commit()
    calls=[]
    class Client:
        def get_conversation(self,_): return {'inbox_id':128859,'meta':{}}
        def list_inbox_agents(self,_): return [{'id':11},{'id':12}]
        def create_private_notification(self,cid,content,bot):
            calls.append((cid,content,bot));return {'id':55,'private':True,'sender':{'type':'agent_bot','id':7}}
        def close(self): pass
    monkeypatch.setattr('app.notification_dispatch.client_for',lambda _:Client())
    assert process_notification_delivery(session_factory)
    assert len(calls)==1 and 'mention://user/12/' in calls[0][1]
    with session_factory() as db:
        assert db.scalar(select(NotificationDelivery)).status=='delivered'


def test_notify_selected_advisor_preserves_different_conversation_owner(session_factory,monkeypatch):
    from app.notification_dispatch import process_notification_delivery
    setup(session_factory,monkeypatch)
    monkeypatch.setattr(settings,'live_reply_inbox_id',128859)
    with session_factory() as db:
        configure(db,rules=[Rule(id='notify',name='通知指定顾问',event='customer.message',agent_ids=[12],action='notify').model_dump()])
        save_setting(db,'notification_settings',{'enabled':True,'channel':'chatwoot','agent_id':11,'bot_id':7})
        c=db.get(ConversationState,1);c.assignee_id=11
        assert emit(db,c,['customer.message'],'notify').agent_id==12
        db.commit()
    calls=[]
    class Client:
        def get_conversation(self,_): return {'inbox_id':128859,'meta':{'assignee':{'id':11}}}
        def list_inbox_agents(self,_): return [{'id':11},{'id':12}]
        def assign_conversation(self,*_): pytest.fail('Notify must not change ownership')
        def create_private_notification(self,cid,content,bot):
            calls.append(content);return {'id':55,'private':True,'sender':{'type':'agent_bot','id':7}}
        def close(self): pass
    fake=Client()
    monkeypatch.setattr('app.chatwoot_service.client_for',lambda _:fake)
    monkeypatch.setattr('app.notification_dispatch.client_for',lambda _:fake)
    assert process_assignment(session_factory)
    assert process_notification_delivery(session_factory)
    assert 'mention://user/12/' in calls[0]
    with session_factory() as db:
        assert db.get(ConversationState,1).assignee_id==11
        assert not db.scalar(select(HandoffTask))
