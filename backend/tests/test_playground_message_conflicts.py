import pytest
from fastapi import HTTPException

from app import automation_api
from app.automation_models import AutomationSession
from app.models import User
from app.reception_v3 import service
from test_reception_v3 import create


@pytest.mark.parametrize('duplicate_request', [False, True])
def test_customer_message_reloads_after_concurrent_commit_without_loss_or_duplicates(
        session_factory, monkeypatch, duplicate_request):
    with session_factory() as db:
        row = create(db, monkeypatch)
        sid, generation = row.id, row.generation
        user = db.get(User, 1)
        original = service.add_message
        attempts = []

        def conflicting_add(db, row, content, client_key, content_type):
            if not attempts:
                with session_factory() as other:
                    current = other.get(AutomationSession, sid)
                    current.memory = {'worker_update': 'preserved'}
                    if duplicate_request:
                        original(other, current, content, client_key, content_type)
                    other.commit()
            attempts.append(row.revision)
            original(db, row, content, client_key, content_type)

        monkeypatch.setattr(service, 'add_message', conflicting_add)
        payload = automation_api.MessageInput(content='Is airfare included?', client_key='customer-retry-key')
        response = automation_api.message(sid, payload, user, db)
        assert len(attempts) == 2
        assert sum(m.get('client_key') == payload.client_key for m in response['messages']) == 1
        assert row.memory['worker_update'] == 'preserved'
        assert row.generation == generation + 1
        repeated = automation_api.message(sid, payload, user, db)
        assert sum(m.get('client_key') == payload.client_key for m in repeated['messages']) == 1
        assert row.generation == generation + 1


@pytest.mark.parametrize('stop_session', [False, True])
def test_conflict_rechecks_stopped_state_and_bounds_retries(session_factory, monkeypatch, stop_session):
    with session_factory() as db:
        row = create(db, monkeypatch)
        sid = row.id
        user = db.get(User, 1)
        original = service.add_message
        attempts = []

        def conflicting_add(db, row, content, client_key, content_type):
            with session_factory() as other:
                current = other.get(AutomationSession, sid)
                current.memory = {'tick': len(attempts) + 1}
                if stop_session:
                    service.control(current, 'stop')
                other.commit()
            attempts.append(row.revision)
            original(db, row, content, client_key, content_type)

        monkeypatch.setattr(service, 'add_message', conflicting_add)
        payload = automation_api.MessageInput(content='Show me the hotel', client_key='conflict-key')
        with pytest.raises(HTTPException) as caught:
            automation_api.message(sid, payload, user, db)
        assert caught.value.detail['code'] == ('journey_not_running' if stop_session else 'session_update_conflict')
        assert len(attempts) == (1 if stop_session else 3)
        assert not any(m.get('client_key') == payload.client_key for m in db.get(AutomationSession, sid).messages)
