import json
import pytest
import respx
from sqlalchemy import select, func

from app import notification_dispatch
from app.models import (ConversationState, ChatwootConnection, HandoffTask,
                        Notification, NotificationDelivery, OutboundMessage, Contact)
from app.operations import save_setting
from app.reception_v3 import live, service
from live_fixture import setup
from test_live_audit_fixes import session, event
from test_runtime_reliability import mock_chatwoot


@pytest.mark.parametrize('condition', ['fresh', 'old', 'answered', 'unknown', 'human'])
def test_ai_added_after_customer_message_starts_only_fresh_unanswered_session(session_factory, monkeypatch, condition):
    from datetime import timedelta
    from app.models import MessageEvent, utcnow
    from app.runtime_settings import dt, iso
    setup(session_factory, monkeypatch)
    with session_factory() as db:
        conv = db.get(ConversationState, 1)
        conv.labels = []
        conv.ai_label_present = False
        conv.ai_mode = 'disabled'
        message = db.get(MessageEvent, 1)
        message.content = '西藏桃花節＋珠峰11日'
        if condition == 'old':
            message.created_at = iso(dt(utcnow()) - timedelta(hours=2))
        elif condition == 'answered':
            db.add(MessageEvent(conversation_state_id=1, chatwoot_message_id=101,
                                direction='outgoing', content='already answered'))
        elif condition == 'unknown':
            db.add(OutboundMessage(conversation_state_id=1, idempotency_key='unknown-start',
                                  source_type='ai', content='test', status='unknown'))
        elif condition == 'human':
            conv.labels = ['人工接管']
        db.commit()
        labels = ['ai', '人工接管'] if condition == 'human' else ['ai']
        event(db, 'conversation_updated', labels, 20)
        row = live.session_for(db, 1)
        if condition != 'fresh':
            assert row is None
            return
        assert row is not None
        assert any(m['content'] == message.content for m in row.messages)
        assert service.state(row)['pending_event'] == 'customer_message'
        event(db, 'conversation_updated', labels, 21)
        assert live.session_for(db, 1).id == row.id
        assert len([m for m in row.messages if m.get('id') == 'cw-100']) == 1


def test_label_handoff_creates_one_private_notification_and_stops_drafts(session_factory, monkeypatch):
    setup(session_factory, monkeypatch)
    with session_factory() as db:
        db.get(ChatwootConnection, 1).base_url = 'https://chatwoot.invalid'
        save_setting(db, 'notification_settings', {'enabled': True, 'channel': 'chatwoot',
            'agent_id': 7, 'bot_id': 8, 'event_types': ['handoff.created', 'handoff.overdue']})
        row = session(db)
        row.messages = [{'id': 'draft', 'status': 'draft', 'content': 'pending'}]
        event(db, 'conversation_updated', ['ai', '人工接管'], 10)
        assert row.controls['simulation']['status'] == 'stopped'
        assert row.messages[0]['status'] == 'cancelled'
        assert db.scalar(select(func.count(HandoffTask.id))) == 1
        assert db.scalar(select(func.count(NotificationDelivery.id))) == 1
        event(db, 'conversation_updated', ['ai', '人工接管'], 11)
        event(db, 'conversation_updated', ['ai'], 12)
        assert not live.permitted(db, db.get(ConversationState, 1))
        assert db.scalar(select(func.count(Notification.id))) == 1
    with respx.mock() as router:
        sent = mock_chatwoot(router)
        assert notification_dispatch.process_notification_delivery(session_factory)
        assert sent.call_count == 1
        payload = json.loads(sent.calls[0].request.content)
        assert payload['private'] and 'mention://user/7/' in payload['content']


def test_remove_and_readd_ai_resumes_without_replaying_cancelled_reply(session_factory, monkeypatch):
    setup(session_factory, monkeypatch)
    with session_factory() as db:
        row = session(db)
        row.messages = [{'id': 'old', 'status': 'draft', 'content': 'old reply'}]
        event(db, 'conversation_updated', [], 10)
        event(db, 'conversation_updated', ['ai'], 11)
        assert row.controls['simulation']['status'] == 'running'
        assert row.messages[0]['status'] == 'cancelled'
        assert not service.state(row).get('pending_event')
        assert db.get(ConversationState, 1).effective_ai_state == 'AI_ACTIVE'
        event(db, 'message_created', ['ai'], 12, 111, 'incoming')
        assert service.state(row)['pending_event'] == 'customer_message'


