from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.models import ConversationState, OutboundMessage, utcnow
from app.reception_v3 import live, service
from app.runtime_settings import dt
from live_fixture import setup
from test_conversation_controls import FakeChatwoot
from test_live_audit_fixes import session, event


@pytest.mark.parametrize('age,expected', [
    (timedelta(0), True), (timedelta(hours=24, microseconds=-1), True),
    (timedelta(hours=24), False), (timedelta(days=6), False),
    (timedelta(seconds=-1), False),
])
def test_facebook_exact_boundary(age, expected):
    now = '2026-11-02T14:00:00+00:00'
    conversation = SimpleNamespace(inbox=SimpleNamespace(channel_type='Channel::FacebookPage'),
                                  last_customer_message_at=(dt(now) - age).isoformat())
    assert live.automated_window_open(conversation, now) is expected


@pytest.mark.parametrize('last', [None, '', 'invalid'])
def test_missing_or_invalid_customer_time_fails_closed(last):
    conversation = SimpleNamespace(inbox=SimpleNamespace(channel_type='Channel::FacebookPage'),
                                  last_customer_message_at=last)
    assert not live.automated_window_open(conversation)


def test_expired_model_work_pauses_and_real_customer_reply_resumes(session_factory, monkeypatch):
    setup(session_factory, monkeypatch)
    monkeypatch.setattr(service, 'run_agent', lambda _: pytest.fail('Expired window invoked model'))
    with session_factory() as db:
        row = session(db)
        conversation = db.get(ConversationState, 1)
        conversation.last_customer_message_at = (dt(utcnow()) - timedelta(days=3)).isoformat()
        db.commit()
        assert conversation.can_reply  # Human extension must not authorize AI.
        service.tick(db, environment='live', advance_clock=False)
        assert service.state(row)['last_reason'] == 'automated_window_closed'
        assert row.controls['simulation']['status'] == 'stopped'
        event(db, 'conversation_updated', ['ai'], 20)
        assert service.state(row)['last_reason'] == 'automated_window_closed'
        event(db, 'message_created', ['ai'], 21, 200, 'incoming')
        assert row.controls['simulation']['status'] == 'running'
        assert service.state(row)['pending_event'] == 'customer_message'
        assert live.permitted(db, conversation)


@pytest.mark.parametrize('content_type', ['text', 'image', 'video'])
@pytest.mark.parametrize('expires_during_read', [False, True])
def test_no_submission_outside_window_including_final_recheck(session_factory, monkeypatch, content_type, expires_during_read):
    setup(session_factory, monkeypatch)
    fake = FakeChatwoot()
    monkeypatch.setattr(live, 'client_for', lambda _: fake)
    with session_factory() as db:
        row = session(db)
        conversation = db.get(ConversationState, 1)
        original_read = fake.get_conversation

        def expire():
            conversation.last_customer_message_at = (dt(utcnow()) - timedelta(days=3)).isoformat()
            db.commit()

        def read(cid):
            expire()
            return original_read(cid)

        if expires_during_read:
            monkeypatch.setattr(fake, 'get_conversation', read)
        else:
            expire()
        value = service.state(row)
        service.append_plan(row, value, [{'content':'test', 'content_type':content_type}], 'reply', 1, 999)
        service.save(row, value)
        db.commit()
        live.deliver_one(db, row)
        assert not db.scalar(select(OutboundMessage.id))
        assert service.state(row)['last_reason'] == 'automated_window_closed'
        assert not any(m.get('status') == 'draft' for m in row.messages)
