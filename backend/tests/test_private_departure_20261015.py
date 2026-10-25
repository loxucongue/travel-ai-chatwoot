"""The #642 regression: four guests should not be forced onto a weekday."""
from copy import deepcopy
import pytest
from app.reception_v3 import runtime
from app.reception_v3.seasonal_quotes import departure_quote, quote_error, validate_pricing
from test_seasonal_runtime_journeys import runtime_context, model, final, check_date, results
from test_tibet_release_20261014 import EIGHT, TEN
from test_seasonal_retention import catalog, NOW


@pytest.mark.parametrize('route,day',[
    (EIGHT,'2027-08-08'),(TEN,'2027-08-08'),
    ('peach_9d_2027','2027-03-23'),('peach_11d_2027','2027-03-23')])
@pytest.mark.parametrize('party,group,status',[
    (3,'undecided','ask_date'),(4,'undecided','private_departure'),
    (4,'private','private_departure'),(6,'private','private_departure'),
    (4,'shared','ask_date'),(None,'undecided','ask_date')])
def test_person_count_and_group_choice_select_schedule(route,day,party,group,status):
    result=departure_quote(catalog(),route,day,day,NOW,party_size=party,group_type=group)
    assert result['status']==status
    if status=='private_departure':
        assert result['departure_examples']==[day]
        assert 'price_cny' not in result and 'script_id' not in result
    if party is None:assert result['reason_code']=='party_size_required'


@pytest.mark.parametrize('route,day,now,status',[
    (EIGHT,'2027-04-22',NOW,'unavailable'),(TEN,'2027-03-28',NOW,'unavailable'),
    (EIGHT,'2027-08-08','2027-07-20T00:00:00Z','advisor'),
    (EIGHT,'2026-10-30','2026-10-15T00:00:00Z','advisor'),
    (EIGHT,'2027-05-02',NOW,'holiday'),(EIGHT,'2027-12-12',NOW,'unavailable'),
    (EIGHT,'2028-08-08',NOW,'unavailable'),('peach_11d_2027','2027-06-08',NOW,'unavailable')])
def test_private_date_does_not_override_product_restrictions(route,day,now,status):
    assert departure_quote(catalog(),route,day,day,now,party_size=4,group_type='private')['status']==status


def test_private_date_cannot_authorize_shared_price_even_with_previous_check():
    c=runtime_context(EIGHT)
    availability={EIGHT:departure_quote(c['skills'],EIGHT,'2027-08-08','',NOW,party_size=4)}
    decision=runtime.Decision(route_variant=EIGHT,messages=[{'script_id':'price_peak'}]).model_dump()
    assert quote_error(decision,c['skills'],{(EIGHT,'price_peak')},availability)
    decision.update(action='handoff',messages=[])
    assert quote_error(decision,c['skills'],set(),availability) is None


def test_runtime_uses_known_party_when_legacy_tool_call_omits_it(monkeypatch):
    c=runtime_context(EIGHT);c['profile']={'party_size':4}
    requests=model(monkeypatch,[check_date(EIGHT,'2027-08-08','2027-08-08'),
        final(route_variant=EIGHT,messages=[{'text':'您們4位可以自己一團選8/8出發。'}])])
    decision,_,_=runtime.run_agent(c)
    assert results(requests[1])[-1]['status']=='private_departure'
    assert decision['profile']['_departure_check']['party_size']==4


def test_latest_party_and_shared_choice_override_stale_memory(monkeypatch):
    c=runtime_context(EIGHT);c['profile']={'party_size':4,'group_type':'private'}
    call=check_date(EIGHT,'2027-08-08','2027-08-08')
    import json
    args=json.loads(call['tool_calls'][0]['function']['arguments']);args.update(party_size=3,group_type='shared')
    call['tool_calls'][0]['function']['arguments']=json.dumps(args)
    requests=model(monkeypatch,[call,final(route_variant=EIGHT,messages=[{'text':'3位拼團需按週四定班。'}])])
    runtime.run_agent(c)
    assert results(requests[1])[-1]['status']=='ask_date'


def test_invalid_private_policy_rejected():
    policy=deepcopy(catalog()['routes'][EIGHT]['seasonal_pricing'])
    policy['private_departure_min_party_size']=True
    with pytest.raises(ValueError,match='private_departure_min_party_size_invalid'):validate_pricing(policy)


def test_three_person_peach_receives_verified_nearby_scheduled_dates():
    result=departure_quote(catalog(),'peach_11d_2027','2027-03-23','',NOW,party_size=3)
    assert result['suggested_departures']==['2027-03-26','2027-03-27','2027-03-28']
