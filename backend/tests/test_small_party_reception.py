from app.reception_v3.seasonal_quotes import departure_quote
from test_seasonal_retention import catalog,NOW
from test_tibet_release_20261014 import TEN
import pytest


@pytest.mark.parametrize('count,group,status',[(4,'undecided','shared_departure_request'),(4,'shared','shared_departure_request'),
    (5,'shared','shared_departure_request'),(5,'private','private_departure'),(6,'private','private_departure'),(3,'shared','ask_date')])
def test_custom_shared_departure_is_not_private(count,group,status):
    r=departure_quote(catalog(),TEN,'2027-08-08','2027-08-08',NOW,party_size=count,group_type=group)
    assert r['status']==status
    assert 'price_cny' not in r


def test_four_people_cannot_override_operating_dates():
    assert departure_quote(catalog(),TEN,'2027-03-28','2027-03-28',NOW,party_size=4,group_type='shared')['status']=='unavailable'
