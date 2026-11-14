from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from app import webhook_recovery as r

def msg(i=1, kind=0, at=None, **kw):
    return dict(id=i, message_type=kind, created_at=at or r.CUTOFF+60, private=False, **kw)

def remote(**kw):
    return dict(status='open', can_reply=True, labels=[], **kw)

@pytest.mark.parametrize('messages,known,expected', [
    ([msg()], set(), 'candidate'),
    ([msg(),msg(2,1)],{2},'already_answered'),
    ([msg(1,1),msg(2)],set(),'human_or_unknown_sender'),
    ([msg(1,1),msg(2)],{1},'candidate'),
    ([msg(at=r.CUTOFF-1)],set(),'outside_recovery_window'),
    ([],set(),'no_public_message'),
])
def test_candidate(messages,known,expected):
    assert r.candidate(remote(),messages,known,set(),r.CUTOFF+3600)==expected

def test_blocked_and_closed():
    for field,value,expected in [('labels',['human'],'blocked_label'),('status','resolved','closed_or_unreplyable'),('can_reply',False,'closed_or_unreplyable')]:
        c=remote();c[field]=value
        assert r.candidate(c,[msg()],set(),{'human'},r.CUTOFF+3600)==expected

def test_private_note_is_not_customer_question():
    m=msg(2,1);m['private']=True
    assert r.candidate(remote(),[msg(),m],set(),set(),r.CUTOFF+3600)=='candidate'

@pytest.mark.parametrize('messages,known,seen,allowed,cancelled',[
    ([msg()],[],[{'id':'cw-1'}],True,False),
    ([msg(),msg(2)],[],[{'id':'cw-1'}],False,False),
    ([msg(),msg(2,1)],[],[{'id':'cw-1'}],False,True),
    ([msg(),msg(2,1)],[2],[{'id':'cw-1'}],True,False),
])
def test_presend_guard(tmp_path,monkeypatch,messages,known,seen,allowed,cancelled):
    marker=tmp_path/'enabled';marker.touch();monkeypatch.setattr(r,'MARKER',marker)
    cancel=Mock();monkeypatch.setattr(r.live,'cancel',cancel)
    db=Mock();db.scalars.return_value=known
    client=Mock();client.get_messages.return_value={'payload':messages}
    assert r.before_send(db,client,SimpleNamespace(id=1,chatwoot_conversation_id=909),SimpleNamespace(messages=seen)) is allowed
    assert cancel.called is cancelled

def test_presend_failure_closed(tmp_path,monkeypatch):
    marker=tmp_path/'enabled';marker.touch();monkeypatch.setattr(r,'MARKER',marker)
    client=Mock();client.get_messages.side_effect=TimeoutError()
    assert not r.before_send(Mock(),client,SimpleNamespace(id=1,chatwoot_conversation_id=1),Mock())

def test_presend_disabled_no_api(tmp_path,monkeypatch):
    monkeypatch.setattr(r,'MARKER',tmp_path/'absent');client=Mock()
    assert r.before_send(Mock(),client,Mock(),Mock())
    client.get_messages.assert_not_called()

def test_recovery_transaction_keeps_times_and_deduplicates(session_factory,monkeypatch):
    from app.models import ChatwootConnection, InboxBinding, MessageEvent, WebhookEvent
    from sqlalchemy import select
    monkeypatch.setattr(r,'SessionLocal',session_factory)
    monkeypatch.setattr(r.time,'time',lambda:r.CUTOFF+120)
    monkeypatch.setattr(r,'blocking_labels',lambda db:set())
    monkeypatch.setattr(r.live,'enable_new_customer',lambda *a:None)
    monkeypatch.setattr(r.live,'permitted',lambda *a:True)
    from app import engagement_labels
    monkeypatch.setattr(engagement_labels,'observe_reply',lambda *a:None)
    accepted=[];monkeypatch.setattr(r.live,'accept',lambda db,state,m:accepted.append(m.chatwoot_message_id))
    with session_factory() as db:
        db.add(ChatwootConnection(tenant_id=1,account_id=180474,connection_key='test'))
        db.add(InboxBinding(tenant_id=1,chatwoot_inbox_id=7,name='test'))
        db.commit()
    c=remote(id=909,inbox_id=7,meta={'sender':{'id':9,'name':'test'}})
    m=msg();m['content']='two guests'
    client=Mock();client.get_conversation.return_value=c
    client.get_contact_labels.return_value={'payload':[]}
    client.get_messages.return_value={'payload':[m]}
    monkeypatch.setattr(r,'fetch_message_history',lambda *a,**kw:([m],{}))
    assert r.recover_one(client,909,True)['result']=='scheduled'
    assert r.recover_one(client,909,True)['result']=='already_ingested'
    assert accepted==[1]
    with session_factory() as db:
        stored=db.scalar(select(MessageEvent))
        assert stored.created_at==r.message_timestamp(m['created_at'])
        assert stored.direction=='incoming'
        assert db.scalar(select(WebhookEvent)).status=='live_observed'
