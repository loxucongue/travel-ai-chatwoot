import json

import pytest

from app.reception_v3.travel_dates import calculate_travel_window
from app.reception_v3 import runtime
from test_reception_v3_skills import context, final, model, tool


def test_full_holiday_is_inclusive_and_cannot_be_used_as_departure_window():
    result = calculate_travel_window(9, 1, 1, start='2027-04-03', end='2027-04-11')
    assert result['available_days'] == 9
    assert result['required_total_days'] == 11
    assert result['shortfall_days'] == 2
    assert result['available_itinerary_days'] == 7
    assert result['fits_duration'] is False
    assert result['possible_departure_window'] is None


@pytest.mark.parametrize('start,end,days,before,after,earliest,latest', [
    ('2027-04-03', '2027-04-13', 9, 1, 1, '2027-04-04', '2027-04-04'),
    ('2027-04-03', '2027-04-15', 9, 1, 1, '2027-04-04', '2027-04-06'),
    ('2027-12-28', '2028-01-07', 9, 1, 1, '2027-12-29', '2027-12-29'),
    ('2028-02-27', '2028-03-08', 9, 1, 1, '2028-02-28', '2028-02-28'),
    # Explicitly completed transfers / longer connections are caller inputs.
    ('2027-04-03', '2027-04-12', 9, 0, 1, '2027-04-03', '2027-04-03'),
    ('2027-04-03', '2027-04-17', 9, 3, 2, '2027-04-06', '2027-04-07'),
])
def test_possible_starts_leave_room_for_entire_itinerary_and_return(start,end,days,before,after,earliest,latest):
    result = calculate_travel_window(days, before, after, start=start, end=end)
    assert result['fits_duration'] is True
    assert result['possible_departure_window'] == {'start': earliest, 'end': latest}
    assert result['scope'] == 'duration_only_not_departure_or_flight_availability'


def test_day_count_without_dates_and_required_duration_without_holiday():
    result = calculate_travel_window(11, 1, 1, available_days=12)
    assert result['required_total_days'] == 13 and result['shortfall_days'] == 1
    assert 'possible_departure_window' not in result
    result = calculate_travel_window(8, 1, 1)
    assert result['required_total_days'] == 10
    assert 'fits_duration' not in result


@pytest.mark.parametrize('args', [
    {'itinerary_days': True}, {'itinerary_days': 9.5}, {'days_before': -1},
    {'start': '2027-04-03'}, {'start': '2027-04-11', 'end': '2027-04-03'},
    {'start': '2027-02-29', 'end': '2027-03-09'},
    {'start': '2027-04-03', 'end': '2027-04-11', 'available_days': 10},
])
def test_invalid_or_conflicting_model_arguments_are_rejected(args):
    with pytest.raises(ValueError):
        calculate_travel_window(**{'itinerary_days': 9, 'days_before': 1, 'days_after': 1, **args})


def test_runtime_returns_calendar_result_to_model_without_rewriting_answer(monkeypatch):
    args = {'itinerary_days': 9, 'days_before': 1, 'days_after': 1,
            'start': '2027-04-03', 'end': '2027-04-11'}
    answer = '這條9日行程連同往返建議留11天，您的假期還差2天。'
    requests = model(monkeypatch, [
        {'content': '', 'tool_calls': [tool('calculate_travel_window', args)]},
        final(interrupt=True, messages=[{'text': answer}]),
    ])
    decision, logs, _ = runtime.run_agent(context('peach_9d_2027'))
    result = next(m for m in requests[1]['messages'] if m['role'] == 'tool')
    assert json.loads(result['content'])['shortfall_days'] == 2
    assert logs[0]['tool_calls'][0]['result']['possible_departure_window'] is None
    assert decision['messages'][0]['text'] == answer
    assert decision['interrupt'] and not decision['start_introduction']


def test_runtime_exposes_bad_arguments_for_model_correction(monkeypatch):
    requests = model(monkeypatch, [
        {'content': '', 'tool_calls': [tool('calculate_travel_window', {
            'itinerary_days': 9, 'days_before': 1, 'days_after': 1,
            'start': '2027-04-03', 'end': '2027-04-11', 'available_days': 11})]},
        final(messages=[{'text': '讓我按您給的日期核對。'}]),
    ])
    runtime.run_agent(context('peach_9d_2027'))
    result = next(m for m in requests[1]['messages'] if m['role'] == 'tool')
    assert json.loads(result['content']) == {'error': 'available_days_conflicts_with_dates'}


