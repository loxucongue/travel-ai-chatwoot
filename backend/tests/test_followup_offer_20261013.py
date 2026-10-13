from copy import deepcopy
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from app.automation_models import CustomerQuoteOffer, AutomationRun
from app.reception_config import SilenceSettings
from app.reception_v3 import service
from app.reception_v3.quotes import personalize_quotes, reserve_quoted_offers, get_offer, subject_key
from app.reception_v3.skills import effective_price_scripts
from scripts.update_followup_offer_20261013 import update_config, update_package
from test_v3_intake_followup import create, config
from test_reception_v3 import answer, step, delivered


def spec():
    return update_config(config())


@pytest.mark.parametrize('entry,expected',[
    ('2026-10-13T09:00:00+08:00',['09:10','09:25','09:40','10:00','13:00','16:00','19:00','22:00','07:00']),
    ('2026-10-13T02:00:00+08:00',['02:10','02:25','02:40','03:00','09:00','12:00','15:00','18:00','21:00','00:00']),
    ('2026-10-13T00:00:00+08:00',['00:10','00:25','00:40','01:00','07:00','10:00','13:00','16:00','19:00','22:00']),
])
def test_full_cadence_skips_quiet_slots_without_catchup(entry,expected):
    value={'followup_entry_at':entry}; now=entry; seen=[]
    for _ in range(20):
        service.schedule_followup(value,spec(),now)
        target=value['next_check_at']
        if not target: break
        seen.append(service.date(target).strftime('%H:%M'))
        now=service.later(target,3); value['silence_step']+=1
    assert seen == expected


def test_entry_anchor_not_intro_end_and_late_worker_does_not_burst():
    value={'followup_entry_at':'2026-10-13T02:00:00+08:00'}
    service.schedule_followup(value,spec(),'2026-10-13T02:02:00+08:00',reset=True)
    assert service.date(value['next_check_at']).strftime('%H:%M')=='02:10'
    service.schedule_followup(value,spec(),'2026-10-13T02:42:00+08:00')
    assert service.date(value['next_check_at']).strftime('%H:%M')=='03:00'


def test_customer_reply_uses_three_hours_and_no_first_hour_override():
    value={'followup_entry_at':'2026-10-13T00:00:00+08:00','followup_customer_replied':True}
    service.schedule_followup(value,spec(),'2026-10-13T00:25:00+08:00',reset=True)
    # 03:25 and 06:25 are skipped; not deferred to 07:00.
    assert service.date(value['next_check_at']).strftime('%H:%M')=='09:25'
    assert service.followup_intervals(value,spec()['silence']) == [180]*7


def test_actual_night_first_hour_sends_four_times_and_then_skips(session_factory,monkeypatch):
    events=[]
    with session_factory() as db:
        row=create(db,monkeypatch,entry='先看資料',model=lambda c: (events.append(c['event']) or answer(messages=[{'text':'看看這段沿途風景～'}])))
        monkeypatch.setattr(service,'compile_skills',lambda db:spec())
        row.virtual_now='2026-10-13T02:00:00+08:00'
        row.messages=[{'id':'entry','direction':'incoming','created_at':row.virtual_now,'content':'先看資料'}]
        value={'followup_entry_at':row.virtual_now,'opening_started':True,'followup_settings':spec()['silence']}
        service.schedule_followup(value,spec(),row.virtual_now,reset=True); service.save(row,value);db.commit()
        for clock in ['02:10','02:25','02:40','03:00']:
            assert service.date(service.state(row)['next_check_at']).strftime('%H:%M')==clock
            service.advance_next(row); db.commit(); step(db,row,0); step(db,row,2)
        assert events==['silence_due']*4
        assert len(delivered(row))==4
        assert service.date(service.state(row)['next_check_at']).strftime('%H:%M')=='09:00'


def test_new_incoming_cancels_queued_photo_caption(session_factory,monkeypatch):
    with session_factory() as db:
        row=create(db,monkeypatch)
        value=service.state(row)
        service.append_plan(row,value,[{'content':'圖片說明','content_type':'text','proactive':True}],'reply',2)
        service.save(row,value)
        service.add_message(db,row,'請問费用','reply')
        assert not any(m.get('proactive') and m.get('status')=='draft' for m in row.messages)
        assert service.state(row)['followup_customer_replied']
        assert service.state(row)['pending_event']=='customer_message'


@pytest.mark.parametrize('initial,skip',[(True,False),(False,True)])
def test_delivery_crossing_one_am_skips_later_touches_only(session_factory,monkeypatch,initial,skip):
    with session_factory() as db:
        row=create(db,monkeypatch);row.virtual_now='2026-10-13T01:00:01+08:00'
        value=service.state(row);value.update(followup_settings=spec()['silence'],followup_anchor='2026-10-12T21:00:00+08:00',silence_step=5)
        service.append_plan(row,value,[{'content':'圖','proactive':True,'first_hour_followup':initial}],'reply',2);service.save(row,value)
        assert service.skip_quiet_delivery(row,row.messages[-1]) is skip
        if skip:
            assert row.messages[-1]['status']=='cancelled'
            assert service.date(service.state(row)['next_check_at']).strftime('%H:%M')=='07:00'


