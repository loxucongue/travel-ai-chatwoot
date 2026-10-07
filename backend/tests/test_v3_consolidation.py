from copy import deepcopy
from threading import Event
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from app.db import Base
from app.automation_models import AutomationSession, AutomationRun
from app.models import User, Tenant, ConversationState, MessageEvent, OutboundMessage, HandoffTask, utcnow
from app.operations import save_setting
from app.reception_config import get_reception_configuration
from app.reception_v3 import service, skills, worker, live
from test_reception_v3 import bundle, answer, create, step, delivered
from live_fixture import setup


def test_saved_configuration_reaches_v3_skill(authenticated, session_factory):
    client, csrf = authenticated
    payload={'reply': {'goal':'以顾问接手为目标','tone_guidance':'优先台湾原话'},
             'silence': {'intervals_minutes':[3,5]},
             'handoff': {'large_group_enabled':True,'large_group_minimum':10},
             'routing': {'enabled_route_variants':['peach_9d_2027']},
             'common_scripts':[{'id':'hello','name':'招呼','scenario':'新客','text':'您好呀～'}]}
    response=client.patch('/v1/automation/reception-config',json=payload,headers={'X-CSRF-Token':csrf})
    assert response.status_code==200,response.text
    with session_factory() as db:
        compiled=skills.compile_skills(db)
        assert set(compiled['routes'])=={'peach_9d_2027'}
        body=skills.SkillRegistry(compiled).load('tibet-reception')['instructions']
        assert all(text in body for text in ['以顾问接手为目标','优先台湾原话','您好呀～','10','[3, 5]'])
        assert 'v2_intervals_minutes' not in compiled['silence']


def test_old_displayed_interval_is_migrated_once(session_factory):
    with session_factory() as db:
        save_setting(db,'route_reception_config',{'schema_version':6,
            'silence':{'intervals_minutes':[1,2,3], 'v2_intervals_minutes':[3,5]}})
        config=get_reception_configuration(db)
        assert config['silence']['intervals_minutes']==[3,5]
        assert 'business_rules' not in config and 'stage_journey' not in config


def test_failed_turn_keeps_customer_questions_and_new_message_recovers(session_factory,monkeypatch):
    with session_factory() as db:
        row=create(db,monkeypatch)
        monkeypatch.setattr(service,'run_agent',lambda c: (_ for _ in ()).throw(TimeoutError('timeout')))
        step(db,row);step(db,row)
        value=service.state(row)
        assert value['pending_event']=='customer_message' and value['buffered_questions']
        assert value['attempts']==1
        service.add_message(db,row,'还有小费呢','new');db.commit()
        assert 'attempts' not in service.state(row)
        monkeypatch.setattr(service,'run_agent',lambda c:answer(messages=[{'text':'保留原文回答'}],stop_followup=True))
        step(db,row);step(db,row)
        assert '保留原文回答' in delivered(row)


def test_new_input_during_model_never_gets_cleared(session_factory,monkeypatch):
    with session_factory() as db:
        row=create(db,monkeypatch)
        def model(context):
            with session_factory() as other:
                current=other.get(AutomationSession,row.id)
                service.add_message(other,current,'我改成四位','newest')
                other.commit()
            return answer(messages=[{'text':'过期答案'}])
        monkeypatch.setattr(service,'run_agent',model)
        step(db,row);step(db,row)
        db.refresh(row)
        assert 'newest' in service.state(row)['buffered_questions']
        assert not any(m.get('content')=='过期答案' for m in row.messages)
        assert db.scalar(select(AutomationRun)).status=='cancelled'


def test_slow_model_does_not_block_other_timed_delivery(tmp_path,monkeypatch):
    engine=create_engine(f'sqlite:///{tmp_path / "parallel.db"}',connect_args={'check_same_thread':False})
    Base.metadata.create_all(engine)
    factory=sessionmaker(bind=engine,expire_on_commit=False)
    monkeypatch.setattr(worker,'SessionLocal',factory)
    entered,release=Event(),Event()
    def model(context):
        entered.set()
        assert release.wait(5)
        return answer(action='wait',stop_followup=True)
    monkeypatch.setattr(service,'run_agent',model)
    with factory() as db:
        db.add(Tenant(id=1,name='test'));db.add(User(id=1,email='test@test.com',display_name='test',password_hash='test'));db.commit()
        first=create(db,monkeypatch)
        service.advance(first);service.advance(first,service.later(first.controls['simulation']['last_wall_at'],3));db.commit()
        second=create(db,monkeypatch)
        first_id,second_id=first.id,second.id
    with ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(worker.model_turn,first_id,'playground')
        assert entered.wait(3)
        worker.delivery_turn(second_id,'playground')
        with factory() as db:
            assert delivered(db.get(AutomationSession,second_id))==['配置开场一']
        assert not future.done()
        release.set();future.result(5)
    engine.dispose()


