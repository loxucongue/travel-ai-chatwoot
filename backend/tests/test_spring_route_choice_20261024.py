"""Deterministic safety checks. Real model journeys are reported separately."""
from copy import deepcopy
import json

import pytest

from app.model_transport import EvaluationCallError
from app.automation_models import AutomationRun
from app.reception_v3 import runtime, service
from app.reception_v3.route_choice import choice_error, rejected_asset, explicit_selection
from app.reception_v3.seasonal_quotes import explicit_date_window, reply_calendar_error
from test_linzhi_route_20261020 import ctx, setup_row, LINZHI, TEN
from test_reception_v3_skills import model, final, tool

NINE = 'peach_9d_2027'
ELEVEN = 'peach_11d_2027'


@pytest.mark.parametrize('text,unsafe',[
    ('不能保證不會高反',False),('沒辦法保證一定不會高反，行前評估',False),
    ('無法保證不會高反',False),('不會高反，放心',True),
    ('我們保證安全',True),('不能保證不會高反，但供氧保證安全',True),
])
def test_journey_checker_distinguishes_disclaimer_from_guarantee(text,unsafe):
    from scripts.evaluate_reception_journeys import has_altitude_guarantee
    assert has_altitude_guarantee(text) is unsafe


@pytest.mark.parametrize('text,expected',[
    ('好，確定2027年6月1日出發，林芝8天','2027-06-01'),
    ('如果改成2028/6/1呢？','2028-06-01'),
    ('2027-02-29出發',None),('不要2027年6月1日',None),
    ('2027年6月1日或6月8日都可以',None),('2027年6月1日至8日',None),
    ('2027年6月1日和2027年6月8日比較',None),
])
def test_only_unambiguous_full_dates_are_checked_without_model_inference(text,expected):
    value=explicit_date_window(text)
    assert (value or {}).get('start')==expected


@pytest.mark.parametrize('text,invalid',[
    ('3月26日是週一',True),('2027年3月26日是週五',False),
    ('3/26（星期五）',False),('2028年3月26日是週日',False),
    ('每週五、六、日、一出發',False),
])
def test_weekday_claims_use_calendar_not_model_guess(text,invalid):
    checks={ELEVEN:{'route':ELEVEN,'start':'2027-03-26','end':'2027-03-26'}}
    assert bool(reply_calendar_error(decision(route_variant=ELEVEN,messages=[{'text':text}]),ctx(),checks))==invalid


def test_month_only_is_not_confirmed_as_fitting_partial_peach_season():
    c=ctx();checks={ELEVEN:{'route':ELEVEN,'start':'2027-04-01','end':'2027-04-30'}}
    d=decision(route_variant=ELEVEN,messages=[{'text':'您們時間剛好對得上。'}])
    assert reply_calendar_error(d,c,checks)
    checks[ELEVEN]['start']=checks[ELEVEN]['end']='2027-04-03'
    assert reply_calendar_error(d,c,checks) is None


@pytest.mark.parametrize('weekday,invalid',[('五',True),('六',False)])
def test_weekday_pronoun_after_season_range_uses_customer_date(weekday,invalid):
    checks={ELEVEN:{'route':ELEVEN,'start':'2027-04-03','end':'2027-04-03'}}
    text=f'2027年4月3日在3/20–4/10的出發區間內，這天是週{weekday}。'
    d=decision(route_variant=ELEVEN,messages=[{'text':text}])
    assert bool(reply_calendar_error(d,ctx(),checks)) is invalid


@pytest.mark.parametrize('text,status,expected',[
    ('好，改3/26，想去珠峰，11天可以','offered',True),
    ('11天可以嗎？','offered',False),
    ('我們兩位想去珠峰','offered',False),
    ('11天可以','rejected',False),
])
def test_length_acceptance_uses_durable_offer_not_last_two_paragraphs(text,status,expected):
    from app.reception_v3.route_choice import explicit_selection
    c=ctx()
    c['profile']={'discussion_route':ELEVEN,'_route_offer':{'route':ELEVEN,'status':status}}
    c['messages']=[{'direction':'outgoing','status':'delivered','content':t} for t in
                   ['桃花＋珠峰11日','最早3月20日出發','您方便調整日期嗎？']]
    assert explicit_selection(text,ELEVEN,c) is expected