def test_offer_shared_by_contact_across_live_conversations(session_factory,monkeypatch):
    from live_fixture import setup
    from app.models import ConversationState
    from app.automation_models import AutomationSession
    setup(session_factory,monkeypatch)
    with session_factory() as db:
        row=create(db,monkeypatch);row.environment='live';row.conversation_state_id=1
        row.virtual_now='2026-10-13T09:00:00+08:00'
        bundle=personalize_quotes(db,row,price_bundle())
        reserve_quoted_offers(db,row,bundle,[{'content':bundle['routes']['nine']['scripts'][0]['answer_text']}]);db.commit()
        db.add(ConversationState(id=2,tenant_id=1,inbox_binding_id=1,contact_id=1,chatwoot_conversation_id=27));db.flush()
        other=AutomationSession(owner_id=1,environment='live',conversation_state_id=2,messages=[],memory={},controls={},virtual_now='2026-10-20T09:00:00+08:00');db.add(other);db.flush()
        assert get_offer(db,other,'nine')['valid_until']=='2026-11-12'


def price_policy():
    return {'id':'price','status':'active','answer_text':'11日 12,180元（截至{offer_deadline}前報名，限時優惠）',
            'valid_for_days':30,'fallback_text':'11日 12,780元'}


def price_bundle():
    result=spec(); p=price_policy()
    result['routes']['nine'].update(quote_policy=p,scripts=effective_price_scripts([p]),facts=[{'id':'nine.current_quote','text':'old'}])
    return result


@pytest.mark.parametrize('day,deadline',[('2026-10-13','2026/11/12'),('2026-12-20','2027/01/19'),('2028-02-01','2028/03/02')])
def test_first_quote_plus_thirty_calendar_days(day,deadline):
    now=datetime.fromisoformat(day).replace(tzinfo=ZoneInfo('Asia/Taipei'))
    assert deadline in effective_price_scripts([price_policy()],now)[0]['answer_text']


def test_quote_starts_only_when_quoted_repeat_and_expiry_fixed(session_factory,monkeypatch):
    with session_factory() as db:
        row=create(db,monkeypatch); row.virtual_now='2026-10-13T09:00:00+08:00'
        bundle=personalize_quotes(db,row,price_bundle())
        reserve_quoted_offers(db,row,bundle,[{'content':'請問跟家人還是朋友一起？'}]); db.commit()
        assert get_offer(db,row,'nine') is None
        parts=[{'content':bundle['routes']['nine']['scripts'][0]['answer_text']}]
        reserve_quoted_offers(db,row,bundle,parts);db.commit()
        assert get_offer(db,row,'nine')['valid_until']=='2026-11-12'
        for day,expected in [('2026-10-20','12,180'),('2026-11-12','12,180'),('2026-11-13','12,780')]:
            row.virtual_now=day+'T09:00:00+08:00'
            quote=personalize_quotes(db,row,price_bundle())['routes']['nine']['scripts'][0]
            assert expected in quote['answer_text']
            assert quote['quote_offer']['valid_until']=='2026-11-12'
            reserve_quoted_offers(db,row,personalize_quotes(db,row,price_bundle()),[{'content':quote['answer_text']}]);db.commit()
        assert len(db.scalars(select(CustomerQuoteOffer)).all())==1


def test_stale_concurrent_quote_uses_existing_customer_deadline(session_factory,monkeypatch):
    with session_factory() as db:
        row=create(db,monkeypatch);row.virtual_now='2026-10-13T09:00:00+08:00'
        stale=personalize_quotes(db,row,price_bundle())
        db.add(CustomerQuoteOffer(subject_key=subject_key(db,row),route_variant='nine',offer={'first_quoted_on':'2026-10-11','valid_until':'2026-11-10'}));db.commit()
        parts=[{'content':stale['routes']['nine']['scripts'][0]['answer_text']}]
        reserve_quoted_offers(db,row,stale,parts);db.commit()
        assert '2026/11/10' in parts[0]['content'] and '2026/11/12' not in parts[0]['content']


def test_api_accepts_rolling_and_rejects_ambiguous_validity():
    from app.automation_api import RouteFixedAnswerInput
    from app.route_packages import load_route_packages
    from scripts.update_business_feedback_20261011 import update_package as previous
    package=update_package(previous(load_route_packages()['peach_11d_2027']))
    price=next(s for s in package['fixed_answers'] if s['id']=='price')
    assert RouteFixedAnswerInput.model_validate(price).valid_for_days==30
    for patch in ({'valid_until':'2026-10-31'}, {'answer_text':'missing placeholder'}, {'fallback_text':None}):
        with pytest.raises(ValueError): RouteFixedAnswerInput.model_validate({**price,**patch})


def test_silence_validation_accepts_confirmed_schedule():
    cfg=SilenceSettings.model_validate(spec()['silence'])
    assert cfg.initial_checkpoints_minutes==[10,25,40,60]
    for points in ([10,10,40,60],[10,25,40,61],[10,60]):
        with pytest.raises(ValueError): SilenceSettings.model_validate({**cfg.model_dump(),'initial_checkpoints_minutes':points})
