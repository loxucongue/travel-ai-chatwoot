from copy import deepcopy
import pytest
from sqlalchemy import select

from app.reception_v3 import service
from test_v3_intake_followup import create, config
from test_reception_v3 import answer, step


def photo(key, digest, route='nine', **extra):
    return dict(key=key, media_hash=digest, routes=[route], content_type='image', **extra)


def test_followup_catalog_uses_delivery_history_and_hashes_across_aliases():
    materials = [photo('old', 'same'), photo('renamed', 'same'), photo('bare', 'new'),
                 photo('captioned', 'new', what_it_shows='布達拉宮'), photo('other-route', 'other', 'eleven'),
                 photo('pdf', 'pdf'), photo('draft', 'draft'),
                 photo('opening-alias', 'opening', media_id=102)]
    materials[5]['content_type'] = 'file'
    messages = [dict(direction='outgoing', status='delivered', asset_key='old'),
                dict(direction='outgoing', status='read', media_hash='removed-catalog-hash'),
                dict(direction='outgoing', status='submitted', media_id=102),
                dict(direction='outgoing', status='draft', asset_key='draft'),
                dict(direction='incoming', status='read', asset_key='captioned')]
    materials.append(photo('removed-alias', 'removed-catalog-hash'))
    before = deepcopy(materials)
    assert [m['key'] for m in service.followup_materials(materials, messages, 'nine')] == ['captioned', 'draft']
    assert materials == before
    assert service.followup_materials(materials, messages, '') == []


def test_six_hour_photo_and_caption_are_delivered_once_in_order(session_factory, monkeypatch):
    seen = []
    def model(context):
        seen.append(context)
        return answer(route_variant='nine', messages=[
            {'asset_keys':[context['followup_materials'][0]['key']]},
            {'text':'這張是布達拉宮～有想了解的地方，隨時跟我說。'}])
    with session_factory() as db:
        row = create(db, monkeypatch, entry='先看看', model=model)
        spec = config(); spec['silence']['intervals_minutes'] = [360]
        monkeypatch.setattr(service, 'compile_skills', lambda db: spec)
        monkeypatch.setattr(service, 'candidate_materials', lambda *a: [photo('potala', 'hash', media_id=9)])
        monkeypatch.setattr(service, 'resolve_materials', lambda db, keys, route, tenant: [
            dict(content_type='image', asset_key=keys[0], media_id=9, media_hash='hash', route_variant=route)])
        row.controls = {**row.controls, 'route_variant':'nine'}
        value = dict(opening_started=True, silence_step=0, completed_introductions=['nine'])
        service.schedule_followup(value, spec, row.virtual_now, reset=True)
        service.save(row, value); db.commit()
        step(db, row, 21599)
        assert seen == []
        step(db, row, 1)
        for _ in range(3): step(db, row, 2)
        sent = [m for m in row.messages if m.get('status') == 'simulated_delivered']
        assert [m['content_type'] for m in sent] == ['image', 'text']
        assert sent[0]['media_id'] == 9 and sent[0]['asset_key'] == 'potala'
        assert all(m.get('proactive') for m in sent)
        assert service.state(row)['next_check_at'] is None
        assert service.followup_materials(seen[0]['available_materials'], row.messages, 'nine') == []
        step(db, row, 21600)
        assert len(seen) == 1


@pytest.mark.parametrize('takeover_after', [None, 0, 1])
def test_followup_photo_uses_live_attachment_outbox_then_caption(session_factory, monkeypatch, tmp_path, takeover_after):
    from app.reception_v3 import live
    from app.models import StoredMedia, OutboundMessage, MessageEvent, utcnow
    from live_fixture import setup
    from test_live_audit_fixes import session
    setup(session_factory, monkeypatch)
    path = tmp_path / 'potala.jpg'; path.write_bytes(b'photo-test')
    sent = []
    class Client:
        def close(self): pass
        def get_conversation(self, cid): return {'id':cid, 'can_reply':True}
        def get_conversation_labels(self, cid):
            return ['ai', '人工接管'] if takeover_after is not None and len(sent) >= takeover_after else ['ai']
        def create_attachment_message(self, cid, text, filename, mime):
            assert cid == 26 and filename == str(path) and mime == 'image/jpeg'
            sent.append(('image', text)); return {'id':701}
        def create_text_message(self, cid, text):
            sent.append(('text', text)); return {'id':702}
    monkeypatch.setattr(live, 'client_for', lambda *a: Client())
    with session_factory() as db:
        db.add(StoredMedia(id=99, tenant_id=1, created_by=1, original_name='potala.jpg',
                          media_type='image', mime_type='image/jpeg', file_size=10, storage_path=str(path)))
        row = session(db)
        value = dict(opening_started=True, silence_step=1)
        service.append_plan(row, value, [dict(content='', content_type='image', media_id=99,
            media_hash='hash', asset_key='potala', proactive=True),
            dict(content='這張是布達拉宮～', content_type='text', proactive=True)], interval=0, kind='reply')
        service.save(row, value); db.commit()
        live.deliver_one(db, row)
        row.virtual_now = utcnow(); db.commit()
        live.deliver_one(db, row)
        live.deliver_one(db, row)
        if takeover_after is not None:
            assert sent == [('image','')][:takeover_after]
            assert len(list(db.scalars(select(OutboundMessage)))) == takeover_after
            assert row.controls['simulation']['status'] == 'stopped'
            assert not any(m.get('status') == 'draft' for m in row.messages)
            assert service.state(row).get('next_check_at') is None
            return
        assert sent == [('image',''), ('text','這張是布達拉宮～')]
        out = list(db.scalars(select(OutboundMessage).order_by(OutboundMessage.id)))
        assert [(m.content_type, m.media_id, m.chatwoot_message_id, m.status) for m in out] == [
            ('image',99,701,'submitted'), ('text',None,702,'submitted')]
        assert db.scalar(select(MessageEvent).where(MessageEvent.chatwoot_message_id==701)).attribution == 'ai'
        assert service.state(row).get('next_check_at') is None
