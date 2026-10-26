from copy import deepcopy
import pytest
from app.reception_v3.party_quotes import discount_per_person, price_scripts, quote_error, validate_party_pricing, apply_price_scripts
from app.reception_v3.quotes import personalize_quotes, subject_key
from app.reception_v3.runtime import Decision
from app.reception_v3 import service
from app.automation_models import CustomerQuoteOffer
from scripts.configure_party_quote_tiers_20261026 import update_package
from test_party_quotes_20261025 import package, spec, amounts
from test_reception_v3 import create


def current(days=11):
    value=spec(days)
    value['routes']['nine']['party_pricing']=update_package(package(days))['party_pricing']
    apply_price_scripts(value['routes']['nine'])
    return value


@pytest.mark.parametrize('count,discount',[(1,0),(2,0),(3,0),(4,200),(5,200),(6,400),(7,400),
                                         (8,600),(9,600),(10,800),(11,800),(12,1000),(13,1000),(20,1800),(21,1800)])
def test_reached_threshold_and_progression(count,discount):
    policy=current()['routes']['nine']['party_pricing']
    assert discount_per_person(policy,count)==discount


@pytest.mark.parametrize('count',[0,-1,True,5.0,'5'])
def test_count_must_be_positive_integer(count):
    with pytest.raises(ValueError,match='party_size_invalid'):
        discount_per_person(current()['routes']['nine']['party_pricing'],count)


@pytest.mark.parametrize('days,base',[(9,10780),(11,12780)])
def test_templates_cover_odd_shared_count_but_do_not_invent_private_fee_or_large_group(days,base):
    route=current(days)['routes']['nine'];prices=amounts(route)
    assert prices['price_shared_3']==base
    assert prices['price_shared_5']==base-200
    assert prices['price_shared_6']==base-400
    assert prices['price_private_4']==base+400-200
    assert prices['price_private_6']==base-400
    assert 'price_private_5' not in prices
    assert all(not key.endswith(('_7','_8','_9','_10')) for key in prices)


@pytest.mark.parametrize('change',[
    {'discount_progression':{}},
    {'discount_progression':{'starts_at':4,'party_size_step':0,'amount_step_cny':200}},
    {'discount_progression':{'starts_at':True,'party_size_step':2,'amount_step_cny':200}},
    {'discount_progression':{'starts_at':4,'party_size_step':2,'amount_step_cny':-200}},
    {'auto_quote_party_size_max':True},{'auto_quote_party_size_max':101},
    {'discounts_per_person':{'5':400}},{'base_price_cny':400},
    {'private_supplements_per_person':{'7':0}},
])
def test_invalid_progression_rejected(change):
    policy=current()['routes']['nine']['party_pricing'];policy.update(change)
    with pytest.raises(ValueError):validate_party_pricing(policy)


def test_progression_is_configuration_not_fixed_arithmetic():
    policy=current()['routes']['nine']['party_pricing']
    policy['discount_progression']={'starts_at':3,'party_size_step':3,'amount_step_cny':150}
    assert [discount_per_person(policy,n) for n in (2,3,5,6,8,9)]==[0,150,150,300,300,450]


def test_patch_only_changes_discount_policy_fact_and_version_and_is_idempotent():
    source=package();source['party_pricing']['base_price_cny']=13000
    before=deepcopy(source);updated=update_package(source)
    assert source==before and update_package(updated)==updated
    check=deepcopy(updated)
    for key in ('party_pricing','knowledge_facts','package_version'):check[key]=source[key]
    assert check==source
    for field in ('base_price_cny','private_supplements_per_person','shared_template','private_template','legacy_offer_price_cny'):
        assert updated['party_pricing'][field]==source['party_pricing'][field]
    assert update_package({'route_variant':'other'})=={'route_variant':'other'}


@pytest.mark.parametrize('days,base',[(9,10780),(11,12780)])
def test_consecutive_count_changes_render_correct_prices_without_rewriting_party_size(session_factory,monkeypatch,days,base):
    with session_factory() as db:
        row=create(db,monkeypatch);row.memory={'party_size':2,'group_type':'shared'}
        value=current(days)
        for count,discount in [(3,0),(5,200),(4,200),(6,400),(5,200),(3,0)]:
            decision=Decision(route_variant='nine',profile={'party_size':count},messages=[{'script_id':f'price_shared_{count}'}]).model_dump()
            _,parts=service.parts_for(db,row,value,decision)
            assert f'{base-discount:,}' in parts[0]['content']
            row.memory={**row.memory,**decision['profile']}
            assert row.memory['party_size']==count
        for count,group in [(5,'private'),(7,'shared')]:
            decision=Decision(route_variant='nine',profile={'party_size':count,'group_type':group},messages=[{'script_id':'price_shared_5'}]).model_dump()
            assert quote_error(decision,value,row.memory)


def test_five_person_price_keeps_old_valid_lower_promise(session_factory,monkeypatch):
    with session_factory() as db:
        row=create(db,monkeypatch);row.virtual_now='2026-10-26T09:00:00+08:00'
        db.add(CustomerQuoteOffer(subject_key=subject_key(db,row),route_variant='nine',offer={'first_quoted_on':'2026-10-13','valid_until':'2026-11-12'}));db.flush()
        assert amounts(personalize_quotes(db,row,current())['routes']['nine'])['price_shared_5']==12180
        row.virtual_now='2026-11-13T09:00:00+08:00'
        assert amounts(personalize_quotes(db,row,current())['routes']['nine'])['price_shared_5']==12580
