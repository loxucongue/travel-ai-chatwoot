"""Taiwan dollar amounts must survive import, calculation and outbound validation."""
from pathlib import Path

import pytest

from app.reception_v3.money import currency_text_error, money_fields
from app.reception_v3.party_quotes import price_scripts, pricing_evidence, validate_party_pricing
from app.reception_v3.seasonal_quotes import (
    contains_trip_quote, departure_quote, quote_error, validate_pricing, validate_availability,
)
from app.route_packages import _validate, RoutePackageError


def party():
    # Synthetic regression data, not a published business quote.
    return {'currency': 'TWD', 'base_price': 8500, 'source_ref': 'test-only',
            'discounts_per_person': {'2': 0, '4': 200}, 'private_supplements_per_person': {'4': 500},
            'shared_template': '拼團每人 NT${amount}；包含車導；不含機票。',
            'private_template': '獨團每人 NT${amount}；包含車導；不含機票。'}


def seasonal():
    return {'currency': 'TWD', 'years': [2027], 'minimum_notice_days': 1, 'weekday': 4,
            'bands': [{'script_id': 'price_spring', 'ranges': [['03-01', '05-31']],
                       'price': 8500, 'single_supplement': 2000}],
            'special_departures': [{'date': '2027-01-15', 'price': 7500,
                                   'script_id': 'price_special', 'group_type': 'shared', 'source_ref': 'test'}]}


def package():
    return {'schema_version': 1, 'route_variant': 'test_twd', 'package_version': 'test',
            'knowledge_version': 'test', 'branch': 'test', 'name': 'Test route',
            'selection_title': 'Test route', 'default_entry_message': 'Details',
            'required_slots': ['party_size'],
            'knowledge_facts': [{'id': 'test.fact', 'text': 'Test only', 'source_ref': 'test'}],
            'content_groups': {'intro': {'approved_text': 'Test route', 'asset_keys': [],
                              'delivery_mode': 'text_only', 'evidence_refs': ['test.fact']}},
            'content_sequence': ['intro'], 'party_pricing': party()}


def test_twd_arithmetic_and_evidence_are_explicit_without_conversion():
    policy = party()
    scripts = {s['id']: s for s in price_scripts(policy)}
    assert scripts['price_shared_4']['price'] == 8300
    assert scripts['price_private_4']['price'] == 8800
    assert 'NT$8,800' in scripts['price_private_4']['answer_text']
    assert all(s['currency'] == 'TWD' and 'price_cny' not in s for s in scripts.values())
    evidence = pricing_evidence({'routes': {'test': {'party_pricing': policy}}})
    assert evidence['test']['per_person_prices'][-1] == {'party_size': 4, 'group_type': 'private',
                                                       'currency': 'TWD', 'price': 8800}
    policy['discounts_per_person'] = {}
    policy['discount_progression'] = {'starts_at': 2, 'party_size_step': 2, 'amount_step': 100}
    policy['auto_quote_party_size_max'] = 6
    assert next(s for s in price_scripts(policy) if s['id'] == 'price_shared_6')['price'] == 8200
    assert next(s for s in price_scripts(policy, legacy_ceiling=8000)
                if s['id'] == 'price_shared_2')['price'] == 8000


@pytest.mark.parametrize('change', [
    {'base_price_cny': 8500}, {'base_price': True}, {'currency': 'USD'},
    {'shared_template': '每人人民币{amount}元'}, {'private_template': '每人{amount}元'},
    {'legacy_offer_price_cny': 8000},
    {'discounts_per_person': {}, 'auto_quote_party_size_max': 6,
     'discount_progression': {'starts_at': 2, 'party_size_step': 2, 'amount_step_cny': 100}},
])
def test_twd_rejects_mixed_or_ambiguous_price_configuration(change):
    with pytest.raises(ValueError):
        validate_party_pricing({**party(), **change})


def test_legacy_cny_is_still_read_and_not_relabeled():
    policy = party()
    policy.update(currency='CNY', base_price_cny=policy.pop('base_price'),
                  shared_template='人民幣{amount}/人', private_template='人民幣{amount}/人')
    values = price_scripts(policy)
    assert values[0]['price_cny'] == values[0]['price'] == 8500
    assert values[0]['currency'] == 'CNY'
    assert money_fields({'currency': 'CNY'}, {'price': 8000}, 'price')['price_cny'] == 8000
    with pytest.raises(ValueError):
        validate_party_pricing({**policy, 'base_price': 8500})


