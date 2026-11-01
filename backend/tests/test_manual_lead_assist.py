"""Manual LINE reception and advisor notification without exclusive takeover."""
from copy import deepcopy
from datetime import timedelta
from unittest.mock import Mock

import pytest
from fastapi import HTTPException
from sqlalchemy import select, func

from app import notification_dispatch
from app.automation_models import AutomationRun
from app.models import (ConversationState, ConversationLabelSync, HandoffTask, MessageEvent,
                        Notification, NotificationDelivery, User, utcnow)
from app.operations import ensure_handoff, save_setting, setting_value
from app.reception_config import get_reception_configuration
from app.reception_v3 import live, service, human_followup
from app.reception_v3.lead_assist import notify, contact_key
from app.reception_v3.runtime import Decision
from app.reception_v3.skills import SkillRegistry
from app.runtime_settings import dt, iso
from live_fixture import setup
from test_live_audit_fixes import session, event
from test_reception_v3 import bundle


def configure(db, manual=False):
    spec = get_reception_configuration(db)
    spec['routing'].update(live_inbox_ids=[128859], manual_inbox_ids=[128859] if manual else [])
    spec['lead_capture']['after_contact'] = 'assist_until_advisor'
    save_setting(db, 'route_reception_config', spec)
    save_setting(db, 'notification_settings', {'enabled': True, 'channel': 'chatwoot',
        'agent_id': 7, 'bot_id': 8, 'event_types': ['handoff.created', 'handoff.overdue']})
    return {**bundle(), 'lead_capture': spec['lead_capture']}


def captured(db, manual=False):
    spec = configure(db, manual)
    row = session(db)
    row.memory = {'contact_channel': 'Email', 'contact_value': 'customer@example.com', 'party_size': 2}
    row.controls = {**row.controls, 'route_variant': 'nine'}
    value = service.state(row)
    notify(db, row, value, {'handoff_reason': '客户已提供邮箱，询问是否含餐'}, spec)
    service.save(row, value)
    db.commit()
    return row, spec


def test_contact_notice_and_answer_are_one_transaction_without_human_labels(session_factory, monkeypatch):
    setup(session_factory, monkeypatch)
    with session_factory() as db:
        spec = configure(db)
        row = session(db)
        run = AutomationRun(session_id=row.id, generation=row.generation, module='reply',
                            idempotency_key='contact-reply', input_snapshot={'event': 'customer_message'})
        db.add(run); db.flush()
        decision = Decision(notify_contact=True, interrupt=True, route_variant='nine',
            handoff_type='contact', handoff_reason='提供邮箱并问餐食',
            profile={'contact_channel': 'Email', 'contact_value': 'customer@example.com', 'party_size': 2},
            messages=[{'text': 'Breakfast is included. Lunch and dinner are not included.'}]).model_dump()
        monkeypatch.setattr(service, 'candidate_materials', lambda *a: [])
        service.apply_decision(db, row, run, spec, decision)
        db.commit()
        task = db.scalar(select(HandoffTask))
        assert task.status == 'pending' and task.ai_assist_allowed
        assert db.scalar(select(func.count(Notification.id))) == 1
        assert db.scalar(select(func.count(NotificationDelivery.id))) == 1
        assert not db.get(ConversationLabelSync, 1)
        assert live.permitted(db, db.get(ConversationState, 1))
        assert row.controls['simulation']['status'] == 'running'
        assert any(m.get('status') == 'draft' and 'Breakfast' in m['content'] for m in row.messages)
        assert service.state(row)['lead_assist'] and not service.state(row)['next_check_at']
        event(db, 'message_created', ['ai'], 11, 110, 'incoming')
        assert service.state(row)['pending_event'] == 'customer_message'
        assert live.permitted(db, db.get(ConversationState, 1))
        assert db.get(ConversationState, 1).effective_ai_state == 'AI_ACTIVE'


def test_same_contact_is_not_renotified_after_retry_or_operator_resume(session_factory, monkeypatch):
    setup(session_factory, monkeypatch)
    with session_factory() as db:
        row, spec = captured(db)
        value = service.state(row)
        row.memory = {**row.memory, 'contact_value': 'CUSTOMER@example.com'}
        notify(db, row, value, {}, spec)
        service.save(row, value)
        event(db, 'conversation_updated', [], 20)
        event(db, 'conversation_updated', ['ai'], 21)
        value = service.state(row)
        notify(db, row, value, {}, spec)
        assert db.scalar(select(func.count(HandoffTask.id))) == 1
        assert db.scalar(select(func.count(Notification.id))) == 1
        assert db.scalar(select(HandoffTask)).status == 'cancelled'
        assert not value.get('handoff')
        assert row.controls['simulation']['status'] == 'running'


