import base64
from copy import deepcopy
from io import BytesIO
import json
from pathlib import Path

from PIL import Image, PngImagePlugin
import pytest
from sqlalchemy import select

from app.automation_models import AutomationRun
from app.reception_v3 import customer_images, runtime, service
from app.reception_v3.customer_reactions import recognize_sticker, remember_reactions, stored_reaction
from test_reception_v3_skills import context, final, model

THUMB = Path(__file__).with_name('fixtures') / 'verified-blue-thumb.png'
SOURCE = 'https://app.chatwoot.com/rails/active_storage/blobs/redirect/test/thumb.png'
ATTACHMENT = {'id': 1, 'file_type': 'image', 'file_size': 1678, 'data_url': SOURCE}


def encoded(data):
    return 'data:image/png;base64,' + base64.b64encode(data).decode()


def image_context(text=''):
    value = context('peach_11d_2027')
    value['messages'] = [{'id': 'thumb', 'direction': 'incoming', 'content': text,
                          'content_type': 'text', 'attachments': [deepcopy(ATTACHMENT)]}]
    value['buffered_questions'] = deepcopy(value['messages'])
    return value


@pytest.fixture(autouse=True)
def sticker_enabled(monkeypatch):
    monkeypatch.setattr(customer_images.settings, 'customer_sticker_recognition_enabled', True)
    monkeypatch.setattr(customer_images.settings, 'customer_image_vision_enabled', False)
    monkeypatch.setattr(customer_images, 'download_image', lambda *a: encoded(THUMB.read_bytes()))


def test_actual_reported_original_is_recognized():
    assert recognize_sticker(encoded(THUMB.read_bytes()))['kind'] == 'thumbs_up'


def test_recompressed_same_pixels_are_recognized():
    output = BytesIO()
    with Image.open(THUMB) as img:
        metadata = PngImagePlugin.PngInfo()
        metadata.add_text('note', 'compression does not change meaning')
        img.save(output, format='PNG', compress_level=0, pnginfo=metadata)
    assert output.getvalue() != THUMB.read_bytes()
    assert recognize_sticker(encoded(output.getvalue()))['kind'] == 'thumbs_up'


@pytest.mark.parametrize('variant', ['one_pixel', 'resized', 'other_picture', 'animated'])
def test_similar_or_unknown_picture_is_not_guessed(variant):
    with Image.open(THUMB) as img:
        copy = img.convert('RGBA')
    output = BytesIO()
    if variant == 'one_pixel':
        copy.putpixel((0,0), (255,0,0,255))
    elif variant == 'resized':
        copy = copy.resize((144,144))
    elif variant == 'other_picture':
        copy = Image.new('RGBA', (72,72), 'red')
    if variant == 'animated':
        copy.save(output, format='PNG', save_all=True, append_images=[Image.new('RGBA', copy.size, 'red')])
    else:
        copy.save(output, format='PNG')
    assert recognize_sticker(encoded(output.getvalue())) is None


@pytest.mark.parametrize('data', ['garbage', 'data:image/png;base64,***', encoded(b'not an image')])
def test_invalid_encoded_data_is_not_an_ack(data):
    assert recognize_sticker(data) is None


def test_disabled_feature_does_not_fetch(monkeypatch):
    monkeypatch.setattr(customer_images.settings, 'customer_sticker_recognition_enabled', False)
    monkeypatch.setattr(customer_images, 'download_image', lambda *a: pytest.fail('unexpected network'))
    assert customer_images.read_customer_media(image_context()) == ({}, {})


def test_known_thumb_uses_normal_text_model_with_verified_meaning(monkeypatch):
    requests = model(monkeypatch, [final(action='wait')])
    decision, logs, _ = runtime.run_agent(image_context())
    dialogue = requests[0]['messages'][1]['content']
    assert '嗯嗯／收到' in dialogue and '本轮未读取' not in dialogue
    assert requests[0]['model'] == runtime.settings.deepseek_model
    assert all(isinstance(m['content'], str) for m in requests[0]['messages'])
    assert SOURCE not in json.dumps(requests[0], ensure_ascii=False)
    assert logs[0]['recognized']['thumb']['kind'] == 'thumbs_up'
    assert decision['action'] == 'wait'


