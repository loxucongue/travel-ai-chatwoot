from copy import deepcopy
from types import SimpleNamespace
import pytest
from app.reception_v3 import alternative_followup as a,service
from app.reception_v3.route_followup import reference_preview_allowed,policy_for

NOW='2026-11-17T10:00:00+00:00'
ROUTE='peach_11d_2027'


def row():
    return SimpleNamespace(memory={},virtual_now=NOW,controls={'v3':{'delivery_plan_id':'p',
        'delivery_kind':'reply','delivery_interval':2,'next_check_minutes':1440,
        'followup_settings':{'enabled':True,'daily_at':'20:00'}}},messages=[
        {'id':'c','direction':'incoming','content':'想看桃花','created_at':NOW},
        {'id':'q','plan_id':'p','direction':'outgoing','status':'draft','content':'想上珠峰嗎？',
         'alternative_offer_route':ROUTE,'alternative_source_message_id':'c'}])


def ctx(r,now):
    return {'event':'silence_due','state':service.state(r),'profile':r.memory,'messages':r.messages,'now':now}


def test_delivery_anchors_hour_and_survives_serialization():
    import json
    r=row();service.complete_delivery(r,'q','sent')
    assert not a.task(ctx(r,service.later(NOW,3599)))
    c=json.loads(json.dumps(ctx(r,service.later(NOW,3600))))
    assert a.task(c)['route']==ROUTE
    before=deepcopy(service.state(r)['alternative_pending'])
    r.virtual_now=service.later(NOW,30);service.complete_delivery(r,'q','read')
    assert service.state(r)['alternative_pending']==before


@pytest.mark.parametrize('status',['failed','cancelled','submission_unknown'])
def test_failed_offer_not_armed(status):
    r=row();service.complete_delivery(r,'q',status)
    assert not service.state(r).get('alternative_pending')


def test_customer_reply_and_takeover_cancel():
    r=row();service.complete_delivery(r,'q','sent')
    v=service.state(r)
    r.messages.append({'id':'n','direction':'incoming','created_at':service.later(NOW,5)})
    assert not a.eligible(v,{},r.messages)
    r.messages.pop();v['handoff']=True
    assert not a.eligible(v,{},r.messages)


def test_postquote_has_priority():
    r=row();service.complete_delivery(r,'q','sent')
    c=ctx(r,service.later(NOW,3601));c['state']['post_quote_pending']={'due_at':NOW}
    assert a.task(c) is None
    c['state']['next_check_at']=NOW;a.schedule(c['state'])
    assert c['state']['next_check_at']==NOW


def test_contact_received_or_invited_invalidates_alternative():
    r=row();service.complete_delivery(r,'q','sent');v=service.state(r)
    assert not a.eligible(v,{'contact_value':'a-customer-id'},r.messages)
    r.messages.append({'direction':'outgoing','status':'sent','contact_invitation':True,'created_at':NOW})
    assert not a.eligible(v,{},r.messages)


def test_late_delivery_failure_revokes_armed_offer():
    r=row();service.complete_delivery(r,'q','sent');v=service.state(r)
    r.messages[1]['status']='failed'
    assert not a.eligible(v,{},r.messages)


def test_preview_does_not_select_customer_route():
    r=row();service.complete_delivery(r,'q','sent');c=ctx(r,service.later(NOW,3600))
    c['skills']={'routes':{ROUTE:{}}}
    d={'route_choice':'recommend','profile':{'discussion_route':ROUTE},'start_introduction':False}
    assert reference_preview_allowed(c,d)
    d['start_introduction']=True
    assert not reference_preview_allowed(c,d)
    assert a.validation_error(d,c)


def test_unselected_light_followup_does_not_steal_hour_task():
    from app.reception_v3 import undecided_followup
    r=row();service.complete_delivery(r,'q','sent');c=ctx(r,service.later(NOW,3600))
    c['state'].pop('appointment_pending',None)
    c['skills']={'routes':{ROUTE:{}},'silence':{'undecided_followup':{'enabled':True}}}
    assert a.task(c)
    assert undecided_followup.task(c) is None


def test_legacy_checkpoint_is_hour_without_mutating_config():
    policy={'enabled':True,'checkpoints':{'peach_interest_wait':5},'guidance':'五分鐘'}
    b={'routes':{'r':{'followup_policy':policy}}}
    assert policy_for(b,'r',{})['checkpoints']['peach_interest_wait']==60
    assert policy['checkpoints']['peach_interest_wait']==5


def test_actual_customer_interest_required():
    d={'messages':[{'alternative_offer_route':ROUTE,'alternative_source_message_id':'x'}]}
    c={'event':'customer_message','skills':{'routes':{ROUTE:{}}},'messages':[{'id':'x','direction':'outgoing','content':'桃花'}]}
    assert a.validation_error(d,c)
    c['messages'][0]['direction']='incoming'
    assert not a.validation_error(d,c)
