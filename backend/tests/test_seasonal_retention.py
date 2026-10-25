"""Seasonal closures keep the conversation open without inventing products."""
from copy import deepcopy
import json

import pytest
from sqlalchemy import select

from app.route_packages import PACKAGE_ROOT, _validate, RoutePackageError
from app.reception_v3.seasonal_quotes import departure_quote, quote_error
from app.reception_v3 import live, service
from app.reception_v3.runtime import Decision
from app.models import HandoffTask, Notification, OutboundMessage
from scripts.configure_seasonal_retention_20261014 import update_package
from test_tibet_release_20261014 import PACKAGES, EIGHT, TEN
from test_silent_handoff import run_for
from test_live_audit_fixes import session
from live_fixture import setup

NOW = '2026-10-14T00:00:00Z'
PEACH = 'peach_11d_2027'


def catalog():
    routes = deepcopy(PACKAGES)
    for days in (9, 11):
        p = json.loads((PACKAGE_ROOT/f'peach-{days}d-2027/route-package.json').read_text(encoding='utf8'))
        routes[p['route_variant']] = p
    return {'routes': routes}


def test_closed_ten_day_spring_returns_dated_enabled_alternatives_not_a_handoff():
    result = departure_quote(catalog(), TEN, '2027-03-20', '2027-04-10', NOW)
    assert result['status'] == 'unavailable' and result['reason_code'] == 'season_closed'
    assert result['next_action'] == 'offer_alternatives'
    assert {a['route_variant'] for a in result['alternatives']} == {EIGHT, 'peach_9d_2027', PEACH}
    for a in result['alternatives']:
        assert all('2027-03-20' <= d <= '2027-04-10' for d in a['departure_examples'])
        assert 'price_cny' not in a
    assert 'price_cny' not in result


def test_disabled_or_undated_product_is_never_recommended():
    bundle = catalog()
    del bundle['routes'][PEACH]
    bundle['routes']['unknown_route'] = {'name': '未知日期的线路'}
    result = departure_quote(bundle, TEN, '2027-03-20', '2027-04-10', NOW)
    assert {a['route_variant'] for a in result['alternatives']} == {EIGHT, 'peach_9d_2027'}


def test_fixed_customer_date_does_not_silently_move_to_another_weekday():
    result = departure_quote(catalog(), TEN, '2027-03-23', '', NOW)
    assert result['alternatives'] == []  # Tuesday; Thursday/Friday is not this date.
    assert result['next_action'] == 'offer_reopening_notification'


@pytest.mark.parametrize('route,start,end,reason', [
    (EIGHT, '2027-04-21', '2027-04-24', 'season_closed'),
    (EIGHT, '2027-12-16', '', 'winter_catalog_unavailable'),
    (TEN, '2028-06-01', '2028-06-30', 'year_unpublished'),
])
def test_no_matching_product_offers_reopening_notification(route, start, end, reason):
    result = departure_quote(catalog(), route, start, end, NOW)
    assert result['status'] == 'unavailable' and result['reason_code'] == reason
    assert result['alternatives'] == []
    assert result['next_action'] == ('offer_winter_recommendation' if reason == 'winter_catalog_unavailable' else 'offer_reopening_notification')
    assert 'price_cny' not in result


def test_peach_outside_flowering_season_recommends_supported_summer_products():
    result = departure_quote(catalog(), PEACH, '2027-06-01', '2027-06-30', NOW)
    assert result['status'] == 'unavailable'
    assert {a['route_variant'] for a in result['alternatives']} == {EIGHT, TEN}


def test_peach_in_season_uses_existing_quote_template_without_changing_offer():
    result = departure_quote(catalog(), PEACH, '2027-03-20', '2027-04-10', NOW)
    assert result['status'] == 'not_seasonal' and result['availability_checked']
    assert result['departure_examples']
    package = catalog()['routes'][PEACH]
    quote = next(s for s in package['fixed_answers'] if s['id'] == 'price')
    assert '12,180' in quote['answer_text'] and quote['valid_for_days'] == 30


@pytest.mark.parametrize('route,start,end,status', [
    (EIGHT, '2027-04-25', '2027-05-05', 'holiday'),
    (TEN, '2027-05-01', '2027-05-05', 'holiday'),
    (TEN, '2027-09-25', '2027-10-14', 'holiday'),
    (EIGHT, '2027-04-19', '2027-04-24', 'ask_date'),
    (TEN, '2027-06-29', '2027-07-02', 'ask_date'),
])
def test_special_quotes_and_partial_date_windows_are_not_called_closed(route, start, end, status):
    assert departure_quote(catalog(), route, start, end, NOW)['status'] == status