def test_business_source_is_traceable_and_transfer_details_load_on_request(monkeypatch):
    import hashlib
    from app.reception_v3.skills import service_knowledge
    from app.route_packages import PACKAGE_ROOT
    knowledge = service_knowledge()
    source = next(s for s in knowledge['sources'] if s['id'] == 'business.feedback.20261101')
    assert hashlib.sha256((PACKAGE_ROOT.parent/'service-knowledge'/source['snapshot_path']).read_bytes()).hexdigest() == source['snapshot_sha256']
    detail = next(f['details'] for f in knowledge['facts'] if f['id'] == 'service.tibet_transfer_details')
    ctx = context('peach_9d_2027')
    ctx['skills']['service_knowledge'] = {'version': knowledge['knowledge_version'], 'facts': knowledge['facts']}
    requests = model(monkeypatch, [
        {'content': '', 'tool_calls': [tool('get_service_facts', {})]}, final(),
    ])
    runtime.run_agent(ctx)
    assert detail not in json.dumps(requests[0]['messages'], ensure_ascii=False)
    result = next(m for m in requests[1]['messages'] if m['role'] == 'tool')
    assert detail in result['content']
    assert 'service.shared_trip_travel_days' in json.dumps(requests[0]['messages'])


def test_model_can_answer_feasibility_before_intake_starts_introduction(session_factory, monkeypatch):
    from app.reception_v3 import service
    from test_reception_v3 import create, step, answer, delivered
    with session_factory() as db:
        row = create(db, monkeypatch)
        row.controls = {**row.controls, 'route_variant': 'nine'}
        service.save(row, {**service.state(row), 'opening_started': True, 'intake_status': 'done',
                           'intake_answered': True, 'pending_event': 'customer_message'})
        text = '行程連同往返需要11天，您的假期還差2天。'
        monkeypatch.setattr(service, 'run_agent', lambda c: answer(route_variant='nine',
            interrupt=True, start_introduction=False, messages=[{'text': text}],
            profile={'party_size': 2, 'holiday_window': {'start': '2027-04-03', 'end': '2027-04-11', 'scope': 'whole_trip'}}))
        db.commit()
        for _ in range(6):
            step(db, row)
        assert text in delivered(row)
        assert not any(m.get('v3_kind') == 'introduction' for m in row.messages)
        assert row.memory['holiday_window']['end'] == '2027-04-11'


def test_time_scope_from_semantic_model_reaches_generation_with_customer_evidence(monkeypatch):
    from app.reception_v3 import route_understanding as semantic
    from test_route_understanding_20261031 import ctx, result, semantic_model
    ctx = ctx('4/3到4/11包含台北來回，我們4位包團能排嗎？')
    ctx['profile']['group_type'] = 'private'
    understanding = result(ctx, 'inquire')
    understanding['travel_time'] = {'task': 'check_actual_connections', 'date_scope': 'whole_trip',
        'group_type': 'private', 'start': '2027-04-03', 'end': '2027-04-11',
        'context_note': '包團需核實航班，不能直接套拼團交通預留。',
        'evidence': [{'message_id': 'customer', 'quote': '我們4位包團'}]}
    small = semantic_model(monkeypatch, [understanding])
    main = model(monkeypatch, [final(route_variant=ctx['route_variant'], interrupt=True,
                                    messages=[{'text': '需要按台北往返的實際航班核對。您們有看好的航班嗎？'}])])
    runtime.run_agent(ctx)
    data = json.loads(small[0]['messages'][1]['content'])
    assert data['known_customer_preferences']['group_type'] == 'private'
    assert 'check_actual_connections' in json.dumps(main[0]['messages'])
    # No content-based Python decision replaces the model's interpretation.
    assert all('calculate_travel_window' != call['function']['name']
               for m in main[0]['messages'] for call in m.get('tool_calls', []))
    bad = {**understanding, 'travel_time': {**understanding['travel_time'],
        'evidence': [{'message_id': 'offer', 'quote': '桃花＋珠峰11日'}]}}
    with pytest.raises(ValueError, match='travel_scope_requires_customer_evidence'):
        semantic.validate(bad, semantic.task(ctx))
