"""Exercise the real tool loop and delivery; model responses are simulated.

These tests verify enforcement and state transitions, not model language quality.
"""
from copy import deepcopy
from datetime import date, timedelta
import json

import pytest

from app.reception_v3 import runtime, service
from app.reception_v3.seasonal_quotes import departure_quote
from app.model_transport import EvaluationCallError
from test_reception_v3 import create
from test_reception_v3_skills import context, model, final, tool
from test_seasonal_retention import catalog, NOW, PEACH
from test_tibet_release_20261014 import EIGHT, TEN
from test_silent_handoff import run_for


def runtime_context(route=TEN):
    c = context(route)
    c['now'] = NOW
    c['skills']['routes'] = {
        key: {**deepcopy(p), 'version': p['package_version'],
              'facts': deepcopy(p['knowledge_facts']), 'scripts': deepcopy(p['fixed_answers']),
              'groups': {}, 'introduction_sequence': [], 'interval_seconds': 2}
        for key, p in catalog()['routes'].items()}
    c['skills']['reply']['opening_interval_seconds'] = 2
    c['skills']['silence'] = {'enabled': True, 'intervals_minutes': [180]}
    return c


def check_date(route=TEN, start='2027-06-01', end='2027-06-30', call_id='date'):
    return {'tool_calls': [tool('get_departure_quote', dict(route=route, start=start, end=end), call_id)]}


def results(request):
    return [json.loads(m['content']) for m in request['messages'] if m['role'] == 'tool']


def test_seasonal_runtime_quote_tool_template_and_hook_reach_delivery(session_factory, monkeypatch):
    c = runtime_context()
    script = departure_quote(c['skills'], TEN, '2027-06-01', '2027-06-30', NOW)['script_id']
    # Keep the expected template tied to the actual approved seasonal ID.
    model(monkeypatch, [check_date(), final(route_variant=TEN, messages=[
        {'script_id': script}, {'text': '您這次預計跟家人還是朋友一起出發呢？'}])])
    decision, logs, _ = runtime.run_agent(c)
    assert any(t['name'] == 'get_departure_quote' for log in logs for t in log['tool_calls'])
    with session_factory() as db:
        row = create(db, monkeypatch)
        _, parts = service.parts_for(db, row, c['skills'], decision)
        approved = next(s['answer_text'] for s in c['skills']['routes'][TEN]['scripts'] if s['id'] == script)
        assert [p['content'] for p in parts] == [approved, '您這次預計跟家人還是朋友一起出發呢？']
        assert all(p['content_type'] == 'text' for p in parts)
        assert '12,280' in approved and '不含' in approved


@pytest.mark.parametrize('route,start,end', [
    (TEN, '2027-03-20', '2027-04-10'),
    (EIGHT, '2027-04-21', '2027-04-24'),
    (PEACH, '2027-06-01', '2027-06-30'),
])
def test_seasonal_runtime_repair_stops_closed_route_and_old_drafts(session_factory, monkeypatch, route, start, end):
    c = runtime_context(route)
    requests = model(monkeypatch, [check_date(route, start, end),
        final(route_variant=route, start_introduction=True),
        final(route_variant=route, interrupt=True, messages=[{'text': '這個時段目前未開放，您想先了解其他選擇嗎？'}])])
    decision, _, _ = runtime.run_agent(c)
    assert results(requests[1])[-1]['status'] == 'unavailable'
    assert len(requests) == 3 and decision['interrupt'] and not decision['start_introduction']
    with session_factory() as db:
        row = create(db, monkeypatch)
        row.controls = {**row.controls, 'route_variant': route}
        value = service.state(row)
        value.update(intake_answered=True, intake_field='party_size', intake_deadline=row.virtual_now)
        service.append_plan(row, value, [{'content': '过时介绍', 'content_type': 'text'},
            {'content': '', 'content_type': 'image', 'asset_key': 'old-photo'}], 'introduction', 2)
        old_ids = {m['id'] for m in row.messages if m.get('status') == 'draft'}
        service.save(row, value)
        service.apply_decision(db, row, run_for(db, row), c['skills'], decision)
        assert all(m['status'] == 'cancelled' for m in row.messages if m['id'] in old_ids)
        assert not service.state(row).get('handoff')
        assert service.state(row)['intake_deadline'] is None
        assert row.controls['route_variant'] == route
        assert [m['content'] for m in row.messages if m.get('status') == 'draft'] == [decision['messages'][0]['text']]


