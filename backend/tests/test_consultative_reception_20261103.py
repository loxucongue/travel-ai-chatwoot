from copy import deepcopy

import pytest

from app.reception_v3 import service
from app.reception_v3.party_quotes import pricing_evidence, quote_error, shared_reference
from app.reception_v3.quotes import personalize_quotes, subject_key
from app.automation_models import CustomerQuoteOffer
from app.reception_v3.runtime import Decision
from app.reception_v3.seasonal_quotes import quote_error as date_error
from test_party_quote_tiers_20261026 import current
from test_reception_v3 import create, answer, step, delivered
from test_v3_intake_followup import create as intake_create


@pytest.mark.parametrize('days,amount', [(9, 10780), (11, 12780)])
@pytest.mark.parametrize('profile', [{}, {'party_size_range': [2, 4]}, {'group_type': 'undecided'}])
def test_reference_price_keeps_unknown_customer_count(session_factory, monkeypatch, days, amount, profile):
    with session_factory() as db:
        row = create(db, monkeypatch)
        row.memory = deepcopy(profile)
        value = current(days)
        key = pricing_evidence(value)['nine']['reference_script_id']
        decision = Decision(route_variant='nine', messages=[{'script_id': key}]).model_dump()
        assert quote_error(decision, value, profile) is None
        _, parts = service.parts_for(db, row, value, decision)
        assert f'{amount:,}' in parts[0]['content']
        assert parts[0]['content'].count('包含：') == 1
        assert row.memory == profile and decision['profile'] == {}


@pytest.mark.parametrize('profile', [{'party_size': 4}, {'group_type': 'private'}, {'party_size': 7}])
def test_reference_cannot_override_known_party_or_private_price(profile):
    value = current()
    key = shared_reference(value['routes']['nine'])['id']
    decision = Decision(route_variant='nine', messages=[{'script_id': key}]).model_dump()
    assert quote_error(decision, value, profile)


def test_reference_still_checks_amounts_enabled_prices_and_dates():
    value = current()
    key = shared_reference(value['routes']['nine'])['id']
    decision = Decision(route_variant='nine', messages=[{'script_id': key}]).model_dump()
    assert date_error(decision, value, set(), {'nine': {'status': 'unavailable'}})
    for text in ('每人價人民幣¥ 12,345 /人', '每人價人民幣¥ 12,380 /人'):
        bad = Decision(route_variant='nine', messages=[{'script_id': key, 'text': text}]).model_dump()
        assert quote_error(bad, value, {})
    for s in value['routes']['nine']['scripts']:
        s['status'] = 'disabled'
    assert quote_error(decision, value, {})
    assert pricing_evidence(value)['nine']['reference_script_id'] is None


def test_reference_honors_existing_valid_lower_price(session_factory, monkeypatch):
    with session_factory() as db:
        row = create(db, monkeypatch)
        row.virtual_now = '2026-11-03T09:00:00+08:00'
        db.add(CustomerQuoteOffer(subject_key=subject_key(db, row), route_variant='nine',
            offer={'first_quoted_on': '2026-10-13', 'valid_until': '2026-11-12'}))
        db.flush()
        assert shared_reference(personalize_quotes(db, row, current())['routes']['nine'])['price_cny'] == 12180


def test_quote_evidence_ids_round_trip_and_repair_unknown_references():
    value = current()
    for script in value['routes']['nine']['scripts']:
        if script['id'] == 'price_shared_6':
            script['status'] = 'disabled'
    evidence = pricing_evidence(value)['nine']['per_person_prices']
    assert 'price_shared_6' not in {p['script_id'] for p in evidence}
    for price in evidence:
        decision = Decision(route_variant='nine', messages=[{'quote_options': [price['script_id']]}]).model_dump()
        assert quote_error(decision, value, {'group_type': price['group_type']}) is None
    invalid = Decision(route_variant='nine', messages=[{'quote_options': ['nine.per_person_prices.4.shared']}]).model_dump()
    error = quote_error(invalid, value, {})
    assert 'price_shared_4' in error and 'price_shared_6' not in error


@pytest.mark.parametrize('start_intro', [False, True])
def test_intake_does_not_discard_a_model_answer(session_factory, monkeypatch, start_intro):
    text = '團費包含酒店早餐，午晚餐不包含。'
    with session_factory() as db:
        row = intake_create(db, monkeypatch, model=lambda c: answer(
            route_variant='nine', start_introduction=start_intro, messages=[{'text': text}]))
        step(db, row, 0)
        service.add_message(db, row, '人數還沒定，先問一下有含餐嗎？', 'question')
        db.commit()
        step(db, row, 0)
        for _ in range(6):
            step(db, row, 2)
        assert text in delivered(row)
        if not start_intro:
            assert '完整行程图' not in delivered(row)
        else:
            assert delivered(row).index(text) < delivered(row).index('完整行程图')
        assert 'party_size' not in row.memory


def test_first_question_answer_does_not_get_replaced_by_intake(session_factory, monkeypatch):
    with session_factory() as db:
        text = '團費有包含酒店早餐。'
        row = intake_create(db, monkeypatch, entry='九日的早餐有含嗎？', model=lambda c: answer(
            route_variant='nine', start_introduction=True, messages=[{'text': text}]))
        for _ in range(7):
            step(db, row, 2)
        assert text in delivered(row)
        assert not any(m.get('content_type') == 'input_select' for m in row.messages)


@pytest.mark.parametrize('known', [{}, {'party_size': 2}, {'party_size_range': [2, 4]}])
def test_hypothetical_single_option_keeps_actual_party(session_factory, monkeypatch, known):
    with session_factory() as db:
        row = create(db, monkeypatch)
        row.memory = deepcopy(known)
        decision = Decision(route_variant='nine', messages=[{'quote_options': ['price_shared_4']}]).model_dump()
        value = current()
        assert quote_error(decision, value, known) is None
        _, parts = service.parts_for(db, row, value, decision)
        assert '4位報名：' in parts[0]['content'] and '12,580' in parts[0]['content']
        assert row.memory == known and decision['profile'] == {}


def test_reference_never_uses_a_discount_that_requires_an_unknown_party_count():
    value = current()
    spec = value['routes']['nine']
    spec['scripts'] = [s for s in spec['scripts'] if s['party_size_min'] >= 4]
    assert shared_reference(spec) is None
    decision = Decision(route_variant='nine', messages=[{'script_id': 'price_shared_4'}]).model_dump()
    assert quote_error(decision, value, {})