@pytest.mark.parametrize('signal', ['human_reply', 'remove_ai', 'human_label', 'global_off'])
def test_lead_assistance_stops_on_operator_or_global_control(session_factory, monkeypatch, signal):
    setup(session_factory, monkeypatch)
    with session_factory() as db:
        row, _ = captured(db, manual=True)
        row.messages = [{'id': 'queued-answer', 'content': 'not sent yet', 'status': 'draft'}]
        if signal == 'human_reply':
            event(db, 'message_created', ['ai'], 20, 120, 'outgoing')
            task = db.scalar(select(HandoffTask))
            assert task.status == 'claimed' and task.sla_due_at is None
        elif signal == 'global_off':
            save_setting(db, 'global_message_sending', {'enabled': False})
        else:
            event(db, 'conversation_updated', [] if signal == 'remove_ai' else ['ai', '人工接管'], 20)
        assert not live.permitted(db, db.get(ConversationState, 1))
        if signal != 'global_off':
            assert row.controls['simulation']['status'] == 'stopped'
            assert next(m for m in row.messages if m['id'] == 'queued-answer')['status'] == 'cancelled'
        assert not human_followup.enabled(row)


def test_claim_pauses_before_network_and_uncertain_receipt_stays_paused(session_factory, monkeypatch):
    from app.ops_api import claim_for
    setup(session_factory, monkeypatch)
    with session_factory() as db:
        row, _ = captured(db)
        user = db.get(User, 1); user.chatwoot_agent_id = 7; db.commit()
        remote = Mock()
        def uncertain(*args):
            with session_factory() as other:
                assert not live.permitted(other, other.get(ConversationState, 1))
                assert live.session_for(other, 1).controls['simulation']['status'] == 'stopped'
            raise TimeoutError('uncertain assignment')
        remote.assign_conversation.side_effect = uncertain
        monkeypatch.setattr('app.ops_api.client_for', lambda _: remote)
        task = db.scalar(select(HandoffTask))
        with pytest.raises(HTTPException) as error:
            claim_for(db, task, db.get(ConversationState, 1), user, user, task.version)
        assert error.value.detail['code'] == 'assignment_unknown'
        assert not live.permitted(db, db.get(ConversationState, 1))
        assert task.assignment_target_user_id == user.id


def test_overdue_reminder_does_not_take_back_ai_and_advisor_reply_cancels_reminder(session_factory, monkeypatch):
    setup(session_factory, monkeypatch)
    with session_factory() as db:
        row, _ = captured(db, manual=True)
        task = db.scalar(select(HandoffTask))
        task.sla_due_at = iso(dt(utcnow()) - timedelta(seconds=1))
        db.commit()
    assert notification_dispatch.process_handoff_overdue(session_factory)
    assert not notification_dispatch.process_handoff_overdue(session_factory)
    with session_factory() as db:
        assert live.permitted(db, db.get(ConversationState, 1))
        assert db.scalar(select(func.count(Notification.id))) == 2
        event(db, 'message_created', ['ai'], 22, 122, 'outgoing')
        db.scalars(select(NotificationDelivery).order_by(NotificationDelivery.id)).first().status = 'delivered'
        db.commit()
    assert notification_dispatch.process_notification_delivery(session_factory)
    with session_factory() as db:
        last = db.scalars(select(NotificationDelivery).order_by(NotificationDelivery.id)).all()[-1]
        assert last.status == 'cancelled'


def test_explicit_human_request_after_contact_retains_request_reason(session_factory, monkeypatch):
    setup(session_factory, monkeypatch)
    with session_factory() as db:
        row, _ = captured(db)
        # Complete this lead notification so the requested-human handoff is new.
        db.scalar(select(HandoffTask)).status = 'completed'
        value = service.state(row); value.update(handoff_type='requested', handoff_reason='客户要求真人接手')
        service.save(row, value)
        live.handoff_from_decision(db, row)
        task = db.scalars(select(HandoffTask).order_by(HandoffTask.id)).all()[-1]
        assert task.reason_code == 'requested' and not task.ai_assist_allowed
        assert not live.permitted(db, db.get(ConversationState, 1))


