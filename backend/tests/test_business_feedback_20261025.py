"""Business data migration and actual tool-loop enforcement, without live sends."""
from copy import deepcopy
from pathlib import Path
import json

import pytest

from app.route_packages import _validate
from app.reception_v3 import runtime
from scripts.configure_business_feedback_20261025 import update_package, ROUTES, GROUP_FACT, REST_FACT
from test_linzhi_route_20261020 import PACKAGE, compile_package, ctx
from test_winter_spring_20261017 import winter_catalog, RID
from test_reception_v3_skills import model, final


def packages():
    return {**winter_catalog()['routes'], PACKAGE['route_variant']: deepcopy(PACKAGE)}


@pytest.mark.parametrize('route', sorted(ROUTES))
def test_group_fact_migration_keeps_prices_private_groups_assets_and_dates(route):
    original = packages()[route]
    before = deepcopy(original)
    after = update_package(original)
    assert original == before and update_package(after) == after
    _validate(deepcopy(after), Path('feedback.json'))
    for key in ('seasonal_pricing', 'departure_availability', 'content_groups', 'content_sequence'):
        assert after.get(key) == original.get(key), key
    for script, old_script in zip(after.get('fixed_answers', []), original.get('fixed_answers', []), strict=True):
        if script['id'] in ('small_party', 'group_party'):
            assert '滿2人發團、6人滿團' in script['answer_text']
        else:
            assert script == old_script
    by_id = {f['id']:f for f in after['knowledge_facts']}
    assert by_id[route+'.departure_group_policy']['text'] == GROUP_FACT
    assert by_id[route+'.long_drive_rest']['text'] == REST_FACT
    if route+'.group' in by_id:
        text = by_id[route+'.group']['text']
        assert '4人成團' not in text and '4人成团' not in text
        if '400' in next(f['text'] for f in original['knowledge_facts'] if f['id']==route+'.group'):
            assert '400' in text


def context_with_winter(start='2027-05-01',end='2027-05-31'):
    c = ctx('peach_9d_2027')
    c['skills']['routes'][RID] = compile_package(packages()[RID])
    c['messages']=[{'direction':'incoming','content':'8天，不去珠峰，先看行程','content_type':'text'}]
    c['profile']={'party_size':1,'everest_preference':'avoid','trip_days':8,
                  'departure_window':start[:7],
                  '_departure_check':{'route':'peach_9d_2027','start':start,'end':end,'result':{'status':'unavailable'}}}
    return c


def winter_offer():
    return final(route_variant='peach_9d_2027',route_choice='recommend',
                 profile={'discussion_route':RID},messages=[{'text':'推薦冬遊冰川8日。'}])


def test_may_winter_recommendation_is_rejected_before_customer_delivery(monkeypatch):
    c=context_with_winter()
    requests=model(monkeypatch,[winter_offer(),winter_offer(),
        final(route_variant='peach_9d_2027',messages=[{'text':'依您的五月、8天、不上珠峰需求，先核對林芝8日。'}])])
    decision,logs,_=runtime.run_agent(c)
    assert len(requests)==3 and decision['route_choice']=='unchanged'
    assert '冬遊' not in decision['messages'][0]['text']
    assert any(l.get('status')=='quote_rejected' and '尚不適用' in l.get('validation_error','') for l in logs)


def test_winter_still_available_when_customer_really_chooses_winter_dates(monkeypatch):
    c=context_with_winter('2026-12-17','2026-12-17')
    model(monkeypatch,[winter_offer(),winter_offer()])
    decision,logs,_=runtime.run_agent(c)
    assert decision['route_choice']=='recommend'
    assert decision['profile']['_offered_departure_check']['result']['status']=='quoted'
    assert not any(l.get('status')=='quote_rejected' for l in logs)


def test_known_date_without_tool_evidence_requires_check_for_winter_too(monkeypatch):
    c=context_with_winter()
    c['profile'].pop('_departure_check')
    requests=model(monkeypatch,[winter_offer(),winter_offer(),
        final(route_variant='peach_9d_2027',messages=[{'text':'我先按您提供的五月核對可用行程。'}])])
    decision,logs,_=runtime.run_agent(c)
    assert decision['route_choice']=='unchanged'
    assert any('須用get_departure_quote' in l.get('validation_error','') for l in logs)