def test_full_customer_date_is_checked_before_intro_even_if_model_skips_tool(monkeypatch):
    c=ctx(LINZHI);c['messages']=[{'direction':'incoming','content':'確定2027年6月1日，請介紹林芝8天'}]
    c['profile']={'departure_window':'2027年6月','party_size':2}
    model(monkeypatch,[final(route_variant=LINZHI,start_introduction=True)])
    d,_,_=runtime.run_agent(c)
    assert d['start_introduction']
    assert d['profile']['_departure_check']['start']=='2027-06-01'
    assert d['profile']['_departure_check']['end']=='2027-06-01'


def test_unavailable_date_explanation_stops_old_queue_without_model_flag(monkeypatch,session_factory):
    c=ctx();c['messages']=[{'id':'customer-date','status':'received','direction':'incoming',
        'content_type':'text','content':'2027年3月15日出發，兩位'}]
    answer=final(route_variant=TEN,profile={'party_size':2,'discussion_route':ELEVEN},
        messages=[{'text':'3月15日沒有開放團期，日期方便調整嗎？'}])
    model(monkeypatch,[answer])
    d,_,_=runtime.run_agent(c)
    assert d['interrupt'] and not d['start_introduction']
    assert d['route_choice']=='unchanged' and 'discussion_route' not in d['profile']
    assert d['profile']['party_size']==2
    with session_factory() as db:
        row=setup_row(db,monkeypatch,c)
        value=service.state(row)
        service.append_plan(row,value,[{'content':'尚未發送的舊行程','content_type':'text'}],'introduction',2)
        service.save(row,value)
        old_ids={m['id'] for m in row.messages if m.get('direction')=='outgoing'}
        service.apply_decision(db,row,run_for(db,row),c['skills'],d)
        assert all(m['status']=='cancelled' for m in row.messages if m['id'] in old_ids)
        assert service.state(row)['delivery_kind']!='introduction'


def test_unvalidated_discussion_route_is_not_persisted(session_factory,monkeypatch):
    c=ctx()
    with session_factory() as db:
        row=setup_row(db,monkeypatch,c)
        service.apply_decision(db,row,run_for(db,row),c['skills'],decision(route_variant=TEN,
            profile={'discussion_route':ELEVEN},messages=[{'text':'先確認日期能否調整。'}]))
        assert 'discussion_route' not in row.memory and '_route_offer' not in row.memory


def run_for(db,row):
    run=AutomationRun(session_id=row.id,module='reception_v3',generation=row.generation,
        idempotency_key=f'spring:{row.id}:{row.generation}',input_snapshot={'event':'customer_message'})
    db.add(run);db.flush()
    return run


def decision(**kw):
    return runtime.Decision(**kw).model_dump()


@pytest.mark.parametrize('route',[NINE,ELEVEN,LINZHI])
def test_every_alternative_requires_explicit_selection_even_without_linzhi(route):
    c=ctx(); c['skills']['routes'].pop(LINZHI) if route != LINZHI else None
    d=decision(route_variant=route,start_introduction=True)
    assert choice_error(d,c)
    d['route_choice']='selected'
    assert choice_error(d,c) is None


@pytest.mark.parametrize('event',['silence_due','introduction_completed'])
@pytest.mark.parametrize('change',[{'peach_preference':'want'},{'peach_preference':'avoid'}, {'discussion_route':ELEVEN}])
def test_noncustomer_events_cannot_invent_preferences(event,change):
    c=ctx(event=event)
    assert choice_error(decision(route_variant=TEN,profile=change),c)


