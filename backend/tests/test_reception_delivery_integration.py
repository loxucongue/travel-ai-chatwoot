from copy import deepcopy
from types import SimpleNamespace
from app.reception_v3 import service, material_rules, consultation, alternative_followup
from app.reception_v3.runtime import Decision
from test_reception_v3 import create, step, answer, bundle
from app.automation_models import AutomationSession

TARGET = 'peach_11d_2027'


def catalog():
    b = bundle()
    b['routes'][TARGET] = deepcopy(b['routes']['nine'])
    b['routes'][TARGET]['groups'].update({
        'advisor_greeting': {'text':'开场','assets':[]},
        'contact_request': {'text':'要联系方式','assets':[]},
        'map': {'text':'参考行程','assets':['map'],'delivery_mode':'assets_then_text'},
        'hotel': {'text':'已看过住宿','assets':['hotel'],'delivery_mode':'assets_then_text'},
        'car': {'text':'车辆配置','assets':['car'],'delivery_mode':'assets_then_text'}})
    b['routes'][TARGET]['introduction_sequence'] = ['advisor_greeting','map','hotel','car','contact_request']
    b['silence'].update(daily_at='20:00',max_proactive_messages_per_day=1)
    return b


def assets(monkeypatch):
    materials = [{'key':k,'content_type':'image','routes':[TARGET],'media_hash':k,'media_id':i}
                 for i,k in enumerate(('map','hotel','car'),1)]
    monkeypatch.setattr(service,'tenant_for_session',lambda *a:None)
    monkeypatch.setattr(service,'candidate_materials',lambda *a:materials)
    monkeypatch.setattr(service,'resolve_materials',lambda db,keys,*a:[{
        'content_type':'image','asset_key':k,'media_hash':k,'media_id':next(m['media_id'] for m in materials if m['key']==k)} for k in keys])


def test_reference_delivery_keeps_selected_route_and_skips_seen_theme(monkeypatch):
    assets(monkeypatch)
    now='2026-11-17T10:00:00+00:00'
    r=SimpleNamespace(id=1,generation=1,conversation_state_id=None,environment='playground',
        virtual_now=now,controls={'route_variant':'nine','v3':{'alternative_pending':{
            'route':TARGET,'due_at':now,'offered_at':now}}},memory={'discussion_route':TARGET},messages=[
            {'id':'old','direction':'outgoing','status':'sent','content':'已看过住宿','media_hash':'hotel'}])
    d=Decision(route_variant='nine',route_choice='recommend',profile={'discussion_route':TARGET},
               messages=[{'text':'先给您参考适合的桃花行程。'}]).model_dump()
    selected,parts=service.parts_for(None,r,catalog(),d,followup=True)
    assert selected=='nine' and r.controls['route_variant']=='nine'
    assert [p['asset_key'] for p in parts if p.get('asset_key')]==['map','car']
    assert [p['content'] for p in parts if p.get('content')]==['先给您参考适合的桃花行程。','参考行程','车辆配置']
    assert not any(p.get('contact_invitation') for p in parts)
    for part in parts:
        if part.get('reference_group'):
            assert alternative_followup.reference_part_allowed(part,catalog(),{})
    removed=catalog();removed['routes'][TARGET]['groups']['car']['assets']=[]
    car=next(p for p in parts if p.get('asset_key')=='car')
    assert not alternative_followup.reference_part_allowed(car,removed,{})
    assert not alternative_followup.reference_part_allowed({'content_type':'image','asset_key':'car'},catalog(),{})


def test_hour_task_restarts_on_existing_clock_and_cancels_before_delivery(session_factory,monkeypatch):
    b=catalog();events=[]
    def model(c):
        events.append(c['event'])
        return answer(route_variant='nine',route_choice='recommend',profile={'discussion_route':TARGET},
                      messages=[{'text':'给您参考桃花。'}])
    monkeypatch.setattr(service,'run_agent',model)
    with session_factory() as db:
        r=create(db,monkeypatch)
        assets(monkeypatch)
        monkeypatch.setattr(service,'compile_skills',lambda db:deepcopy(b))
        r.controls={**r.controls,'route_variant':'nine'}
        r.memory={'discussion_route':TARGET}
        r.messages=[{'id':'in','direction':'incoming','content':'桃花','created_at':r.virtual_now}]
        service.save(r,{'alternative_pending':{'route':TARGET,'due_at':service.later(r.virtual_now,3600),
             'offered_at':r.virtual_now},'next_check_at':service.later(r.virtual_now,3600),
             'next_check_minutes':60,'appointment_pending':True,'opening_started':True,'intake_status':'done'})
        db.commit();key=r.id;db.expunge_all();r=db.get(AutomationSession,key)
        step(db,r,3599);assert events==[]
        step(db,r,1);assert events==['silence_due']
        assert r.controls['route_variant']=='nine'
        assert any(m.get('reference_group')=='car' for m in r.messages)
        service.add_message(db,r,'不要发了','new-reply')
        assert not service.state(r).get('alternative_pending')
        assert not any(m.get('status')=='draft' and m.get('proactive') for m in r.messages)


