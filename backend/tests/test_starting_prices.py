from app.reception_v3.starting_prices import reference
from test_seasonal_retention import catalog,NOW
from test_tibet_release_20261014 import EIGHT,TEN


def test_reference_is_future_valid_per_person_price():
    b=catalog();r=reference(b,EIGHT,NOW,{})
    assert r['status']=='reference' and r['price_cny']==7980 and r['currency']=='CNY'
    assert all(x['example_departure'].startswith('2027-') for x in r['examples'])


def test_no_shared_price_for_unknown_private_quote():
    assert reference(catalog(),EIGHT,NOW,{'party_size':5,'group_type':'private'})['status']=='advisor'


def test_expired_year_not_used():
    assert reference(catalog(),TEN,'2028-01-01T00:00:00Z',{})['status']=='unavailable'


def test_fixed_quote_keeps_existing_source():
    assert reference({'routes':{'r':{}}},'r',NOW,{}) is None
