from copy import deepcopy
from pathlib import Path

import pytest
from sqlalchemy import select, func

from app import route_packages as catalog
from app.models import InboxBinding, KnowledgeVersion, MaterialAsset, StoredMedia
from app.material_library import candidate_materials
from app.reception_config import MaterialRule
from app.reception_v3 import service
from app.reception_v3.runtime import Decision
from app.reception_v3.material_rules import eligible_rules
from scripts import configure_linger_feedback_20261025 as previous
from scripts import configure_linger_materials_20261025 as revision
from test_v3_intake_followup import create, config

ROOT = Path(__file__).resolve().parents[2]
FILES = ROOT / 'data/knowledge/china2go/followup-20261025'


def test_revision_preserves_operator_controls_and_existing_rule_identities():
    old = previous.update_rules([])
    old[0].update(enabled=False, any_labels=['洗單回'], route_variants=['peach_11d_2027'])
    untouched = deepcopy(old)
    updated = revision.update_rules(old)
    assert old == untouched
    assert len(updated) == 5 and revision.update_rules(updated) == updated
    for rule in updated:
        MaterialRule.model_validate(rule)
        assert len(rule['asset_keys']) == 1
        original = next((r for r in old if r['id'] == rule['id']), None)
        if original:
            for field in ('id', 'enabled', 'any_labels', 'purpose', 'route_variants'):
                assert rule[field] == original[field]


def test_import_reuses_old_media_and_binds_all_five_correctly(session_factory, tmp_path):
    with session_factory() as db:
        db.add(KnowledgeVersion(tenant_id=1, version_key=catalog.KNOWLEDGE_VERSION,
                               title='test', content_hash='test'))
        db.flush()
        first = previous.configure_assets(db, tmp_path, {k: FILES/p['filename'] for k,p in previous.PHOTOS.items()})
        imported = revision.configure_assets(db, tmp_path, {k: FILES/p['filename'] for k,p in revision.PHOTOS.items()})
        db.commit()
        assert len(imported) == 5
        assert {a['key']:a['media_id'] for a in first}.items() <= {a['key']:a['media_id'] for a in imported}.items()
        assert revision.configure_assets(db, tmp_path, {k: FILES/p['filename'] for k,p in revision.PHOTOS.items()}) == imported
        assert db.scalar(select(func.count()).select_from(StoredMedia)) == 5
        assert db.scalar(select(func.count()).select_from(MaterialAsset)) == 5
        materials = {m['key']:m for m in candidate_materials(db, 1)}
        for topic, photo in revision.PHOTOS.items():
            item = materials[photo['key']]
            assert item['media_hash'] == photo['hash']
            assert item['recommended_caption'] == revision.COPY[topic]


@pytest.mark.parametrize('topic', list(revision.COPY))
def test_followup_photo_delivery_history_and_reselection(session_factory, monkeypatch, tmp_path, topic):
    """Use real stored media and resolver, then persist a sent message and retry."""
    route = 'peach_11d_2027'
    spec = config()
    spec['routes'][route] = deepcopy(spec['routes']['nine'])
    spec['material_rules'] = revision.update_rules([])
    spec['silence']['material_selection'] = 'configured_rules'
    with session_factory() as db:
        db.add(KnowledgeVersion(tenant_id=1, version_key=catalog.KNOWLEDGE_VERSION,
                               title='test', content_hash='test'))
        db.flush()
        revision.configure_assets(db, tmp_path, {k: FILES/p['filename'] for k,p in revision.PHOTOS.items()})
        row = create(db, monkeypatch)
        inbox = InboxBinding(tenant_id=1, chatwoot_inbox_id=123, name='test')
        db.add(inbox); db.flush()
        row.inbox_binding_id = inbox.id
        row.controls = {**row.controls, 'route_variant': route}
        monkeypatch.setattr(service, 'candidate_materials', candidate_materials)
        decision = Decision(route_variant=route, messages=[{
            'material_rule_id': revision.PREFIX + topic, 'text': '跟您分享這個體驗～'}]).model_dump()
        _, parts = service.parts_for(db, row, spec, decision, followup=True)
        photo = revision.PHOTOS[topic]
        images = [p for p in parts if p['content_type'] == 'image']
        assert len(images) == 1 and images[0]['asset_key'] == photo['key']
        assert images[0]['media_hash'] == photo['hash']
        row.messages = [*row.messages, *[{**p, 'direction':'outgoing', 'status':'delivered'} for p in parts]]
        db.commit(); db.expire_all()
        _, again = service.parts_for(db, row, spec, decision, followup=True)
        assert not any(p['content_type'] == 'image' for p in again)
        rule = next(r for r in spec['material_rules'] if r['id'] == revision.PREFIX + topic)
        rule['enabled'] = False
        with pytest.raises(ValueError, match='material_rule_not_eligible'):
            service.parts_for(db, row, spec, decision, followup=True)


def test_stars_stay_route_scoped_and_disabled_or_unmatched_labels_stay_excluded():
    rules = revision.update_rules([])
    material = [{'key': p['key'], 'routes':p['routes'], 'content_type':'image'} for p in revision.PHOTOS.values()]
    for route in revision.ROUTES:
        choices = eligible_rules({'material_rules':rules}, material, route, [], 'silence_due', [])
        assert any(r['id'].endswith('stars') for r in choices) == (route in previous.EVEREST)
    for r in rules:
        r['any_labels'] = ['洗單回']
    assert eligible_rules({'material_rules':rules}, material, 'peach_11d_2027', [], 'silence_due', []) == []