def test_existing_hard_handoff_cannot_be_downgraded_to_assistance(session_factory, monkeypatch):
    setup(session_factory, monkeypatch)
    with session_factory() as db:
        task = ensure_handoff(db, db.get(ConversationState, 1), 'requested')
        same = ensure_handoff(db, db.get(ConversationState, 1), 'contact', allow_ai_assist=True)
        assert task is same and not task.ai_assist_allowed
        assert not live.permitted(db, db.get(ConversationState, 1))


def test_manual_channel_does_not_enable_new_customer_or_old_automatic_timer(session_factory, monkeypatch):
    setup(session_factory, monkeypatch)
    with session_factory() as db:
        configure(db, manual=True)
        conv = db.get(ConversationState, 1)
        conv.inbox.channel_type = 'Channel::Line'
        conv.labels = []; conv.ai_mode = 'disabled'; conv.ai_label_present = False
        save_setting(db, 'live_reply', {**setting_value(db, 'live_reply', {}), 'auto_new_customers_since': utcnow()})
        monkeypatch.setattr(live, 'client_for', lambda *_: pytest.fail('manual new customer must not call Chatwoot'))
        live.enable_new_customer(db, conv, db.get(MessageEvent, 1))
        assert not conv.labels and live.session_for(db, 1) is None
        row = session(db)
        row.controls = {**row.controls, 'human_followup': {'enabled': True, 'phase': 'waiting',
            'mode': 'automatic', 'resume_at': iso(dt(utcnow())-timedelta(minutes=1))}}
        human_followup.activate_due(db, conv, row)
        assert row.controls['simulation']['status'] == 'stopped'
        assert not human_followup.enabled(row)
        assert not live.permitted(db, conv)


def test_manual_ai_reads_advisor_history_and_only_answers_outstanding_customer(session_factory, monkeypatch):
    setup(session_factory, monkeypatch)
    with session_factory() as db:
        configure(db, manual=True)
        conv = db.get(ConversationState, 1)
        conv.labels = ['人工接管']; conv.ai_mode = 'disabled'; conv.ai_label_present = False
        first = db.get(MessageEvent, 1); first.content = '想去西藏'; first.created_at = iso(dt(utcnow())-timedelta(minutes=4))
        db.add(MessageEvent(conversation_state_id=1, chatwoot_message_id=101, direction='outgoing',
            attribution='human', content='您之前有跟我們聊過嗎？幾位同行？', created_at=iso(dt(utcnow())-timedelta(minutes=3))))
        db.add(MessageEvent(conversation_state_id=1, chatwoot_message_id=102, direction='incoming',
            content='第一次問，兩位，想看桃花。含早餐嗎？', created_at=iso(dt(utcnow())-timedelta(minutes=2))))
        db.flush()
        event(db, 'conversation_updated', ['ai'], 20)
        row = live.session_for(db, 1)
        assert live.permitted(db, conv)
        assert len(row.messages) == 3
        assert row.messages[1]['direction'] == 'outgoing' and row.messages[1]['attribution'] == 'human'
        assert service.state(row)['buffered_questions'] == ['cw-102']
        assert row.controls['last_advisor_event_id'] == 101
        # A delayed duplicate advisor webhook must not take ownership again.
        live.record_human_reply(db, conv, db.scalar(select(MessageEvent).where(MessageEvent.chatwoot_message_id == 101)))
        assert live.permitted(db, conv)
        generation = row.generation
        event(db, 'conversation_updated', ['ai'], 21)
        assert row.generation == generation


def test_lead_assistance_never_schedules_proactive_followup(session_factory, monkeypatch):
    setup(session_factory, monkeypatch)
    with session_factory() as db:
        row, spec = captured(db)
        value = service.state(row)
        value['next_check_at'] = iso(dt(utcnow())-timedelta(minutes=1))
        service.schedule_followup(value, spec, row.virtual_now, reset=True)
        assert value['next_check_at'] is None
        assert value['next_check_minutes'] is None


def test_new_contact_configuration_is_visible_in_loaded_skill():
    spec = bundle()
    spec['lead_capture'] = {'after_contact': 'assist_until_advisor', 'customer_initiated_channels': ['Email']}
    skill = SkillRegistry(spec).load('tibet-reception')
    assert 'assist_until_advisor' in str(skill) and 'customer_initiated_channels' in str(skill)


def test_contact_execution_flag_cannot_silently_be_saved_as_profile_data():
    with pytest.raises(ValueError, match='notify_contact'):
        Decision(profile={'notify_contact': True})
    assert Decision(notify_contact=True).notify_contact


