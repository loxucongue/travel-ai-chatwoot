from datetime import timedelta
from sqlalchemy import select
import pytest
import respx

from app.config import settings
from app.models import ConversationState, MessageEvent, HandoffTask, NotificationDelivery, utcnow
from app.operations import save_setting
from app.runtime_settings import dt, iso
from app.reception_v3 import live
from live_fixture import setup
from test_runtime_reliability import notification_setup, mock_chatwoot
from app import notification_dispatch


def test_auto_entry_confirmed_revision_preserves_opening_until_real_operator_change(session_factory,monkeypatch):
    from app.reception_v3 import service
    from test_reception_v3 import bundle
    from test_live_audit_fixes import event
    setup(session_factory,monkeypatch)
    monkeypatch.setattr(service,'compile_skills',lambda db:bundle())
    labels=[]
    class Client:
        def list_labels(self):return {'payload':[{'title':'ai'}]}
        def get_conversation(self,cid):
            return {'id':cid,'inbox_id':128859,'created_at':utcnow(),'can_reply':True,
                    'updated_at':20 if labels else 10,'labels':list(labels)}
        def get_conversation_labels(self,cid):return {'payload':list(labels)}
        def set_conversation_labels(self,cid,value):labels[:]=value
        def close(self):pass
    monkeypatch.setattr(live,'client_for',lambda _:Client())
    with session_factory() as db:
        conv=db.get(ConversationState,1)
        conv.labels=[];conv.ai_mode='disabled';conv.ai_label_present=False
        cutoff=iso(dt(utcnow())-timedelta(minutes=1))
        save_setting(db,'live_reply',{'armed_at':cutoff,'auto_new_customers_since':cutoff})
        msg=db.get(MessageEvent,1);msg.content='1'
        live.enable_new_customer(db,conv,msg)
        live.accept(db,conv,msg)
        db.commit()
        row=live.session_for(db,conv.id)
        drafts=[m['id'] for m in row.messages if m.get('status')=='draft']
        assert len(drafts)==2 and conv.chatwoot_control_updated_at==20
        event(db,'conversation_updated',[],10)
        event(db,'conversation_updated',['ai'],20)
        assert row.controls['simulation']['status']=='running'
        assert [m['id'] for m in row.messages if m.get('status')=='draft']==drafts
        event(db,'conversation_updated',[],21)
        assert row.controls['simulation']['status']=='stopped'
        assert all(m.get('status')!='draft' for m in row.messages)


@pytest.mark.parametrize('old,blocked,history,allowed', [
    (False,False,False,True),(True,False,False,False),
    (False,True,False,False),(False,False,True,False)])
def test_new_customer_auto_entry_does_not_resume_old_or_stopped_chat(session_factory, monkeypatch, old, blocked, history, allowed):
    setup(session_factory, monkeypatch)
    with session_factory() as db:
        row=db.get(ConversationState,1)
        row.labels=[];row.ai_mode='disabled';row.ai_label_present=False
        cutoff=iso(dt(utcnow())-timedelta(minutes=1))
        save_setting(db,'live_reply',{'armed_at':cutoff,'auto_new_customers_since':cutoff})
        if history:
            db.add(MessageEvent(conversation_state_id=1,chatwoot_message_id=99,direction='outgoing',content='human'))
        db.commit()
        labels=['人工接管'] if blocked else []
        catalog=[]
        class Client:
            def list_labels(self):return {'payload':catalog}
            def create_label(self,title,*args):
                item={'id':1,'title':title};catalog.append(item);return item
            def get_conversation(self, cid):
                return {'id':cid,'inbox_id':128859,'created_at':iso(dt(cutoff)+timedelta(seconds=-1 if old else 1)),'can_reply':True}
            def get_conversation_labels(self,cid):return {'payload':labels}
            def set_conversation_labels(self,cid,value):labels[:]=value
            def close(self):pass
        monkeypatch.setattr(live,'client_for',lambda connection:Client())
        live.enable_new_customer(db,row,db.get(MessageEvent,1))
        assert ('ai' in labels)==allowed
        assert live.permitted(db,row)==allowed
        assert not db.scalar(select(HandoffTask))
        assert bool(catalog)==allowed


@pytest.mark.parametrize('failure', ['create_failed', 'not_confirmed'])
def test_new_customer_catalog_failure_hands_off_before_enabling_ai(session_factory, monkeypatch, failure):
    setup(session_factory, monkeypatch)
    with session_factory() as db:
        row=db.get(ConversationState,1)
        row.labels=[];row.ai_mode='disabled';row.ai_label_present=False
        cutoff=iso(dt(utcnow())-timedelta(minutes=1))
        save_setting(db,'live_reply',{'armed_at':cutoff,'auto_new_customers_since':cutoff})
        db.commit()
        writes=[]
        class Client:
            def get_conversation(self,cid):
                return {'id':cid,'inbox_id':128859,'created_at':utcnow(),'can_reply':True}
            def get_conversation_labels(self,cid):return {'payload':[]}
            def list_labels(self):return {'payload':[]}
            def create_label(self,*args):
                if failure=='create_failed':raise RuntimeError('catalog_unavailable')
                return {'id':1,'title':'ai'}
            def set_conversation_labels(self,cid,value):writes.append(value)
            def close(self):pass
        monkeypatch.setattr(live,'client_for',lambda _:Client())
        live.enable_new_customer(db,row,db.get(MessageEvent,1))
        assert not writes and not live.permitted(db,row)
        assert db.scalar(select(HandoffTask)).reason_code=='new_customer_activation_failed'


@pytest.mark.parametrize('assignee,assignments', [(None,1),(9,0),(7,0)])
def test_handoff_assigns_configured_advisor_and_mentions_same_person(session_factory,monkeypatch,assignee,assignments):
    notification_setup(session_factory,monkeypatch)
    with session_factory() as db:
        save_setting(db,'notification_settings',{'enabled':True,'channel':'chatwoot','agent_id':7,'bot_id':8,'assign_on_handoff':True})
        db.commit()
    with respx.mock() as router:
        note=mock_chatwoot(router)
        router.get('https://chatwoot.invalid/api/v1/accounts/180474/inbox_members/128859').respond(200,json={'payload':[{'id':7},{'id':9}]})
        router.get('https://chatwoot.invalid/api/v1/accounts/180474/conversations/26').respond(200,json={
            'id':26,'inbox_id':128859,'meta':{'assignee':{'id':assignee} if assignee else None}})
        assignment=(router.post('https://chatwoot.invalid/api/v1/accounts/180474/conversations/26/assignments')
            .respond(200,json={'id':7})) if assignments else None
        assert notification_dispatch.process_notification_delivery(session_factory)
        assert (assignment.call_count if assignment else 0)==assignments and note.call_count==1
        import json
        assert f'mention://user/{assignee or 7}/' in json.loads(note.calls[0].request.content)['content']
    with session_factory() as db:
        assert db.scalar(select(NotificationDelivery)).status=='delivered'
