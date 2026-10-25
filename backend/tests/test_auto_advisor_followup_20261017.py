"""Exercise real webhook/control paths in an isolated database; no customer sends."""
from copy import deepcopy
from datetime import timedelta
import pytest
from sqlalchemy import select
from app.models import ConversationState, ConversationLabelSync, HandoffTask, MessageEvent, OutboundMessage, utcnow
from app.operations import save_setting
from app.reception_config import get_reception_configuration, SETTING_KEY
from app.reception_v3 import live, service, human_followup as hf
from app.runtime_settings import dt
from test_human_return_20261016 import prepared, sync, activate
from test_live_audit_fixes import event


def test_automatic_timer_runs_model_and_submits_one_followup(session_factory, monkeypatch):
    from test_reception_v3 import bundle, answer
    from test_conversation_controls import FakeChatwoot
    from app.automation_models import AutomationRun
    enable(session_factory, monkeypatch)
    rules = bundle()
    rules['silence'].update(live_enabled=True, daily_at='09:00', active_start='07:00', active_end='01:00', max_proactive_messages_per_day=1)
    monkeypatch.setattr(service, 'compile_skills', lambda db: deepcopy(rules))
    monkeypatch.setattr(service, 'candidate_materials', lambda *a: [])
    monkeypatch.setattr(service, 'active_web_facts', lambda *a, **k: ([], 'test'))
    captured = []
    def model(context):
        captured.append(context)
        return answer(messages=[{'text': '您和家人討論得如何呢？'}])
    monkeypatch.setattr(service, 'run_agent', model)
    with session_factory() as db:
        event(db, 'message_created', ['ai'], 30, 102, 'outgoing')
    fake = sync(session_factory, monkeypatch)
    sent = []
    fake.create_text_message = lambda cid, content: (sent.append((cid, content)) or {'id': 900, 'status': 'sent'})
    monkeypatch.setattr(live, 'client_for', lambda c: fake)
    with session_factory() as db:
        row = live.session_for(db, 1)
        row.virtual_now = service.state(row)['next_check_at']
        wall = row.controls['simulation']['last_wall_at']
        service.tick(db, session_id=row.id, wall_now=wall, environment='live')
        assert not captured  # No model or message before the new labels are confirmed.
        assert db.get(ConversationLabelSync, 1).target == 'followup'
        assert db.get(ConversationLabelSync, 1).status == 'pending'
    sync(session_factory, monkeypatch, fake)
    with session_factory() as db:
        row = live.session_for(db, 1)
        service.tick(db, session_id=row.id, wall_now=wall, environment='live')
        assert len(captured) == 1 and captured[0]['state']['human_followup']
        assert captured[0]['messages'][-1]['content'] == 'hello'
        assert db.scalar(select(AutomationRun)).status == 'completed'
        assert len([m for m in row.messages if m.get('status') == 'draft']) == 1
        live.deliver_one(db, row)
        live.deliver_one(db, row)
        assert sent == [(26, '您和家人討論得如何呢？')]
        assert len(db.scalars(select(OutboundMessage)).all()) == 1
        assert db.scalar(select(OutboundMessage)).status == 'submitted'
        assert not any(m.get('v3_kind') == 'introduction' for m in row.messages)


def enable(factory, monkeypatch):
    task = prepared(factory, monkeypatch)
    with factory() as db:
        config = get_reception_configuration(db)
        config['silence'].update(enabled=True, live_enabled=True, advisor_reply_followup_enabled=True)
        save_setting(db, SETTING_KEY, config)
        db.commit()
    return task


