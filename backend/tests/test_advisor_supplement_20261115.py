from sqlalchemy import select
import pytest
from app.models import ConversationState, MessageEvent, HandoffTask, utcnow
from app.reception_v3 import live, service
from live_fixture import setup
from test_live_audit_fixes import session
from test_live_audit_fixes import event
from unittest.mock import Mock


@pytest.mark.parametrize('newer_customer', [False, True])
def test_supplement_cancels_stale_plan_and_keeps_newer_customer(session_factory, monkeypatch, newer_customer):
    setup(session_factory, monkeypatch)
    monkeypatch.setattr(live, 'permitted', lambda *a, **kw: True)
    with session_factory() as db:
        row = session(db)
        row.messages = [{'id': 'draft', 'direction': 'outgoing', 'status': 'draft', 'content': 'old'}]
        advisor = MessageEvent(conversation_state_id=1, chatwoot_message_id=101,
            direction='outgoing', attribution='human', content='Six people can travel as a private group.', created_at=utcnow())
        db.add(advisor)
        if newer_customer:
            db.add(MessageEvent(conversation_state_id=1, chatwoot_message_id=102,
                direction='incoming', content='Which month and how many days?', created_at=utcnow()))
        db.flush()
        conversation = db.get(ConversationState, 1)
        live.record_human_reply(db, conversation, advisor)
        assert row.controls['simulation']['status'] == 'running'
        assert not row.controls.get('human')
        assert next(m for m in row.messages if m['id'] == 'draft')['status'] == 'cancelled'
        assert any(m.get('attribution') == 'human' and m['content'] == advisor.content for m in row.messages)
        assert not db.scalar(select(HandoffTask))
        assert service.state(row)['pending_event'] == ('customer_message' if newer_customer else None)
        assert conversation.labels == ['ai']


@pytest.mark.parametrize('blocked', ['no_ai', 'permission', 'paused'])
def test_supplement_cannot_override_ownership(session_factory, monkeypatch, blocked):
    setup(session_factory, monkeypatch)
    monkeypatch.setattr(live, 'permitted', lambda *a, **kw: blocked != 'permission')
    with session_factory() as db:
        row = session(db, status='stopped' if blocked == 'paused' else 'running')
        conversation = db.get(ConversationState, 1)
        if blocked == 'no_ai':
            conversation.labels = ['human']
        message = MessageEvent(conversation_state_id=1, chatwoot_message_id=101, direction='outgoing')
        assert not live.record_advisor_supplement(db, conversation, message)


def test_real_webhook_supplement_then_customer_continues(session_factory, monkeypatch):
    setup(session_factory, monkeypatch)
    with session_factory() as db:
        row = session(db)
        event(db, 'message_created', ['ai'], 1, 101, 'outgoing')
        assert not row.controls.get('human')
        assert live.permitted(db, db.get(ConversationState, 1))
        event(db, 'message_created', ['ai'], 2, 102, 'incoming')
        assert service.state(row)['pending_event'] == 'customer_message'
        assert any(m.get('attribution') == 'inferred_human' for m in row.messages)
        assert not db.scalar(select(HandoffTask))


def test_outage_new_advisor_invalidates_plan_without_permanent_stop(session_factory, monkeypatch, tmp_path):
    from app import webhook_recovery as recovery
    setup(session_factory, monkeypatch)
    marker = tmp_path/'enabled'; marker.touch(); monkeypatch.setattr(recovery, 'MARKER', marker)
    client = Mock()
    client.get_conversation.return_value = {'id': 26, 'inbox_id': 128859, 'labels': ['ai'],
        'can_reply': True, 'status': 'open', 'meta': {'sender': {'id': 55}}}
    client.get_messages.return_value = {'payload': [{'id': 101, 'message_type': 1,
        'content': 'Advisor supplement', 'created_at': utcnow(), 'private': False}]}
    with session_factory() as db:
        row = session(db)
        row.messages = [{'id': 'draft', 'direction': 'outgoing', 'content': 'old', 'status': 'draft'}]
        conversation = db.get(ConversationState, 1)
        assert not recovery.before_send(db, client, conversation, row)
        assert row.controls['simulation']['status'] == 'running'
        assert not row.controls.get('human')
        assert next(m for m in row.messages if m['id'] == 'draft')['status'] == 'cancelled'
        assert any(m.get('content') == 'Advisor supplement' for m in row.messages)
        assert live.permitted(db, conversation)
        generation = row.generation
        # Late native echo must not cancel a subsequent generation a second time.
        event(db, 'message_created', ['ai'], 1, 101, 'outgoing')
        assert row.generation == generation
        event(db, 'message_created', ['ai'], 2, 102, 'incoming')
        assert service.state(row)['pending_event'] == 'customer_message'
