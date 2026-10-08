from copy import deepcopy
import json
from sqlalchemy import select

from app.models import ConversationState, MessageEvent, HandoffTask, Notification
from app.reception_v3 import live, service, runtime, skills
from scripts.update_line_qr_wording import update_configuration, update_package, QR_INVITATION
from live_fixture import setup
from test_reception_v3_skills import context, model, final


def test_wording_migration_preserves_other_config_and_is_idempotent():
    original = {'reply': {'opening_message': '保留開場'}, 'silence': {'enabled': True}, 'common_scripts': [
        {'id': 'contact_family', 'text': '您先和家人討論～方便留一下您的 LINE ID 嗎？', 'enabled': False},
        {'id': 'contact_email', 'text': '請提供信箱', 'enabled': True}]}
    updated = update_configuration(original)
    assert updated['reply'] == original['reply'] and updated['silence'] == original['silence']
    assert updated['common_scripts'][0]['text'] == '您先和家人討論～' + QR_INVITATION
    assert updated['common_scripts'][0]['enabled'] is False
    assert updated['common_scripts'][1] == original['common_scripts'][1]
    assert update_configuration(updated) == updated
    package = {'content_groups': {'contact': {'approved_text': '方便留一下您的LINE ID嗎？', 'asset_keys': ['photo']}},
               'fixed_answers': [{'id': 'contact', 'answer_text': '方便留一下您的LINE ID嗎？'}], 'knowledge_facts': ['unchanged']}
    updated = update_package(package)
    assert updated['content_groups']['contact']['approved_text'] == QR_INVITATION
    assert updated['fixed_answers'][0]['answer_text'] == QR_INVITATION
    assert updated['knowledge_facts'] == package['knowledge_facts']
    assert updated['content_groups']['contact']['asset_keys'] == ['photo']
    assert update_package(updated) == updated


def test_incoming_image_caption_and_source_reach_model_as_customer_attachment(monkeypatch):
    c = context('peach_9d_2027')
    item = {'id': 'cw-123', 'direction': 'incoming', 'content': '這是我的LINE', 'content_type': 'image',
            'attachments': [{'id': 45, 'file_type': 'image'}]}
    c['messages'] = [{'direction': 'outgoing', 'content': QR_INVITATION}, item]
    c['buffered_questions'] = [item]
    requests = model(monkeypatch, [final(action='handoff', interrupt=True)])
    runtime.run_agent(c)
    outgoing = requests[0]['messages'][-2]
    assert outgoing['role'] == 'user'
    assert '這是我的LINE' in outgoing['content'] and 'cw-123' in outgoing['content']
    assert '客户附件' in outgoing['content'] and '已交付附件' not in outgoing['content']
    buffered = json.loads(requests[0]['messages'][-1]['content'])['buffered_questions'][0]
    assert buffered['source_message_id'] == 'cw-123' and buffered['attachments'][0]['id'] == 45


def test_image_source_survives_live_intake_and_handoff_stops_followup(session_factory, monkeypatch):
    setup(session_factory, monkeypatch)
    with session_factory() as db:
        message = db.get(MessageEvent, 1)
        message.content = '這是我的LINE'
        message.attachments = [{'id': 45, 'file_type': 'image', 'data_url': 'private-customer-url'}]
        live.accept(db, db.get(ConversationState, 1), message)
        row = live.session_for(db, 1)
        row.controls = {**row.controls, 'route_variant': 'peach_9d_2027'}
        incoming = row.messages[-1]
        assert incoming['attachments'] == [{'id': 45, 'file_type': 'image'}]
        assert 'private-customer-url' not in json.dumps(row.messages)
        from app.automation_models import AutomationRun
        run = AutomationRun(session_id=row.id, generation=row.generation, module='reply',
                            idempotency_key='qr-test', status='processing', input_snapshot={})
        db.add(run); db.flush()
        decision = runtime.Decision(action='handoff', route_variant='peach_9d_2027', interrupt=True,
            profile={'contact_channel': 'LINE', 'contact_status': 'attachment_pending_verification', 'contact_message_id': 'cw-100'},
            handoff_reason='請查看cw-100的圖片，確認LINE QR Code後加好友',
            messages=[{'text': '圖片收到了，我請顧問確認後再加您的 LINE。'}]).model_dump()
        service.apply_decision(db, row, run, skills.compile_skills(db), decision)
        for item in list(row.messages):
            if item.get('status') == 'draft':
                service.complete_delivery(row, item['id'], 'submitted')
        live.deliver_one(db, row)
        task = db.scalar(select(HandoffTask))
        assert task and 'cw-100' in task.reason_detail
        assert db.scalar(select(Notification)).event_type == 'handoff.created'
        assert service.state(row)['handoff'] and service.state(row).get('next_check_at') is None
        assert not row.memory.get('contact_value')
