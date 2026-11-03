import pytest

from app.reception_v3.material_rules import followup_material_route, followup_contract_error


def test_followup_evidence_does_not_confuse_reply_invitation_with_proactive_delivery():
    from app.reception_v3.runtime import _followup_delivery_evidence
    incoming={'direction':'incoming','created_at':'2026-11-03T15:00:00+08:00'}
    reply={'direction':'outgoing','status':'delivered','contact_invitation':True,
           'content':'Please leave LINE','created_at':'2026-11-03T15:00:18+08:00'}
    context={'now':'2026-11-04T09:00:00+08:00','messages':[incoming,reply]}
    evidence=_followup_delivery_evidence(context)
    assert evidence['proactive_message_count_after_customer']==0
    assert evidence['last_team_message_at']==reply['created_at']
    for status in ('draft','failed','cancelled'):
        context['messages'].append({**reply,'proactive':True,'status':status})
    assert _followup_delivery_evidence(context)['proactive_message_count_after_customer']==0
    context['messages'].append({**reply,'id':'sent-followup','proactive':True})
    evidence=_followup_delivery_evidence(context)
    assert evidence['proactive_message_count_after_customer']==1
    assert evidence['recent_proactive_deliveries'][0]['id']=='sent-followup'
    context['messages'].append({**incoming,'created_at':'2026-11-04T10:00:00+08:00'})
    evidence=_followup_delivery_evidence(context)
    assert evidence['proactive_message_count_after_customer']==0 and evidence['last_team_message_at'] is None


@pytest.mark.parametrize('text',['22','4/2','2？'])
def test_ambiguous_numeric_message_cannot_select_single_available_route(text):
    from app.reception_v3.route_choice import choice_error
    context={'event':'customer_message','route_variant':'','skills':{'routes':{'trip':{'name':'Xiamen','aliases':[]}}},
             'messages':[{'direction':'incoming','content':text}]}
    decision={'action':'reply','route_variant':'trip','route_choice':'selected','profile':{},'messages':[]}
    assert choice_error(decision,context)
    assert choice_error({**decision,'route_variant':'','route_choice':'unchanged'},context) is None


def test_material_scope_requires_unique_delivered_opening_not_customer_selection():
    bundle={'routes':{'trip':{},'other':{}}}
    materials=[{'key':'photo','routes':['trip'],'media_id':7,'media_hash':'hash'}]
    opening={'v3_kind':'opening','direction':'outgoing','status':'delivered','asset_key':'photo'}
    assert followup_material_route(bundle,materials,'',{},[opening])=='trip'
    assert followup_material_route(bundle,materials,'',{},[{**opening,'asset_key':'alias','media_hash':'hash'}])=='trip'
    for status in ('draft','cancelled','failed'):
        assert followup_material_route(bundle,materials,'',{},[{**opening,'status':status}])==''
    assert followup_material_route(bundle,materials,'',{},[{**opening,'v3_kind':'reply'}])==''
    assert followup_material_route(bundle,materials,'',{'discussion_route':'other'},[opening])==''
    assert followup_material_route({'routes':{}},materials,'',{},[opening])==''
    mixed=materials+[{'key':'other_photo','routes':['other']}]
    assert followup_material_route(bundle,mixed,'',{},[opening,{**opening,'asset_key':'other_photo'}])==''
    assert followup_material_route(bundle,materials,'other',{},[opening])=='other'


def test_material_scope_cannot_turn_into_a_selected_trip_or_introduction():
    context={'event':'silence_due','route_variant':'','followup_material_route':'trip',
             'skills':{'silence':{'material_selection':'configured_rules'}},'material_rules':[{'id':'rule'}]}
    valid={'route_variant':'','route_choice':'unchanged','messages':[{'material_rule_id':'rule','asset_keys':[]}]}
    assert followup_contract_error(valid,context) is None
    for patch in ({'route_variant':'trip'},{'route_choice':'selected'},{'start_introduction':True}):
        assert followup_contract_error({**valid,**patch},context)