@pytest.mark.parametrize('event',['silence_due','introduction_completed'])
def test_noncustomer_repeating_existing_preferences_is_not_a_mutation(event):
    c=ctx(event=event);c['profile']={'peach_preference':'want','everest_preference':'avoid','discussion_route':NINE}
    assert choice_error(decision(route_variant=TEN,profile=deepcopy(c['profile'])),c) is None


@pytest.mark.parametrize('text',['嗯嗯，了解','了解，謝謝','👍'])
def test_pending_offer_acknowledgement_cannot_repeat_invitation_or_start_intro(monkeypatch,text):
    c=ctx();c['profile']={'discussion_route':ELEVEN}
    c['messages']=[{'direction':'incoming','content':text}]
    requests=model(monkeypatch,[])
    d,_,_=runtime.run_agent(c)
    assert not requests and d['action']=='wait' and not d['messages'] and not d['start_introduction']


@pytest.mark.parametrize('text,target,expected',[
    ('我們2位，不上珠峰，也有時間看桃花',NINE,False),
    ('好，那改成桃花加珠峰11日，給我看行程',ELEVEN,True),
    ('好，酒店如何？',ELEVEN,False),('嗯嗯，了解',ELEVEN,False),
    ('好，就改這條，請介紹',ELEVEN,True),
    ('想了一下還是不上珠峰，有林芝8天可選嗎？',LINZHI,False),
    ('好，改林芝8日，給我看行程',LINZHI,True),
])
def test_selection_requires_acceptance_of_offered_product_or_named_choice(text,target,expected):
    c=ctx();c['profile']={'discussion_route':ELEVEN}
    assert explicit_selection(text,target,c) is expected


@pytest.mark.parametrize('text,previous,expected',[
    ('那2027年3月26日可以，我們想去珠峰，11天也沒問題','桃花＋珠峰11日從3月20日開始，日期可以調整嗎？',True),
    ('11天可以嗎？','桃花＋珠峰11日從3月20日開始',False),
    ('11天也沒問題，但還沒決定改','桃花＋珠峰11日從3月20日開始',False),
    ('11天也沒問題','精華8天每週四出發',False),
    ('嗯嗯，了解','桃花＋珠峰11日從3月20日開始',False),
])
def test_customer_can_accept_conditional_offer_in_context(text,previous,expected):
    c=ctx();c['messages']=[{'direction':'outgoing','content':previous,'status':'simulated_delivered'},
                          {'direction':'incoming','content':text}]
    assert explicit_selection(text,ELEVEN,c) is expected


@pytest.mark.parametrize('days,route',[(8,NINE),(8,ELEVEN),(10,ELEVEN)])
def test_short_trip_is_not_relabelled_as_a_longer_peach_product(days,route):
    c=ctx(); c['profile']={'trip_days':days}
    assert choice_error(decision(route_variant=TEN,route_choice='recommend',profile={'discussion_route':route}),c)
    assert choice_error(decision(route_variant=route,route_choice='selected',start_introduction=True),c)
    assert choice_error(decision(route_variant=route,route_choice='selected',start_introduction=True,
        profile={'trip_days':11}),c) is None