@pytest.mark.parametrize('bad', [
    {'action':'handoff','handoff_type':'knowledge'},
    {'action':'handoff','handoff_type':'contact_image'},
    {'messages':[{'text':'這張圖片我沒能看清楚，方便用文字說明嗎？'}]},
    {'messages':[{'text':'方便提供LINE QR Code嗎？'}]},
    {'messages':[{'text':'重新傳圖片給我'}]},
    {'profile':{'party_size':4}},
    {'profile':{'contact_status':'provided'}},
    {'profile':{'departure_window':'三月底'}},
    {'route_variant':'peach_9d_2027'},
    {'start_introduction':True},
    {'interrupt':True},
    {'opt_out':False},
    {'opt_out':True},
    {'stop_followup':True},
    {'next_check_minutes':30},
    {'messages':[{'asset_keys':['map-11']}]},
])
def test_plain_thumb_cannot_trigger_image_question_or_customer_commitment(monkeypatch, bad):
    requests = model(monkeypatch, [final(**bad)])
    decision, logs, _ = runtime.run_agent(image_context())
    assert decision['action'] == 'wait' and decision['messages'] == []
    assert decision['profile'] == {} and decision['route_variant'] == ''
    assert decision['opt_out'] is None and not decision['interrupt']
    assert len(requests) == 1 and logs[-1]['status'] == 'acknowledgement_guard'


def test_plain_thumb_cannot_authorize_route_choice_extension():
    # Also valid against the small-only release, whose Decision schema does not
    # include the separately developed Linzhi route-choice extension yet.
    from app.reception_v3.customer_reactions import invalid_ack_decision
    decision = {**runtime.Decision(action='wait').model_dump(), 'route_choice':'selected'}
    assert invalid_ack_decision(decision, image_context())


@pytest.mark.parametrize('mode', ['caption', 'after_thumb', 'before_thumb'])
def test_explicit_human_request_takes_precedence_over_sticker(monkeypatch, mode):
    c = image_context('請轉人工' if mode == 'caption' else '')
    if mode != 'caption':
        message = {'id':'question', 'direction':'incoming', 'content':'請轉人工'}
        c['messages'].insert(0 if mode == 'before_thumb' else 1, message)
        c['buffered_questions'] = deepcopy(c['messages'])
    model(monkeypatch, [final(action='handoff', handoff_type='requested')])
    decision, _, _ = runtime.run_agent(c)
    assert decision['action'] == 'handoff'


def test_following_route_is_not_swallowed(monkeypatch):
    c = image_context()
    c['messages'].append({'id':'route','direction':'incoming','content':'想了解不上珠峰9日'})
    c['buffered_questions'] = deepcopy(c['messages'])
    requests = model(monkeypatch, [final(messages=[{'text':'我先給您看9日的安排。'}])])
    decision, _, _ = runtime.run_agent(c)
    assert decision['messages'][0]['text'] == '我先給您看9日的安排。'
    assert requests[0]['messages'][2]['content'] == '想了解不上珠峰9日'


@pytest.mark.parametrize('extra', [
    {'id':2,'file_type':'file'},
    {'id':2,'file_type':'image','data_url':'https://untrusted.invalid/qr.png'},
    {'id':2,'file_type':'image','data_url':SOURCE},
])
def test_unknown_extra_attachment_is_not_hidden(extra):
    c = image_context()
    c['messages'][0]['attachments'].append(extra)
    c['buffered_questions'] = deepcopy(c['messages'])
    assert customer_images.read_customer_media(c)[1] == {}


def test_download_failure_does_not_guess_a_like(monkeypatch):
    def fail(*a):
        raise TimeoutError('expired')
    monkeypatch.setattr(customer_images, 'download_image', fail)
    assert customer_images.read_customer_media(image_context()) == ({}, {})


def test_unrecognized_image_still_uses_vision_when_enabled(monkeypatch):
    monkeypatch.setattr(customer_images.settings, 'customer_image_vision_enabled', True)
    monkeypatch.setattr(customer_images, 'download_image', lambda *a: encoded(b'unknown'))
    images, reactions = customer_images.read_customer_media(image_context())
    assert 'thumb' in images and not reactions


def test_unknown_image_is_not_turned_into_ack_when_vision_disabled(monkeypatch):
    monkeypatch.setattr(customer_images, 'download_image', lambda *a: encoded(b'unknown'))
    requests = model(monkeypatch, [final(messages=[{'text':'您想詢問圖片中的哪一部分？'}])])
    decision, _, _ = runtime.run_agent(image_context())
    assert '本轮未读取' in requests[0]['messages'][1]['content']
    assert decision['messages']


def test_saved_meaning_survives_manual_return_and_followup_without_redownload(monkeypatch):
    c = image_context()
    _, recognized = customer_images.read_customer_media(c)
    c['messages'] = remember_reactions(c['messages'], [{'node':'customer_reactions','recognized':recognized}])
    monkeypatch.setattr(customer_images, 'download_image', lambda *a: pytest.fail('already verified'))
    assert customer_images.read_customer_media(c)[1] == recognized
    c['event'] = 'silence_due'
    requests = model(monkeypatch, [final(action='wait')])
    runtime.run_agent(c)
    assert '嗯嗯／收到' in requests[0]['messages'][1]['content']
    assert '本轮未读取' not in requests[0]['messages'][1]['content']