def test_public_reply_automatically_arms_and_customer_returns_to_advisor(session_factory, monkeypatch):
    task = enable(session_factory, monkeypatch)
    with session_factory() as db:
        event(db, 'message_created', ['ai'], 30, 102, 'outgoing')
        row = live.session_for(db, 1)
        assert hf.enabled(row) and row.controls['human_followup']['mode'] == 'automatic'
        assert db.get(HandoffTask, task).status == 'completed'
        assert not live.permitted(db, db.get(ConversationState, 1), purpose='followup')
        assert db.get(ConversationLabelSync, 1).target == 'human'
        assert not db.scalar(select(OutboundMessage.id))
    fake = sync(session_factory, monkeypatch)
    assert '人工接管' in fake.current_labels and 'ai' not in fake.current_labels
    with session_factory() as db:
        row = live.session_for(db, 1)
        assert not live.permitted(db, db.get(ConversationState, 1), purpose='followup')
        assert not live.permitted(db, db.get(ConversationState, 1))
        due = service.state(row)['next_check_at']
        row.virtual_now = (dt(due)-timedelta(seconds=1)).isoformat()
        service.advance(row, wall_now=row.controls['simulation']['last_wall_at'])
        assert service.state(row)['pending_event'] is None
        row.virtual_now = due
        service.advance(row, wall_now=row.controls['simulation']['last_wall_at'])
        assert service.state(row)['pending_event'] == 'silence_due'
        hf.activate_due(db, db.get(ConversationState, 1), row)
        assert not live.permitted(db, db.get(ConversationState, 1), purpose='followup')
        db.commit()
    sync(session_factory, monkeypatch, fake)
    assert set(fake.current_labels) == {'ai', 'AI跟进', '外部新增'}
    with session_factory() as db:
        row = live.session_for(db, 1)
        assert live.permitted(db, db.get(ConversationState, 1), purpose='followup')
        event(db, 'message_created', ['ai', 'AI跟进'], 31, 103, 'incoming')
        assert not hf.enabled(row) and row.controls['human']
        assert service.state(row)['next_check_at'] is None
        # A duplicate old advisor webhook must not release the new human task.
        old = db.scalar(select(MessageEvent).where(MessageEvent.chatwoot_message_id == 102))
        live.record_human_reply(db, db.get(ConversationState, 1), old)
        assert not hf.enabled(row)
        # The advisor's next actual reply starts a fresh wait.
        event(db, 'message_created', ['人工接管'], 32, 104, 'outgoing')
        assert hf.enabled(row)
        assert row.controls['human_followup']['last_human_message_id'] == 104


def test_no_task_required_and_no_backfill_on_config_enable(session_factory, monkeypatch):
    task = enable(session_factory, monkeypatch)
    with session_factory() as db:
        row = live.session_for(db, 1)
        assert not hf.enabled(row)  # enabling does not touch existing human sessions
        db.get(HandoffTask, task).status = 'completed'
        event(db, 'message_created', ['ai'], 30, 102, 'outgoing')
        assert hf.enabled(row) and row.controls['human_followup']['task_id'] is None


@pytest.mark.parametrize('blocker', ['disabled', 'optout', 'complaint', 'lead', 'unknown', 'closed',
    'expired', 'manual_pause', 'wrong_inbox', 'global_off', 'assignment', 'other_task', 'stale', 'failed'])
def test_automatic_return_respects_protection(session_factory, monkeypatch, blocker):
    task = enable(session_factory, monkeypatch)
    with session_factory() as db:
        row = live.session_for(db, 1)
        conv = db.get(ConversationState, 1)
        msg = db.scalar(select(MessageEvent).where(MessageEvent.chatwoot_message_id == 101))
        if blocker == 'disabled':
            config = get_reception_configuration(db)
            config['silence']['advisor_reply_followup_enabled'] = False
            save_setting(db, SETTING_KEY, config)
        if blocker == 'optout': service.save(row, {**service.state(row), 'opt_out': True})
        if blocker == 'complaint': conv.labels = ['客诉']
        if blocker == 'lead': conv.labels = ['已留资']
        if blocker == 'unknown': db.add(OutboundMessage(conversation_state_id=1, idempotency_key='unknown', source_type='ai', status='unknown', content='test'))
        if blocker == 'closed': conv.can_reply = False
        if blocker == 'expired': conv.last_customer_message_at = (dt(utcnow())-timedelta(days=8)).isoformat()
        if blocker == 'manual_pause': row.controls = {**row.controls, 'auto_followup_paused': True}
        if blocker == 'wrong_inbox': conv.inbox.chatwoot_inbox_id = 999
        if blocker == 'global_off': save_setting(db, 'global_message_sending', {'enabled': False})
        if blocker == 'assignment': db.get(HandoffTask, task).assignment_target_user_id = 1
        if blocker == 'other_task': db.add(HandoffTask(conversation_state_id=1, status='claimed', reason_code='other'))
        if blocker == 'stale': msg.created_at = (dt(utcnow())-timedelta(minutes=6)).isoformat()
        if blocker == 'failed': msg.status = 'failed'
        db.flush()
        live.record_human_reply(db, conv, msg)
        assert not hf.enabled(row), blocker
        assert not db.scalar(select(OutboundMessage.id).where(OutboundMessage.status != 'unknown'))


