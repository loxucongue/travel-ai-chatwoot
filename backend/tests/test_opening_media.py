from io import BytesIO


import errno


from pathlib import Path


import pytest


from PIL import Image


from sqlalchemy import select


from app.config import settings


from app.models import StoredMedia, OutboundMessage, utcnow


from app.opening_messages import OpeningItem, delivery_items, opening_media_info


from app.reception_config import ReplySettings


SELECTION_QUESTION = "您想先了解哪一條行程呢？"


def png():
    output = BytesIO()
    Image.new('RGB', (32, 32), 'green').save(output, format='PNG')
    return output.getvalue()


@pytest.mark.parametrize('error_number', [errno.ENOSPC, errno.EDQUOT])
def test_full_upload_storage_reports_error_without_partial_media(authenticated, session_factory, tmp_path, monkeypatch, error_number):
    client, csrf = authenticated
    monkeypatch.setattr(settings, 'upload_dir', str(tmp_path))
    write_bytes = Path.write_bytes

    def full_storage(path, data):
        write_bytes(path, data[:8])
        raise OSError(error_number, 'storage unavailable')

    monkeypatch.setattr(Path, 'write_bytes', full_storage)
    response = client.post('/v1/media', files={'file': ('opening.png', png(), 'image/png')},
                           headers={'X-CSRF-Token': csrf, 'Origin': settings.origins[0]})
    assert response.status_code == 507
    assert response.json()['error']['code'] == 'media_storage_full'
    assert response.headers['access-control-allow-origin'] == settings.origins[0]
    assert not list(tmp_path.iterdir())
    with session_factory() as db:
        assert db.scalar(select(StoredMedia)) is None


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
    config['reply']['opening_items'] = [{'key': 'photo', 'content_type': 'image', 'content': '', 'media_id': response.json()['id']}, {'key':'question','content_type':'text','content':SELECTION_QUESTION}]
    config['reply']['opening_message'] = ''
    config['reply']['opening_messages'] = []
    saved = client.patch('/v1/automation/reception-config', json={'reply': {k:v for k,v in config['reply'].items() if k not in {'opening_message','opening_messages'}}}, headers=headers)
    assert saved.status_code == 200, saved.text
    reply = saved.json()['config']['reply']
    assert len(reply['opening_items'][0]['media_hash']) == 64
    assert reply['opening_messages'] == [SELECTION_QUESTION]
    assert reply['opening_items'][-1]['content'] == SELECTION_QUESTION
    assert client.get('/v1/automation/reception-config').json()['config']['reply'] == reply
    (next(tmp_path.iterdir())).write_bytes(b'changed')
    rejected = client.patch('/v1/automation/reception-config', json={'reply': {'opening_items':saved.json()['config']['reply']['opening_items']}}, headers=headers)
    assert rejected.status_code == 422


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
