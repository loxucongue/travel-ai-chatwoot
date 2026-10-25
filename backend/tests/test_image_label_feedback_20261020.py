from copy import deepcopy
from datetime import timedelta
import httpx
import pytest
from sqlalchemy import select
from app.models import ConversationState, ConversationLabelSync, HandoffTask, MessageEvent, NotificationDelivery, utcnow
from app.reception_v3 import live, service, runtime
from app.reception_v3.customer_images import attachment_metadata, select_images
from app.reception_v3 import customer_images
from app import conversation_labels as labels
from app.operations import save_setting
from app.runtime_settings import dt
from live_fixture import setup
from test_live_audit_fixes import event, session
from test_reception_v3_skills import context, model, final
from test_human_return_20261016 import prepared, arm, sync
from test_conversation_controls import FakeChatwoot

IMAGE={'id':1,'file_type':'image','file_size':1678,'data_url':'https://app.chatwoot.com/rails/active_storage/blobs/redirect/test/thumb.png'}
INLINE='data:image/jpeg;base64,dGVzdA=='

@pytest.fixture(autouse=True)
def mock_image_fetch(monkeypatch):
    monkeypatch.setattr(customer_images.settings,'customer_image_vision_enabled',True)
    monkeypatch.setattr(customer_images,'download_image',lambda url,deadline:INLINE)

def test_disabled_vision_never_downloads_images(monkeypatch):
    monkeypatch.setattr(customer_images.settings,'customer_image_vision_enabled',False)
    monkeypatch.setattr(customer_images,'download_image',lambda *a:pytest.fail('disabled vision fetched an image'))
    assert customer_images.readable_images(image_context())=={}

def image_context():
    c=context()
    c['messages']=[{'id':'image','direction':'incoming','content':'','content_type':'image','attachments':[IMAGE]}]
    c['buffered_questions']=deepcopy(c['messages'])
    return c

def test_actual_image_in_model_input_without_duplicate_signed_url_in_context(monkeypatch):
    requests=model(monkeypatch,[final(action='wait')])
    _,logs,_=runtime.run_agent(image_context())
    payload=requests[0]
    assert payload['model']=='deepseek-flash'
    assert payload['messages'][1]['content'][1]['image_url']['url']==INLINE
    assert IMAGE['data_url'] not in payload['messages'][-1]['content']
    assert any(x.get('vision_image_count')==1 for x in logs)

@pytest.mark.parametrize('url', ['http://app.chatwoot.com/rails/active_storage/x','https://evil.com/a.png',
 'https://app.chatwoot.com.evil.com/rails/active_storage/a','https://app.chatwoot.com/admin',
 'https://user:pass@app.chatwoot.com/rails/active_storage/a','https://127.0.0.1/rails/active_storage/a'])
def test_untrusted_attachment_urls_not_sent_to_model(url):
    assert 'data_url' not in attachment_metadata([{**IMAGE,'data_url':url}])[0]

def test_only_current_turn_images_are_read_and_followups_never_reload_images():
    c=image_context();c['messages'].insert(0,{'id':'old','direction':'incoming','attachments':[{**IMAGE,'data_url':IMAGE['data_url']+'old'}]})
    assert list(select_images(c))==['image']
    c['event']='silence_due'
    assert select_images(c)=={}

def test_image_and_following_route_are_both_in_same_request(monkeypatch):
    c=image_context();c['messages'].append({'id':'route','direction':'incoming','content':'西藏林芝桃花'})
    c['buffered_questions']=deepcopy(c['messages'])
    requests=model(monkeypatch,[final(messages=[{'text':'您想了解桃花9日還是11日呢？'}])])
    runtime.run_agent(c)
    assert requests[0]['messages'][1]['content'][1]['type']=='image_url'
    assert requests[0]['messages'][2]['content']=='西藏林芝桃花'

def test_rejected_image_falls_back_to_available_text_instead_of_transport_handoff(monkeypatch):
    requests=[]
    def post(payload,timeout):
        requests.append(deepcopy(payload))
        req=httpx.Request('POST','https://test.invalid')
        if len(requests)==1:
            response=httpx.Response(400,request=req)
            raise httpx.HTTPStatusError('bad image',request=req,response=response)
        return httpx.Response(200,request=req,json={'choices':[{'message':final(action='wait')}],'usage':{}})
    monkeypatch.setattr(runtime,'_post_with_deadline',post)
    decision,logs,_=runtime.run_agent(image_context())
    assert decision['action']=='wait' and len(requests)==2
    assert all(isinstance(m['content'],str) for m in requests[1]['messages'])
    assert logs[0]['status']=='unavailable'

@pytest.mark.parametrize('two_steps',[False,True])
def test_operator_returns_pending_question_cancels_old_handoff_and_notifications(session_factory,monkeypatch,two_steps):
    setup(session_factory,monkeypatch)
    with session_factory() as db:
        save_setting(db,'notification_settings',{'enabled':True,'channel':'chatwoot','event_types':['handoff.created']})
        row=session(db)
        event(db,'conversation_updated',['人工接管'],10)
        task=db.scalar(select(HandoffTask))
        event(db,'message_created',['人工接管'],11,112,'incoming')
        if two_steps:event(db,'conversation_updated',[],12)
        event(db,'conversation_updated',['ai','以開口'],13)
        generation=row.generation
        assert task.status=='cancelled'
        assert live.permitted(db,db.get(ConversationState,1))
        assert service.state(row)['pending_event']=='customer_message'
        assert 'cw-112' in service.state(row)['buffered_questions']
        assert dt(row.due_at)<=dt(utcnow())
        assert not row.controls['human']
        assert all(d.status=='cancelled' for d in db.scalars(select(NotificationDelivery)))
        event(db,'conversation_updated',['ai','以開口'],14)
        assert row.generation==generation
        assert db.get(ConversationLabelSync,1).target=='ai'
    assert not labels.process_label_sync(session_factory)