def test_twd_seasons_specials_and_single_supplements():
    policy = seasonal()
    validate_pricing(policy)
    bundle = {'routes': {'test': {'seasonal_pricing': policy}}}
    quote = departure_quote(bundle, 'test', '2027-03-05', None, '2026-10-29T10:00:00+08:00')
    assert (quote['status'], quote['currency'], quote['price'], quote['single_supplement']) == (
        'quoted', 'TWD', 8500, 2000)
    assert 'price_cny' not in quote and 'single_supplement_cny' not in quote
    quote = departure_quote(bundle, 'test', '2027-01-15', None, '2026-10-29T10:00:00+08:00')
    assert quote['price'] == 7500 and quote['currency'] == 'TWD' and quote['special_departure']
    options = departure_quote(bundle, 'test', '2027-01-14', '2027-01-16', '2026-10-29T10:00:00+08:00')
    assert options['departures'] == [{'date': '2027-01-15', 'currency': 'TWD', 'price': 7500}]
    availability = {k: v for k, v in policy.items() if k not in ('bands', 'weekday')}
    availability.update(ranges=[['01-01', '12-31']], weekdays=[4])
    validate_availability(availability)
    availability['special_departures'][0]['price_cny'] = 7500
    with pytest.raises(ValueError):
        validate_availability(availability)


@pytest.mark.parametrize('field', ['price', 'single_supplement'])
def test_twd_seasonal_bands_reject_cny_fields(field):
    policy = seasonal()
    policy['bands'][0][field + '_cny'] = policy['bands'][0].pop(field)
    with pytest.raises(ValueError):
        validate_pricing(policy)


@pytest.mark.parametrize('text', ['NT$8,500', 'NT $ 8,500', 'TWD 8500', 'NTD 8500',
                                   '新台幣8500', '新臺幣8,500', '台币8500', '8500新台幣', 'NT$800'])
def test_twd_free_text_cannot_bypass_date_quote_checks(text):
    assert contains_trip_quote(text)
    decision = {'route_variant': 'test', 'action': 'reply', 'messages': [{'text': text}]}
    assert quote_error(decision, {'routes': {'test': {'seasonal_pricing': seasonal(), 'scripts': []}}}, set())


@pytest.mark.parametrize('text', ['機票 NT$8,500', '單房差新台幣2000', '小費 NT$100', '預算TWD 8000'])
def test_twd_other_fees_are_not_trip_quotes(text):
    assert not contains_trip_quote(text)


@pytest.mark.parametrize('text', ['人民幣8500', 'RMB 8,500', '¥8500', '8500 CNY'])
def test_wrong_currency_amounts_are_rejected(text):
    assert currency_text_error(text, 'TWD')
    assert not currency_text_error('本公司不使用人民幣，使用新台幣。', 'TWD')
    assert not currency_text_error('NT$8,500', 'TWD')


def test_package_import_enforces_company_currency(monkeypatch):
    from app import route_packages
    monkeypatch.setitem(route_packages.PROFILE, 'currency', 'TWD')
    _validate(package(), Path('test.json'))
    for change in ({'currency': 'CNY'}, {'party_pricing': {**party(), 'currency': 'CNY'}},
                   {'knowledge_facts': [{'id': 'test.fact', 'text': '人民币8500', 'source_ref': 'test'}]}):
        with pytest.raises(RoutePackageError, match='currency_invalid'):
            _validate({**package(), **change}, Path('test.json'))


def test_outgoing_guard_checks_resolved_scripts_and_free_text(session_factory, monkeypatch):
    from app.reception_v3.service import parts_for
    from app.reception_v3.runtime import Decision
    from test_reception_v3 import create, bundle
    with session_factory() as db:
        row = create(db, monkeypatch)
        skills = bundle()
        skills['currency'] = 'TWD'
        skills['common_scripts'].append({'id': 'wrong_currency', 'text': 'RMB 8500', 'enabled': True})
        for message in ({'text': '人民幣8500'}, {'script_id': 'wrong_currency', 'text': ''}):
            decision = Decision(action='reply', route_variant='nine', messages=[message], reason='test').model_dump()
            with pytest.raises(ValueError, match='quote_currency_mismatch'):
                parts_for(db, row, skills, decision)
