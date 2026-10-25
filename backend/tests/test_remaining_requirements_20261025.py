"""Isolated boundary and multi-event checks for the remaining business work."""
from copy import deepcopy
import json
import pytest
from sqlalchemy import select
from app.reception_v3 import service, live
from app.reception_v3.runtime import Decision
from app.reception_v3.material_rules import eligible_rules, selected_asset
from app.reception_config import ReceptionConfiguration, SilenceSettings
from app.reception_rollout import reception_inbox_ids
from app.operations import save_setting
from app.models import ConversationState, InboxBinding, MessageEvent, ConversationLabelAddition
from live_fixture import setup
from test_reception_v3 import create, bundle


def touch(status='delivered', **kw):
    return {'id':'followup-1','direction':'outgoing','proactive':True,'status':status,
            'plan_id':'plan-1','created_at':'2026-10-25T01:00:00+00:00',**kw}


@pytest.mark.parametrize('status', ['draft','cancelled','failed','submission_unknown'])
def test_unsent_touch_does_not_attribute_response(status):
    assert service.followup_response([{'direction':'incoming'},touch(status)]) is None


@pytest.mark.parametrize('interruption', [{'direction':'incoming'},touch(proactive=False)])
def test_intervening_response_or_advisor_breaks_attribution(interruption):
    assert service.followup_response([touch(),interruption]) is None


def test_followup_reply_cancels_unsent_caption_once_and_keeps_evidence(session_factory, monkeypatch):
    with session_factory() as db:
        row=create(db,monkeypatch)
        row.messages=[*row.messages,touch(),touch('draft',id='caption')]
        service.add_message(db,row,'預計3、4位','reply-1')
        proof=deepcopy(service.state(row)['followup_return'])
        assert proof['message_id']=='followup-1' and proof['reply_message_id']=='reply-1'
        assert next(m for m in row.messages if m['id']=='caption')['status']=='cancelled'
        service.add_message(db,row,'duplicate','reply-1')
        assert service.state(row)['followup_return']==proof
        service.add_message(db,row,'家人','reply-2')
        assert service.state(row)['followup_return']==proof
        value=service.state(row);value['buffered_questions']=[];service.save(row,value)
        service.add_message(db,row,'已看完','reply-3')
        assert 'followup_return' not in service.state(row)


def test_live_return_adds_business_tag_without_ownership_change(session_factory,monkeypatch):
    setup(session_factory,monkeypatch)
    with session_factory() as db:
        conv=db.get(ConversationState,1)
        live.accept(db,conv,db.get(MessageEvent,1))
        row=live.session_for(db,1)
        row.messages=[*row.messages,touch()]
        msg=MessageEvent(conversation_state_id=1,chatwoot_message_id=101,direction='incoming',content='3、4位')
        db.add(msg);db.flush()
        live.accept(db,conv,msg);db.flush()
        job=db.get(ConversationLabelAddition,(1,'洗單回'))
        assert job and job.source_message_id==msg.id and conv.labels==['ai']
        live.accept(db,conv,msg);db.flush()
        assert len(db.scalars(select(ConversationLabelAddition)).all())==1


def test_multi_channel_opt_in_and_inbox_switch(session_factory,monkeypatch):
    setup(session_factory,monkeypatch)
    with session_factory() as db:
        conv=db.get(ConversationState,1)
        assert live.permitted(db,conv)
        new=InboxBinding(tenant_id=1,chatwoot_inbox_id=999,name='new',ai_enabled=True)
        db.add(new);db.flush()
        conv.inbox_binding_id=new.id;db.flush();db.expire(conv,['inbox'])
        assert not live.permitted(db,conv)
        save_setting(db,'route_reception_config',{'routing':{'live_inbox_ids':[128859,999]}})
        assert reception_inbox_ids(db)==[128859,999]
        assert live.permitted(db,conv)
        new.ai_enabled=False
        assert not live.permitted(db,conv)
        new.ai_enabled=True
        save_setting(db,'route_reception_config',{'routing':{'live_inbox_ids':[]}})
        assert not live.permitted(db,conv)


def gallery():
    rule={'id':'vehicle','name':'车','enabled':True,'purpose':'both','route_variants':['nine'],
          'any_labels':[],'scenario':'客人问车辆','approved_text':'批准说明','asset_keys':['one','two']}
    materials=[{'key':key,'routes':['nine'],'content_type':'image','media_hash':key,'media_id':index}
               for index,key in enumerate(['one','two'],1)]
    return rule,materials


