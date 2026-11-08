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
    assert r['tax_status']=='not_specified' and 'quote_summary' not in r
    assert all(o['tax_status']=='not_specified' and 'quote_summary' not in o for o in r['options'])
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


@pytest.mark.parametrize('start,base', [('2027-03-02',10480),('2027-06-01',12280),
    ('2027-08-03',12880),('2027-09-07',12880),('2027-10-19',10480)])
@pytest.mark.parametrize('n,group,delta', [(1,'shared',0),(2,'shared',0),(3,'shared',0),
    (4,'shared',-200),(5,'shared',-200),(4,'private',200),(6,'private',-400),
    (6,'shared',-400),(6,'undecided',-400)])
def test_business_matrix_all_supported_seasons_and_group_sizes(start,base,n,group,delta):
    r=quote(n,group,start)
    assert r['status']=='quoted'
    assert r['price_cny']==base+delta
    assert r['total_price_cny']==(base+delta)*n
    assert r['early_booking_discount_cny']==0


@pytest.mark.parametrize('n,group',[(5,'private'),(7,'private'),(8,'private'),(10,'private')])
@pytest.mark.parametrize('start',['2027-06-01','2027-08-03','2027-10-19'])
def test_unknown_costs_stay_unknown_in_every_season(n,group,start):
    r=quote(n,group,start)
    assert r['status']=='advisor'
    assert 'price_cny' not in r


@pytest.mark.parametrize('first,second',[(4,6),(6,4),(2,4),(4,2),(5,6),(6,5)])
def test_count_changes_do_not_accumulate_discounts(first,second):
    expected={2:10480,4:10280,5:10280,6:10080}
    assert quote(first,'shared')['price_cny']==expected[first]
    assert quote(second,'shared')['price_cny']==expected[second]


def test_prepared_quotes_explicitly_preserve_unknown_private_costs():
    from app.reception_v3.seasonal_quotes import prepared_group_quotes
    c={'skills':bundle(),'route_variant':TEN,'now':NOW,'route_understanding':{
        'travel_time':{'start':'2027-10-19','group_type':'private'}}}
    period=prepared_group_quotes(c)['periods'][0]
    assert any(q['party_size']==5 and q['status']=='advisor' for q in period['unquoted'])
    assert not any(q['party_size']==5 for q in period['options'])


@pytest.mark.parametrize('counts,expected', [([6],{6}),([2,4],{2,4}),([5],set()),([8],set())])
def test_prepared_quotes_only_include_model_understood_requested_counts(counts,expected):
    from app.reception_v3.seasonal_quotes import prepared_group_quotes
    c={'skills':bundle(),'route_variant':TEN,'now':NOW,'profile':{'party_size':4},
       'route_understanding':{'travel_time':{'start':'2027-10-19','group_type':'private' if counts in ([5],[8]) else 'undecided',
                                          'quote_party_sizes':counts}}}
    period=prepared_group_quotes(c)['periods'][0]
    assert {q['party_size'] for q in period['options']}==expected
    if not expected: assert period['status']=='advisor' and period['unquoted']


def test_presentation_migration_preserves_business_data_and_is_idempotent():
    from scripts.configure_quote_presentation_20261108 import update_package as present
    p=bundle()['routes'][TEN]
    result=present(p)
    assert present(result)==result
    for key in p:
        if key not in ('ai_guidance','knowledge_facts','fixed_answers'): assert result[key]==p[key]
    assert present(bundle()['routes'][EIGHT])==bundle()['routes'][EIGHT]


def test_uncomputed_early_booking_is_not_a_claim_of_ineligibility():
    r=quote(4,'shared')
    assert r['early_booking_status']=='requires_confirmation'
    assert r['early_booking_discount_cny']==0
    assert r['price_cny']==10280


def test_confirmed_six_final_price_does_not_deduct_early_booking_again():
    r=quote(6)
    assert r['price_cny']==10080 and r['total_price_cny']==60480
    assert r['early_booking_status']=='no_additional_discount'
    assert quote(6,start='2027-09-07')['early_booking_status']=='requires_confirmation'


def test_prepared_quote_exposes_confirmed_early_booking_status_to_model():
    from app.reception_v3.seasonal_quotes import prepared_group_quotes
    c={'skills':bundle(),'route_variant':TEN,'now':NOW,'route_understanding':{
        'travel_time':{'start':'2027-10-19','group_type':'undecided','quote_party_sizes':[6]}}}
    assert prepared_group_quotes(c)['periods'][0]['early_booking'][0]['status']=='no_additional_discount'


