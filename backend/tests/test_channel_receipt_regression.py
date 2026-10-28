"""Replay #484's statusless errors across callbacks, sending and polling."""
import pytest
from sqlalchemy import select

from app import delivery_status as receipts, live_reply
from app.automation_models import AutomationSession
from app.models import ConversationState, HandoffTask, MessageEvent, Notification, OutboundMessage, WebhookEvent, utcnow
from app.reception_v3 import live, service, worker
from live_fixture import setup
from test_live_audit_fixes import session
from test_priority_audit_20261027 import outgoing

ERROR = {'external_error': '(#551) 這名用戶目前沒空。'}


def callback(db, mid, kind='message_updated', attributes=None, status=None):
    payload = {'event': kind, 'id': mid, 'message_type': 'outgoing', 'private': False,
        'content': '圖片介紹', 'created_at': utcnow(), 'sender': {'id': 9, 'type': 'agent_bot'},
        'conversation': {'id': 26, 'inbox_id': 128859, 'labels': ['ai'], 'can_reply': True,
                         'meta': {'sender': {'id': 55, 'name': 'Test'}}},
        'content_attributes': attributes or {}}
    if status is not None:
        payload['status'] = status
    event = WebhookEvent(connection_id=1, event=kind, account_id=180474, resource_id=str(mid),
        idempotency_key=f'{mid}:{kind}:{utcnow()}', payload=payload, received_at=utcnow())
    db.add(event); db.flush()
    live_reply.mirror_event(db, event)


@pytest.mark.parametrize('status', [None, 'sent', 'submitted', 'failed'])
def test_statusless_error_stops_plan_once_and_survives_late_creation(session_factory, monkeypatch, status):
    setup(session_factory, monkeypatch)
    monkeypatch.setattr(worker, 'SessionLocal', session_factory)
    monkeypatch.setattr(live, 'client_for', lambda *a: pytest.fail('must not send after refusal'))
    with session_factory() as db:
        row = session(db)
        value = {'opening_started': True}
        service.append_plan(row, value, [{'content': '下一張圖'}, {'content': '下一段介紹'}], interval=0, kind='introduction')
        service.save(row, value)
        out = outgoing(db); db.commit()
        callback(db, 201, attributes=ERROR, status=status)
        callback(db, 201, kind='message_created')
        callback(db, 201, attributes=ERROR)
        assert out.status == 'failed'
        assert out.content_attributes == ERROR
        assert db.scalar(select(MessageEvent).where(MessageEvent.chatwoot_message_id == 201)).status == 'failed'
        assert row.controls['simulation']['status'] == 'stopped'
        assert len(db.scalars(select(HandoffTask)).all()) == 1
        assert len(db.scalars(select(Notification).where(Notification.event_type == 'handoff.created')).all()) == 1
        row_id = row.id
    worker.delivery_turn(row_id, 'live')
    worker.delivery_turn(row_id, 'live')
    with session_factory() as db:
        assert len(db.scalars(select(OutboundMessage)).all()) == 1


def test_error_callback_before_outbox_link_is_preserved(session_factory, monkeypatch):
    setup(session_factory, monkeypatch)
    with session_factory() as db:
        session(db)
        callback(db, 201, attributes=ERROR)
        callback(db, 201, kind='message_created')
        out = OutboundMessage(conversation_state_id=1, source_type='ai', source_id=1,
            idempotency_key='live-v3:early-callback', content='介紹', status='submitted', chatwoot_message_id=201)
        db.add(out); db.flush()
        receipts.apply_receipt(db, out, None)
        assert out.status == 'failed' and out.content_attributes == ERROR
        assert db.scalar(select(HandoffTask)).reason_code == 'channel_send_failed'


@pytest.mark.parametrize('status', ['delivered', 'read'])
def test_success_receipt_dominates_stale_error_and_clears_diagnostic(session_factory, monkeypatch, status):
    setup(session_factory, monkeypatch)
    with session_factory() as db:
        row = session(db); out = outgoing(db)
        callback(db, 201, attributes=ERROR)
        callback(db, 201, status=status)
        callback(db, 201, attributes=ERROR)
        assert out.status == status and out.error_code is None
        assert 'external_error' not in out.content_attributes
        assert not receipts.channel_delivery_blocked(db, 1)
        # Recovered receipts must not restart automation or undo advisor ownership.
        assert row.controls['simulation']['status'] == 'stopped'
        assert len(db.scalars(select(HandoffTask)).all()) == 1


