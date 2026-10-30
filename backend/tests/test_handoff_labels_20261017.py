"""Ownership, confirmed labels and concise advisor notifications in isolated tests."""
import json
import pytest
import respx
from sqlalchemy import select
from app.models import ConversationState, ConversationLabelSync, HandoffTask, MessageEvent, Notification, OutboundMessage, utcnow
from app.reception_v3 import live, service, human_followup as hf
from app.handoff_text import handoff_summary, notification_summary
from app import notification_dispatch as dispatch
from app.operations import ensure_handoff, save_setting
from app.reception_config import get_reception_configuration, SETTING_KEY
from test_human_return_20261016 import prepared, arm, sync, activate
from test_live_audit_fixes import event
from test_auto_advisor_followup_20261017 import enable
from test_runtime_reliability import notification_setup, mock_chatwoot


def test_own_human_label_echo_preserves_wait_and_timer(session_factory, monkeypatch):
    task = prepared(session_factory, monkeypatch)
    arm(session_factory, task)
    with session_factory() as db:
        row = live.session_for(db, 1)
        due = service.state(row)['next_check_at']
        event(db, 'conversation_updated', ['人工接管'], 30)
        assert hf.waiting(row) and service.state(row)['next_check_at'] == due
        assert not row.controls['auto_followup_paused']
        assert db.get(HandoffTask, task).status == 'completed'
        assert len(db.scalars(select(HandoffTask)).all()) == 1
        assert not live.permitted(db, db.get(ConversationState, 1), purpose='followup')


def test_waiting_customer_reply_keeps_human_labels_and_cancels_timer(session_factory, monkeypatch):
    task = prepared(session_factory, monkeypatch)
    arm(session_factory, task)
    fake = sync(session_factory, monkeypatch)
    with session_factory() as db:
        event(db, 'message_created', ['人工接管'], 30, 102, 'incoming')
        row = live.session_for(db, 1)
        assert not hf.enabled(row) and row.controls['human']
        assert service.state(row)['next_check_at'] is None
        assert db.get(ConversationLabelSync, 1).target == 'human'
        assert not db.scalar(select(OutboundMessage.id))
    assert '人工接管' in fake.current_labels and 'ai' not in fake.current_labels


def test_advisor_reply_during_active_followup_restores_human_wait(session_factory, monkeypatch):
    task = prepared(session_factory, monkeypatch)
    arm(session_factory, task); activate(session_factory)
    fake = sync(session_factory, monkeypatch)
    assert 'AI跟进' in fake.current_labels
    with session_factory() as db:
        event(db, 'message_created', ['ai', 'AI跟进'], 30, 102, 'outgoing')
        row = live.session_for(db, 1)
        assert hf.waiting(row)
        assert row.controls['human_followup']['last_human_message_id'] == 102
        assert db.get(ConversationLabelSync, 1).target == 'human'
        assert not live.permitted(db, db.get(ConversationState, 1), purpose='followup')
    sync(session_factory, monkeypatch, fake)
    assert set(fake.current_labels) == {'人工接管', '外部新增'}


def test_manual_ai_label_returns_ownership_without_rewriting_operator_labels(session_factory, monkeypatch):
    prepared(session_factory, monkeypatch)
    fake = sync(session_factory, monkeypatch)
    with session_factory() as db:
        assert db.get(ConversationLabelSync, 1).status == 'synced'
        event(db, 'conversation_updated', ['ai'], 30)
        job = db.get(ConversationLabelSync, 1)
        assert job.target == 'ai' and job.status == 'synced'
        assert live.permitted(db, db.get(ConversationState, 1))
    fake.current_labels = ['ai', '外部新增']
    from app.conversation_labels import process_label_sync
    assert not process_label_sync(session_factory)
    assert set(fake.current_labels) == {'ai', '外部新增'}


@pytest.mark.parametrize('blocker', ['complaint', 'contact_block', 'disabled', 'closed', 'global_off', 'unanswered'])
def test_new_protection_during_wait_prevents_activation(session_factory, monkeypatch, blocker):
    task = enable(session_factory, monkeypatch)
    arm(session_factory, task)
    sync(session_factory, monkeypatch)
    with session_factory() as db:
        row = live.session_for(db, 1); conv = db.get(ConversationState, 1)
        row.controls = {**row.controls, 'human_followup': {**row.controls['human_followup'], 'mode': 'automatic'}}
        row.virtual_now = row.controls['human_followup']['resume_at']
        if blocker == 'complaint': conv.labels = ['人工接管', '客诉']
        if blocker == 'contact_block': conv.contact.labels = ['人工接管']
        if blocker == 'disabled':
            config = get_reception_configuration(db)
            config['silence']['advisor_reply_followup_enabled'] = False
            save_setting(db, SETTING_KEY, config)
        if blocker == 'closed': conv.can_reply = False
        if blocker == 'global_off': save_setting(db, 'global_message_sending', {'enabled': False})
        if blocker == 'unanswered':
            db.add(MessageEvent(conversation_state_id=1, chatwoot_message_id=102, direction='incoming', content='还有问题'))
        db.flush()
        hf.activate_due(db, conv, row)
        assert not hf.enabled(row) and row.controls['human']
        assert db.get(ConversationLabelSync, 1).target == 'human'
        assert not db.scalar(select(OutboundMessage.id))


