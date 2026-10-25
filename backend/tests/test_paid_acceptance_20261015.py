"""Regressions observed in paid replay of the original #631 and #632 histories."""
import pytest

from app.reception_v3 import runtime
from app.reception_v3.seasonal_quotes import contains_trip_quote, departure_quote, quote_error
from test_seasonal_runtime_journeys import runtime_context
from test_reception_v3_skills import model, final
from test_tibet_release_20261014 import EIGHT


def winter_context():
    c=runtime_context(EIGHT)
    result=departure_quote(c['skills'],EIGHT,'2026-12-01','2027-02-28',c['now'])
    c.update(event='silence_due',state={'delivery_kind':None},profile={
        '_departure_check':{'route':EIGHT,'result':result,'start':'2026-12-01','end':'2027-02-28'}})
    return c


def test_completed_winter_introduction_needs_no_model_retry_for_interrupt(monkeypatch):
    c=winter_context()
    text='如果想了解季節限定冰川行程的安排，隨時跟我說就好。'
    requests=model(monkeypatch,[final(route_variant=EIGHT,interrupt=False,messages=[{'text':text}])])
    decision,logs,_=runtime.run_agent(c)
    assert len(requests)==1 and logs[0]['status']=='completed'
    # The system now fills the unavailable-product cancellation flag itself.
    # An empty queue needs no second model call and the reply stays unchanged.
    assert decision['messages'][0]['text']==text and not decision['start_introduction']


@pytest.mark.parametrize('fields',[
    {'start_introduction':True}, {'action':'queue'},
    {'messages':[{'asset_keys':['regular_room'],'text':'常規絨布旅館'}]},
    {'messages':[{'text':'團費人民幣7,980元'}]},
])
def test_silence_exemption_keeps_product_and_price_guards(fields):
    c=winter_context()
    d=runtime.Decision(route_variant=EIGHT,**fields).model_dump()
    assert quote_error(d,c['skills'],set(),{EIGHT:c['profile']['_departure_check']['result']},require_interrupt=False)


@pytest.mark.parametrize('kind',['introduction','intake'])
def test_inflight_winter_introduction_still_requires_cancellation(monkeypatch,kind):
    c=winter_context();c['state']['delivery_kind']=kind
    requests=model(monkeypatch,[final(route_variant=EIGHT,messages=[{'text':'冬季另有冰川行程'}])])
    decision,logs,_=runtime.run_agent(c)
    assert len(requests)==1 and decision['interrupt']
    assert logs[0]['status']=='completed'


def test_original_discount_question_is_not_a_total_quote():
    text='4位獨立成團，是在原團費基礎上每人加人民幣400元；目前這個限時優惠價能不能再一起套用、加完400元後每人實際多少，我們需要再確認一下，才能給您準確金額。'
    assert not contains_trip_quote(text)
    assert contains_trip_quote(text+'團費合計人民幣8,380元。')
