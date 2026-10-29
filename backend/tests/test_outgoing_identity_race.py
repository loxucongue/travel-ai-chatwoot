"""#498: a public AI image callback arrived 0.8s before its POST completed."""
from datetime import timedelta

import pytest
from sqlalchemy import select

from app import live_reply, outgoing_identity
from app.automation_models import AutomationSession
from app.models import (AuditLog, ConversationLabelSync, ConversationState, HandoffTask,
                        MessageEvent, Notification, OutboundMessage, StoredMedia, WebhookEvent, utcnow)
from app.reception_v3 import live, service, worker
from app.runtime_settings import dt
from live_fixture import setup
from test_live_audit_fixes import session, event as control_event


def callback(db, mid=201, kind='message_created', **overrides):
    payload = {'event': kind, 'id': mid, 'message_type': 'outgoing', 'private': False,
        'content': '', 'created_at': utcnow(), 'sender': {'id': 264715, 'type': 'user'},
        'conversation': {'id': 26, 'inbox_id': 128859, 'labels': ['ai'], 'can_reply': True,
                         'meta': {'sender': {'id': 55, 'name': 'Test'}}}, **overrides}
    item = WebhookEvent(connection_id=1, event=kind, account_id=180474, resource_id=str(mid),
        idempotency_key=f'{mid}:{kind}:{utcnow()}', payload=payload, received_at=utcnow())
    db.add(item); db.flush()
    live_reply.mirror_event(db, item)
    return item.id


def unlinked(db, conversation_id=1):
    out = OutboundMessage(conversation_state_id=conversation_id, source_type='ai', source_id=1,
        idempotency_key=f'live-v3:race:{utcnow()}', content='', content_type='image',
        status='submission_unknown')
    db.add(out); db.commit()
    return out


def plan(factory, monkeypatch, kind='image'):
    setup(factory, monkeypatch)
    monkeypatch.setattr(worker, 'SessionLocal', factory)
    with factory() as db:
        row = session(db)
        media = StoredMedia(tenant_id=1, original_name='hotel.jpg', media_type='image',
            mime_type='image/jpeg', file_size=1, storage_path='fake-hotel.jpg', created_by=1)
        db.add(media); db.flush()
        value = {'opening_started': True}
        parts = [{'content': '' if kind == 'image' else '第一段', 'content_type': kind}]
        if kind == 'image':
            parts[0]['media_id'] = media.id
        if kind == 'input_select':
            parts[0]['content_attributes'] = {'items': [{'title': '兩位'}]}
        parts += [{'content': '第二段'}, {'content': '介紹結束'}]
        service.append_plan(row, value, parts, interval=0, kind='introduction')
        service.save(row, value); db.commit()
        return row.id


class Client:
    def close(self): pass
    def get_conversation(self, cid): return {'id': cid, 'can_reply': True}
    def get_conversation_labels(self, cid): return ['ai']


@pytest.mark.parametrize('kind', ['image', 'text', 'input_select'])
def test_callback_before_response_continues_whole_introduction_once(session_factory, monkeypatch, kind):
    row_id = plan(session_factory, monkeypatch, kind)
    sends, retries = [], []
    class EchoClient(Client):
        def send(self, *args):
            mid = 201 + len(sends); sends.append(mid)
            with session_factory() as other:
                retries.append(callback(other, mid))
                assert outgoing_identity.pending(other, 1)
                assert other.get(WebhookEvent, retries[-1]).attempts == 0
                # Receipts can also precede the POST response and must not clear the guard.
                callback(other, mid, kind='message_updated', status='read')
                assert outgoing_identity.pending(other, 1)
                assert not other.scalar(select(HandoffTask.id))
                assert not other.scalar(select(ConversationLabelSync.conversation_state_id))
            return {'id': mid, 'status': 'sent'}
        create_attachment_message = send
        create_text_message = send
        create_input_select_message = send
    monkeypatch.setattr(live, 'client_for', lambda *a: EchoClient())
    for _ in range(3):
        worker.delivery_turn(row_id, 'live')
        with session_factory() as db:
            # A fresh DB session models worker restart/retry, not an in-memory flag.
            item = db.get(WebhookEvent, retries[-1])
            live_reply.mirror_event(db, item)
            assert item.status == 'live_observed' and item.error_code is None
            assert not outgoing_identity.pending(db, 1)
    with session_factory() as db:
        row = db.get(AutomationSession, row_id)
        assert sends == [201, 202, 203]
        assert [o.status for o in db.scalars(select(OutboundMessage))] == ['read'] * 3
        assert not db.scalar(select(HandoffTask.id)) and not db.scalar(select(Notification.id))
        assert not db.scalar(select(AuditLog.id).where(AuditLog.action == 'handoff.advisor_replied'))
        assert not row.controls.get('human') and not row.controls.get('human_followup', {}).get('enabled')
        assert row.controls['simulation']['status'] == 'running'
        assert service.state(row)['pending_event'] == 'introduction_completed'
        # Duplicate creation must still be identified by the exact outbound ID.
        callback(db, 201)
        assert not db.scalar(select(HandoffTask.id))