def test_followup_label_sync_failure_never_allows_sending(session_factory, monkeypatch):
    task = prepared(session_factory, monkeypatch)
    arm(session_factory, task)
    fake = sync(session_factory, monkeypatch)
    activate(session_factory)
    original = fake.set_conversation_labels
    def offline(*args): raise RuntimeError('offline')
    monkeypatch.setattr(fake, 'set_conversation_labels', offline)
    sync(session_factory, monkeypatch, fake)
    with session_factory() as db:
        assert db.get(ConversationLabelSync, 1).attempts == 1
        assert not live.permitted(db, db.get(ConversationState, 1), purpose='followup')
        assert not db.scalar(select(OutboundMessage.id))
        db.get(ConversationLabelSync, 1).due_at = utcnow(); db.commit()
    monkeypatch.setattr(fake, 'set_conversation_labels', original)
    sync(session_factory, monkeypatch, fake)
    with session_factory() as db:
        assert live.permitted(db, db.get(ConversationState, 1), purpose='followup')
    assert set(fake.current_labels) == {'ai', 'AI跟进', '外部新增'}


@pytest.mark.parametrize('code,detail,scene,reason', [
    ('custom', "{'party_size': 4, 'route_id': 'tibet'}\n4位客人希望全程含午晚餐，需要确认安排和费用。更多解释。", '定制需求', '4位客人希望全程含午晚餐，需要确认安排和费用。'),
    ('model_error', 'RuntimeError: agent_failed {retry: 3}', '自动回复异常', '自动回复未完成，请顾问接待。'),
    ('label', 'Chatwoot 标签触发：人工接管', '人工接待', '会话已标记为需要顾问处理。'),
    ('contact', '客户已提供 LINE 联系方式。', '收到联系方式', '客户已提供 LINE 联系方式。'),
])
def test_plain_scene_and_reason(code, detail, scene, reason):
    result = handoff_summary(code, detail)
    assert result == {'scene': scene, 'reason': reason, 'text': f'场景：{scene}\n原因：{reason}'}


def test_handoff_api_formats_old_records_without_destroying_internal_code(authenticated, session_factory, monkeypatch):
    client, _ = authenticated
    task = prepared(session_factory, monkeypatch)
    with session_factory() as db:
        row = db.get(HandoffTask, task)
        row.reason_code = 'custom'
        row.reason_detail = "{'route_id': 'winter'}\n4位客人希望独立成团，需要核实费用。第二句不展示。"
        db.commit()
    response = client.get('/v1/handoffs?status=claimed').json()
    item = next(x for x in response['items'] if x['id'] == task)
    assert item['reason_label'] == '定制需求'
    assert item['reason_detail'] == '4位客人希望独立成团，需要核实费用。'
    assert item['reason_code'] == 'custom'


def test_notification_is_two_chinese_lines_with_mention_and_no_debug_footer(session_factory, monkeypatch):
    notification_setup(session_factory, monkeypatch)
    with session_factory() as db:
        note = db.scalar(select(Notification))
        note.body = '场景：定制需求\n原因：4位客人希望全程含午晚餐，需要确认安排和费用。'
        db.commit()
    with respx.mock() as router:
        sent = mock_chatwoot(router)
        assert dispatch.process_notification_delivery(session_factory)
        assert not dispatch.process_notification_delivery(session_factory)
        assert sent.call_count == 1
        payload = json.loads(sent.calls[0].request.content)
        assert payload['private'] is True and payload['sender_type'] == 'AgentBot'
        assert payload['content'] == '[@顾问 7](mention://user/7/%E9%A1%BE%E9%97%AE%207)\n场景：定制需求\n原因：4位客人希望全程含午晚餐，需要确认安排和费用。'
        assert 'ai-notification:' not in payload['content']


def test_overdue_notification_hides_diagnostic_text():
    assert notification_summary('handoff.overdue', '人工接管 SLA 超时', 'handoff:12 · 会话 #26 已超过 SLA') == '场景：接待提醒\n原因：客户仍在等待，请及时回复。'