def test_api_response_error_stops_subsequent_parts(session_factory, monkeypatch):
    setup(session_factory, monkeypatch)
    monkeypatch.setattr(worker, 'SessionLocal', session_factory)
    sends = []
    class Client:
        def close(self): pass
        def get_conversation(self, cid): return {'id': cid, 'can_reply': True}
        def get_conversation_labels(self, cid): return ['ai']
        def create_text_message(self, cid, text):
            sends.append(text)
            return {'id': 201, 'content_attributes': ERROR}
    monkeypatch.setattr(live, 'client_for', lambda *a: Client())
    with session_factory() as db:
        row = session(db); value = {'opening_started': True}
        service.append_plan(row, value, [{'content': '第一段'}, {'content': '第二段'}], interval=0, kind='introduction')
        service.save(row, value); db.commit(); row_id = row.id
    worker.delivery_turn(row_id, 'live')
    worker.delivery_turn(row_id, 'live')
    with session_factory() as db:
        assert sends == ['第一段']
        assert db.scalar(select(OutboundMessage)).status == 'failed'
        assert db.scalar(select(HandoffTask)).reason_code == 'channel_send_failed'


def test_poll_reconciles_whole_batch_with_statusless_errors_without_sends(session_factory, monkeypatch):
    setup(session_factory, monkeypatch)
    monkeypatch.setattr(receipts, 'SessionLocal', session_factory)
    calls = []
    class Client:
        def __init__(self, *a, **k): pass
        def close(self): pass
        def get_messages(self, cid, before=None):
            calls.append(before)
            return {'payload': [{'id': mid, 'message_type': 1, 'private': False, 'content_attributes': ERROR}
                                for mid in range(201, 207)]}
    monkeypatch.setattr(receipts, 'ReadOnlyChatwootClient', Client)
    with session_factory() as db:
        session(db)
        for mid in range(201, 207): outgoing(db, mid)
        db.commit()
    assert receipts.reconcile_live_delivery({}) == 6
    assert calls == [207]
    with session_factory() as db:
        assert {o.status for o in db.scalars(select(OutboundMessage))} == {'failed'}
        assert len(db.scalars(select(HandoffTask)).all()) == 1


def test_missing_status_without_error_is_not_failure(session_factory, monkeypatch):
    setup(session_factory, monkeypatch)
    with session_factory() as db:
        session(db); out = outgoing(db); db.commit()
        callback(db, 201, kind='message_created')
        assert out.status == 'submitted'
        assert db.scalar(select(HandoffTask)) is None


def test_poll_prioritizes_unresolved_but_still_checks_delivered(session_factory, monkeypatch):
    setup(session_factory, monkeypatch)
    monkeypatch.setattr(receipts, 'SessionLocal', session_factory)
    calls = []
    class Client:
        def __init__(self, *a, **k): pass
        def close(self): pass
        def get_messages(self, cid, before=None):
            calls.append((cid, before))
            return {'payload': [{'id': before - 1, 'status': 'sent' if cid == 26 else 'read',
                                  'message_type': 1, 'private': False}]}
    monkeypatch.setattr(receipts, 'ReadOnlyChatwootClient', Client)
    with session_factory() as db:
        outgoing(db, 500)
        db.add(ConversationState(id=2, tenant_id=1, inbox_binding_id=1, contact_id=1,
                                 chatwoot_conversation_id=27))
        db.flush()
        db.add(OutboundMessage(conversation_state_id=2, source_type='ai', idempotency_key='live:old:200',
                               chatwoot_message_id=200, status='delivered', content='older'))
        db.commit()
    checked = {1: 100}
    for _ in range(5): receipts.reconcile_live_delivery(checked)
    assert [cid for cid, _ in calls] == [26, 26, 26, 26, 27]


def test_unresolved_target_is_not_hidden_by_twenty_delivered_messages(session_factory, monkeypatch):
    setup(session_factory, monkeypatch)
    monkeypatch.setattr(receipts, 'SessionLocal', session_factory)
    calls = []
    class Client:
        def __init__(self, *a, **k): pass
        def close(self): pass
        def get_messages(self, cid, before=None):
            calls.append(before)
            return {'payload': [{'id': before - 1, 'message_type': 1, 'private': False,
                                  'status': 'read'}]}
    monkeypatch.setattr(receipts, 'ReadOnlyChatwootClient', Client)
    with session_factory() as db:
        for mid in range(201, 222): outgoing(db, mid, 'delivered')
        outgoing(db, 500); db.commit()
    receipts.reconcile_live_delivery({})
    assert calls[0] == 501
    with session_factory() as db:
        assert db.scalar(select(OutboundMessage).where(OutboundMessage.chatwoot_message_id == 500)).status == 'read'