def test_real_advisor_during_ai_post_is_not_swallowed_even_with_same_sender_and_text(session_factory, monkeypatch):
    row_id = plan(session_factory, monkeypatch, 'text')
    sends, retries = [], []
    class HumanClient(Client):
        def create_text_message(self, cid, text):
            sends.append(text)
            with session_factory() as other:
                retries.append(callback(other, 999, content=text))
            return {'id': 201}
    monkeypatch.setattr(live, 'client_for', lambda *a: HumanClient())
    worker.delivery_turn(row_id, 'live')
    # The real reply is not yet reconciled: do not send the next introduction part.
    worker.delivery_turn(row_id, 'live')
    assert sends == ['第一段']
    with session_factory() as db:
        item = db.get(WebhookEvent, retries[0])
        live_reply.mirror_event(db, item)
        live_reply.mirror_event(db, item)
        assert item.error_code is None
        assert db.get(ConversationState, 1).effective_ai_state == 'HUMAN_HANDOFF'
        assert db.get(ConversationLabelSync, 1).target == 'human'
        assert len(db.scalars(select(AuditLog).where(AuditLog.action == 'handoff.advisor_replied')).all()) == 1
    worker.delivery_turn(row_id, 'live')
    assert sends == ['第一段']


def test_unknown_send_times_out_without_false_advisor_timer_or_resend(session_factory, monkeypatch):
    row_id = plan(session_factory, monkeypatch)
    monkeypatch.setattr(live, 'client_for', lambda *a: pytest.fail('must not send while origin unknown'))
    with session_factory() as db:
        unlinked(db)
        eid = callback(db)
        at = db.get(WebhookEvent, eid).received_at
    worker.delivery_turn(row_id, 'live')
    monkeypatch.setattr(outgoing_identity, 'utcnow', lambda: (dt(at) + timedelta(seconds=31)).isoformat())
    with session_factory() as db:
        item = db.get(WebhookEvent, eid)
        live_reply.mirror_event(db, item)
        live_reply.mirror_event(db, item)
        assert item.status == 'live_observed' and item.error_code == 'outgoing_origin_unknown'
        assert not outgoing_identity.pending(db, 1)
        tasks = db.scalars(select(HandoffTask)).all()
        assert len(tasks) == 1 and tasks[0].reason_code == 'delivery_unknown'
        assert not db.scalar(select(AuditLog.id).where(AuditLog.action == 'handoff.advisor_replied'))
        assert not db.get(AutomationSession, row_id).controls.get('human_followup', {}).get('enabled')
    worker.delivery_turn(row_id, 'live')


def test_deferred_event_resolves_after_restart_past_fresh_window(session_factory, monkeypatch):
    plan(session_factory, monkeypatch)
    with session_factory() as db:
        out = unlinked(db); oid = out.id
        eid = callback(db)
        item = db.get(WebhookEvent, eid)
        item.received_at = (dt(utcnow()) - timedelta(minutes=6)).isoformat()
        out.chatwoot_message_id, out.status = 201, 'submitted'
        db.commit()
    with session_factory() as db:
        live_reply.mirror_event(db, db.get(WebhookEvent, eid))
        assert not outgoing_identity.pending(db, 1)
        assert db.scalar(select(MessageEvent).where(MessageEvent.chatwoot_message_id == 201)).attribution == 'ai'
        assert not db.scalar(select(HandoffTask.id))


