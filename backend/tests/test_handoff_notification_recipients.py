import json
from datetime import timedelta

import httpx
import pytest
import respx
from sqlalchemy import select, func

from app import notification_dispatch as dispatch
from app.models import ChatwootAgent, ConversationState, HandoffTask, Notification, NotificationDelivery, utcnow
from app.operations import ensure_handoff, save_setting, setting_value
from app.runtime_settings import dt, iso
from test_runtime_reliability import notification_setup, mock_chatwoot

BASE = 'https://chatwoot.invalid/api/v1/accounts/180474'


def configure(factory, ids=(7, 10, 11)):
    with factory() as db:
        config = setting_value(db, 'notification_settings', {})
        save_setting(db, 'notification_settings', {**config, 'assign_on_handoff': True,
            'handoff_notify_agent_ids': list(ids)})
        db.commit()


def remote(router, assignee=9, members=(7, 9, 10, 11)):
    note = mock_chatwoot(router)
    router.get(f'{BASE}/conversations/26').respond(200, json={'id': 26, 'inbox_id': 128859,
        'meta': {'assignee': {'id': assignee} if assignee else None}})
    router.get(f'{BASE}/inbox_members/128859').respond(200, json={'payload': [{'id': i} for i in members]})
    return note


@pytest.mark.parametrize('owner,expected', [(9, [9, 7, 10, 11]), (7, [7, 10, 11]), (None, [7, 10, 11])])
def test_handoff_one_note_union_and_owner_unchanged(session_factory, monkeypatch, owner, expected):
    notification_setup(session_factory, monkeypatch)
    configure(session_factory, [7, 10, 11, 7])
    with session_factory() as db:
        ensure_handoff(db, db.get(ConversationState, 1), 'test', 'same event')
        db.commit()
        assert db.scalar(select(func.count()).select_from(NotificationDelivery)) == 1
    with respx.mock() as router:
        note = remote(router, owner)
        assignment = router.post(f'{BASE}/conversations/26/assignments').respond(200, json={'id': 7}) if owner is None else None
        assert dispatch.process_notification_delivery(session_factory)
        assert not dispatch.process_notification_delivery(session_factory)
        assert note.call_count == 1
        body = json.loads(note.calls[0].request.content)
        assert body['private'] is True and body['sender_type'] == 'AgentBot'
        assert body['content'].split('\n')[0] == ' '.join(f'[@顾问](mention://user/{i}/advisor)' for i in expected)
        if assignment:
            assert json.loads(assignment.calls[0].request.content)['assignee_id'] == 7


def test_missing_member_does_not_block_valid_recipients_and_warning_visible(session_factory, monkeypatch, authenticated):
    notification_setup(session_factory, monkeypatch)
    configure(session_factory)
    with respx.mock() as router:
        note = remote(router, members=(7, 9))
        assert dispatch.process_notification_delivery(session_factory)
        assert 'mention://user/9/' in json.loads(note.calls[0].request.content)['content']
        assert 'mention://user/10/' not in json.loads(note.calls[0].request.content)['content']
    with session_factory() as db:
        d = db.scalar(select(NotificationDelivery))
        assert d.status == 'delivered' and d.error_code == 'notification_recipients_skipped:10,11'
    client, _ = authenticated
    data = client.get('/v1/settings/notifications').json()
    assert data['recent_recipient_warnings'][0]['conversation_id'] == 26
    assert data['recipient_access_gaps']


def test_invalid_primary_never_assigns_extra_dispatcher(session_factory, monkeypatch):
    notification_setup(session_factory, monkeypatch)
    configure(session_factory)
    with respx.mock() as router:
        note = remote(router, None, members=(10,))
        dispatch.process_notification_delivery(session_factory)
        assert 'mention://user/10/' in json.loads(note.calls[0].request.content)['content']
    with session_factory() as db:
        assert db.scalar(select(NotificationDelivery)).error_code == 'notification_recipients_skipped:7,11'


