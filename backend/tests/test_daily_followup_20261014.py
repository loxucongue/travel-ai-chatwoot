from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.reception_config import SilenceSettings
from app.reception_v3 import service
from app.reception_v3.service_voice import service_voice
from scripts.update_daily_followup_20261014 import replan
from test_v3_intake_followup import create, config
from test_reception_v3 import answer, step, delivered


def spec():
    result = config(120)
    result['silence'].update(daily_at='09:00', initial_checkpoints_minutes=None,
                            active_start='07:00',active_end='01:00',max_proactive_messages_per_day=1)
    return result


@pytest.mark.parametrize('patch', [{}, {'daily_at':'09:00:00'}, {'daily_at':'02:00'},
                                 {'daily_at':'25:00'}, {'initial_checkpoints_minutes':[10,25,40,60]},
                                 {'max_proactive_messages_per_day':2}])
def test_daily_settings_validation(patch):
    if not patch:
        assert SilenceSettings.model_validate(spec()['silence']).daily_at == '09:00'
    else:
        with pytest.raises(ValueError):
            SilenceSettings.model_validate({**spec()['silence'],**patch})


@pytest.mark.parametrize('start,expected', [
    ('2026-10-11T19:22:00+08:00','2026-10-12T09:00:00+08:00'),
    ('2026-10-11T23:59:00+08:00','2026-10-12T09:00:00+08:00'),
    ('2026-10-11T08:00:00+08:00','2026-10-12T09:00:00+08:00'),
    ('2026-10-11T09:05:00+08:00','2026-10-12T09:00:00+08:00'),
    ('2026-10-11T09:06:00+08:00','2026-10-12T09:00:00+08:00'),
    ('2026-10-11T11:22:00+00:00','2026-10-12T09:00:00+08:00'),
])
def test_daily_calendar_and_channel_boundaries(start, expected):
    value={'followup_entry_at':start,'followup_last_customer_at':start}
    service.schedule_followup(value,spec(),start,reset=True)
    assert value['next_check_at']==expected


def test_daily_missed_slot_not_burst_and_no_window_extension():
    value={'followup_last_customer_at':'2026-10-11T19:22:00+08:00','followup_anchor':'2026-10-11T19:22:00+08:00'}
    service.schedule_followup(value,spec(),'2026-10-12T10:00:00+08:00')
    assert value['next_check_at']=='2026-10-13T09:00:00+08:00'
    assert service.date(value['followup_until'])==service.date('2026-10-18T19:22:00+08:00')


@pytest.mark.parametrize('text,expected',[
    ('這天的實際名額要請顧問確認一下喔。','這天的實際名額要我們確認一下喔。'),
    ('我可以請顧問接著協助您確認出發安排。','我可以接著協助您確認出發安排。'),
    ('顧問可以依您的需求提供建議。','我們可以依您的需求提供建議。'),
    ('我会请顾问确认。','我会接著确认。'),
    ('團費12,180元；導遊每天30元。','團費12,180元；導遊每天30元。'),
    ('我是旅遊顧問Amber。','我是旅遊顧問Amber。'),
    ('您想直接找顧問嗎？','您想直接找顧問嗎？'),
])
def test_service_voice_narrow_replacements(text,expected):
    assert service_voice(text)==expected
    assert service_voice(expected)==expected


def test_screenshot_326_daily_model_and_delivery(session_factory,monkeypatch):
    seen=[]
    with session_factory() as db:
        row=create(db,monkeypatch,entry='先和朋友討論',model=lambda c:(seen.append(c['event']) or answer(messages=[{'text':'我們可以依您的需求協助確認。'}])))
        monkeypatch.setattr(service,'compile_skills',lambda db:spec())
        row.virtual_now='2026-10-11T19:22:00+08:00'
        row.messages=[{'id':'in','direction':'incoming','content':'Ok！謝謝','created_at':row.virtual_now}]
        value={'opening_started':True,'followup_entry_at':row.virtual_now,'followup_last_customer_at':row.virtual_now,'followup_settings':spec()['silence']}
        service.schedule_followup(value,spec(),row.virtual_now,reset=True); service.save(row,value);db.commit()
        service.advance_next(row);db.commit();step(db,row,0);step(db,row,2)
        assert seen==['silence_due']
        assert len(delivered(row))==1
        assert service.state(row)['next_check_at']=='2026-10-13T09:00:00+08:00'
        row.virtual_now='2026-10-12T12:00:00+08:00'
        service.add_message(db,row,'謝謝，我們兩位','in2');db.commit();step(db,row,0);step(db,row,2)
        assert seen==['silence_due','customer_message']
        assert service.date(service.state(row)['next_check_at']).strftime('%d %H:%M')=='13 09:00'


def migration_row(**overrides):
    base=dict(controls={'simulation':{'status':'running'},'v3':{'next_check_at':'2026-10-14T20:00:00+08:00', 'pending_event':'silence_due'}},
              messages=[{'id':'in','direction':'incoming','created_at':'2026-10-14T19:22:00+08:00'}],
              generation=1,due_at=None)
    base.update(overrides)
    return SimpleNamespace(**base)


def test_old_customer_replan_cancels_old_drafts_without_replay():
    row=migration_row()
    row.messages.append({'id':'draft','proactive':True,'status':'draft','plan_id':'old'})
    row.controls['v3'].update(delivery_plan_id='old',delivery_due_at='2026-10-14T20:00:00+08:00')
    result=replan(row,spec(),'2026-10-14T20:00:00+08:00')
    assert result=='scheduled'
    assert service.state(row)['next_check_at']=='2026-10-15T09:00:00+08:00'
    assert row.messages[0]['created_at']=='2026-10-14T19:22:00+08:00'
    assert row.messages[1]['status']=='cancelled'
    assert service.state(row)['delivery_due_at'] is None
    assert service.state(row)['pending_event'] is None


@pytest.mark.parametrize('state_patch,controls_patch,reason',[
    ({'handoff':True},{},'human_or_optout'),({'opt_out':True},{},'human_or_optout'),
    ({},{'human':True},'human_or_optout'), ({},{'simulation':{'status':'stopped'}},'stopped_session'),
    ({'appointment_pending':True},{},'explicit_appointment_preserved'),
])
def test_migration_preserves_human_optout_and_appointments(state_patch,controls_patch,reason):
    row=migration_row();row.controls.update(controls_patch);row.controls['v3'].update(state_patch)
    before=deepcopy(vars(row))
    assert replan(row,spec(),'2026-10-14T20:00:00+08:00')==reason
    assert vars(row)==before


def test_expired_historical_customer_is_not_forced_to_reopen():
    row=migration_row()
    assert replan(row,spec(),'2026-10-22T20:00:00+08:00')=='outside_channel_window'
    assert service.state(row)['next_check_at'] is None
    assert service.state(row)['pending_event'] is None


def test_reply_pending_not_discarded_by_migration():
    row=migration_row();row.controls['v3']['pending_event']='customer_message'
    assert replan(row,spec(),'2026-10-14T20:00:00+08:00')=='active_reception_preserved'
    assert service.state(row)['pending_event']=='customer_message'


def test_public_text_only_not_internal_handoff_reason(session_factory,monkeypatch):
    with session_factory() as db:
        row=create(db,monkeypatch);value=service.state(row)
        value['handoff_reason']='請顧問確認客人資料'
        service.append_plan(row,value,[{'content':'我可以請顧問接著協助您確認。'}],'reply',1)
        service.save(row,value)
        assert row.messages[-1]['content']=='我可以接著協助您確認。'
        assert service.state(row)['handoff_reason']=='請顧問確認客人資料'
