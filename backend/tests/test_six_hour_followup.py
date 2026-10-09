from copy import deepcopy
import pytest

from app.reception_config import SilenceSettings
from app.reception_v3 import service
from scripts.update_six_hour_followup_20261009 import reschedule, updated
from test_v3_intake_followup import create, config
from test_reception_v3 import answer, step


def test_only_after_six_hours_and_only_once(session_factory, monkeypatch):
    seen=[]
    def model(c):
        seen.append(c['event'])
        return answer(messages=[{'text':'有想了解的地方，隨時跟我說。'}])
    with session_factory() as db:
        row=create(db,monkeypatch,entry='先看看',model=model)
        spec=config();spec['silence']['intervals_minutes']=[360]
        monkeypatch.setattr(service,'compile_skills',lambda db:spec)
        for _ in range(4):step(db,row,2)
        deadline=service.state(row)['next_check_at']
        remaining=(service.date(deadline)-service.date(row.virtual_now)).total_seconds()
        step(db,row,remaining-1)
        assert seen.count('silence_due')==0
        step(db,row,3)  # A small worker delay still consumes the six-hour checkpoint.
        assert seen.count('silence_due')==1
        step(db,row,2)
        assert service.state(row)['next_check_at'] is None
        step(db,row,3600)
        assert seen.count('silence_due')==1


def test_default_and_configuration_preserve_other_fields():
    assert SilenceSettings().intervals_minutes==[360]
    before={'silence':{'intervals_minutes':[1,4,10],'enabled':False},'intake':{'wait_seconds':60},'reply':{'text':'unchanged'}}
    after=updated(before)
    assert after=={**before,'silence':{**before['silence'],'intervals_minutes':[360]}}
    assert before['silence']['intervals_minutes']==[1,4,10]


def test_customer_reply_restarts_six_hours_from_new_answer(session_factory, monkeypatch):
    with session_factory() as db:
        row=create(db,monkeypatch,entry='先看看',model=lambda c:answer(messages=[{'text':'回答'}]))
        spec=config();spec['silence']['intervals_minutes']=[360]
        monkeypatch.setattr(service,'compile_skills',lambda db:spec)
        for _ in range(4):step(db,row,2)
        old=service.state(row)['next_check_at']
        step(db,row,3600)
        service.add_message(db,row,'還想問住宿','new-question');db.commit()
        assert service.state(row)['next_check_at'] is None
        step(db,row,0);step(db,row,0)
        assert service.state(row)['next_check_at']==service.later(row.virtual_now,21600)
        assert service.date(service.state(row)['next_check_at'])>service.date(old)


def controls():
    return {'simulation':{'status':'running'},'v3':{'followup_anchor':'2026-10-09T02:00:00+00:00',
        'followup_intervals':[1,4,10,45,120,180],'next_check_at':'2026-10-09T02:01:00+00:00',
        'pending_event':'silence_due','delivery_kind':'reply','delivery_due_at':'2026-10-09T02:01:00+00:00'}}


def test_pending_old_touch_cancelled_without_touching_customer_answer():
    value=controls()
    messages=[{'id':'old-touch','status':'draft','proactive':True}, {'id':'answer','status':'draft'}]
    revised,parts,due=reschedule(value,messages,'old','2026-10-09T02:01:00+00:00')
    assert due is None and revised['v3']['pending_event'] is None
    assert revised['v3']['next_check_at']=='2026-10-09T08:00:00+00:00'
    assert parts[0]['status']=='cancelled' and parts[1]['status']=='draft'
    assert reschedule(revised,parts,due,'2026-10-09T02:01:00+00:00')==(revised,parts,due)


@pytest.mark.parametrize('flag',['handoff','opt_out','appointment_pending'])
def test_existing_protections_and_customer_appointment_not_rescheduled(flag):
    value=controls();value['v3'][flag]=True
    assert reschedule(value,[],'due','2026-10-09T02:01:00+00:00')==(value,[],'due')


def test_stopped_session_and_expired_history_not_replayed():
    value=controls();value['simulation']['status']='stopped'
    assert reschedule(value,[],None,'2026-10-09T02:01:00+00:00')==(value,[],None)
    revised,_,due=reschedule(controls(),[],'old','2026-10-09T09:00:00+00:00')
    assert revised['v3']['next_check_at'] is None and due is None