@pytest.mark.parametrize('failure,status,count', [('no_members', 'dead', 0), ('timeout', 'submission_unknown', 1), ('bad_receipt', 'submission_unknown', 1)])
def test_failed_or_uncertain_delivery_is_not_repeated(session_factory, monkeypatch, failure, status, count):
    notification_setup(session_factory, monkeypatch)
    configure(session_factory)
    with respx.mock(assert_all_called=False) as router:
        note = remote(router, members=() if failure == 'no_members' else (7, 9, 10, 11))
        if failure == 'timeout':
            note.mock(side_effect=httpx.ReadTimeout('uncertain'))
        if failure == 'bad_receipt':
            note.respond(200, json={'id': 99, 'private': False})
        assert dispatch.process_notification_delivery(session_factory)
        assert not dispatch.process_notification_delivery(session_factory)
        assert note.call_count == count
    with session_factory() as db:
        assert db.scalar(select(NotificationDelivery)).status == status


def test_overdue_then_advisor_claim_cancels_queued_reminder(session_factory, monkeypatch):
    notification_setup(session_factory, monkeypatch)
    configure(session_factory)
    with respx.mock() as router:
        note = remote(router)
        dispatch.process_notification_delivery(session_factory)
        with session_factory() as db:
            db.scalar(select(HandoffTask)).sla_due_at = iso(dt(utcnow()) - timedelta(minutes=1))
            db.commit()
        assert dispatch.process_handoff_overdue(session_factory)
        assert not dispatch.process_handoff_overdue(session_factory)
        with session_factory() as db:
            task = db.scalar(select(HandoffTask))
            task.status = 'claimed'
            task.claimed_at = utcnow()
            db.commit()
        dispatch.process_notification_delivery(session_factory)
        assert note.call_count == 1
    with session_factory() as db:
        assert [d.status for d in db.scalars(select(NotificationDelivery).order_by(NotificationDelivery.id))] == ['delivered', 'cancelled']


def test_overdue_notifies_same_union_and_other_events_do_not(session_factory, monkeypatch):
    notification_setup(session_factory, monkeypatch)
    configure(session_factory)
    with respx.mock() as router:
        note = remote(router)
        dispatch.process_notification_delivery(session_factory)
        with session_factory() as db:
            db.scalar(select(HandoffTask)).sla_due_at = iso(dt(utcnow()) - timedelta(minutes=1))
            db.commit()
        dispatch.process_handoff_overdue(session_factory)
        dispatch.process_notification_delivery(session_factory)
        assert note.call_count == 2
        assert json.loads(note.calls[1].request.content)['content'].count('mention://') == 4
        with session_factory() as db:
            n = Notification(tenant_id=1, event_type='customer.message', title='消息', body='需要处理', conversation_id=26)
            db.add(n); db.flush()
            db.add(NotificationDelivery(notification_id=n.id)); db.commit()
        dispatch.process_notification_delivery(session_factory)
        assert json.loads(note.calls[2].request.content)['content'].count('mention://') == 1


def test_save_refresh_and_old_client_preserve_settings(authenticated, session_factory):
    client, csrf = authenticated
    headers = {'X-CSRF-Token': csrf}
    with session_factory() as db:
        save_setting(db, 'notification_settings', {'enabled': True, 'channel': 'chatwoot', 'agent_id': 7,
            'bot_id': 8, 'assign_on_handoff': True, 'event_types': ['handoff.created', 'handoff.overdue']})
        db.commit()
    assert client.get('/v1/settings/notifications').json()['handoff_notify_agent_ids'] == []
    result = client.patch('/v1/settings/notifications', json={'handoff_notify_agent_ids': [7, 10, 11, 7]}, headers=headers)
    assert result.status_code == 200, result.text
    saved = client.get('/v1/settings/notifications').json()
    assert saved['handoff_notify_agent_ids'] == [7, 10, 11]
    assert saved['enabled'] and saved['assign_on_handoff'] and saved['bot_id'] == 8
    assert saved['event_types'] == ['handoff.created', 'handoff.overdue']
    client.patch('/v1/settings/notifications', json={'bot_id': 12}, headers=headers)
    assert client.get('/v1/settings/notifications').json()['handoff_notify_agent_ids'] == [7, 10, 11]
    assert client.patch('/v1/settings/notifications', json={'handoff_notify_agent_ids': [-1]}, headers=headers).status_code == 422
    assert client.patch('/v1/settings/notifications', json={'handoff_notify_agent_ids': []}, headers=headers).status_code == 200
    assert client.get('/v1/settings/notifications').json()['handoff_notify_agent_ids'] == []
