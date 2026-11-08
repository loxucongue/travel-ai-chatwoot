from copy import deepcopy
import pytest
from scripts.configure_seasonal_party_20261108 import update_package
from app.reception_v3.seasonal_quotes import departure_quote, validate_pricing
from test_seasonal_retention import catalog, NOW
from test_tibet_release_20261014 import TEN, EIGHT


def bundle():
    b = catalog()
    b['routes'][TEN] = update_package(b['routes'][TEN])
    return b


def quote(n, group='undecided', start='2027-10-19', end=''):
    return departure_quote(bundle(), TEN, start, end, NOW, party_size=n, group_type=group)


@pytest.mark.parametrize('n,group,amount,total', [(4,'shared',10280,41120),
    (4,'private',10680,42720),(6,'undecided',10080,60480),(6,'shared',10080,60480),
    (6,'private',10080,60480),(3,'shared',10480,31440),(5,'shared',10280,51400)])
def test_verified_prices(n,group,amount,total):
    r=quote(n,group)
    assert r['status']=='quoted' and r['price_cny']==amount and r['total_price_cny']==total
    assert r['script_id']=='' and r['early_booking_discount_cny']==0
    assert r['base_price_cny'] + r['private_supplement_per_person_cny'] - r['discount_per_person_cny']==amount
    if n==6: assert r['exclusive_group'] and r['can_choose_departure_date']


def test_four_undecided_compares_without_requiring_choice():
    r=quote(4)
    assert 'price_cny' not in r
    assert [(o['group_type'],o['price_cny']) for o in r['options']]==[('shared',10280),('private',10680)]


@pytest.mark.parametrize('n,group',[(5,'private'),(7,'undecided'),(8,'private')])
def test_missing_group_cost_never_quotes_base_as_final(n,group):
    r=quote(n,group)
    assert r['status']=='advisor' and 'price_cny' not in r


@pytest.mark.parametrize('start,group,status,amount',[
    ('2027-10-20','private','quoted',10080),('2027-09-07','private','quoted',12480),
    ('2027-10-01','private','holiday',None),('2027-04-06','private','unavailable',None),
    ('2028-10-19','private','unavailable',None)])
def test_dates_and_seasons_are_not_bypassed(start,group,status,amount):
    r=quote(6,group,start)
    assert r['status']==status
    assert r.get('price_cny')==amount


def test_shared_weekday_and_cross_season_still_apply():
    assert quote(4,'shared','2027-10-20')['status']=='ask_date'
    assert quote(6,'private','2027-06-29','2027-07-02')['status']=='ask_date'


def test_migration_is_idempotent_and_does_not_change_other_routes():
    b=catalog()
    assert update_package(b['routes'][EIGHT])==b['routes'][EIGHT]
    p=update_package(b['routes'][TEN])
    assert update_package(p)==p
    assert p['seasonal_pricing']['bands']==b['routes'][TEN]['seasonal_pricing']['bands']
    validate_pricing(p['seasonal_pricing'])
    p['seasonal_pricing']['group_pricing']['discounts_per_person']['4']=-200
    with pytest.raises(ValueError):validate_pricing(p['seasonal_pricing'])


def test_four_then_six_recomputes_and_does_not_discount_prior_quote():
    assert quote(4,'shared')['price_cny']==10280
    assert quote(6)['price_cny']==10080
    assert quote(None)['price_kind']=='reference'


def test_six_private_does_not_take_special_shared_price():
    b=bundle()
    b['routes'][TEN]['seasonal_pricing']['special_departures']=[{
        'date':'2027-02-10','price_cny':12980,'script_id':'price_special'}]
    r=departure_quote(b,TEN,'2027-02-10','',NOW,party_size=6)
    assert r['status']=='advisor' and 'price_cny' not in r


def test_model_receives_computed_seasonal_evidence_not_only_peach_prices():
    from app.reception_v3.party_quotes import pricing_evidence
    evidence=pricing_evidence({'routes': {TEN: bundle()['routes'][TEN]}})[TEN]
    low=next(b for b in evidence['seasonal_prices'] if ['10-15','12-31'] in b['ranges'])
    four=[p for p in low['per_person_prices'] if p['party_size']==4]
    six=[p for p in low['per_person_prices'] if p['party_size']==6]
    assert [p['price_cny'] for p in four]==[10280,10680]
    assert [(p['group_type'],p['price_cny']) for p in six]==[('private',10080)]


def test_pre_generation_prices_split_months_holidays_and_winter():
    from app.reception_v3.seasonal_quotes import prepared_group_quotes
    c={'skills':bundle(),'route_variant':TEN,'now':NOW,'route_understanding':{
        'travel_time':{'start':'2027-09','end':'2027-12','group_type':'undecided'}}}
    result=prepared_group_quotes(c)
    september=next(p for p in result['periods'] if p['start']=='2027-09-01')
    october=next(p for p in result['periods'] if p['start']=='2027-10-15')
    assert [o['price_cny'] for o in september['options'] if o['party_size']==4]==[12680,13080]
    assert [o['price_cny'] for o in october['options'] if o['party_size']==4]==[10280,10680]
    for p in result['periods']:
        if p['start'] in ('2027-09-25','2027-10-01','2027-12-01'):
            assert not p['options'] and p['status']!='quoted'
    c['route_understanding']['travel_time']={'start':'2027-10-19','group_type':'private'}
    assert all(o['group_type']=='private' for o in prepared_group_quotes(c)['periods'][0]['options'])


def test_pre_generation_dates_are_not_guessed_from_customer_words():
    from app.reception_v3.seasonal_quotes import prepared_group_quotes
    c={'skills':bundle(),'route_variant':TEN,'now':NOW,'messages':[{'content':'明年九月四個人多少錢'}]}
    assert prepared_group_quotes(c) is None


def test_runtime_passes_six_instead_of_stale_four_to_tool(monkeypatch):
    import json
    from app.reception_v3 import runtime
    from test_seasonal_runtime_journeys import runtime_context, model, final, check_date, results
    c=runtime_context(TEN)
    c['skills']['routes'][TEN]['seasonal_pricing']=bundle()['routes'][TEN]['seasonal_pricing']
    c['profile']={'party_size':4}
    call=check_date(TEN,'2027-10-19','2027-10-19')
    call['tool_calls'][0]['function']['arguments']=json.dumps({
        'route':TEN,'start':'2027-10-19','end':'2027-10-19','party_size':6,'group_type':'undecided'})
    requests=model(monkeypatch,[call,final(route_variant=TEN,messages=[{'text':'6位獨團每人10,080元。'}])])
    decision,_,_=runtime.run_agent(c)
    result=results(requests[1])[-1]
    assert result['price_cny']==10080 and result['group_type']=='private'
    assert decision['profile']['_departure_check']['party_size']==6
    assert decision['messages'][0]['text']=='6位獨團每人10,080元。'