@pytest.mark.parametrize('label', ['人工接管', '客訴', 'ai_off', '已留資', '已成交', '拒絕聯繫', '黑名單'])
def test_all_stopping_labels_block_replies_and_followup(session_factory, monkeypatch, label):
    setup(session_factory, monkeypatch)
    with session_factory() as db:
        row = session(db)
        event(db, 'conversation_updated', ['ai', label], 10)
        assert not live.permitted(db, db.get(ConversationState, 1))
        assert row.controls['simulation']['status'] == 'stopped'
        assert not service.state(row).get('next_check_at')


def test_stale_handoff_label_event_does_not_create_task(session_factory, monkeypatch):
    setup(session_factory, monkeypatch)
    with session_factory() as db:
        row = session(db)
        event(db, 'conversation_updated', ['ai'], 20)
        event(db, 'conversation_updated', ['ai', '人工接管'], 10)
        assert row.controls['simulation']['status'] == 'running'
        assert not db.scalar(select(HandoffTask.id))


def test_other_inbox_label_does_not_create_production_notification(session_factory, monkeypatch):
    from app.config import settings
    setup(session_factory, monkeypatch)
    monkeypatch.setattr(settings, 'live_reply_inbox_id', 999)
    with session_factory() as db:
        session(db)
        event(db, 'conversation_updated', ['ai', '人工接管'], 10)
        assert not db.scalar(select(HandoffTask.id))


def test_explicit_ai_switch_can_release_human_reply_without_task(authenticated, session_factory, monkeypatch):
    from test_conversation_controls import seed_conversation, FakeChatwoot
    client, csrf = authenticated
    seed_conversation(session_factory)
    fake = FakeChatwoot()
    monkeypatch.setattr('app.api.client_for', lambda _: fake)
    with session_factory() as db:
        row = session(db, 'stopped')
        row.controls = {**row.controls, 'human': True, 'v3': {'last_reason': 'human_reply'}}
        db.commit()
    result = client.put('/v1/conversations/10/ai-mode', headers={'X-CSRF-Token': csrf}, json={'enabled': True})
    assert result.status_code == 200
    assert result.json()['ai_state'] == 'AI_ACTIVE'
    with session_factory() as db:
        row = live.session_for(db, 1)
        assert not row.controls['human']
        assert row.controls['simulation']['status'] == 'running'
        assert not service.state(row).get('pending_event')


def test_explicit_ai_switch_rejects_active_handoff_before_remote_write(authenticated, session_factory, monkeypatch):
    from test_conversation_controls import seed_conversation, FakeChatwoot
    client, csrf = authenticated
    seed_conversation(session_factory)
    fake = FakeChatwoot()
    monkeypatch.setattr('app.api.client_for', lambda _: fake)
    with session_factory() as db:
        db.add(HandoffTask(conversation_state_id=1, reason_code='test', status='pending'))
        db.commit()
    result = client.put('/v1/conversations/10/ai-mode', headers={'X-CSRF-Token': csrf}, json={'enabled': True})
    assert result.status_code == 409
    assert fake.calls == []


def test_restore_does_not_override_another_conversation_of_same_customer(session_factory, monkeypatch):
    setup(session_factory, monkeypatch)
    with session_factory() as db:
        other = ConversationState(tenant_id=1, inbox_binding_id=1, contact_id=1,
                                  chatwoot_conversation_id=27, labels=['ai'])
        db.add(other)
        db.flush()
        db.add(HandoffTask(conversation_state_id=other.id, reason_code='test', status='claimed'))
        db.flush()
        assert '其他会话' in live.restore_blocker(db, db.get(ConversationState, 1))


def test_contact_block_prevents_current_conversation_delivery(session_factory, monkeypatch):
    setup(session_factory, monkeypatch)
    with session_factory() as db:
        row = session(db)
        db.get(Contact, 1).labels = ['拒絕聯繫']
        assert not live.permitted(db, db.get(ConversationState, 1))
        assert live.restore_blocker(db, db.get(ConversationState, 1))


@pytest.mark.parametrize('condition', ['human', 'opt_out', 'unknown'])
def test_ai_label_cannot_override_human_optout_or_unknown_delivery(session_factory, monkeypatch, condition):
    setup(session_factory, monkeypatch)
    with session_factory() as db:
        row = session(db)
        if condition == 'human':
            event(db, 'message_created', ['ai'], 9, 112, 'outgoing')
        elif condition == 'opt_out':
            service.save(row, {**service.state(row), 'opt_out': True})
        else:
            db.add(OutboundMessage(conversation_state_id=1, idempotency_key='unknown',
                                  source_type='ai', content='test', status='unknown'))
        event(db, 'conversation_updated', [], 10)
        event(db, 'conversation_updated', ['ai'], 11)
        assert row.controls['simulation']['status'] == 'stopped'
