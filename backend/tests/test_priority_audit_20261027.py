from copy import deepcopy
from datetime import timedelta

import pytest
from sqlalchemy import select

from app import delivery_status as receipts
from app.automation_models import AutomationSession, AutomationRun
from app.models import ConversationState, MessageEvent, OutboundMessage, HandoffTask, utcnow
from app.reception_v3 import live, service, human_followup
from app.runtime_settings import dt
from live_fixture import setup
from test_live_audit_fixes import session
from test_reception_v3 import answer, bundle


def outgoing(db, mid=201, status='submitted', days=0):
    out = OutboundMessage(conversation_state_id=1, source_type='ai', source_id=1,
        idempotency_key=f'live-v3:test:{mid}', content='測試內容', status=status,
        chatwoot_message_id=mid, created_at=(dt(utcnow())-timedelta(days=days)).isoformat())
    db.add(out); db.flush()
    db.add(MessageEvent(conversation_state_id=1, chatwoot_message_id=mid,
        direction='outgoing', content=out.content, status=status, created_at=out.created_at))
    db.flush()
    return out


def test_channel_refusal_survives_label_return_but_new_input_releases_block(session_factory, monkeypatch):
    setup(session_factory, monkeypatch)
    with session_factory() as db:
        row=session(db); conv=db.get(ConversationState,1); out=outgoing(db)
        receipts.apply_receipt(db,out,'failed',{'external_error':'(#551) 這名用戶目前沒空。'})
        db.commit()
        assert out.content_attributes['external_error'].startswith('(#551)')
        assert not live.permitted(db,conv)
        assert not human_followup.scope_enabled(db,conv)
        live.return_by_ai_label(db,conv)
        assert row.controls['simulation']['status']=='stopped'
        assert 'label_return_blocked' in conv.effective_state_reason
        receipts.apply_receipt(db,out,'failed')
        assert len(db.scalars(select(HandoffTask)).all())==1
        # Old inbound replays and new private notes cannot reopen a channel.
        db.add(MessageEvent(conversation_state_id=1,chatwoot_message_id=202,
            direction='incoming',content='old replay',created_at=service.later(out.created_at,-10)))
        db.add(MessageEvent(conversation_state_id=1,chatwoot_message_id=203,
            direction='incoming',private=True,content='note',created_at=service.later(out.created_at,10)))
        db.flush(); assert receipts.channel_delivery_blocked(db,1)
        db.add(MessageEvent(conversation_state_id=1,chatwoot_message_id=204,
            direction='incoming',content='我回來了',created_at=service.later(out.created_at,20)))
        db.flush(); assert not receipts.channel_delivery_blocked(db,1)
        # Existing handoff ownership is preserved; new input is not an auto-send.
        assert not live.permitted(db,conv)
        assert out.status=='failed'


def test_older_submitted_receipt_reconciles_without_sending_or_regressing(session_factory, monkeypatch):
    setup(session_factory,monkeypatch)
    monkeypatch.setattr(receipts,'SessionLocal',session_factory)
    calls=[]
    class Client:
        def __init__(self,*a,**k):pass
        def close(self):pass
        def get_messages(self,cid,before=None):
            calls.append(before)
            return {'payload':[{'id':201,'status':'read','message_type':1,'private':False}]}
    monkeypatch.setattr(receipts,'ReadOnlyChatwootClient',Client)
    with session_factory() as db:
        outgoing(db,days=4);db.commit()
    assert receipts.reconcile_live_delivery({})==1
    assert calls==[202]
    with session_factory() as db:
        out=db.scalar(select(OutboundMessage))
        assert out.status=='read'
        assert receipts.apply_receipt(db,out,'sent') is False
        assert receipts.apply_receipt(db,out,'failed') is False
        assert out.status=='read' and not receipts.channel_delivery_blocked(db,1)
        assert db.scalar(select(HandoffTask)) is None


@pytest.mark.parametrize('takeover',[False,True])
def test_model_apply_conflict_reuses_result_and_rechecks_generation(session_factory,monkeypatch,takeover):
    setup(session_factory,monkeypatch)
    monkeypatch.setattr(service,'compile_skills',lambda db:deepcopy(bundle()))
    monkeypatch.setattr(service,'active_web_facts',lambda *a,**k:([],'test'))
    monkeypatch.setattr(service,'candidate_materials',lambda *a:[])
    calls=[]
    monkeypatch.setattr(service,'run_agent',lambda ctx:(calls.append(ctx) or answer(messages=[{'text':'已收到您的月份'}],profile={'party_size':3})))
    original=service.apply_decision
    attempts=[]
    def conflict(db,row,*args):
        if not attempts:
            with session_factory() as other:
                current=other.get(AutomationSession,row.id)
                current.memory={**current.memory,'concurrent_delivery_marker':True}
                if takeover:live.cancel(other,1,'human_reply')
                other.commit()
        attempts.append(1)
        return original(db,row,*args)
    monkeypatch.setattr(service,'apply_decision',conflict)
    with session_factory() as db:
        row=session(db)
        assert service.tick(db,session_id=row.id,environment='live',advance_clock=False)
        db.refresh(row)
        run=db.scalar(select(AutomationRun))
        assert len(calls)==1
        assert run.status==('cancelled' if takeover else 'completed')
        assert row.memory['concurrent_delivery_marker']
        assert (row.memory.get('party_size')==3) is (not takeover)
        assert db.scalar(select(HandoffTask)) is None