def test_short_notice_still_requires_advisor():
    assert departure_quote(catalog(), TEN, '2027-06-01', '', '2027-05-03T00:00:00Z')['status'] == 'advisor'


@pytest.mark.parametrize('fields', [
    {'start_introduction': True}, {'action': 'queue'}, {'interrupt': False},
    {'messages': [{'script_id': 'price'}]}, {'messages': [{'text': '團費人民幣12,180/人'}]},
])
def test_checked_closed_product_cannot_continue_intake_introduction_or_quote(fields):
    decision = Decision(route_variant=PEACH, interrupt=True, **{k: v for k, v in fields.items() if k != 'interrupt'}).model_dump()
    decision.update(fields)
    checked = {PEACH: {'status': 'unavailable'}}
    assert quote_error(decision, catalog(), set(), checked)
    allowed = Decision(route_variant=PEACH, interrupt=True,
                       messages=[{'text': '這個時段尚未開放，您想先看看其他選擇嗎？'}]).model_dump()
    assert quote_error(allowed, catalog(), set(), checked) is None
    assert quote_error(Decision(route_variant=PEACH, action='handoff').model_dump(), catalog(), set(), checked) is None


@pytest.mark.parametrize('change', [
    {'years': []}, {'weekdays': [7]}, {'minimum_notice_days': 0}, {'ranges': [['04-10', '03-20']]},
])
def test_malformed_departure_availability_is_rejected(change):
    p = catalog()['routes'][PEACH]
    p['departure_availability'].update(change)
    with pytest.raises(RoutePackageError, match='availability_invalid'):
        _validate(p, PACKAGE_ROOT/'bad.json')


def test_availability_migration_preserves_every_other_field_and_operator_changes():
    original = catalog()['routes'][PEACH]
    original.pop('departure_availability')
    original['ai_guidance'] = '运营自己配置的说明'
    updated = update_package(original)
    assert {k: v for k, v in updated.items() if k != 'departure_availability'} == original
    assert update_package(updated) == updated


def test_closure_offer_does_not_create_task_or_switch_route(session_factory, monkeypatch):
    from test_reception_v3 import bundle, create
    with session_factory() as db:
        row = create(db, monkeypatch)
        row.controls['route_variant'] = 'nine'
        value = service.state(row)
        value.update(intake_answered=True, intake_field='party_size', intake_deadline=row.virtual_now)
        service.append_plan(row, value, [{'content': '旧介绍不能再发', 'content_type': 'text'}], 'introduction', 0)
        service.save(row, value)
        service.apply_decision(db, row, run_for(db, row), bundle(), Decision(route_variant='nine', interrupt=True,
            messages=[{'text': '這個時段還沒有開放團期。您想先看看其他時段的選擇嗎？'}]).model_dump())
        assert not service.state(row).get('handoff')
        assert row.controls['route_variant'] == 'nine'
        assert service.state(row)['intake_deadline'] is None
        assert service.state(row)['delivery_kind'] == 'reply'
        assert next(m for m in row.messages if m['content'] == '旧介绍不能再发')['status'] == 'cancelled'
        assert db.scalar(select(HandoffTask)) is None


def test_reopening_consent_and_contact_reach_advisor_without_customer_message(session_factory, monkeypatch):
    setup(session_factory, monkeypatch)
    monkeypatch.setattr(live, 'client_for', lambda *a: pytest.fail('no real channel call'))
    from test_reception_v3 import bundle
    with session_factory() as db:
        row = session(db)
        decision = Decision(action='handoff', handoff_type='contact',
            profile={'contact_channel': 'Email', 'contact_value': 'customer@example.test',
                     'reopening_interest': {'route_variant': PEACH, 'departure_window': '2028年3月',
                                            'notification_requested': True}},
            handoff_reason='客户同意登记下次开放通知，请顾问按Email联系；尚无开放日期。',
            messages=[{'text': '这句不该发给客户'}]).model_dump()
        service.apply_decision(db, row, run_for(db, row), bundle(), decision)
        db.commit()
        task = db.scalar(select(HandoffTask))
        assert task is not None
        assert row.memory['contact_value'] == 'customer@example.test'
        assert row.memory['reopening_interest']['departure_window'] == '2028年3月'
        assert row.memory['reopening_interest']['notification_requested']
        assert task.reason_detail == '客户同意登记下次开放通知，请顾问按Email联系；尚无开放日期。'
        assert 'reopening_interest' not in task.reason_detail
        assert db.scalar(select(Notification)).event_type == 'handoff.created'
        assert db.scalar(select(OutboundMessage)) is None
        assert not any(m.get('status') in ('draft', 'submitted') for m in row.messages)
