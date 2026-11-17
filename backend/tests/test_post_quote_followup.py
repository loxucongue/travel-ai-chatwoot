from copy import deepcopy
from types import SimpleNamespace
from datetime import timedelta
import pytest
from app.reception_v3 import service, post_quote
from app.reception_v3.lead_priority import outcome, validation_error, task
from app.reception_v3.runtime import Decision
from test_lead_priority import context, invitation, configured
from test_reception_v3 import create, step, answer
from app.automation_models import AutomationSession


def quoted_row():
    return SimpleNamespace(memory={}, virtual_now='2026-11-17T10:00:00+00:00',
        controls={'v3':{'delivery_plan_id':'p','delivery_kind':'reply','delivery_interval':2,
                       'next_check_minutes':1440,'followup_settings':{'enabled':True,'daily_at':'20:00'}}},
        messages=[{'id':'q','plan_id':'p','direction':'outgoing','status':'draft','content':'报价', 'post_quote_minutes':5},
                  {'id':'tail','plan_id':'p','direction':'outgoing','status':'draft','content':'费用说明'}])


def test_timer_starts_after_last_part_and_is_idempotent():
    row=quoted_row();service.complete_delivery(row,'q','sent')
    assert not service.state(row).get('post_quote_pending')
    row.virtual_now=service.later(row.virtual_now,10)
    service.complete_delivery(row,'tail','sent')
    pending=service.state(row)['post_quote_pending']
    assert service.date(pending['due_at'])-service.date(row.virtual_now)==timedelta(minutes=5)
    before=deepcopy(pending)
    row.virtual_now=service.later(row.virtual_now,40);service.complete_delivery(row,'tail','delivered')
    assert service.state(row)['post_quote_pending']==before
    assert service.state(row)['next_check_at']==before['due_at']


@pytest.mark.parametrize('status',['failed','cancelled','submission_unknown'])
def test_failed_plan_does_not_arm(status):
    row=quoted_row();service.complete_delivery(row,'q',status);service.complete_delivery(row,'tail','sent')
    assert not service.state(row).get('post_quote_pending')


@pytest.mark.parametrize('field,value',[('contact_value','known'),('contact_capture_declined',True),('contact_status','provided')])
def test_known_contact_does_not_arm(field,value):
    row=quoted_row();row.memory[field]=value
    service.complete_delivery(row,'q','sent');service.complete_delivery(row,'tail','sent')
    assert not service.state(row).get('post_quote_pending')


def due_context():
    c=context();c['skills']['lead_capture']['post_quote_invitation_minutes']=5
    c['now']='2026-10-25T01:00:00+00:00'
    c['state']['post_quote_pending']={'due_at':c['now'],'quote_ids':['a1'],'plan_id':'p'}
    return c


def test_same_day_due_invites_and_overrides_route_three_hour_policy():
    c=due_context();assert outcome(invitation(),c)=='invite'
    c['skills']['routes']['nine']['followup_policy']={'enabled':True,'lead_priority_override':True,'checkpoints':{'quote_wait':180}}
    assert task(c)['post_quote_due'] and outcome(invitation(),c)=='invite'
    c['now']='2026-10-25T00:59:59+00:00'
    assert not post_quote.due(c)


@pytest.mark.parametrize('status,direction',[('invited','outgoing'),('provided','incoming'),('declined','incoming'),('deferred','incoming')])
def test_semantic_evidence_suppresses_both_invitation_and_other_wash(status,direction):
    c=due_context();c['messages'].append({'id':'e','direction':direction,'content':'真实历史证据','status':'sent'})
    d=invitation();d['lead_review']={'contact_status':status,'contact_message_id':'e'}
    assert outcome(d,c)=='wait' and validation_error(d,c)
    d.update(action='wait',messages=[]);assert validation_error(d,c) is None
    d.update(action='reply',messages=[{'text':'洗单风景','purpose':'reply'}]);assert validation_error(d,c)


def test_incoming_and_stop_cancel_existing_timer(session_factory,monkeypatch):
    with session_factory() as db:
        row=create(db,monkeypatch);v=service.state(row);v['post_quote_pending']={'due_at':row.virtual_now};service.save(row,v)
        service.add_message(db,row,'新的问题','customer-new')
        assert not service.state(row).get('post_quote_pending')
        v=service.state(row);v['post_quote_pending']={'due_at':row.virtual_now};service.save(row,v)
        service.control(row,'stop');assert not service.state(row).get('post_quote_pending')


def test_daily_cap_does_not_lose_future_daily_schedule(session_factory,monkeypatch):
    with session_factory() as db:
        row=create(db,monkeypatch);spec=configured();spec['silence']['daily_at']='20:00'
        spec['lead_capture']['post_quote_invitation_minutes']=5
        monkeypatch.setattr(service,'compile_skills',lambda db:deepcopy(spec))
        row.controls={**row.controls,'route_variant':'nine'}
        row.messages=[{'id':'in','direction':'incoming','content':'多少钱','created_at':row.virtual_now},
                      {'id':'old','direction':'outgoing','proactive':True,'status':'sent','run_id':1,'created_at':row.virtual_now}]
        service.save(row,{'pending_event':'silence_due','post_quote_pending':{'due_at':row.virtual_now},'next_check_minutes':5})
        service.tick(db,session_id=row.id,advance_clock=False)
        assert service.state(row)['pending_event'] is None
        assert not service.state(row).get('post_quote_pending')
        assert '20:00:00' in service.state(row)['next_check_at']


def test_quote_restart_five_minutes_then_next_daily_no_duplicate(session_factory,monkeypatch):
    seen=[]
    spec=configured();spec['silence']['daily_at']='20:00'
    spec['lead_capture']['post_quote_invitation_minutes']=5
    def model(c):
        seen.append(c['event'])
        if c['event']=='customer_message':
            return answer(route_variant='nine',messages=[{'text':'九日团费人民币9280元每位。','is_trip_quote':True}])
        if c['state'].get('post_quote_pending'):
            return invitation(),[],'hash'
        return answer(action='wait')
    monkeypatch.setattr(service,'run_agent',model)
    with session_factory() as db:
        row=create(db,monkeypatch);monkeypatch.setattr(service,'compile_skills',lambda db:deepcopy(spec))
        row.controls={**row.controls,'route_variant':'nine'}
        row.messages=[{'id':'customer','direction':'incoming','content':'多少钱','created_at':row.virtual_now}]
        service.save(row,{'pending_event':'customer_message','buffered_questions':['customer'],'opening_started':True,'intake_status':'done'})
        service.tick(db,session_id=row.id,advance_clock=False);step(db,row,3)
        assert service.state(row).get('post_quote_pending')
        db.commit();key=row.id;db.expunge_all();row=db.get(AutomationSession,key)
        step(db,row,299);assert seen==['customer_message']
        step(db,row,1);assert seen==['customer_message','silence_due']
        step(db,row,3)
        invitations=[m for m in row.messages if m.get('contact_invitation') and m.get('status')=='simulated_delivered']
        assert len(invitations)==1 and invitations[0]['post_quote_contact_invitation']
        assert not service.state(row).get('post_quote_pending')
        assert '20:00:00' in service.state(row)['next_check_at']
        service.advance_next(row);service.tick(db,session_id=row.id,advance_clock=False)
        assert len([m for m in row.messages if m.get('contact_invitation')])==1