def test_offer_interrupts_old_delivery_and_rejection_survives_unrelated_reply(session_factory,monkeypatch):
    c=ctx(); c['profile']={'party_size':2,'departure_window':'2027年3月'}
    with session_factory() as db:
        row=setup_row(db,monkeypatch,c)
        v=service.state(row)
        service.append_plan(row,v,[{'content':'旧行程','content_type':'text'},{'asset_key':'everest-old','content_type':'image'}],'introduction',2)
        service.save(row,v)
        old={m['id'] for m in row.messages}
        service.add_message(db,row,'三月，想先看看桃花','month')
        assert service.state(row)['route_review_pending']
        offer=decision(route_variant=TEN,route_choice='recommend',profile={'discussion_route':ELEVEN},
            interrupt=True,messages=[{'text':'可以看看桃花加珠峰11日，要看看嗎？'}])
        service.apply_decision(db,row,run_for(db,row),c['skills'],offer)
        assert all(m['status']=='cancelled' for m in row.messages if m['id'] in old)
        assert row.controls['route_variant']==TEN
        assert row.memory['_route_offer']['status']=='offered'
        assert row.memory['_route_offer']['source_message_id']=='month'
        service.add_message(db,row,'不喜歡桃花，只想看珠峰','no-peach')
        reject=decision(route_variant=TEN,profile={'peach_preference':'avoid','everest_preference':'want','discussion_route':None},
            interrupt=True,messages=[{'text':'了解，先看常規珠峰的日期。'}])
        service.apply_decision(db,row,run_for(db,row),c['skills'],reject)
        assert 'discussion_route' not in row.memory and row.memory['_route_offer']['status']=='rejected'
        service.add_message(db,row,'那酒店有獨立衛浴嗎？','hotel')
        service.apply_decision(db,row,run_for(db,row),c['skills'],decision(route_variant=TEN,messages=[{'text':'按本線酒店資料說明。'}]))
        assert row.memory['peach_preference']=='avoid'
        assert row.memory['party_size']==2 and row.memory['departure_window']=='2027年3月'
        assert choice_error(offer,{**c,'profile':row.memory})


def test_peach_rejection_filters_only_incompatible_assets():
    materials=[{'key':'flower','routes':[NINE,ELEVEN]}, {'key':'shared','routes':[ELEVEN,TEN]}]
    assert rejected_asset('flower',materials,{'peach_preference':'avoid'})
    assert not rejected_asset('shared',materials,{'peach_preference':'avoid'})
    c=ctx(ELEVEN);c['profile']={'peach_preference':'avoid'}
    c['available_materials']=materials
    assert choice_error(decision(route_variant=ELEVEN,messages=[{'asset_keys':['flower']}]),c)
    assert choice_error(decision(route_variant=ELEVEN,route_choice='selected',profile={'peach_preference':'want'},
        start_introduction=True),c) is None


def test_pending_offer_cannot_resume_old_product_photos():
    media=[{'key':'old','routes':[TEN],'content_type':'image','media_hash':'hash'}]
    assert service.followup_materials(media,[],TEN,{'discussion_route':ELEVEN})==[]
    assert service.followup_materials(media,[],TEN,{})==media


def test_acknowledgement_with_attachment_must_still_be_understood():
    from app.reception_v3.route_choice import pending_offer_acknowledgement
    c=ctx();c['profile']={'discussion_route':ELEVEN}
    c['messages']=[{'direction':'incoming','content':'嗯嗯','content_type':'image',
                    'attachments':[{'id':42,'file_type':'image'}]}]
    assert not pending_offer_acknowledgement(c)


@pytest.mark.parametrize('month',['三月','四月'])
def test_month_review_checks_both_peach_products_without_selecting_one(monkeypatch,month):
    c=ctx();c['messages']=[{'direction':'incoming','content':month}]
    result=final(route_variant=TEN,route_choice='recommend',interrupt=True,
        profile={'discussion_route':ELEVEN},messages=[{'text':'先看看2027年桃花加珠峰11日嗎？'}])
    requests=model(monkeypatch,[result,result])
    d,logs,_=runtime.run_agent(c)
    task=json.loads(requests[0]['messages'][-1]['content'])
    checks=task['spring_departure_options']
    assert set(checks)=={NINE,ELEVEN}
    assert all(checks[r]['availability_checked'] for r in checks)
    assert d['route_variant']==TEN and d['route_choice']=='recommend' and not d['start_introduction']
    assert d['profile']['departure_window']==month
    assert logs[0]['status']=='skill_activation'


def test_first_seasonal_offer_retries_when_display_year_is_missing(monkeypatch):
    c=ctx();c['messages']=[{'direction':'incoming','content':'三月'}]
    omitted=final(route_variant=TEN,route_choice='recommend',interrupt=True,
        profile={'discussion_route':ELEVEN},messages=[{'text':'3/20到4/10的桃花11日，要看看嗎？'}])
    dated=final(route_variant=TEN,route_choice='recommend',interrupt=True,
        profile={'discussion_route':ELEVEN},messages=[{'text':'2027年3/20到4/10的桃花11日，要看看嗎？'}])
    model(monkeypatch,[omitted,omitted,dated])
    d,logs,_=runtime.run_agent(c)
    assert '2027' in d['messages'][0]['text']
    assert any('2027年' in l.get('validation_error','') for l in logs)


