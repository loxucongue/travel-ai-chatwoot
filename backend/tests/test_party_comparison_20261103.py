from copy import deepcopy

import pytest

from app.automation_models import CustomerQuoteOffer
from app.reception_v3 import service
from app.reception_v3.party_quotes import quote_error, pricing_evidence, comparison_text
from app.reception_v3.quotes import personalize_quotes, subject_key
from app.reception_v3.runtime import Decision
from app.reception_v3.seasonal_quotes import quote_error as date_error
from test_party_quote_tiers_20261026 import current
from test_reception_v3 import create


def comparison(counts=(2, 4), group='shared'):
    return Decision(route_variant='nine', messages=[{
        'quote_options': [f'price_{group}_{n}' for n in counts]}]).model_dump()


@pytest.mark.parametrize('counts,amounts', [
    ((2, 4), (12780, 12580)), ((3, 5), (12780, 12580)),
    ((4, 6), (12580, 12380)), ((2, 3), (12780, 12780)),
    ((2, 4, 6), (12780, 12580, 12380)),
])
def test_hypothetical_options_delivered_without_confirming_party(session_factory, monkeypatch, counts, amounts):
    with session_factory() as db:
        row = create(db, monkeypatch)
        row.memory = {'party_size_range': [2, 6], 'group_type': 'shared'}
        old = deepcopy(row.memory)
        value = current()
        decision = comparison(counts)
        assert quote_error(decision, value, row.memory) is None
        _, parts = service.parts_for(db, row, value, decision)
        text = '\n'.join(p['content'] for p in parts)
        for count, amount in zip(counts, amounts):
            assert any(f'{count}位報名：' in line and f'{amount:,} /人' in line for line in text.splitlines())
        assert text.count('包含：') == text.count('不含：') == 1
        assert text.count('西藏桃花節11日遊') == 1
        assert row.memory == old and decision['profile'] == {}


def test_comparison_does_not_need_to_override_an_existing_confirmed_count():
    assert quote_error(comparison(), current(), {'party_size': 3}) is None


@pytest.mark.parametrize('ids,group', [
    (['price_shared_2'], 'shared'), (['price_shared_2'] * 2, 'shared'),
    (['price_shared_2', 'price_shared_7'], 'shared'),
    (['price_private_4', 'price_private_5'], 'private'),
    (['price_shared_2', 'price_private_4'], 'shared'),
    (['price_shared_2', 'price_shared_4'], 'private'),
])
def test_invalid_or_incompatible_options_remain_rejected(ids, group):
    decision = Decision(route_variant='nine', messages=[{'quote_options': ids}]).model_dump()
    assert quote_error(decision, current(), {'group_type': group})


@pytest.mark.parametrize('extra', [{'text': '團費¥1/人'}, {'script_id': 'price_shared_2'},
    {'asset_keys': ['map']}, {'material_rule_id': 'promo'}])
def test_comparison_cannot_replace_verified_amounts_or_hide_conflicting_actions(extra):
    decision = comparison()
    decision['messages'][0].update(extra)
    assert quote_error(decision, current(), {})


def test_no_extra_freehand_quote_and_no_disabled_prices():
    decision = comparison()
    decision['messages'].append({'text': '團費人民幣¥ 12,180 /人'})
    assert quote_error(decision, current(), {})
    value = current()
    value['routes']['nine']['scripts'] = []
    assert quote_error(comparison(), value, {})
    assert pricing_evidence(value)['nine']['per_person_prices'] == []


def test_comparison_cannot_bypass_unavailable_date_or_private_departure_gate():
    for result in ({'status': 'unavailable', 'winter_recommendation': None}, {'status': 'private_departure'}):
        assert date_error(comparison(), current(), set(), {'nine': result})


def test_other_route_cannot_reuse_peach_count_prices():
    value = current()
    value['routes']['nine'].pop('party_pricing')
    assert quote_error(comparison(), value, {})


def test_comparison_cannot_quote_a_route_the_customer_rejected():
    from app.reception_v3.route_choice import choice_error
    decision = comparison()
    decision['route_variant'] = 'peach_11d_2027'
    context = {'route_variant': 'peach_11d_2027', 'profile': {'everest_preference': 'avoid'},
               'state': {}, 'event': 'customer_message', 'skills': {'routes': {'peach_11d_2027': {}}}}
    assert choice_error(decision, context)


def test_existing_legacy_promise_used_in_both_evidence_and_output(session_factory, monkeypatch):
    with session_factory() as db:
        row = create(db, monkeypatch)
        row.virtual_now = '2026-11-03T09:00:00+08:00'
        db.add(CustomerQuoteOffer(subject_key=subject_key(db,row), route_variant='nine',
            offer={'first_quoted_on': '2026-10-13', 'valid_until': '2026-11-12'}))
        db.flush()
        value = personalize_quotes(db, row, current())
        text = comparison_text(value['routes']['nine'], comparison()['messages'][0]['quote_options'])
        assert text.count('12,180') == 2 and '12,780' not in text
        prices = pricing_evidence(value)['nine']['per_person_prices']
        assert {p['price_cny'] for p in prices if p['group_type'] == 'shared'} == {12180}
        assert next(p for p in prices if p['group_type'] == 'private' and p['party_size'] == 4)['price_cny'] == 12980


def test_render_preserves_configured_room_terms_and_inclusions():
    spec = current()['routes']['nine']
    spec['party_pricing']['shared_template'] = '測試線\n兩人一房，¥{amount}/人（含早餐）\n不含機票'
    text = comparison_text(spec, ['price_shared_2', 'price_shared_4'])
    assert '2位報名：兩人一房，¥12,780/人（含早餐）' in text
    assert '4位報名：兩人一房，¥12,580/人（含早餐）' in text
    assert text.count('不含機票') == 1
