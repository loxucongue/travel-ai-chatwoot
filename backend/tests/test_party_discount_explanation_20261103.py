import pytest

from app.automation_models import CustomerQuoteOffer
from app.reception_v3 import service
from app.reception_v3.party_quotes import apply_price_scripts, pricing_evidence, quote_error
from app.reception_v3.quotes import personalize_quotes, subject_key
from app.reception_v3.runtime import Decision
from test_party_quote_tiers_20261026 import current
from test_reception_v3 import create


def test_published_guidance_update_preserves_prices_operator_settings_and_version_order():
    from copy import deepcopy
    from pathlib import Path
    import json
    from scripts.configure_discount_explanation_20261103 import update_package
    source = json.loads((Path(__file__).parents[2] /
        'data/knowledge/china2go/route-packages/peach-11d-2027/route-package.json').read_text(encoding='utf8'))
    source['package_version'] = '2026-11-02.operator-publication'
    source['operator_custom'] = {'retained': True}
    source['party_pricing']['base_price_cny'] = 13000
    fact = next(f for f in source['knowledge_facts'] if f['id'].endswith('.pricing_policy'))
    fact['text'] = '4人每人減200元。不對外列折扣拆解或截止日。'
    next(s for s in source['fixed_answers'] if s['id'] == 'price')['usage_note'] = '不展示早鳥、限時或折扣拆解。'
    before = deepcopy(source)
    updated = update_package(source)
    assert source == before and update_package(updated) == updated
    assert updated['package_version'].startswith(before['package_version'])
    assert '不對外列折扣拆解' not in str(updated['knowledge_facts'])
    check = deepcopy(updated)
    for key in ('knowledge_facts', 'fixed_answers', 'package_version'):
        check[key] = source[key]
    assert check == source
    assert update_package({'route_variant': 'other'}) == {'route_variant': 'other'}


@pytest.mark.parametrize('days,base', [(9,10780),(11,12780)])
def test_explanation_evidence_uses_configured_per_person_tiers(days,base):
    evidence = pricing_evidence(current(days))['nine']
    assert evidence['base_price_cny'] == base
    shared = {p['party_size']: p for p in evidence['per_person_prices'] if p['group_type'] == 'shared'}
    for count, discount in ((2,0),(3,0),(4,200),(5,200),(6,400)):
        p = shared[count]
        assert p['discount_per_person_cny'] == discount
        assert p['price_cny'] == p['standard_price_cny'] == base-discount
        assert p['private_supplement_per_person_cny'] == 0
        assert p['honors_prior_lower_quote'] is False
    assert 7 not in shared


def test_policy_edit_updates_both_explanation_and_quote():
    value = current()
    route = value['routes']['nine']
    route['party_pricing']['discount_progression']['amount_step_cny'] = 250
    apply_price_scripts(route)
    evidence = pricing_evidence(value)['nine']['per_person_prices']
    for count, discount in ((4,250),(5,250),(6,500)):
        p = next(p for p in evidence if p['party_size'] == count and p['group_type'] == 'shared')
        assert p['discount_per_person_cny'] == discount
        assert p['price_cny'] == 12780-discount


def test_private_surcharge_is_separate_from_group_discount():
    prices = pricing_evidence(current())['nine']['per_person_prices']
    p = next(p for p in prices if p['party_size'] == 4 and p['group_type'] == 'private')
    assert p['discount_per_person_cny'] == 200
    assert p['private_supplement_per_person_cny'] == 400
    assert p['price_cny'] == p['standard_price_cny'] == 12980


def test_preserved_lower_quote_does_not_masquerade_as_new_group_discount(session_factory,monkeypatch):
    with session_factory() as db:
        row = create(db,monkeypatch)
        row.virtual_now = '2026-11-03T09:00:00+08:00'
        db.add(CustomerQuoteOffer(subject_key=subject_key(db,row),route_variant='nine',
            offer={'first_quoted_on':'2026-10-13','valid_until':'2026-11-12'}))
        db.flush()
        evidence = pricing_evidence(personalize_quotes(db,row,current()))['nine']['per_person_prices']
        for count,discount in ((2,0),(4,200),(6,400)):
            p = next(p for p in evidence if p['party_size']==count and p['group_type']=='shared')
            assert p['price_cny'] == 12180
            assert p['standard_price_cny'] == 12780-discount
            assert p['honors_prior_lower_quote'] is True
            assert p['discount_per_person_cny'] == discount