def test_poll_detects_operator_handback_before_old_human_write(session_factory,monkeypatch):
    prepared(session_factory,monkeypatch)
    with session_factory() as db:
        conv=db.get(ConversationState,1);conv.labels=['人工接管'];conv.chatwoot_control_updated_at=10;db.commit()
    fake=FakeChatwoot();fake.current_labels=['ai','以開口']
    original=fake.get_conversation
    monkeypatch.setattr(fake,'get_conversation',lambda cid:{**original(cid),'updated_at':30})
    monkeypatch.setattr(labels,'client_for',lambda _:fake)
    assert labels.process_label_sync(session_factory)
    assert fake.current_labels==['ai','以開口']
    assert not any(c[0]=='labels' for c in fake.calls)
    with session_factory() as db:
        assert live.permitted(db,db.get(ConversationState,1))
        assert db.get(ConversationLabelSync,1).status=='synced'

def test_manual_return_replaces_waiting_advisor_timer(session_factory,monkeypatch):
    task=prepared(session_factory,monkeypatch);arm(session_factory,task);sync(session_factory,monkeypatch)
    with session_factory() as db:
        event(db,'conversation_updated',['ai','AI跟进'],30)
        row=live.session_for(db,1)
        assert row.controls['simulation']['status']=='running'
        assert not row.controls['human_followup']['enabled']
        assert service.state(row).get('next_check_at') is not None
        assert db.get(ConversationLabelSync,1).target=='ai'
        assert not service.state(row).get('pending_event') # Advisor already answered.

def test_return_does_not_send_outside_customer_window(session_factory,monkeypatch):
    setup(session_factory,monkeypatch)
    with session_factory() as db:
        conv=db.get(ConversationState,1);conv.labels=['人工接管']
        db.get(MessageEvent,1).created_at=(dt(utcnow())-timedelta(days=8)).isoformat()
        db.commit();event(db,'conversation_updated',['ai'],20)
        assert not service.state(live.session_for(db,1)).get('pending_event')

def test_label_sync_retry_keeps_generated_reply_queued(session_factory,monkeypatch):
    setup(session_factory,monkeypatch)
    with session_factory() as db:
        row=session(db);conv=db.get(ConversationState,1)
        conv.labels=['人工接管'];db.commit()
        event(db,'conversation_updated',['ai','AI跟进'],30)
        assert db.get(ConversationLabelSync,1).status=='pending'
        row.messages=[*row.messages,{'id':'answer','direction':'outgoing','status':'draft','content':'回答'}]
        service.save(row,{**service.state(row),'delivery_due_at':row.virtual_now})
        live.deliver_one(db,row)
        assert row.controls['simulation']['status']=='running'
        assert row.messages[-1]['status']=='draft'

def test_business_wording_changes_exact_screenshot_phrase_and_is_idempotent():
    from scripts.configure_wording_20261020 import update_package
    original={'package_version':'v1','text':'全酒店都有供氧。房間數較少，人數也是有限定的喔！'}
    result=update_package(original)
    assert result['text']=='所有酒店都有供氧。房間數較少，訂完就沒有了喔！'
    assert update_package(result)==result and original['package_version']=='v1'

def test_manual_label_return_immediately_generates_answer_and_preserves_advisor_context(session_factory,monkeypatch):
    from test_reception_v3 import bundle,answer
    prepared(session_factory,monkeypatch)
    monkeypatch.setattr(service,'compile_skills',lambda db:bundle())
    monkeypatch.setattr(service,'active_web_facts',lambda *a,**k:([],'test'))
    monkeypatch.setattr(service,'candidate_materials',lambda *a:[])
    calls=[]
    def respond(c):
        calls.append(c)
        return answer(messages=[{'text':'所有酒店都有供氧喔。'}])
    monkeypatch.setattr(service,'run_agent',respond)
    with session_factory() as db:
        conv=db.get(ConversationState,1);conv.labels=['人工接管'];db.commit()
        event(db,'message_created',['人工接管'],11,112,'incoming')
        event(db,'conversation_updated',['ai'],12)
        row=live.session_for(db,1)
        service.tick(db,session_id=row.id,advance_clock=False,environment='live')
        assert len(calls)==1 and calls[0]['event']=='customer_message'
        assert calls[0]['buffered_questions'][0]['id']=='cw-112'
        assert any(m['content'].startswith('這個安排已經確認') for m in calls[0]['messages'])
        assert any(m.get('status')=='draft' and m.get('content')=='所有酒店都有供氧喔。' for m in row.messages)

def test_image_arriving_before_route_does_not_drop_later_route(session_factory,monkeypatch):
    from test_reception_v3 import create,answer
    calls=[]
    with session_factory() as db:
        row=create(db,monkeypatch)
        row.messages=[]
        row.controls={**row.controls,'v3':{'opening_started':True}}
        service.add_message(db,row,'','thumb','image',attachments=[IMAGE]);db.commit()
        def respond(c):
            calls.append(c)
            if len(calls)==1:
                service.add_message(db,row,'西藏林芝桃花','route');db.commit()
                return answer(action='wait')
            return answer(messages=[{'text':'您想了解桃花9日還是11日呢？'}])
        monkeypatch.setattr(service,'run_agent',respond)
        service.tick(db,session_id=row.id,advance_clock=False)
        assert service.state(row)['pending_event']=='customer_message'
        service.tick(db,session_id=row.id,advance_clock=False)
        assert any(m.get('content')=='西藏林芝桃花' for m in calls[-1]['messages'])
        assert not service.state(row).get('handoff')