def test_xiamen_unselected_customer_runs_daily_model_then_delivers_one_new_material(tmp_path):
    from test_business_isolation import run_isolated
    from test_xiamen_install import BOOTSTRAP
    run_isolated(tmp_path, BOOTSTRAP + '''
result=installer.install(apply=True)
from app.automation_models import AutomationSession
from app.reception_v3 import service
from app.reception_v3.runtime import Decision
from app.reception_v3.skills import compile_skills
from app.models import utcnow
from copy import deepcopy
with SessionLocal() as db:
    row=AutomationSession(owner_id=9,inbox_binding_id=result['inbox_id'],environment='playground',
        mode='journey',engine_version='v3',controls={},memory={},messages=[],virtual_now='2026-11-03T15:00:00+08:00')
    db.add(row);db.flush();service.start(db,row,'22')
    for _ in range(8):service.advance(row,service.later(row.controls['simulation']['last_wall_at'],4))
    value=service.state(row);value.update(pending_event=None,buffered_questions=[],next_check_minutes=1440)
    bundle=compile_skills(db);value['followup_settings']=bundle['silence']
    service.schedule_followup(value,bundle,row.virtual_now,reset=True);service.save(row,value);db.commit()
    assert service.state(row)['next_check_at']=='2026-11-04T09:00:00+08:00'
    seen=[]
    def model(context):
        seen.append(context)
        assert context['route_variant']=='' and context['followup_material_route']=='xiamen_3_6d'
        keys={k for r in context['material_rules'] for k in r['asset_keys']}
        assert keys=={'xiamen_food','xiamen_city_stairs','xiamen_city_view','xiamen_travelers_video'}
        return Decision(messages=[{'material_rule_id':'xiamen-followup-food'}]).model_dump(),[],'test'
    service.run_agent=model
    service.advance_next(row);db.commit()
    service.tick(db,session_id=row.id,advance_clock=False)
    for _ in range(4):service.advance(row,service.later(row.controls['simulation']['last_wall_at'],4))
    db.commit()
    sent=[m for m in row.messages if m.get('proactive') and m.get('status')=='simulated_delivered']
    assert len(seen)==1 and [m['content_type'] for m in sent]==['text','image']
    assert sent[1]['asset_key']=='xiamen_food'
    assert not row.controls.get('route_variant')
    assert service.state(row)['next_check_at']=='2026-11-05T09:00:00+08:00'
    value=service.state(row);value['pending_event']='silence_due';service.save(row,value);db.commit()
    service.tick(db,session_id=row.id,advance_clock=False)
    assert len(seen)==1 and service.state(row)['followup_skip_reason']=='daily_limit'
    # An inbound cancels the old wakeup and resumes normal answering.
    service.add_message(db,row,'What is included?','returned')
    assert service.state(row)['pending_event']=='customer_message'
    assert service.state(row)['next_check_at'] is None
''')


@pytest.mark.parametrize('revoked', [False, True])
def test_live_final_check_uses_preview_scope_and_honors_revoked_rule(session_factory, monkeypatch, tmp_path, revoked):
    from sqlalchemy import select
    from app.models import StoredMedia, OutboundMessage, utcnow
    from app.reception_v3 import live, service, skills
    from app import material_library
    from app.reception_v3.material_rules import rule_digest
    from live_fixture import setup
    from test_live_audit_fixes import session
    setup(session_factory, monkeypatch)
    rule={'id':'food','enabled':True,'purpose':'followup','route_variants':['trip'],
          'any_labels':[],'asset_keys':['new'],'approved_text':'New photo'}
    original_digest=rule_digest(rule)
    if revoked:rule['enabled']=False
    bundle={'routes':{'trip':{}},'material_rules':[rule],'silence':{'material_selection':'configured_rules'}}
    monkeypatch.setattr(skills,'compile_skills',lambda db:bundle)
    monkeypatch.setattr(material_library,'candidate_materials',lambda *a:[{'key':'welcome','routes':['trip']}])
    path=tmp_path/'test.jpg';path.write_bytes(b'test')
    sent=[]
    class Client:
        def close(self):pass
        def get_conversation(self,cid):return {'id':cid,'can_reply':True}
        def get_conversation_labels(self,cid):return ['ai']
        def create_attachment_message(self,*args):sent.append(args);return {'id':701}
    monkeypatch.setattr(live,'client_for',lambda *a:Client())
    with session_factory() as db:
        db.add(StoredMedia(id=99,tenant_id=1,created_by=1,original_name='test.jpg',media_type='image',
            mime_type='image/jpeg',file_size=4,storage_path=str(path)))
        row=session(db);row.controls={**row.controls,'route_variant':''}
        row.messages=[{'direction':'outgoing','v3_kind':'opening','asset_key':'welcome','status':'submitted','created_at':utcnow()}]
        value={'opening_started':True,'pending_event':None}
        service.append_plan(row,value,[{'content':'','content_type':'image','media_id':99,'asset_key':'new',
            'proactive':True,'material_rule_id':'food','material_rule_digest':original_digest}], 'reply',0)
        service.save(row,value);db.commit();live.deliver_one(db,row)
        assert len(sent)==(0 if revoked else 1)
        assert len(db.scalars(select(OutboundMessage)).all())==len(sent)
        assert not row.controls.get('route_variant')