@pytest.mark.parametrize('change,event,labels', [({'enabled':False},'customer_message',[]),
    ({'route_variants':['other']},'customer_message',[]),({'any_labels':['洗單回']},'customer_message',[]),
    ({'purpose':'reply'},'silence_due',[]),({},'introduction_completed',[])])
def test_gallery_eligibility(change,event,labels):
    rule,materials=gallery();rule.update(change)
    assert eligible_rules({'material_rules':[rule]},materials,'nine',labels,event,[])==[]


def test_gallery_dedup_hash_alias_and_stable_selection():
    rule,materials=gallery()
    rule['any_labels']=['洗單回']
    result=eligible_rules({'material_rules':[rule]},materials,'nine',['洗單回'],'customer_message',
        [touch(asset_key='old-alias',media_hash='one')])[0]
    assert result['asset_keys']==['two']
    assert selected_asset(result,1,10)==selected_asset(result,1,10)==['two']
    assert len(selected_asset(rule,1,10))==1


def test_gallery_delivery_and_revocation_before_send(session_factory,monkeypatch):
    rule,materials=gallery()
    spec=bundle();spec['material_rules']=[rule]
    with session_factory() as db:
        row=create(db,monkeypatch)
        row.controls={**row.controls,'route_variant':'nine'}
        monkeypatch.setattr(service,'candidate_materials',lambda *a:materials)
        monkeypatch.setattr(service,'resolve_materials',lambda db,keys,*a:[{'content_type':'image','asset_key':k} for k in keys])
        decision=Decision(route_variant='nine',messages=[{'material_rule_id':'vehicle'}]).model_dump()
        _,parts=service.parts_for(db,row,spec,decision)
        assert sum(p['content_type']=='image' for p in parts)==1
        assert parts[0]['content']=='批准说明'
        spec['material_rules'][0]['enabled']=False
        with pytest.raises(ValueError,match='material_rule_not_eligible'):
            service.parts_for(db,row,spec,decision)


def test_seven_day_config_and_duplicate_rule_validation():
    assert SilenceSettings(intervals_minutes=[2880, 4320]).intervals_minutes == [2880, 4320]
    assert sum(SilenceSettings(intervals_minutes=[1440]*7).intervals_minutes)==10080
    with pytest.raises(ValueError):SilenceSettings(intervals_minutes=[1440]*8)
    rule,_=gallery()
    with pytest.raises(ValueError,match='duplicate_material_rule_ids'):
        ReceptionConfiguration(material_rules=[rule,rule])


def test_budget_uses_new_authorization_and_keeps_old_cost(tmp_path,monkeypatch):
    from app import model_metering as meter
    path=tmp_path/'ledger.json'
    path.write_text(json.dumps({'limit_cny':50,'calls':[{'id':'old','charged_or_reserved_cny':21}]}))
    monkeypatch.setattr(meter,'_ledger_path',lambda:path)
    meter._update({'id':'new','charged_or_reserved_cny':2},reserve=True)
    assert json.loads(path.read_text())['charged_or_reserved_cny']==23
    with pytest.raises(RuntimeError,match='exhausted'):
        meter._update({'id':'too-much','charged_or_reserved_cny':28},reserve=True)
    assert json.loads(path.read_text())['charged_or_reserved_cny']==23


def test_config_api_persists_gallery_and_scope_and_thirty_minute_switch(authenticated):
    client,csrf=authenticated
    rule,_=gallery()
    rule['route_variants']=['peach_9d_2027']
    changes={'routing':{'live_inbox_ids':[128859,999]},'material_rules':[rule],
             'silence':{'material_selection':'configured_rules','advisor_reply_followup_enabled':True,'intervals_minutes':[1440,1440]}}
    response=client.patch('/v1/automation/reception-config',json=changes,headers={'X-CSRF-Token':csrf})
    assert response.status_code==200,response.text
    saved=client.get('/v1/automation/reception-config').json()
    assert saved['runtime']['live_inbox_ids']==[128859,999]
    assert saved['config']['material_rules']==[rule]
    assert saved['config']['silence']['advisor_reply_followup_enabled'] is True
    assert saved['config']['silence']['material_selection'] == 'configured_rules'