@pytest.mark.parametrize('date',['2027-03-15','2027-04-20','2028-04-01'])
def test_no_peach_offer_for_fixed_dates_outside_published_period(monkeypatch,date):
    c=ctx();c['profile']={'departure_window':date}
    c['messages']=[{'direction':'incoming','content':'只可以'+date+'出發'}]
    checked={'tool_calls':[tool('get_departure_quote',{'route':ELEVEN,'start':date,'end':date})]}
    offer=final(route_variant=TEN,route_choice='recommend',interrupt=True,
        profile={'discussion_route':ELEVEN},messages=[{'text':'推薦桃花11日'}])
    clarification=final(route_variant=TEN,interrupt=True,messages=[{'text':'這個日期沒有已公布的桃花團期，可以調整日期嗎？'}])
    model(monkeypatch,[checked,offer,offer,clarification])
    d,logs,_=runtime.run_agent(c)
    assert d['route_choice']=='unchanged' and not d['start_introduction']
    assert any(l.get('status')=='quote_rejected' for l in logs)


def test_failed_correction_does_not_return_sendable_peach_plan(monkeypatch):
    c=ctx();c['profile']={'peach_preference':'avoid'}
    bad=final(route_variant=TEN,route_choice='recommend',profile={'discussion_route':ELEVEN})
    model(monkeypatch,[bad,bad])
    with pytest.raises(EvaluationCallError,match='route_choice_not_validated'):
        runtime.run_agent(c)


def test_wanting_everest_does_not_reject_peach(monkeypatch):
    c=ctx();c['profile']={'everest_preference':'want'}
    assert choice_error(decision(route_variant=TEN,route_choice='recommend',profile={'discussion_route':ELEVEN}),c) is None


def test_accepting_offer_uses_new_offer_date_instead_of_old_route_date(monkeypatch):
    c=ctx();c['profile']={'departure_window':'2027年6月','party_size':2,
        '_departure_check':{'route':TEN,'start':'2027-06-01','end':'2027-06-30'}}
    c['messages']=[{'id':'new-date','direction':'incoming','content':'改到2027年4月3日，想看桃花加珠峰'}]
    offer=final(route_variant=TEN,route_choice='recommend',interrupt=True,
        profile={'discussion_route':ELEVEN,'departure_window':'2027年4月3日'},messages=[{'text':'要看看2027年桃花加珠峰11日嗎？'}])
    model(monkeypatch,[{'tool_calls':[tool('get_departure_quote',{'route':ELEVEN,'start':'2027-04-03','end':'2027-04-03'})]},offer,offer])
    d,_,_=runtime.run_agent(c)
    assert d['profile']['_offered_departure_check']['start']=='2027-04-03'
    c['profile'].update(d['profile'])
    c['messages'].append({'id':'accept','direction':'incoming','content':'好，就改這條，請介紹'})
    accept=final(route_variant=ELEVEN,route_choice='selected',interrupt=True,start_introduction=True)
    model(monkeypatch,[accept,accept])
    d,_,_=runtime.run_agent(c)
    assert d['profile']['_departure_check']['start']=='2027-04-03'
    assert d['profile']['_departure_check']['route']==ELEVEN


def test_model_cannot_forge_offer_audit(session_factory,monkeypatch):
    c=ctx()
    with session_factory() as db:
        row=setup_row(db,monkeypatch,c)
        service.apply_decision(db,row,run_for(db,row),c['skills'],decision(route_variant=TEN,
            profile={'_route_offer':{'route':ELEVEN,'status':'accepted'}}))
        assert '_route_offer' not in row.memory