@pytest.mark.parametrize('explanation', [
    '4位一起報名每人減200元，6位則每人減400元，上面的每人價都已經算進去了。',
    '5位和4位屬於同一檔，每人都減人民幣200元，已包含在上面的價格裡。',
    '這是每人減200元，不是整團只減200元，報價裡已經包含了。',
])
def test_model_explanation_delivered_alongside_comparison_without_being_removed(session_factory,monkeypatch,explanation):
    with session_factory() as db:
        row = create(db,monkeypatch)
        row.memory = {'party_size_range':[4,6],'group_type':'shared'}
        decision = Decision(route_variant='nine',messages=[
            {'quote_options':['price_shared_4','price_shared_6']},{'text':explanation}]).model_dump()
        value = current()
        assert quote_error(decision,value,row.memory) is None
        _,parts = service.parts_for(db,row,value,decision)
        assert parts[-1]['content'] == explanation
        assert '12,580' in parts[0]['content'] and '12,380' in parts[0]['content']
        assert decision['profile'] == {}


def test_rules_question_does_not_require_confirmed_headcount_or_full_quote():
    decision = Decision(route_variant='nine',messages=[{'text':
        '4到5位一起報名，每人減200元；6位每人減400元，給您的價格會直接包含適用優惠。'}]).model_dump()
    assert quote_error(decision,current(),{}) is None


def test_rule_explanation_headcounts_are_not_mistaken_for_tour_prices():
    from app.reception_v3.seasonal_quotes import trip_quote_amounts
    text = '4位和5位同一優惠檔，每人減人民幣200元；6位每人減400元。所以每人團費是一樣的，6位才會再低一些。'
    assert list(trip_quote_amounts(text)) == []
    assert list(trip_quote_amounts('團費12,780，優惠價12,580元。')) == [12780, 12580]
    decision = Decision(route_variant='nine', messages=[{'text': text}]).model_dump()
    assert quote_error(decision, current(), {}) is None


@pytest.mark.parametrize('script', ['', 'price_shared_4'])
def test_explaining_existing_price_can_repeat_verified_amount_without_resending_template(session_factory, monkeypatch, script):
    explanation = '12,580元已經是原價12,780元扣掉每人200元的價格，不能再減一次。'
    with session_factory() as db:
        row = create(db, monkeypatch)
        row.memory = {'party_size': 4, 'group_type': 'shared'}
        decision = Decision(route_variant='nine', messages=[
            {'script_id': script, 'text': explanation}]).model_dump()
        assert quote_error(decision, current(), row.memory) is None
        _, parts = service.parts_for(db, row, current(), decision)
        assert [p['content'] for p in parts] == [explanation]


def test_comparison_allows_explanation_referencing_its_verified_prices():
    decision = Decision(route_variant='nine', messages=[
        {'quote_options': ['price_shared_2', 'price_shared_4']},
        {'script_id': 'price_shared_4', 'text':
         '2位每人12,780元，4位每人12,580元，4位每人少200元，已含在上面的價錢裡。'},
    ]).model_dump()
    assert quote_error(decision, current(), {'party_size_range': [2, 4]}) is None
    decision['messages'][1]['text'] = '4位每人12,381元。'
    assert quote_error(decision, current(), {'party_size_range': [2, 4]})


def test_same_price_comparison_may_explain_a_supported_next_discount_tier():
    decision = Decision(route_variant='nine', messages=[
        {'quote_options': ['price_shared_2', 'price_shared_3']},
        {'text': '2位和3位都是每人12,780元；4位每人減200元，是每人12,580元。'},
    ]).model_dump()
    assert quote_error(decision, current(), {'party_size_range': [2, 3]}) is None
    decision['messages'][1]['text'] = '4位獨團每人12,980元。'
    assert quote_error(decision, current(), {'party_size_range': [2, 3]})


@pytest.mark.parametrize('script,text', [
    ('price_shared_6', '每人12,380元已含優惠。'),
    ('price_shared_4', '12,580元再減200後每人12,380元。'),
    ('price_private_4', '獨團每人12,980元。'),
    ('', '12,380元已含優惠。'),
])
def test_explanation_cannot_bypass_source_count_group_or_price_validation(script, text):
    decision = Decision(route_variant='nine', messages=[{'script_id': script, 'text': text}]).model_dump()
    assert quote_error(decision, current(), {'party_size': 4, 'group_type': 'shared'})


def test_explanation_does_not_bypass_unavailable_date():
    from app.reception_v3.seasonal_quotes import quote_error as date_quote_error
    decision = Decision(route_variant='nine', interrupt=True, messages=[
        {'script_id': 'price_shared_4', 'text': '每人12,580元已含每人200元優惠。'}]).model_dump()
    assert date_quote_error(decision, current(), set(), {'nine': {'status': 'unavailable'}})
