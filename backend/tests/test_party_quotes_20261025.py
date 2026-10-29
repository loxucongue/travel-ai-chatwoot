from copy import deepcopy
import json
from pathlib import Path

import pytest
from sqlalchemy import select
from app.automation_models import CustomerQuoteOffer
from app.reception_v3 import service
from app.reception_v3.runtime import Decision
from app.reception_v3.party_quotes import apply_price_scripts, price_scripts, quote_error, validate_party_pricing
from app.route_packages import PACKAGE_ROOT
from app.reception_v3.quotes import personalize_quotes, reserve_quoted_offers, subject_key
from scripts.configure_party_quotes_20261025 import update_package
from test_reception_v3 import create, bundle

ROOT=Path(__file__).resolve().parents[2]


def package(days=11):
    return update_package(json.loads((PACKAGE_ROOT/f'peach-{days}d-2027/route-package.json').read_text(encoding='utf8')))


def spec(days=11):
    result=bundle()
    result['routes']['nine']['party_pricing']=package(days)['party_pricing']
    apply_price_scripts(result['routes']['nine'])
    return result


def amounts(route):
    return {s['id']:s['price_cny'] for s in route['scripts'] if s['id'].startswith('price_')}


@pytest.mark.parametrize('days,expected',[(11,[12780,12580,12380,12980,12380]),(9,[10780,10580,10380,10980,10380])])
def test_exact_per_person_prices_and_layout(days,expected):
    route=spec(days)['routes']['nine']
    prices=amounts(route)
    assert [prices[k] for k in ('price_shared_2','price_shared_4','price_shared_6','price_private_4','price_private_6')]==expected
    assert 'price_shared_5' not in prices and 'price_private_2' not in prices
    for script in route['scripts']:
        text=script['answer_text']
        assert f'西藏桃花節{days}日遊' in text
        assert '包含：' in text and '不含：' in text and '/人' in text
        assert all(s not in text for s in ('早鳥','截至','限時','減200','減400'))
    assert route['quote_policy'] is None


@pytest.mark.parametrize('change',[{'base_price_cny':True},{'base_price_cny':0},{'discounts_per_person':{'4':12780}},
                                 {'discounts_per_person':{'four':200}},{'private_supplements_per_person':{'5':400}},
                                 {'shared_template':'{amount} {other}'},{'legacy_offer_price_cny':-1}])
def test_invalid_configuration_rejected(change):
    policy=package()['party_pricing'];policy.update(change)
    with pytest.raises(ValueError):validate_party_pricing(policy)


def test_invalid_import_uses_existing_package_error_contract():
    from app.route_packages import _validate, RoutePackageError
    value=package();value['party_pricing']['base_price_cny']=-1
    with pytest.raises(RoutePackageError,match='route_package_party_pricing_invalid'):
        _validate(value,Path('invalid.json'))


@pytest.mark.parametrize('old,new,script,valid',[
    ({'party_size':2},{'party_size':4},'price_shared_4',True),
    ({'party_size':2},{'party_size':4},'price_shared_2',False),
    ({'party_size':4,'group_type':'shared'},{'group_type':'private'},'price_private_4',True),
    ({'party_size':4,'group_type':'private'},{},'price_shared_4',False),
    ({'party_size':4},{'party_size_range':[4,6]},'price_shared_4',False),
    ({'party_size_range':[4,6]},{'party_size':6},'price_shared_6',True),
    ({'party_size':5},{},'price_shared_4',False),
    ({},{},'price_shared_2',False),
])
def test_quote_guard_uses_current_count_and_group(old,new,script,valid):
    decision=Decision(route_variant='nine',profile=new,messages=[{'script_id':script}]).model_dump()
    assert (quote_error(decision,spec(),old) is None)==valid


def test_free_text_quote_and_duplicate_quote_blocked_without_blocking_supplements():
    decision=Decision(route_variant='nine',messages=[{'text':'團費人民幣¥ 12,180 /人'}]).model_dump()
    assert quote_error(decision,spec(),{'party_size':4})
    decision['messages'][0]['text']='4位獨立成團需在原團費基礎上每人加人民幣400元。'
    assert quote_error(decision,spec(),{'party_size':4}) is None
    decision['messages']=[{'script_id':'price_shared_4','text':''}]*2
    assert quote_error(decision,spec(),{'party_size':4})


def test_delivery_guard_and_new_quote_do_not_create_expiry(session_factory,monkeypatch):
    with session_factory() as db:
        row=create(db,monkeypatch)
        row.memory={'party_size':2,'group_type':'shared'}
        value=personalize_quotes(db,row,spec())
        decision=Decision(route_variant='nine',profile={'party_size':4},messages=[{'script_id':'price_shared_4'}]).model_dump()
        _,parts=service.parts_for(db,row,value,decision)
        assert len(parts)==1 and '12,580' in parts[0]['content']
        reserve_quoted_offers(db,row,value,parts);db.flush()
        assert not db.scalars(select(CustomerQuoteOffer)).all()
        decision['messages'][0]['script_id']='price_shared_2'
        with pytest.raises(ValueError,match='party_quote_not_validated'):service.parts_for(db,row,value,decision)


@pytest.mark.parametrize('days,day,shared,private',[(11,'2026-11-12',12180,12980),(11,'2026-11-13',12580,12980),(9,'2026-11-12',10580,10980)])
def test_valid_legacy_promise_is_ceiling_not_extra_discount(session_factory,monkeypatch,days,day,shared,private):
    with session_factory() as db:
        row=create(db,monkeypatch);row.virtual_now=day+'T09:00:00+08:00'
        original={'first_quoted_on':'2026-10-13','valid_until':'2026-11-12'}
        offer=CustomerQuoteOffer(subject_key=subject_key(db,row),route_variant='nine',offer=original)
        db.add(offer);db.flush()
        result=personalize_quotes(db,row,spec(days))
        values=amounts(result['routes']['nine'])
        assert values['price_shared_4']==shared
        assert values['price_private_4']==private
        assert offer.offer==original


def test_migration_idempotent_and_other_routes_unchanged():
    for days in (9,11):
        value=package(days)
        assert update_package(value)==value
        validate_party_pricing(value['party_pricing'])
    other={'route_variant':'unrelated','custom':{'preserved':True}}
    assert update_package(other)==other


def test_configuration_changes_drive_calculation_without_code_change():
    policy=deepcopy(package()['party_pricing']);policy['base_price_cny']=13000
    policy['discounts_per_person']['4']=250
    values={s['id']:s['price_cny'] for s in price_scripts(policy)}
    assert values['price_shared_4']==12750 and values['price_private_4']==13150


def test_disabled_price_is_not_reenabled_by_personalization(session_factory,monkeypatch):
    value=spec();value['routes']['nine']['party_pricing_enabled']=False
    with session_factory() as db:
        row=create(db,monkeypatch)
        result=personalize_quotes(db,row,value)
        assert not amounts(result['routes']['nine'])
        decision=Decision(route_variant='nine',messages=[{'script_id':'price_shared_4'}]).model_dump()
        assert quote_error(decision,result,{'party_size':4})