def test_operator_pause_stays_paused_after_next_reply(session_factory, monkeypatch):
    enable(session_factory, monkeypatch)
    with session_factory() as db:
        event(db, 'message_created', ['ai'], 30, 102, 'outgoing')
    activate(session_factory)
    sync(session_factory, monkeypatch)
    with session_factory() as db:
        event(db, 'conversation_updated', ['人工接管'], 31)
        row = live.session_for(db, 1)
        assert row.controls['auto_followup_paused']
        event(db, 'message_created', ['人工接管'], 32, 103, 'outgoing')
        assert not hf.enabled(row)


def test_confirmed_human_handoff_still_automatically_returns(session_factory, monkeypatch):
    enable(session_factory, monkeypatch)
    sync(session_factory, monkeypatch)
    with session_factory() as db:
        conv = db.get(ConversationState, 1)
        assert conv.ai_mode == 'disabled' and '人工接管' in conv.labels
        event(db, 'message_created', ['人工接管'], 30, 102, 'outgoing')
        assert hf.enabled(live.session_for(db, 1))


def test_explicit_ai_off_api_prevents_automatic_return(authenticated, session_factory, monkeypatch):
    client, csrf = authenticated
    enable(session_factory, monkeypatch)
    from test_conversation_controls import FakeChatwoot
    monkeypatch.setattr('app.api.client_for', lambda connection: FakeChatwoot())
    result = client.put('/v1/conversations/26/ai-mode', headers={'X-CSRF-Token': csrf}, json={'enabled': False})
    assert result.status_code == 200, result.text
    with session_factory() as db:
        row = live.session_for(db, 1)
        assert row.controls['auto_followup_paused']
        event(db, 'message_created', ['人工接管'], 30, 102, 'outgoing')
        assert not hf.enabled(row)


def test_disabling_automatic_switch_blocks_already_scheduled_send(session_factory, monkeypatch):
    enable(session_factory, monkeypatch)
    with session_factory() as db: event(db, 'message_created', ['ai'], 30, 102, 'outgoing')
    activate(session_factory)
    sync(session_factory, monkeypatch)
    with session_factory() as db:
        config = get_reception_configuration(db)
        config['silence']['advisor_reply_followup_enabled'] = False
        save_setting(db, SETTING_KEY, config)
        row = live.session_for(db, 1)
        assert not live.permitted(db, db.get(ConversationState, 1), purpose='followup')
        value = service.state(row)
        service.append_plan(row, value, [{'content': '您和家人討論得如何呢？', 'content_type': 'text', 'proactive': True}], 'reply', 0, 999)
        service.save(row, value)
        live.deliver_one(db, row)
        assert not db.scalar(select(OutboundMessage.id))


@pytest.mark.parametrize('kind', ['private', 'agent_bot', 'external_echo'])
def test_non_advisor_webhooks_do_not_arm(session_factory, monkeypatch, kind):
    from app.models import WebhookEvent
    from app.live_reply import mirror_event
    enable(session_factory, monkeypatch)
    with session_factory() as db:
        payload = {'event': 'message_created', 'id': 102, 'message_type': 'outgoing', 'content': 'note',
            'created_at': utcnow(), 'sender': {'id': 9, 'type': 'user'}, 'private': False,
            'conversation': {'id': 26, 'inbox_id': 128859}}
        if kind == 'private': payload['private'] = True
        if kind == 'agent_bot': payload['sender']['type'] = 'agent_bot'
        if kind == 'external_echo': payload['content_attributes'] = {'external_echo': True}
        webhook = WebhookEvent(connection_id=1, event='message_created', account_id=180474, resource_id='102',
            idempotency_key=kind, received_at=utcnow(), payload=payload)
        db.add(webhook); db.flush(); mirror_event(db, webhook)
        assert not hf.enabled(live.session_for(db, 1))


@pytest.mark.parametrize('before,after', [
    ('您這趟比較期待的是桃花，還是珠峰呢？', '您這趟期待的是桃花，還是珠峰呢？'),
    ('您這次比較想看冰川藍冰，還是珠峰呢？', '您這次想看冰川藍冰，還是珠峰呢？'),
    ('你比较喜欢哪一条？', '你喜欢哪一条？'),
    ('我們可以比較兩條線路的價格。', '我們可以比較兩條線路的價格。'),
    ('八天行程比較短，價格是11,480元。', '八天行程比較短，價格是11,480元。'),
])
def test_preference_wording_on_actual_outgoing_plan(session_factory, monkeypatch, before, after):
    prepared(session_factory, monkeypatch)
    with session_factory() as db:
        row = live.session_for(db, 1)
        service.append_plan(row, service.state(row), [{'content': before, 'content_type': 'text'}], 'reply', 0, 999)
        assert row.messages[-1]['content'] == after
