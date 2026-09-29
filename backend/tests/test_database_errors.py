import sqlite3

from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.automation_models import AutomationSession


def test_full_disk_create_session_returns_readable_error_and_can_recover(authenticated, session_factory, monkeypatch):
    client, csrf = authenticated
    original_commit = Session.commit

    def full_disk_commit(db):
        if any(isinstance(row, AutomationSession) for row in db.identity_map.values()):
            cause = sqlite3.OperationalError('database or disk is full')
            cause.sqlite_errorcode = sqlite3.SQLITE_FULL
            raise OperationalError('COMMIT', {}, cause)
        return original_commit(db)

    headers = {'X-CSRF-Token': csrf, 'Origin': 'http://127.0.0.1:5173', 'X-Request-ID': 'disk-full-test'}
    payload = {'mode': 'journey', 'engine_version': 'v3'}
    monkeypatch.setattr(Session, 'commit', full_disk_commit)
    response = client.post('/v1/playground/sessions', json=payload, headers=headers)
    assert response.status_code == 507
    assert response.json()['error']['code'] == 'storage_full'
    assert response.json()['error']['request_id'] == 'disk-full-test'
    assert response.headers['access-control-allow-origin'] == headers['Origin']
    assert response.headers['access-control-allow-credentials'] == 'true'
    with session_factory() as db:
        assert db.scalar(select(func.count()).select_from(AutomationSession)) == 0
    monkeypatch.setattr(Session, 'commit', original_commit)
    assert client.post('/v1/playground/sessions', json=payload, headers=headers).status_code == 201
