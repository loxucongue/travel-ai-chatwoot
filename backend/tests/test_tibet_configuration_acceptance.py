"""Exercise the actual import/edit API, isolated from live packages and channels."""
from copy import deepcopy

import pytest

from app.models import KnowledgeVersion, MaterialAsset
from test_tibet_release_20261014 import PACKAGES, EIGHT, TEN


@pytest.mark.parametrize('route', [EIGHT, TEN])
def test_new_route_import_and_edit_preserve_pricing_and_global_controls(
    authenticated, session_factory, monkeypatch, tmp_path, route
):
    import app.automation_api as api
    import app.route_packages as catalog
    from app.reception_v3.skills import compile_skills

    client, csrf = authenticated
    headers = {'X-CSRF-Token': csrf}
    original_root = catalog.RUNTIME_PACKAGE_ROOT
    isolated_root = tmp_path / 'routes'
    monkeypatch.setattr(catalog, 'RUNTIME_PACKAGE_ROOT', isolated_root)
    monkeypatch.setattr(api, 'RUNTIME_PACKAGE_ROOT', isolated_root)
    catalog.reload_route_packages()
    package = deepcopy(PACKAGES[route])
    try:
        baseline = client.get('/v1/automation/reception-config').json()['config']
        with session_factory() as db:
            version = KnowledgeVersion(tenant_id=1, version_key=package['knowledge_version'],
                                       title='isolated route test', content_hash='a'*64)
            db.add(version)
            db.flush()
            for key in {a for g in package['content_groups'].values() for a in g['asset_keys']}:
                db.add(MaterialAsset(knowledge_version_id=version.id, asset_key=key,
                                     source_path=str(tmp_path/key), display_name=key, available=True))
            db.commit()
        imported = client.post('/v1/automation/route-products/import',
                               headers=headers, json={'package': package})
        assert imported.status_code == 200, imported.text
        payload = {
            'base_package_version': package['package_version'],
            'default_entry_message': package['default_entry_message'],
            'ai_guidance': '已核對資料；按目前季節報價規則接待。',
            'knowledge_facts': package['knowledge_facts'],
            'content_groups': [{'key': k, **g} for k, g in package['content_groups'].items()],
            'fixed_answers': package['fixed_answers'],
        }
        edited = client.put(f'/v1/automation/route-products/{route}/content',
                            headers=headers, json=payload)
        assert edited.status_code == 200, edited.text
        saved = catalog.ROUTE_PACKAGES[route]
        assert saved['intake'] == package['intake']
        assert saved['seasonal_pricing'] == package['seasonal_pricing']
        assert {s['id']:s['answer_text'] for s in saved['fixed_answers']} == {
            s['id']:s['answer_text'] for s in package['fixed_answers']}
        config = client.get('/v1/automation/reception-config').json()['config']
        for section in ('reply', 'intake', 'silence', 'lead_capture', 'handoff'):
            assert config[section] == baseline[section]
        assert config['routing']['enabled_route_variants'] == [
            *baseline['routing']['enabled_route_variants'], route]
        with session_factory() as db:
            bundle = compile_skills(db)
        assert bundle['routes'][route]['intake']['wait_seconds'] == 30
        assert bundle['routes'][route]['seasonal_pricing'] == package['seasonal_pricing']
        assert '有珠峰的' not in bundle['routes']['peach_11d_2027']['aliases']
    finally:
        monkeypatch.setattr(catalog, 'RUNTIME_PACKAGE_ROOT', original_root)
        monkeypatch.setattr(api, 'RUNTIME_PACKAGE_ROOT', original_root)
        catalog.reload_route_packages()