def test_routing_handoff_rules_keep_lead_assistance_through_overdue(session_factory, monkeypatch):
    from app.advisor_assignment import Rule
    from test_advisor_assignment import configure as assignment_rules
    setup(session_factory, monkeypatch)
    with session_factory() as db:
        assignment_rules(db, rules=[Rule(id='leads', name='留资派单', event='handoff.contact', action='handoff', agent_ids=[11]).model_dump(),
                                   Rule(id='overdue', name='超时派单', event='handoff.overdue', action='handoff', agent_ids=[12]).model_dump()])
        row, _ = captured(db)
        task = db.scalar(select(HandoffTask)); task.sla_due_at = iso(dt(utcnow())-timedelta(seconds=1)); db.commit()
        assert live.permitted(db, db.get(ConversationState, 1))
    assert notification_dispatch.process_handoff_overdue(session_factory)
    with session_factory() as db:
        assert db.scalar(select(HandoffTask)).ai_assist_allowed
        assert live.permitted(db, db.get(ConversationState, 1))
        assert not db.get(ConversationLabelSync, 1)


def test_migration_preserves_existing_handoff_ownership():
    import importlib.util
    from pathlib import Path
    from sqlalchemy import create_engine, text
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    path = Path(__file__).parents[1] / 'alembic/versions/a038_lead_assist.py'
    spec = importlib.util.spec_from_file_location('lead_migration', path)
    migration = importlib.util.module_from_spec(spec); spec.loader.exec_module(migration)
    engine = create_engine('sqlite://')
    with engine.begin() as connection:
        connection.execute(text('CREATE TABLE handoff_tasks (id INTEGER PRIMARY KEY, status TEXT)'))
        connection.execute(text("INSERT INTO handoff_tasks (status) VALUES ('pending'), ('claimed')"))
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            assert connection.execute(text('SELECT status, ai_assist_allowed FROM handoff_tasks')).all() == [('pending', 0), ('claimed', 0)]
            migration.downgrade()
        assert connection.execute(text('SELECT status FROM handoff_tasks')).scalars().all() == ['pending', 'claimed']
    engine.dispose()


def test_channel_mode_save_refresh_and_partial_legacy_save_preserve_other_settings(authenticated, session_factory):
    from test_channel_ai_settings import seed
    client, csrf = authenticated; seed(session_factory)
    with session_factory() as db:
        old = deepcopy(get_reception_configuration(db))
    before = client.get('/v1/settings/channel-ai').json()
    data = {'revision': before['revision'], 'changes': [{'id': 1, 'enabled': True, 'activation_mode': 'manual'}]}
    result = client.patch('/v1/settings/channel-ai', json=data, headers={'X-CSRF-Token': csrf})
    assert result.status_code == 200
    assert result.json() == client.get('/v1/settings/channel-ai').json()
    assert result.json()['items'][0]['activation_mode'] == 'manual'
    with session_factory() as db:
        old['routing']['manual_inbox_ids'] = [100]
        assert get_reception_configuration(db) == old
    assert client.patch('/v1/settings/channel-ai', json=data, headers={'X-CSRF-Token': csrf}).status_code == 409
    data = {'revision': result.json()['revision'], 'changes': [{'id': 1, 'enabled': False}]}
    result = client.patch('/v1/settings/channel-ai', json=data, headers={'X-CSRF-Token': csrf})
    assert result.status_code == 200 and result.json()['items'][0]['activation_mode'] == 'manual'


def test_lead_settings_save_refresh_and_disable_preserve_other_configuration(authenticated, session_factory):
    client, csrf = authenticated
    with session_factory() as db:
        original = get_reception_configuration(db)
        original['routing']['manual_inbox_ids'] = [200]
        save_setting(db, 'route_reception_config', original)
        db.commit()
    original = client.get('/v1/automation/reception-config').json()['config']
    changes = {'after_contact': 'assist_until_advisor', 'customer_initiated_channels': ['Email']}
    response = client.patch('/v1/automation/reception-config', json={'lead_capture': changes},
                            headers={'X-CSRF-Token': csrf})
    assert response.status_code == 200, response.text
    expected = deepcopy(original)
    expected['lead_capture'].update(changes)
    assert response.json()['config'] == expected
    assert client.get('/v1/automation/reception-config').json()['config'] == expected
    response = client.patch('/v1/automation/reception-config',
        json={'lead_capture': {'after_contact': 'handoff'}}, headers={'X-CSRF-Token': csrf})
    assert response.status_code == 200, response.text
    expected['lead_capture']['after_contact'] = 'handoff'
    assert client.get('/v1/automation/reception-config').json()['config'] == expected