def test_seasonal_runtime_failed_repair_never_returns_a_sendable_decision(monkeypatch):
    c = runtime_context(TEN)
    wrong = final(route_variant=TEN, start_introduction=True)
    model(monkeypatch, [check_date(TEN, '2027-03-20', '2027-04-10'), wrong, wrong])
    with pytest.raises(EvaluationCallError, match='seasonal_quote_not_validated'):
        runtime.run_agent(c)


@pytest.mark.parametrize('second_check', [
    check_date(TEN, '2027-05-01', '2027-05-05', 'holiday'),
    check_date(TEN, '2027-03-20', '2027-04-10', 'closed'),
    check_date(TEN, 'unknown', '', 'bad_date'),
    {'tool_calls': [tool('get_departure_quote', {'route': TEN, 'start': '2027-08-01'}, 'missing_end')]},
    {'tool_calls': [{'id': 'invalid-json', 'type': 'function', 'function': {
        'name': 'get_departure_quote', 'arguments': '{invalid'}}]},
    {'tool_calls': [tool('get_departure_quote', None, 'null_arguments')]},
])
def test_seasonal_runtime_new_date_invalidates_previous_quote_permission(monkeypatch, second_check):
    c = runtime_context()
    script = departure_quote(c['skills'], TEN, '2027-06-01', '2027-06-30', NOW)['script_id']
    requests = model(monkeypatch, [check_date(), second_check,
        final(route_variant=TEN, messages=[{'script_id': script}]),
        final(route_variant=TEN, interrupt=True, messages=[{'text': '您方便再確認一下想出發的日期嗎？'}])])
    decision, _, _ = runtime.run_agent(c)
    assert len(requests) == 4
    assert all(not m['script_id'] for m in decision['messages'])


def test_seasonal_runtime_quote_authorization_does_not_cross_routes(monkeypatch):
    c = runtime_context(EIGHT)
    script = departure_quote(c['skills'], EIGHT, '2027-06-01', '2027-06-30', NOW)['script_id']
    requests = model(monkeypatch, [check_date(TEN), final(route_variant=EIGHT, messages=[{'script_id': script}]),
        check_date(EIGHT, call_id='eight-date'), final(route_variant=EIGHT, messages=[{'script_id': script}])])
    decision, _, _ = runtime.run_agent(c)
    assert len(requests) == 4 and decision['messages'][0]['script_id'] == script


@pytest.mark.parametrize('route', [EIGHT, TEN, 'peach_9d_2027', PEACH])
def test_2027_full_calendar_and_all_alternative_departures(route):
    """Independent calendar oracle from the approved business periods (1,460 days)."""
    bundle = catalog()
    for offset in range(365):
        day = date(2027, 1, 1) + timedelta(days=offset)
        md = day.strftime('%m-%d')
        if route.startswith('peach_'):
            expected = ('unavailable' if not '03-20' <= md <= '04-10' else
                        'not_seasonal' if day.weekday() in (0, 4, 5, 6) else 'ask_date')
        elif (route == TEN and '03-11' <= md <= '04-30') or (route == EIGHT and '04-21' <= md <= '04-24'):
            expected = 'unavailable'
        elif any(lo <= md <= hi for lo, hi in [('02-01', '02-16'), ('04-25', '05-05'), ('09-25', '10-14')]):
            expected = 'holiday'
        elif day.month in (1, 2, 12):
            expected = 'unavailable'
        else:
            expected = 'quoted' if day.weekday() == (3 if route == EIGHT else 1) else 'ask_date'
        actual = departure_quote(bundle, route, day.isoformat(), '', NOW)
        assert actual['status'] == expected, (route, day, actual)
        if actual['status'] not in ('quoted', 'not_seasonal'):
            assert 'price_cny' not in actual
        for alternative in actual.get('alternatives', []):
            assert alternative['route_variant'] != route
            assert alternative['departure_examples'] == [day.isoformat()]
            available = departure_quote(bundle, alternative['route_variant'], day.isoformat(), '', NOW)
            assert available['status'] in ('quoted', 'not_seasonal'), (day, alternative, available)