def test_question_markers_count_only_delivered_messages():
    r=SimpleNamespace(messages=[{'id':'q','plan_id':'p','direction':'outgoing','content':'几月出发？',
        'question_topic':'departure','status':'draft'}],memory={},virtual_now='2026-11-17T10:00:00+00:00',
        controls={'v3':{'delivery_plan_id':'p','delivery_kind':'reply','delivery_interval':2}})
    assert consultation.task({'messages':r.messages})['asked']=={}
    service.complete_delivery(r,'q','failed')
    assert consultation.task({'messages':r.messages})['asked']=={}
    service.complete_delivery(r,'q','sent')
    assert consultation.task({'messages':r.messages})['asked']=={'departure':'q'}


def test_ali_theme_dedup_by_actual_hash_text_and_rule():
    route='tibet_ali_15d'
    rule={'id':'lake','enabled':True,'route_variants':[route],'purpose':'followup','any_labels':[],
          'asset_keys':['lake-alias'],'approved_text':'湖边住宿介绍'}
    b={'material_rules':[rule]}
    materials=[{'key':'lake-alias','routes':[route],'content_type':'image','media_hash':'same','media_id':7}]
    for old in ({'asset_key':'old','media_hash':'same'},{'content':'湖边住宿介绍'},{'material_rule_id':'lake'}):
        sent=[{'direction':'outgoing','status':'sent',**old}]
        assert material_rules.eligible_rules(b,materials,route,[],'silence_due',sent,{})==[]
        assert material_rules.eligible_rules(b,materials,route,[],'silence_due',[],{})


def test_hour_task_obeys_daily_cap(session_factory,monkeypatch):
    with session_factory() as db:
        r=create(db,monkeypatch);b=catalog()
        monkeypatch.setattr(service,'compile_skills',lambda db:deepcopy(b))
        monkeypatch.setattr(service,'run_agent',lambda c:(_ for _ in ()).throw(AssertionError('must not call model')))
        r.controls={**r.controls,'route_variant':'nine'}
        r.messages=[{'id':'in','direction':'incoming','content':'桃花','created_at':r.virtual_now},
            {'id':'out','direction':'outgoing','status':'sent','proactive':True,'run_id':1,'created_at':r.virtual_now}]
        service.save(r,{'pending_event':'silence_due','next_check_minutes':60,
            'alternative_pending':{'route':TARGET,'offered_at':r.virtual_now,'due_at':r.virtual_now},
            'followup_settings':b['silence'],'appointment_pending':True})
        service.tick(db,session_id=r.id,advance_clock=False)
        assert service.state(r)['pending_event'] is None
        assert not any(m.get('status')=='draft' for m in r.messages)
        # The pending recommendation is retained for reassessment, never burst-sent.
        assert service.state(r).get('alternative_pending')


def test_reference_draft_is_cancelled_when_delivery_crosses_quiet_boundary():
    r=SimpleNamespace(virtual_now='2026-11-17T17:00:00+00:00',messages=[
        {'id':'ref','plan_id':'p','direction':'outgoing','status':'draft','proactive':True,
         'reference_introduction_route':TARGET,'content_type':'image','asset_key':'map'}],
        controls={'v3':{'followup_settings':{'enabled':True,'daily_at':'20:00','active_start':'07:00','active_end':'01:00'}}})
    assert service.skip_quiet_delivery(r,r.messages[0])
    assert r.messages[0]['status']=='cancelled'


def test_customer_reply_during_hour_model_invalidates_result(session_factory,monkeypatch):
    from sqlalchemy import select
    from app.automation_models import AutomationRun
    with session_factory() as db:
        r=create(db,monkeypatch);b=catalog();assets(monkeypatch)
        monkeypatch.setattr(service,'compile_skills',lambda db:deepcopy(b))
        r.controls={**r.controls,'route_variant':'nine'};r.memory={'discussion_route':TARGET}
        r.messages=[{'id':'in','direction':'incoming','content':'桃花','created_at':r.virtual_now}]
        service.save(r,{'pending_event':'silence_due','next_check_minutes':60,'appointment_pending':True,
            'alternative_pending':{'route':TARGET,'offered_at':r.virtual_now,'due_at':r.virtual_now}})
        def model(c):
            service.add_message(db,r,'先不要介绍了','interrupt');db.commit()
            return answer(route_variant='nine',route_choice='recommend',profile={'discussion_route':TARGET},messages=[{'text':'旧计划'}])
        monkeypatch.setattr(service,'run_agent',model)
        service.tick(db,session_id=r.id,advance_clock=False)
        run=db.scalar(select(AutomationRun).order_by(AutomationRun.id.desc()))
        assert run.status=='cancelled' and run.error_code=='superseded'
        assert not any(m.get('content')=='旧计划' for m in r.messages)
