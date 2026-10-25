from copy import deepcopy
from pathlib import Path
import json
import pytest
from sqlalchemy import select, func
from app import route_packages as rp
from app.automation_models import AutomationSession
from app.models import KnowledgeVersion, MaterialAsset, StoredMedia
from app.material_library import candidate_materials, resolve_materials
from app.reception_v3 import service
from app.reception_v3.runtime import Decision
from scripts.configure_followup_materials_20261019 import (ROUTES, EVEREST, PHOTOS, PREFIX, package_delta, configure_assets)

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / 'data/knowledge/china2go'


@pytest.mark.parametrize('route', ROUTES)
def test_followup_delta_preserves_intro_pricing_and_other_business_facts(route):
    old = json.loads((DATA / 'route-releases/20261019-tsozong' / (route + '.json')).read_text(encoding='utf8'))
    updated = package_delta(old)
    rp._validate(deepcopy(updated), Path(route))
    assert package_delta(updated) == updated
    for key in old:
        if key not in ('package_version', 'content_groups', 'content_sequence', 'knowledge_facts', 'fixed_answers'):
            assert updated[key] == old[key]
    for key, group in old['content_groups'].items():
        assert updated['content_groups'][key] == group
    assert updated['fixed_answers'][:len(old['fixed_answers'])] == old['fixed_answers']
    added = {key: group for key, group in updated['content_groups'].items() if key.startswith(PREFIX)}
    assert len(added) == (3 if route in EVEREST else 2)
    assert all(not group['initial_delivery'] for group in added.values())
    assert added[PREFIX + 'tea']['asset_keys'] == [PHOTOS['tea']['key']]
    assert added[PREFIX + 'trust']['asset_keys'] == ([PHOTOS['trust']['key']] if route == 'tibet_essence_8d' else [])
    assert all('這幾天' not in g['approved_text'] for g in added.values())


@pytest.fixture()
def seeded(session_factory, tmp_path, monkeypatch):
    with session_factory() as db:
        db.add(KnowledgeVersion(tenant_id=1, version_key=rp.KNOWLEDGE_VERSION, title='test', content_hash='test'))
        db.flush()
        files = {k: DATA / 'followup-20261019' / p['filename'] for k, p in PHOTOS.items()}
        before = configure_assets(db, tmp_path / 'media', files)
        db.commit()
        assert configure_assets(db, tmp_path / 'media', files) == before
        db.commit()
        assert db.scalar(select(func.count()).select_from(StoredMedia)) == 3
        assert db.scalar(select(func.count()).select_from(MaterialAsset)) == 3
        assert all(not a.metadata_json['revision_archive'] for a in db.scalars(select(MaterialAsset)))
    monkeypatch.setattr(service, 'tenant_for_session', lambda *a: 1)
    return session_factory


@pytest.mark.parametrize('route', ROUTES)
def test_actual_assets_scope_delivery_and_hash_dedup(seeded, route):
    with seeded() as db:
        materials = candidate_materials(db, 1)
        offered = service.followup_materials(materials, [], route)
        expected = [p['key'] for p in PHOTOS.values() if route in p['routes']]
        assert {m['key'] for m in offered} == set(expected)
        row = AutomationSession(controls={'route_variant': route}, messages=[], memory={})
        bundle = {'routes': {r: {'scripts': [], 'groups': {}} for r in ROUTES}, 'common_scripts': []}
        for key in expected:
            decision = Decision(route_variant=route, messages=[{'text':'測試配文', 'asset_keys':[key]}]).model_dump()
            _, parts = service.parts_for(db, row, bundle, decision, followup=True)
            assert [m['content_type'] for m in parts] == ['text', 'image']
            row.messages += [{**parts[-1], 'direction':'outgoing', 'status':'submitted'}]
            with pytest.raises(ValueError, match='invalid_silence_followup_materials'):
                service.parts_for(db, row, bundle, decision, followup=True)
        assert service.followup_materials(materials, row.messages, route) == []
        # History with only hash, including a renamed import, is also respected.
        assert service.followup_materials(materials, [{'direction':'outgoing','status':'read',
            'media_hash':m['media_hash']} for m in offered], route) == []
        for p in PHOTOS.values():
            if route not in p['routes']:
                with pytest.raises(ValueError, match='material_route_mismatch'):
                    resolve_materials(db, [p['key']], route, 1)
                with pytest.raises(ValueError, match='invalid_silence_followup_materials'):
                    service.parts_for(db, row, bundle, Decision(route_variant=route,
                        messages=[{'asset_keys':[p['key']]}]).model_dump(), followup=True)


def test_followup_cannot_burst_or_switch_route_but_customer_comparison_can(seeded):
    with seeded() as db:
        row = AutomationSession(controls={'route_variant':'peach_9d_2027'}, messages=[], memory={})
        bundle = {'routes':{r:{'scripts':[], 'groups':{}} for r in ROUTES}, 'common_scripts':[]}
        star = Decision(messages=[{'asset_keys':[PHOTOS['stars']['key']]}]).model_dump()
        assert service.parts_for(db, row, bundle, star)[1][0]['route_variant'] in EVEREST
        with pytest.raises(ValueError, match='invalid_silence_followup_materials'):
            service.parts_for(db, row, bundle, star, followup=True)
        star['route_variant'] = 'peach_11d_2027'
        with pytest.raises(ValueError, match='invalid_silence_followup_materials'):
            service.parts_for(db, row, bundle, star, followup=True)
        row.controls = {'route_variant':'peach_11d_2027'}
        star['messages'][0]['asset_keys'].append(PHOTOS['tea']['key'])
        with pytest.raises(ValueError, match='invalid_silence_followup_materials'):
            service.parts_for(db, row, bundle, star, followup=True)
