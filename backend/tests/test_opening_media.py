from io import BytesIO

import pytest
from PIL import Image
from sqlalchemy import select

from app.config import settings
from app.models import StoredMedia, OutboundMessage, utcnow
from app.opening_messages import OpeningItem, delivery_items, opening_media_info, SELECTION_QUESTION
from app.reception_config import ReplySettings


def png():
    output = BytesIO()
    Image.new('RGB', (32, 32), 'green').save(output, format='PNG')
    return output.getvalue()


def media(db, tmp_path, kind='image'):
    data = png() if kind == 'image' else b'\x00\x00\x00\x18ftypisom\x00\x00\x00\x00isommp42'
    path = tmp_path / ('test.png' if kind == 'image' else 'test.mp4')
    path.write_bytes(data)
    row = StoredMedia(tenant_id=1, original_name=path.name, media_type=kind,
                      mime_type='image/png' if kind == 'image' else 'video/mp4',
                      file_size=len(data), storage_path=str(path), created_by=1)
    db.add(row)
    db.commit()
    item = {'key': kind, 'content_type': kind, 'content': '', 'media_id': row.id}
    item['media_hash'] = opening_media_info(db, item, 1)['media_hash']
    return item


def test_typed_opening_upload_and_publication(authenticated, tmp_path, monkeypatch):
    client, csrf = authenticated
    monkeypatch.setattr(settings, 'upload_dir', str(tmp_path))
    headers = {'X-CSRF-Token': csrf}
    response = client.post('/v1/media', files={'file': ('opening.png', png(), 'image/png')}, headers=headers)
    assert response.status_code == 200, response.text
    config = client.get('/v1/automation/reception-config').json()['config']
    config['reply']['opening_items'] = [{'key': 'photo', 'content_type': 'image', 'content': '', 'media_id': response.json()['id']}]
    config['reply']['opening_message'] = ''
    config['reply']['opening_messages'] = []
    saved = client.put('/v1/automation/reception-config', json=config, headers=headers)
    assert saved.status_code == 200, saved.text
    reply = saved.json()['config']['reply']
    assert len(reply['opening_items'][0]['media_hash']) == 64
    assert reply['opening_messages'] == [SELECTION_QUESTION]
    assert reply['opening_items'][-1]['content'] == SELECTION_QUESTION
    assert client.get('/v1/automation/reception-config').json()['config']['reply'] == reply
    (next(tmp_path.iterdir())).write_bytes(b'changed')
    rejected = client.put('/v1/automation/reception-config', json=saved.json()['config'], headers=headers)
    assert rejected.status_code == 422


def test_remove_opening_image_keeps_visible_question_and_runtime_order(authenticated, tmp_path, monkeypatch):
    from app.reception_config import ReceptionConfiguration, policy_from_configuration
    from app.reception_v2.runtime import run_v2_agent
    client, csrf = authenticated
    monkeypatch.setattr(settings, 'upload_dir', str(tmp_path))
    headers = {'X-CSRF-Token': csrf}
    media_id = client.post('/v1/media', files={'file': ('room.png', png(), 'image/png')},
                           headers=headers).json()['id']
    config = client.get('/v1/automation/reception-config').json()['config']
    config['reply']['opening_items'] = [
        {'key': 'greeting', 'content_type': 'text', 'content': '您好～很高興認識您！'},
        {'key': 'room', 'content_type': 'image', 'content': '', 'media_id': media_id},
    ]
    config['reply']['opening_interval_seconds'] = 1
    saved = client.put('/v1/automation/reception-config', json=config, headers=headers)
    assert saved.status_code == 200, saved.text
    visible = client.get('/v1/automation/reception-config').json()['config']
    assert [x['content_type'] for x in visible['reply']['opening_items']] == ['text', 'image', 'text']
    visible['reply']['opening_items'] = [x for x in visible['reply']['opening_items'] if x['key'] != 'room']
    saved = client.put('/v1/automation/reception-config', json=visible, headers=headers)
    assert saved.status_code == 200, saved.text
    config = client.get('/v1/automation/reception-config').json()['config']
    expected = ['您好～很高興認識您！', SELECTION_QUESTION]
    assert config['reply']['opening_messages'] == expected
    monkeypatch.setattr('app.reception_v2.runtime._call', lambda *_a, **_kw: pytest.fail('opening called model'))
    decision, _, _, trace = run_v2_agent({
        'module': 'reply', 'customer_text': '你好，我想咨询旅行行程',
        'context_messages': [], 'context_complete': True,
        'reception_policy': policy_from_configuration(ReceptionConfiguration.model_validate(config).model_dump()),
    })
    assert decision.opening_messages == expected
    assert [item['content'] for item in decision.opening_items] == expected
    assert decision.opening_interval_seconds == 1
    assert trace['model_http_request_count'] == 0


@pytest.mark.parametrize('item', [
    {'key': 'a', 'content_type': 'video'},
    {'key': 'a', 'content_type': 'text', 'content': ' ', 'media_id': 1},
    {'key': 'a', 'content_type': 'audio', 'media_id': 1},
])
def test_invalid_typed_items(item):
    with pytest.raises(ValueError):
        OpeningItem.model_validate(item)


def test_media_tenant_type_and_revision_guards(session_factory, tmp_path):
    with session_factory() as db:
        item = media(db, tmp_path)
        for tenant, updates in [(None, {}), (2, {}), (1, {'content_type': 'video'}), (1, {'media_hash': '0' * 64})]:
            with pytest.raises(ValueError):
                opening_media_info(db, {**item, **updates}, tenant)