def test_live_uses_same_opening_and_persistent_delivery(session_factory,monkeypatch):
    setup(session_factory,monkeypatch)
    monkeypatch.setattr(service,'compile_skills',lambda db:deepcopy(bundle()))
    class Client:
        sent=[]
        def get_conversation(self,id):return {'id':id,'can_reply':True}
        def get_conversation_labels(self,id):return {'payload':['ai']}
        def create_text_message(self,id,text):self.sent.append(text);return {'id':200+len(self.sent)}
        def close(self):pass
    client=Client();monkeypatch.setattr(live,'client_for',lambda connection:client)
    with session_factory() as db:
        live.accept(db,db.get(ConversationState,1),db.get(MessageEvent,1));db.commit()
        row=live.session_for(db,1)
        live.deliver_one(db,row)
        assert client.sent==['配置开场一']
        assert db.scalar(select(OutboundMessage)).status=='submitted'
        live.deliver_one(db,row)
        assert len(client.sent)==1
        row.virtual_now=service.later(row.virtual_now,3);db.commit()
        live.deliver_one(db,row)
        assert client.sent==['配置开场一','配置开场二']


def test_unknown_live_submit_is_not_repeated(session_factory,monkeypatch):
    setup(session_factory,monkeypatch)
    monkeypatch.setattr(service,'compile_skills',lambda db:deepcopy(bundle()))
    class Client:
        count=0
        def get_conversation(self,id):return {'can_reply':True}
        def get_conversation_labels(self,id):return {'payload':['ai']}
        def create_text_message(self,id,text):self.count+=1;raise TimeoutError()
        def close(self):pass
    client=Client();monkeypatch.setattr(live,'client_for',lambda c:client)
    with session_factory() as db:
        live.accept(db,db.get(ConversationState,1),db.get(MessageEvent,1));db.commit()
        row=live.session_for(db,1)
        with pytest.raises(TimeoutError):live.deliver_one(db,row)
        live.deliver_one(db,row)
        assert client.count==1
        assert db.scalar(select(OutboundMessage)).status=='submission_unknown'
        assert db.scalar(select(HandoffTask)) is not None


@pytest.mark.parametrize('receipt', ['sent', 'delivered', 'read', 'failed'])
def test_live_callback_before_http_response_reuses_message_and_receipt(session_factory, monkeypatch, receipt):
    setup(session_factory, monkeypatch)
    monkeypatch.setattr(service, 'compile_skills', lambda db: deepcopy(bundle()))
    class Client:
        sent = []
        def get_conversation(self, cid): return {'can_reply': True}
        def get_conversation_labels(self, cid): return {'payload': ['ai']}
        def create_text_message(self, cid, text):
            self.sent.append(text)
            remote_id = 200 + len(self.sent)
            with session_factory() as webhook:
                webhook.add(MessageEvent(conversation_state_id=1, chatwoot_message_id=remote_id,
                    direction='outgoing', content=text, status=receipt, attribution='inferred_human',
                    sender_id=7, content_attributes={'callback': True}))
                webhook.commit()
            return {'id': remote_id}
        def close(self): pass
    client = Client()
    monkeypatch.setattr(live, 'client_for', lambda connection: client)
    with session_factory() as db:
        live.accept(db, db.get(ConversationState, 1), db.get(MessageEvent, 1)); db.commit()
        row = live.session_for(db, 1)
        live.deliver_one(db, row)
        out = db.scalar(select(OutboundMessage))
        messages = db.scalars(select(MessageEvent).where(MessageEvent.chatwoot_message_id == 201)).all()
        assert out.chatwoot_message_id == 201 and out.status == receipt
        assert len(messages) == 1 and messages[0].attribution == 'ai'
        assert messages[0].sender_id == 7 and messages[0].content_attributes == {'callback': True}
        assert messages[0].status == receipt
        assert bool(db.scalar(select(HandoffTask))) == (receipt == 'failed')
        row.virtual_now = service.later(row.virtual_now, 3); db.commit()
        live.deliver_one(db, row)
        assert client.sent == (['配置开场一'] if receipt == 'failed' else ['配置开场一', '配置开场二'])


def test_intro_finishes_while_question_is_being_classified(session_factory, monkeypatch):
    with session_factory() as db:
        row = create(db, monkeypatch)
        service.advance(row)
        service.advance(row, service.later(row.controls['simulation']['last_wall_at'], 3))
        value = service.state(row)
        value.update(delivery_kind='introduction', buffered_questions=['entry-question'], pending_event='customer_message')
        service.save(row, value)
        db.commit()
        def model(context):
            with session_factory() as other:
                current = other.get(AutomationSession, row.id)
                latest = service.state(current)
                latest.update(delivery_kind=None, pending_event='introduction_completed')
                service.save(current, latest)
                other.commit()
            return answer(action='queue', profile={'party_size': 2})
        monkeypatch.setattr(service, 'run_agent', model)
        service.tick(db, session_id=row.id, advance_clock=False)
        db.refresh(row)
        assert service.state(row)['pending_event'] == 'introduction_completed'
        assert service.state(row)['buffered_questions'] == ['entry-question']
        assert row.memory['party_size'] == 2
