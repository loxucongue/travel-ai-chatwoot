"""#493: a reference map is not a selection, but its alternative can be quoted.

Exercise the actual route/template guards and persisted delivery across turns.
Model outputs are replayed; these tests do not claim live-model quality coverage.
"""
from copy import deepcopy

import pytest

from app.reception_v3 import runtime, service
from app.reception_v3.party_quotes import apply_price_scripts, quote_error
from app.reception_v3.route_choice import choice_error
from test_itinerary_preview_20261025 import preview_context
from test_party_quotes_20261025 import package
from test_reception_v3 import answer, step
from test_reception_v3_skills import model, final
from test_v3_intake_followup import create


NINE = 'peach_9d_2027'
ELEVEN = 'peach_11d_2027'


def catalog():
    c = preview_context()
    for days, route in ((9, NINE), (11, ELEVEN)):
        c['skills']['routes'][route]['party_pricing'] = package(days)['party_pricing']
        apply_price_scripts(c['skills']['routes'][route])
    c['skills']['reply']['opening_interval_seconds'] = 1
    c['skills']['silence'] = {'enabled': False, 'intervals_minutes': [1440]}
    return c


def quote(**changes):
    values = dict(route_choice='recommend', profile={'discussion_route': NINE, 'party_size': 4,
        'group_type': 'private', 'everest_preference': 'avoid'},
        messages=[{'script_id': 'price_private_4'}])
    values.update(changes)
    return runtime.Decision(**values).model_dump()


@pytest.mark.parametrize('selected', ['', ELEVEN])
def test_recommendation_resolves_nine_day_price_without_accepting_it(session_factory, monkeypatch, selected):
    from test_reception_v3 import create as basic_create
    c = catalog()
    with session_factory() as db:
        row = basic_create(db, monkeypatch)
        row.controls = {**row.controls, 'route_variant': selected}
        decision = quote(route_variant=selected)
        c.update(route_variant=selected, profile={}, state={})
        assert choice_error(decision, c) is None
        route, parts = service.parts_for(db, row, c['skills'], decision)
        expected = next(s['answer_text'] for s in c['skills']['routes'][NINE]['scripts']
                        if s['id'] == 'price_private_4')
        assert [p['content'] for p in parts] == [expected]
        assert '10,980' in expected and '12,980' not in expected
        assert route == selected and row.controls['route_variant'] == selected


@pytest.mark.parametrize('bad', [
    {'messages': [{'script_id': 'price_shared_4'}]},
    {'messages': [{'script_id': 'price_private_6'}]},
    {'messages': [{'text': '每人人民幣 9999 元'}]},
    {'profile': {'discussion_route': NINE, 'party_size_range': '4位以上', 'group_type': 'private'}},
])
def test_recommended_product_keeps_price_guards(bad):
    assert quote_error(quote(**bad), catalog()['skills'], {})


def test_runtime_repairs_wrong_group_for_unselected_recommendation(monkeypatch):
    c = catalog()
    c['messages'].append({'direction': 'incoming', 'content': '4人包團不上珠峰價格是多少？'})
    wrong = quote(messages=[{'script_id': 'price_shared_4'}])
    requests = model(monkeypatch, [final(**wrong), final(**wrong), final(**quote())])
    decision, logs, _ = runtime.run_agent(c)
    assert decision['messages'][0]['script_id'] == 'price_private_4'
    assert any(log.get('status') == 'quote_rejected' for log in logs)
    assert len(requests) == 3


def test_three_turn_preview_quote_then_explicit_selection(session_factory, monkeypatch):
    c = catalog()
    spec = c['skills']
    answers = [answer(messages=[{'text': '先給您看11日參考行程。', 'asset_keys': ['map-11']}],
                      profile={'party_size_range': '4位以上', 'party_size_min': 4, 'group_type': 'private'}),
               (quote(), [], 'replayed-493'),
               answer(route_choice='selected', route_variant=NINE, start_introduction=True)]
    with session_factory() as db:
        row = create(db, monkeypatch, entry='我人數有4位以上，想自己包團',
                     model=lambda _: answers.pop(0) if answers else answer(action='wait'))
        monkeypatch.setattr(service, 'compile_skills', lambda _: deepcopy(spec))
        monkeypatch.setattr(service, 'candidate_materials', lambda *a: deepcopy(c['available_materials']))
        monkeypatch.setattr(service, 'tenant_for_session', lambda *a: 1)
        monkeypatch.setattr(service, 'resolve_materials', lambda db, keys, route, tenant: [
            {**m, 'asset_key': m['key'], 'content': '', 'route_variant': route}
            for m in c['available_materials'] if m['key'] in keys])
        for _ in range(8):
            step(db, row, 2)
        assert any(m.get('asset_key') == 'map-11' and m.get('status') == 'simulated_delivered' for m in row.messages)
        assert not row.controls.get('route_variant')
        service.add_message(db, row, '如果是明年4月，4人包團不上珠峰價格是大概是多少呢？', 'quote')
        db.commit()
        for _ in range(8):
            step(db, row, 2)
        quotes = [m['content'] for m in row.messages if m.get('status') == 'simulated_delivered' and '10,980' in m.get('content', '')]
        assert len(quotes) == 1
        assert not service.state(row).get('handoff')
        assert not row.controls.get('route_variant')
        assert row.memory['discussion_route'] == NINE and row.memory['party_size'] == 4
        assert 'party_size_range' not in row.memory
        service.add_message(db, row, '就選9日，請介紹', 'select')
        db.commit()
        for _ in range(8):
            step(db, row, 2)
        assert row.controls['route_variant'] == NINE
        assert service.state(row)['completed_introductions'] == [NINE]


def test_seasonal_recommendation_cannot_bypass_date_check(monkeypatch):
    from app.model_transport import EvaluationCallError
    c = catalog()
    c['skills']['routes'][NINE].pop('party_pricing')
    c['skills']['routes'][NINE]['seasonal_pricing'] = {'test': True}
    d = quote()
    model(monkeypatch, [final(**d)] * 4)
    with pytest.raises(EvaluationCallError, match='seasonal_quote_not_validated'):
        runtime.run_agent(c)


def test_recommended_script_keeps_its_own_seasonal_accommodation_override(session_factory, monkeypatch):
    from test_reception_v3 import create as basic_create
    c = catalog()
    c['skills']['routes'][NINE]['groups']['hotel'] = {
        'text': 'Summer room', 'assets': ['room'],
        'seasonal_override': {'months': [12, 1, 2], 'text': 'Winter alternative', 'undated_text': 'Ask dates'}}
    with session_factory() as db:
        row = basic_create(db, monkeypatch)
        row.memory = {'departure_window': '2月'}
        _, parts = service.parts_for(db, row, c['skills'], quote(messages=[{'script_id': 'hotel'}]))
        assert [p['content'] for p in parts] == ['Winter alternative']