def test_manual_takeover_remains_when_ai_echo_later_resolves(session_factory, monkeypatch):
    plan(session_factory, monkeypatch)
    with session_factory() as db:
        out = unlinked(db); eid = callback(db)
        control_event(db, 'conversation_updated', ['人工接管'], 500)
        out.chatwoot_message_id, out.status = 201, 'submitted'; db.commit()
        live_reply.mirror_event(db, db.get(WebhookEvent, eid))
        assert db.get(ConversationState, 1).effective_ai_state == 'HUMAN_HANDOFF'
        assert live.session_for(db, 1).controls['simulation']['status'] == 'stopped'


@pytest.mark.parametrize('overrides', [{'private': True}, {'sender': {'type': 'agent_bot', 'id': 10866}},
                                     {'content_attributes': {'external_echo': True}}])
def test_non_advisor_messages_do_not_wait_or_trigger_takeover(session_factory, monkeypatch, overrides):
    plan(session_factory, monkeypatch)
    with session_factory() as db:
        unlinked(db); eid = callback(db, **overrides)
        assert db.get(WebhookEvent, eid).status == 'live_observed'
        assert not outgoing_identity.pending(db, 1)
        assert not db.scalar(select(HandoffTask.id))
        assert not live.session_for(db, 1).controls.get('human')


def test_normal_advisor_still_takes_over_immediately(session_factory, monkeypatch):
    plan(session_factory, monkeypatch)
    with session_factory() as db:
        eid = callback(db, content='我來幫您確認')
        assert db.get(WebhookEvent, eid).status == 'live_observed'
        assert db.get(ConversationState, 1).effective_ai_state == 'HUMAN_HANDOFF'
        assert db.get(ConversationLabelSync, 1).target == 'human'


def test_other_conversations_unresolved_send_does_not_delay_advisor(session_factory, monkeypatch):
    plan(session_factory, monkeypatch)
    with session_factory() as db:
        other = ConversationState(tenant_id=1, inbox_binding_id=1, chatwoot_conversation_id=27)
        db.add(other); db.flush(); unlinked(db, other.id)
        eid = callback(db)
        assert db.get(WebhookEvent, eid).status == 'live_observed'
        assert db.get(ConversationState, 1).effective_ai_state == 'HUMAN_HANDOFF'


def test_early_channel_error_still_stops_the_batch_as_delivery_failure(session_factory, monkeypatch):
    row_id = plan(session_factory, monkeypatch)
    sends = []
    class ErrorClient(Client):
        def create_attachment_message(self, *args):
            sends.append(201)
            with session_factory() as other:
                callback(other, content_attributes={'external_error': '(#551) 這名用戶目前沒空。'})
            return {'id': 201}
    monkeypatch.setattr(live, 'client_for', lambda *a: ErrorClient())
    worker.delivery_turn(row_id, 'live')
    with session_factory() as db:
        item = db.scalar(select(WebhookEvent))
        live_reply.mirror_event(db, item)
        assert db.scalar(select(OutboundMessage)).status == 'failed'
        assert db.scalar(select(HandoffTask)).reason_code == 'channel_send_failed'
        assert not db.scalar(select(AuditLog.id).where(AuditLog.action == 'handoff.advisor_replied'))
    worker.delivery_turn(row_id, 'live')
    assert sends == [201]


def test_new_origin_uncertainty_during_control_read_blocks_the_post(session_factory, monkeypatch):
    row_id = plan(session_factory, monkeypatch)
    class RacingClient(Client):
        def get_conversation(self, cid):
            with session_factory() as other:
                unlinked(other); callback(other)
            return {'id': cid, 'can_reply': True}
        def create_attachment_message(self, *args):
            pytest.fail('origin became uncertain during the GET; no POST is allowed')
    monkeypatch.setattr(live, 'client_for', lambda *a: RacingClient())
    worker.delivery_turn(row_id, 'live')
    with session_factory() as db:
        assert outgoing_identity.pending(db, 1)
        assert len(db.scalars(select(OutboundMessage)).all()) == 1
        assert not db.scalar(select(HandoffTask))
        assert db.get(AutomationSession, row_id).controls['simulation']['status'] == 'running'