def test_presentation_migration_aligns_facts_and_adds_atomic_transport():
    from scripts.configure_quote_presentation_20261108 import update_package as present
    original = bundle()['routes'][TEN]
    changed = present(original)
    assert present(changed) == changed
    assert changed['seasonal_pricing'] == original['seasonal_pricing']
    assert present(catalog()['routes'][EIGHT]) == catalog()['routes'][EIGHT]
    fact = next(f for f in changed['knowledge_facts'] if f['id'] == TEN+'.group')
    assert '6位一起報名按獨立成團' in fact['text']
    transport = next(s for s in changed['fixed_answers'] if s['id']=='transport_scope')
    assert '拉日鐵路' in transport['answer_text']
    assert '不含往返西藏' in transport['answer_text']
    assert all(word not in transport['answer_text'] for word in ('早餐','小費','氧氣','保險'))
    # New short fact/script does not change existing approved message templates.
    old_scripts = {s['id']:s for s in original['fixed_answers']}
    assert all(s == old_scripts[s['id']] for s in changed['fixed_answers'] if s['id'] in old_scripts)


@pytest.mark.parametrize('start', ['2027-03-02','2027-06-01','2027-09-07','2027-10-19'])
@pytest.mark.parametrize('n,kind', [(1,'shared'),(2,'shared'),(3,'shared'),(4,'undecided'),(5,'shared'),(6,'shared')])
def test_quote_references_resolve_to_same_precomputed_prices(start,n,kind):
    from app.reception_v3.seasonal_quotes import apply_group_price_scripts
    from app.reception_v3.party_quotes import comparison_text
    spec = bundle()['routes'][TEN]
    spec['scripts'] = []
    apply_group_price_scripts(spec)
    before = deepcopy(spec)
    apply_group_price_scripts(spec)
    assert before == spec
    q = quote(n,kind,start)
    scripts = {s['id']:s for s in spec['scripts']}
    ids=[]
    for option in q['options']:
        script = scripts[option['quote_script_id']]
        assert script['price_cny'] == option['price_cny']
        assert script['quote_group_type'] == option['group_type']
        assert f"{option['price_cny']:,}" in script['answer_text']
        ids.append(script['id'])
    text = comparison_text(spec, ids)
    assert all(f"{o['price_cny']:,}" in text for o in q['options'])
    assert '包含' not in text


def test_group_reference_delivery_uses_selected_templates(session_factory,monkeypatch):
    from app.reception_v3 import runtime,service
    from app.reception_v3.seasonal_quotes import apply_group_price_scripts
    from test_reception_v3 import create
    from test_seasonal_runtime_journeys import runtime_context
    c=runtime_context(TEN)
    c['skills']['routes'][TEN]['seasonal_pricing']=bundle()['routes'][TEN]['seasonal_pricing']
    apply_group_price_scripts(c['skills']['routes'][TEN])
    ids=['price_group_10480_shared_2','price_group_10480_shared_3']
    decision=runtime.Decision(route_variant=TEN,messages=[{'route':TEN,'quote_options':ids,'include_itinerary':False}]).model_dump()
    with session_factory() as db:
        row=create(db,monkeypatch)
        _,parts=service.parts_for(db,row,c['skills'],decision)
        text='\n'.join(p['content'] for p in parts if p['content_type']=='text')
        assert text.count('10,480')==1
        assert '2位／3位' in text and '不含' not in text


def test_quote_references_do_not_discard_model_reply(session_factory,monkeypatch):
    from app.reception_v3 import runtime,service
    from app.reception_v3.seasonal_quotes import apply_group_price_scripts
    from test_reception_v3 import create
    from test_seasonal_runtime_journeys import runtime_context
    c=runtime_context(TEN)
    c['skills']['routes'][TEN]['seasonal_pricing']=bundle()['routes'][TEN]['seasonal_pricing']
    apply_group_price_scripts(c['skills']['routes'][TEN])
    reply='10月四位拼團每人10,280元；連假需另核價。交通含接送機。'
    decision=runtime.Decision(route_variant=TEN,messages=[{'text':reply,'route':TEN,
        'quote_options':['price_group_10480_shared_4'],'include_itinerary':False}]).model_dump()
    with session_factory() as db:
        row=create(db,monkeypatch)
        _,parts=service.parts_for(db,row,c['skills'],decision)
        assert [p['content'] for p in parts if p['content_type']=='text']==[reply]


def test_updated_presentation_package_passes_install_validation():
    from pathlib import Path
    from scripts.configure_quote_presentation_20261108 import update_package
    from app.route_packages import _validate
    p=update_package(bundle()['routes'][TEN])
    _validate(p,Path('test-presentation-package.json'))


def test_holiday_alternatives_keep_same_party_discount_without_claiming_availability():
    from app.reception_v3.seasonal_quotes import prepared_group_quotes
    c={'skills':bundle(),'route_variant':TEN,'now':NOW,'route_understanding':{
        'travel_time':{'start':'2027-10-01','group_type':'private','quote_party_sizes':[6]}}}
    q=prepared_group_quotes(c)
    assert all(not p['options'] for p in q['periods'])
    options=[o for band in q['other_season_reference_prices'] for o in band['options']]
    assert {o['party_size'] for o in options}=={6}
    assert {o['group_type'] for o in options}=={'private'}
    assert {o['price_cny'] for o in options}=={10080,11880,12480}
    assert '具體新日期另核對' in q['reference_scope']