def test_video_signature_is_not_mime_only(session_factory, tmp_path):
    with session_factory() as db:
        item = media(db, tmp_path, 'video')
        assert opening_media_info(db, item, 1)['content_type'] == 'video'
        (tmp_path / 'test.mp4').write_bytes(b'<html>not a video</html>')
        with pytest.raises(ValueError, match='opening_video_format_invalid'):
            opening_media_info(db, item, 1)


def test_typed_opening_plan_remains_verbatim():
    from test_advisor_feedback_revision import packet
    from app.reply_planning import build_reply_plan
    from app.reply_generation import call_reply_generator
    from app.reply_understanding import CustomerUnderstanding
    context = packet()
    items = [{'key': 'photo', 'content_type': 'image', 'content': '先傳相片給您看～', 'media_id': 1}]
    context['reception_policy']['operator_configuration']['opening_items'] = items
    plan = build_reply_plan(context, CustomerUnderstanding(intent='other', semantic_signals=['general_inquiry']))
    reply, logs, _ = call_reply_generator(context, plan)
    assert plan.opening_items == delivery_items(items, [])
    assert reply.body == '先傳相片給您看～\n\n' + SELECTION_QUESTION
    assert logs == [] and plan.allowed_asset_ids == []


@pytest.mark.parametrize('remove_after', ['none', 'text', 'image'])
def test_live_typed_sequence_and_label_removal(session_factory, tmp_path, monkeypatch, remove_after):
    from test_live_reply import setup
    import app.live_reply as live
    from app.deepseek_evaluation import EvaluationDecision
    fake = setup(session_factory, monkeypatch, labels=['ai'])
    fake.remove_ai_after_text = remove_after == 'text'
    fake.remove_ai_after_image = remove_after == 'image'
    with session_factory() as db:
        image = media(db, tmp_path)
        video = media(db, tmp_path, 'video')
    items = [{'key': 'hello', 'content_type': 'text', 'content': '您好～'}, image, video]
    monkeypatch.setattr(live, 'generate_decision', lambda _: (
        EvaluationDecision('reply', 'unclassified', 'other', reply='您好～',
                           opening_items=items, opening_messages=['您好～'],
                           reply_options=['桃花9日', '桃花+珠峰11日']), [], 'hash', {},
    ))
    live.process_job(1)
    with session_factory() as db:
        types = list(db.scalars(select(OutboundMessage.content_type).order_by(OutboundMessage.id)))
    expected = ['text'] if remove_after == 'text' else ['text', 'image'] if remove_after == 'image' else ['text', 'image', 'video', 'input_select']
    assert types == expected
    before = len(fake.sent)
    live.process_job(1)
    assert len(fake.sent) == before


def test_playground_typed_media_sequence(session_factory, tmp_path, monkeypatch):
    from test_playground_journey import setup_journey, decision
    from app.automation_service import queue_passive, process_automation_run, confirm_draft, dt
    with session_factory() as db:
        session, _ = setup_journey(db)
        image, video = media(db, tmp_path), media(db, tmp_path, 'video')
        reply = decision()
        reply.opening_items = [image, video]
        reply.opening_messages = [SELECTION_QUESTION]
        reply.reply_options = ['桃花9日', '桃花+珠峰11日']
        session.due_at = utcnow()
        db.commit()
        assert queue_passive(db, environment='playground')
        monkeypatch.setattr('app.automation_service.generate_decision', lambda _: (reply, [], 'hash', {}))
        assert process_automation_run(db, environment='playground')
        db.refresh(session)
        drafts = [item for item in session.messages if item.get('status') == 'draft']
        assert [item['content_type'] for item in drafts] == ['image', 'video', 'input_select']
        assert (dt(drafts[-1]['created_at']) - dt(drafts[0]['created_at'])).total_seconds() == 4
        assert drafts[-1]['content'] == SELECTION_QUESTION
        confirm_draft(db, session, drafts[0]['id'])
        assert all(item['status'] == 'simulated_delivered' for item in session.messages if item.get('run_id') == drafts[0]['run_id'])


def test_playground_text_opening_keeps_question_separate_from_greeting(session_factory, monkeypatch):
    from test_playground_journey import setup_journey
    from app.automation_service import queue_passive, process_automation_run, dt
    from app.reception_v2.runtime import run_v2_agent
    from app.reception_config import ReceptionConfiguration, policy_from_configuration
    config = ReceptionConfiguration().model_dump()
    config['reply'].update(opening_items=[
        {'key': 'hello', 'content_type': 'text', 'content': '您好～很高興認識您！'},
        {'key': 'selection-question', 'content_type': 'text', 'content': SELECTION_QUESTION},
    ], opening_interval_seconds=1)
    policy = policy_from_configuration(ReceptionConfiguration.model_validate(config).model_dump())
    monkeypatch.setattr('app.reception_v2.runtime._call', lambda *_a, **_kw: pytest.fail('opening called model'))
    with session_factory() as db:
        session, _ = setup_journey(db)
        session.due_at = utcnow()
        db.commit()
        assert queue_passive(db, environment='playground')
        monkeypatch.setattr('app.automation_service.generate_decision', lambda context: run_v2_agent({
            **context, 'reception_policy': policy,
        }))
        assert process_automation_run(db, environment='playground')
        db.refresh(session)
        drafts = [x for x in session.messages if x.get('status') == 'draft']
        assert [x['content'] for x in drafts] == ['您好～很高興認識您！', SELECTION_QUESTION]
        assert [x['content_type'] for x in drafts] == ['text', 'input_select']
        assert 'items' not in drafts[0]['content_attributes']
        assert len(drafts[1]['content_attributes']['items']) == 2
        assert (dt(drafts[1]['created_at']) - dt(drafts[0]['created_at'])).total_seconds() == 1
