"""Structural delivery checks; customer intent remains with the model."""
from copy import deepcopy
from importlib.util import spec_from_file_location, module_from_spec
from pathlib import Path
import json
import time

import pytest

from app.reception_v3 import service, runtime, route_understanding
from app.reception_v3.message_references import message_route
from app.reception_v3.material_rules import answer_visuals
from app.reception_v3.route_understanding import DecisionContractError
from test_reception_v3 import create, bundle
from test_reception_v3_skills import model, final


def catalog():
    b = bundle()
    b['routes']['ten'] = deepcopy(b['routes']['nine'])
    for r, amount in [('nine', '10980'), ('ten', '12980')]:
        b['routes'][r]['groups']['map']['assets'] = ['map-' + r]
        b['routes'][r]['scripts'] = [dict(id='price_special_' + r, status='active', answer_text=r + ' ' + amount),
                                     dict(id='price_low', status='active', answer_text=r + ' low')]
    return b


def materials():
    return [dict(key='map-' + r, name=r, routes=[r], content_type='image',
                 content_group_key='itinerary', media_hash=r) for r in ('nine', 'ten')]


def setup(db, monkeypatch):
    row = create(db, monkeypatch)
    row.controls = {**row.controls, 'route_variant': ''}
    monkeypatch.setattr(service, 'candidate_materials', lambda *a: materials())
    monkeypatch.setattr(service, 'resolve_materials', lambda db, ks, route, tenant: [
        dict(asset_key=k, content_type='image', media_hash=k.removeprefix('map-')) for k in ks])
    monkeypatch.setattr(service, 'tenant_for_session', lambda *a: None)
    return row


def test_two_route_quotes_get_own_map_before_price_without_selection(session_factory, monkeypatch):
    with session_factory() as db:
        row = setup(db, monkeypatch)
        d = runtime.Decision(messages=[dict(route=r, script_id='price_special_' + r) for r in ('nine', 'ten')]).model_dump()
        route, parts = service.parts_for(db, row, catalog(), d)
        assert route == '' and row.controls['route_variant'] == ''
        assert [(p.get('asset_key') or p['content']) for p in parts] == ['map-nine', 'nine 10980', 'map-ten', 'ten 12980']
        row.messages += [{**p, 'direction': 'outgoing', 'status': 'sent'} for p in parts]
        _, again = service.parts_for(db, row, catalog(), d)
        assert [p['content'] for p in again] == ['nine 10980', 'ten 12980']


def test_unique_legacy_id_resolves_other_route_but_shared_id_needs_context():
    b = catalog()
    assert message_route(b, {'script_id': 'price_special_nine'}, 'ten') == 'nine'
    with pytest.raises(DecisionContractError):
        message_route(b, {'script_id': 'price_low'}, '')
    assert message_route(b, {'route': 'ten', 'script_id': 'price_low'}, 'nine') == 'ten'
    with pytest.raises(DecisionContractError):
        message_route(b, {'route': 'ten', 'script_id': 'price_special_nine'}, 'nine')


def test_text_only_quote_and_unknown_reference(session_factory, monkeypatch):
    with session_factory() as db:
        row = setup(db, monkeypatch)
        d = runtime.Decision(messages=[dict(route='ten', script_id='price_low', include_itinerary=False)]).model_dump()
        _, parts = service.parts_for(db, row, catalog(), d)
        assert [p['content'] for p in parts] == ['ten low']
        d['messages'][0]['script_id'] = 'invented'
        with pytest.raises(DecisionContractError):
            service.parts_for(db, row, catalog(), d)


def test_explicit_preview_and_automatic_quote_map_not_duplicated(session_factory, monkeypatch):
    with session_factory() as db:
        row = setup(db, monkeypatch)
        d = runtime.Decision(messages=[dict(asset_keys=['map-ten']), dict(route='ten', script_id='price_low')]).model_dump()
        _, parts = service.parts_for(db, row, catalog(), d)
        assert [p.get('asset_key') for p in parts].count('map-ten') == 1


def test_cross_route_seasonal_hotel_does_not_use_selected_route_season(session_factory, monkeypatch):
    b = catalog()
    b['routes']['ten']['groups']['hotel'] = dict(text='summer', assets=['room'],
        seasonal_override=dict(months=[12, 1, 2], text='winter', undated_text='seasonal'))
    with session_factory() as db:
        row = setup(db, monkeypatch); row.memory = {'departure_window': '2月'}
        d = runtime.Decision(messages=[dict(route='ten', script_id='hotel', asset_keys=['room'])]).model_dump()
        _, parts = service.parts_for(db, row, b, d)
        assert [p['content'] for p in parts] == ['winter']