def test_configured_followup_prevents_catalog_fallback(session_factory, monkeypatch):
    rule,materials=gallery()
    spec=bundle();spec['material_rules']=[rule]
    spec['silence']['material_selection']='configured_rules'
    with session_factory() as db:
        row=create(db,monkeypatch);row.controls={**row.controls,'route_variant':'nine'}
        monkeypatch.setattr(service,'candidate_materials',lambda *a:materials)
        monkeypatch.setattr(service,'resolve_materials',lambda db,keys,*a:[{'content_type':'image','asset_key':k} for k in keys])
        raw=Decision(route_variant='nine',messages=[{'asset_keys':['one']}]).model_dump()
        with pytest.raises(ValueError,match='followup_requires_material_rule'):
            service.parts_for(db,row,spec,raw,followup=True)
        selected=Decision(route_variant='nine',messages=[{'material_rule_id':'vehicle'}]).model_dump()
        _,parts=service.parts_for(db,row,spec,selected,followup=True)
        assert sum(p['content_type']=='image' for p in parts)==1
        spec['material_rules']=[]
        with pytest.raises(ValueError,match='material_rule_not_eligible'):
            service.parts_for(db,row,spec,selected,followup=True)


@pytest.mark.parametrize('change',['disable','delete','edit','labels','old_catalog'])
def test_queued_material_changes_cancel_before_network_submission(session_factory, monkeypatch, change):
    from test_live_audit_fixes import session
    from app.reception_v3 import skills
    from app.reception_v3.material_rules import rule_digest
    from app.models import OutboundMessage
    setup(session_factory,monkeypatch)
    rule,_=gallery();spec=bundle();spec['material_rules']=[rule]
    with session_factory() as db:
        row=session(db);row.controls={**row.controls,'route_variant':'nine'}
        value=service.state(row)
        part={'content':'approved','content_type':'image','proactive':True,
              'material_rule_id':rule['id'],'material_rule_digest':rule_digest(rule)}
        service.append_plan(row,value,[part],interval=0,kind='silence');service.save(row,value)
        if change=='disable':rule['enabled']=False
        if change=='delete':spec['material_rules']=[]
        if change=='edit':rule['approved_text']='updated'
        if change=='labels':rule['any_labels']=['not-present']
        if change=='old_catalog':
            row.messages=[{k:v for k,v in m.items() if k not in ('material_rule_id','material_rule_digest')} for m in row.messages]
            spec['silence']['material_selection']='configured_rules'
        monkeypatch.setattr(skills,'compile_skills',lambda *a:spec)
        monkeypatch.setattr(live,'client_for',lambda *a:pytest.fail('must not create channel client'))
        db.commit();live.deliver_one(db,row)
        assert all(m['status']=='cancelled' for m in row.messages)
        assert db.scalar(select(OutboundMessage)) is None


def test_followup_content_migration_keeps_product_facts_and_assets():
    from scripts.configure_contextual_followup_20261025 import package_delta
    old={'package_version':'original','knowledge_facts':[{'text':'approved'}],
         'fixed_answers':[{'id':'custom-id','topics':['silence_followup'],'answer_text':'approved text','asset_ids':['image'],'usage_note':'old'},
                          {'id':'price','answer_text':'approved price'}]}
    new=package_delta(old)
    assert package_delta(new)==new and old['package_version']=='original'
    assert new['knowledge_facts']==old['knowledge_facts']
    assert new['fixed_answers'][0]['asset_ids']==['image']
    assert new['fixed_answers'][1]==old['fixed_answers'][1]


def test_material_migration_is_idempotent_preserves_operator_changes_and_scope():
    from scripts.configure_contextual_followup_20261025 import material_rules_from_routes
    routes={'nine':{'name':'Nine','scripts':[
        {'id':'tea','status':'active','topics':['silence_followup'],'answer_text':'approved','asset_ids':['tea']},
        {'id':'disabled','status':'disabled','topics':['silence_followup'],'answer_text':'do not import'}]}}
    rules=material_rules_from_routes(routes)
    assert len(rules)==1 and rules[0]['route_variants']==['nine'] and rules[0]['asset_keys']==['tea']
    rules[0]['enabled']=False
    assert material_rules_from_routes(routes,rules)==rules