def test_changed_attachment_does_not_reuse_old_meaning():
    c = image_context()
    _, recognized = customer_images.read_customer_media(c)
    c['messages'] = remember_reactions(c['messages'], [{'node':'customer_reactions','recognized':recognized}])
    assert stored_reaction(c['messages'][0])
    c['messages'][0]['attachments'][0]['id'] = 999
    assert stored_reaction(c['messages'][0]) is None


def test_service_keeps_ai_and_existing_profile_and_saves_recognition(session_factory, monkeypatch):
    from test_reception_v3 import create
    with session_factory() as db:
        row = create(db, monkeypatch)
        row.messages = []
        row.memory = {'party_size':2, 'departure_window':'四月'}
        row.controls = {**row.controls, 'route_variant':'nine', 'v3':{
            'opening_started':True, 'intake_status':'done', 'completed_introductions':['nine']}}
        service.add_message(db, row, '', 'thumb', 'text', [ATTACHMENT])
        def respond(c):
            _, reactions = customer_images.read_customer_media(c)
            return runtime.Decision(action='wait').model_dump(), [
                {'node':'customer_reactions','recognized':reactions}], 'verified-test'
        monkeypatch.setattr(service, 'run_agent', respond)
        service.tick(db, session_id=row.id, advance_clock=False)
        assert not row.controls.get('human') and not service.state(row).get('handoff')
        assert row.memory == {'party_size':2, 'departure_window':'四月'}
        assert stored_reaction(row.messages[0])['kind'] == 'thumbs_up'
        assert not any(m.get('direction') == 'outgoing' for m in row.messages)
        assert service.state(row)['next_check_at'] is not None
        assert db.scalar(select(AutomationRun).where(AutomationRun.session_id==row.id)).status == 'completed'


def test_reprocessing_historical_thumb_does_not_extend_seven_day_deadline(session_factory, monkeypatch):
    from test_reception_v3 import create, bundle
    with session_factory() as db:
        row = create(db, monkeypatch)
        spec = bundle()
        spec['silence'].update(daily_at='09:00', active_start='07:00', active_end='01:00',
                               max_proactive_messages_per_day=1)
        monkeypatch.setattr(service, 'compile_skills', lambda db:deepcopy(spec))
        row.virtual_now = '2026-10-22T03:55:00+00:00'
        row.messages = [{'id':'thumb', 'direction':'incoming', 'content':'',
                         'created_at':'2026-10-18T07:17:41+00:00', 'attachments':[ATTACHMENT]}]
        row.controls = {**row.controls, 'route_variant':'nine', 'v3':{
            'opening_started':True, 'intake_status':'done', 'completed_introductions':['nine'],
            'pending_event':'customer_message', 'buffered_questions':['thumb'],
            'followup_last_customer_at':'2026-10-18T07:17:41+00:00'}}
        def respond(c):
            _, reactions = customer_images.read_customer_media(c)
            return runtime.Decision(action='wait').model_dump(), [
                {'node':'customer_reactions','recognized':reactions}], 'verified-test'
        monkeypatch.setattr(service, 'run_agent', respond)
        service.tick(db, session_id=row.id, advance_clock=False)
        assert service.state(row)['followup_until'] == '2026-10-25T07:17:41+00:00'
        assert row.messages[0]['created_at'] == '2026-10-18T07:17:41+00:00'


def test_superseded_run_does_not_save_old_recognition(session_factory, monkeypatch):
    from test_reception_v3 import create
    with session_factory() as db:
        row = create(db, monkeypatch)
        row.messages = []
        row.controls = {**row.controls, 'v3':{'opening_started':True}}
        service.add_message(db, row, '', 'thumb', 'text', [ATTACHMENT])
        def respond(c):
            _, reactions = customer_images.read_customer_media(c)
            service.add_message(db, row, '請介紹桃花9日', 'later-route')
            db.commit()
            return runtime.Decision(action='wait').model_dump(), [
                {'node':'customer_reactions','recognized':reactions}], 'verified-test'
        monkeypatch.setattr(service, 'run_agent', respond)
        service.tick(db, session_id=row.id, advance_clock=False)
        assert db.scalar(select(AutomationRun).where(AutomationRun.session_id==row.id)).status == 'cancelled'
        assert stored_reaction(row.messages[0]) is None
        assert service.state(row)['pending_event'] == 'customer_message'
        assert 'later-route' in service.state(row)['buffered_questions']