def test_cross_route_asset_without_explicit_namespace_still_obeys_its_season(session_factory, monkeypatch):
    b = catalog()
    b['routes']['ten']['groups']['hotel'] = dict(text='summer', assets=['room'],
        seasonal_override=dict(months=[12, 1, 2], text='winter', undated_text='seasonal'))
    with session_factory() as db:
        row = setup(db, monkeypatch)
        row.controls = {**row.controls, 'route_variant': 'nine'}
        row.memory = {'departure_window': '2月'}
        monkeypatch.setattr(service, 'candidate_materials', lambda *a: [dict(key='room', routes=['ten'], content_type='image')])
        d = runtime.Decision(messages=[dict(text='winter', asset_keys=['room'])]).model_dump()
        _, parts = service.parts_for(db, row, b, d)
        assert [p['content'] for p in parts] == ['winter']


def test_visual_index_keeps_scope_and_delivery_history():
    b = catalog()
    b['routes']['nine']['groups']['oxygen'] = dict(text='equipment', assets=['o'])
    b['routes']['ten']['groups']['hotel'] = dict(text='room', assets=['h'],
        seasonal_override=dict(months=[12,1,2], text='winter', undated_text='unknown'))
    mats = [dict(key='o', routes=['nine'], content_type='image', media_hash='same'),
            dict(key='h', routes=['ten'], content_type='image')]
    sent = [dict(direction='outgoing', status='read', asset_key='old-alias', media_hash='same')]
    result = answer_visuals(b, mats, sent, {})
    assert list(result) == ['nine'] and result['nine'][0]['already_delivered']
    assert 'ten' in answer_visuals(b, mats, [], {'departure_window': '6月'})


def test_month_collection_disabled_without_disabling_party_intake(session_factory, monkeypatch):
    with session_factory() as db:
        row = setup(db, monkeypatch); value = service.state(row); b = catalog()
        b['intake'] = dict(enabled=True, field='party_size', question='people', options=['2'], wait_seconds=90)
        b['routes']['nine']['intake'] = dict(enabled=False, field='departure_window', question='month', options=['6'], wait_seconds=30)
        assert not service.begin_intake(row, value, b, 'nine')
        assert service.begin_intake(row, value, b, 'ten')


def test_package_migration_idempotent_and_preserves_facts():
    spec = spec_from_file_location('visual_config', Path(__file__).parents[2] / 'scripts/configure_visual_reception.py')
    module = module_from_spec(spec); spec.loader.exec_module(module)
    p = dict(intake=dict(field='departure_window'), ai_guidance='先問月份，30秒未答就介紹，介紹後才問人數；已知月份核對',
             seasonal_pricing={'price': 10}, content_groups=[{'asset': 'preserve'}])
    changed = module.updated(p)
    assert not changed['intake']['enabled'] and module.updated(changed) == changed
    assert changed['seasonal_pricing'] == p['seasonal_pricing'] and changed['content_groups'] == p['content_groups']
    assert module.updated({'intake': {'field': 'party_size'}}) == {'intake': {'field': 'party_size'}}


def test_semantic_truncation_retries_with_more_output(monkeypatch):
    seen = []
    def post(payload, timeout):
        seen.append(payload['max_tokens'])
        raise ValueError('deepseek_output_token_limit')
    monkeypatch.setattr(route_understanding, '_post_with_deadline', post)
    c = dict(skills=catalog(), messages=[dict(id='m', direction='incoming', content='test', content_type='text')], state={})
    from app.model_transport import EvaluationCallError
    with pytest.raises(EvaluationCallError):
        route_understanding.interpret(c, time.monotonic()+30)
    assert seen == [2400, 4800]


@pytest.mark.parametrize('profile,expected', [({}, 'UNCONFIRMED_SEASON'),
    ({'departure_window': '2月'}, 'WINTER_ROOM'), ({'departure_window': '6月'}, 'SUMMER_ROOM')])
def test_loaded_skill_uses_same_seasonal_intro_as_delivery(profile, expected):
    from app.reception_v3.skills import SkillRegistry
    from test_reception_v3_skills import context
    c = context('peach_9d_2027')
    c['skills']['routes']['peach_9d_2027']['groups']['map'].update(
        text='SUMMER_ROOM', seasonal_override=dict(months=[12, 1, 2], text='WINTER_ROOM',
        undated_text='UNCONFIRMED_SEASON'))
    loaded = SkillRegistry(c['skills'], profile=profile).load('peach-9d-2027')['instructions']
    intro = loaded.split('### 1. map\n')[1].split('素材：')[0].strip()
    assert intro == expected


def test_model_repairs_invalid_quote_reference_within_same_run(monkeypatch):
    from test_reception_v3_skills import context
    c = context('peach_9d_2027')
    for route in c['skills']['routes'].values():
        for script in route['scripts']:
            script['status'] = 'active'
    requests = model(monkeypatch, [final(messages=[dict(route='peach_11d_2027', script_id='wrong')]),
                                  final(messages=[dict(route='peach_11d_2027', script_id='price-11')])])
    decision, logs, _ = runtime.run_agent(c)
    assert len(requests) == 2
    assert decision['messages'][0]['script_id'] == 'price-11'
    assert decision['route_choice'] == 'unchanged'
