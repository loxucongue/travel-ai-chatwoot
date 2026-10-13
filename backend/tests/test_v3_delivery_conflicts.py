"""Concurrent model/webhook updates must not become failed customer sends."""
import pytest
from sqlalchemy import select
from sqlalchemy.orm.exc import StaleDataError

from app.automation_models import AutomationSession
from app.models import ConversationState, HandoffTask, OutboundMessage
from app.reception_v3 import live, service, worker
from live_fixture import setup
from test_live_audit_fixes import session


@pytest.mark.parametrize('takeover', [False, True])
def test_before_submission_conflict_reloads_without_false_handoff(session_factory, monkeypatch, takeover):
    setup(session_factory, monkeypatch)
    monkeypatch.setattr(worker, 'SessionLocal', session_factory)
    reads, sent = [], []
    with session_factory() as db:
        row = session(db)
        value = {'opening_started': True}
        service.append_plan(row, value, [{'content': '介紹下一段', 'content_type': 'text'}],
                            interval=0, kind='introduction')
        service.save(row, value)
        db.commit()
        session_id = row.id

    class Client:
        def close(self): pass
        def get_conversation(self, cid):
            if not reads:
                # The model or incoming webhook commits while the channel GET runs.
                with session_factory() as other:
                    current = other.get(AutomationSession, session_id)
                    current.memory = {'party_size': 4}
                    if takeover:
                        live.cancel(other, 1, 'human_reply')
                        other.get(ConversationState, 1).effective_ai_state = 'HUMAN_HANDOFF'
                    other.commit()
            reads.append(cid)
            return {'id': cid, 'can_reply': True}
        def get_conversation_labels(self, cid): return ['ai']
        def create_text_message(self, cid, text):
            sent.append(text)
            return {'id': 701}

    monkeypatch.setattr(live, 'client_for', lambda *args: Client())
    worker.delivery_turn(session_id, 'live')
    with session_factory() as db:
        row = db.get(AutomationSession, session_id)
        assert db.scalar(select(HandoffTask)) is None
        assert row.memory == {'party_size': 4}
        if takeover:
            assert sent == []
            assert db.scalar(select(OutboundMessage)) is None
            assert row.controls['simulation']['status'] == 'stopped'
        else:
            assert sent == ['介紹下一段']
            assert db.scalar(select(OutboundMessage)).chatwoot_message_id == 701
            assert row.controls['simulation']['status'] == 'running'
            assert not service.state(row).get('handoff')


def test_conflict_after_submission_stays_unknown_and_never_reposts(session_factory, monkeypatch):
    setup(session_factory, monkeypatch)
    monkeypatch.setattr(worker, 'SessionLocal', session_factory)
    sent = []
    with session_factory() as db:
        row = session(db)
        value = {'opening_started': True}
        service.append_plan(row, value, [{'content': '一次訊息', 'content_type': 'text'}], interval=0, kind='reply')
        service.save(row, value)
        db.commit()
        session_id = row.id

    class Client:
        def close(self): pass
        def get_conversation(self, cid): return {'id': cid, 'can_reply': True}
        def get_conversation_labels(self, cid): return ['ai']
        def create_text_message(self, cid, text):
            sent.append(text)
            raise StaleDataError('fault after external request started')

    monkeypatch.setattr(live, 'client_for', lambda *args: Client())
    worker.delivery_turn(session_id, 'live')
    worker.delivery_turn(session_id, 'live')
    with session_factory() as db:
        assert sent == ['一次訊息']
        assert db.scalar(select(OutboundMessage)).status == 'submission_unknown'
        assert db.scalar(select(HandoffTask)).reason_code == 'delivery_failed'
        assert db.get(AutomationSession, session_id).controls['simulation']['status'] == 'stopped'
