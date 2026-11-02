"""Ali dates/media and durable model-selected follow-up checkpoints."""
from copy import deepcopy
from datetime import date, timedelta
import json
from pathlib import Path

import pytest
from sqlalchemy import select

from app.reception_v3 import service, large_party, route_followup
from app.reception_v3.runtime import Decision
from app.reception_v3.seasonal_quotes import departure_quote, _available_departures
from app.reception_v3.route_choice import choice_error
from app.route_packages import _validate
from scripts.configure_ali_series_20261102 import ALI, RELEASE, update_package, material_rules
from test_large_party_reception import configured, setup, drain
from test_reception_v3 import step, answer, delivered

NOW='2026-11-02T02:00:00Z'


def package():
    return json.loads((RELEASE/'route-package.json').read_text(encoding='utf8'))


def group_bundle():
    result=configured()
    result['routes']['nine']['large_party'].update(notify_then_followup=True,
        followup_policy={'enabled':True,'checkpoints':{'advisor_wait':720,'daily':1440,
            'preference_wait':720},'reference_preview_checkpoints':['preference_wait'],
            'daily_sequence':['hotel']})
    return result


@pytest.mark.parametrize('month,price,supplement',[(5,24800,5100),(6,24800,5100),
    (7,26800,6000),(8,26800,6000),(9,26800,6000),(10,26800,6000)])
def test_exact_departure_amounts_no_standard_discount(month,price,supplement):
    p=package();_validate(p,RELEASE/'route-package.json')
    result=departure_quote({'routes':{ALI:p}},ALI,f'2027-{month:02d}-01',f'2027-{month:02d}-28',NOW,
        party_size=4,group_type='shared')
    assert result['status']=='quoted'
    assert result['price_cny']==price and result['single_supplement_cny']==supplement
    assert result['departure_examples']==[f'2027-{month:02d}-10']
    if month==10:assert result['advisory']


@pytest.mark.parametrize('start,end,party,group,status',[
    ('2027-05-01','2027-05-31',1,'shared','ask_date'),
    ('2027-05-01','2027-05-31',3,'shared','ask_date'),
    ('2027-05-01','2027-05-31',None,'shared','ask_date'),
    ('2027-06-11','',2,'shared','unavailable'),
    ('2028-06-01','2028-06-30',2,'shared','unavailable'),
    ('2027-06-01','2027-07-31',2,'shared','ask_date'),
    ('2027-06-10','',4,'private','advisor'),
    ('2027-06-10','',7,'shared','advisor')])
def test_fixed_dates_do_not_infer_weekly_or_private_prices(start,end,party,group,status):
    result=departure_quote({'routes':{ALI:package()}},ALI,start,end,NOW,party_size=party,group_type=group)
    assert result['status']==status and 'price_cny' not in result


def test_notice_uses_actual_departure_and_other_routes_can_list_fixed_alternatives():
    p=package()
    result=departure_quote({'routes':{ALI:p}},ALI,'2027-06-01','2027-06-30','2027-05-20T00:00:00Z',party_size=2)
    assert result['status']=='advisor'
    assert _available_departures(p,[date(2027,6,d) for d in (9,10,11)],date(2026,11,2),2)==['2027-06-10']
    assert not _available_departures(p,[date(2027,5,10)],date(2026,11,2),2)


def test_package_upgrade_preserves_normal_intro_and_has_all_original_media():
    p=package();updated=update_package(p)
    _validate(updated,RELEASE/'route-package.json')
    assert update_package(updated)==updated
    assert updated['content_sequence']==p['content_sequence']
    assert updated['large_party']['notify_then_followup']
    assert not updated.get('party_pricing')
    manifest=json.loads((RELEASE/'asset-manifest.json').read_text(encoding='utf8'))
    assert len(manifest)==26 and len({a['asset_key'] for a in manifest})==26
    assert len([a for a in manifest if a['asset_key'].startswith('ali15_')])==18
    assert len(material_rules({ALI:updated}))==7
    all_keys={a['asset_key'] for a in manifest}
    assert all(set(g['asset_keys'])<=all_keys for g in updated['content_groups'].values())


def test_group_notice_waits_without_terminal_handoff_and_restart_does_not_notify_twice(session_factory,monkeypatch):
    from app.automation_models import AutomationSession
    with session_factory() as db:
        row=setup(db,monkeypatch,completed=True)
        monkeypatch.setattr(service,'compile_skills',lambda db:deepcopy(group_bundle()))
        step(db,row);drain(db,row)
        value=service.state(row)
        assert value['group_advisor_pending'] and not value.get('handoff') and not row.controls.get('human')
        assert value['route_checkpoint']=='advisor_wait'
        assert value['next_check_minutes']==720
        assert not large_party.finish(db,row)
        sid=row.id;db.commit()
    with session_factory() as db:
        row=db.get(AutomationSession,sid)
        before=deepcopy(row.messages)
        assert not large_party.finish(db,row) and row.messages==before
        service.add_message(db,row,'午餐包含嗎？','new');db.commit()
        monkeypatch.setattr(service,'run_agent',lambda c:answer(messages=[{'text':'午餐自理。'}],followup_checkpoint='daily'))
        step(db,row);drain(db,row)
        assert delivered(row)[-1]=='午餐自理。'
        assert delivered(row).count('請提供聯絡方式')==1


def test_group_notice_live_task_is_assist_until_claim_and_keeps_owner(session_factory,monkeypatch):
    from live_fixture import setup as setup_live
    from app.models import HandoffTask,ConversationState,ConversationLabelAddition
    setup_live(session_factory,monkeypatch)
    with session_factory() as db:
        row=setup(db,monkeypatch,completed=True);row.conversation_state_id=1
        monkeypatch.setattr(service,'compile_skills',lambda db:deepcopy(group_bundle()))
        owner=db.get(ConversationState,1).assignee_id
        step(db,row)
        for part in list(row.messages):service.complete_delivery(row,part['id'],'submitted')
        row.environment='live'
        assert large_party.finish(db,row)
        db.flush()
        task=db.scalar(select(HandoffTask));assert task.ai_assist_allowed
        assert not db.scalar(select(HandoffTask.id).where(HandoffTask.blocks_ai()))
        assert db.get(ConversationState,1).assignee_id==owner
        assert db.get(ConversationLabelAddition,(1,'高價值・獨立成團'))
        assert not large_party.finish(db,row)
        assert len(db.scalars(select(HandoffTask)).all())==1
        task.status='claimed';db.flush()
        assert db.scalar(select(HandoffTask.id).where(HandoffTask.blocks_ai()))==task.id


def test_model_checkpoint_is_persisted_after_delivery_and_stop_cancels_it(session_factory,monkeypatch):
    with session_factory() as db:
        row=setup(db,monkeypatch,profile={'party_size':2},completed=True)
        rules=configured()
        rules['routes']['nine']['followup_policy']={'enabled':True,'checkpoints':{'fitness':5,'quote':180}}
        monkeypatch.setattr(service,'compile_skills',lambda db:deepcopy(rules))
        monkeypatch.setattr(service,'run_agent',lambda c:answer(messages=[{'text':'平常有健走習慣嗎？'}],followup_checkpoint='fitness'))
        step(db,row);drain(db,row)
        value=service.state(row)
        assert value['route_checkpoint']=='fitness' and value['next_check_minutes']==5
        assert service.date(value['next_check_at'])>service.date(row.virtual_now)
        service.add_message(db,row,'暫時不要提醒','pause');db.commit()
        monkeypatch.setattr(service,'run_agent',lambda c:answer(messages=[{'text':'好的'}],stop_followup=True))
        step(db,row);drain(db,row)
        assert not service.state(row).get('next_check_at')


def test_read_receipt_not_assumed_and_checkpoint_cannot_escape_config():
    context={'skills':group_bundle(),'route_variant':'nine','profile':{'party_size':7},'state':{},
        'messages':[{'direction':'outgoing','status':'delivered','content':'請問幾月？'}]}
    assert route_followup.task(context)['last_outgoing_read'] is False
    context['messages'][0]['status']='read'
    assert route_followup.task(context)['last_outgoing_read'] is True
    assert route_followup.validation_error({'followup_checkpoint':'invented'},context)
    context['now']='2026-11-02T23:00:00Z'
    task=route_followup.task(context)
    assert task['business_local_now']=='2026-11-03T07:00:00+08:00'
    assert task['business_timezone']=='Asia/Taipei'


def test_configured_reference_preview_does_not_select_route_or_allow_arbitrary_photo(session_factory,monkeypatch):
    rules=group_bundle()
    rules['routes']['nine']['groups']['map'].update(assets=['map'])
    context={'skills':rules,'route_variant':'','profile':{'party_size':7},'event':'silence_due',
      'state':{'route_checkpoint':'preference_wait'},'messages':[]}
    decision=Decision(route_choice='recommend',profile={'discussion_route':'nine'},
        messages=[{'text':'這張基本行程圖供您參考。','asset_keys':['map']}]).model_dump()
    assert route_followup.reference_preview_allowed(context,decision)
    choice_context=deepcopy(context)
    choice_context['skills']['routes']['peach_9d_2027']=choice_context['skills']['routes'].pop('nine')
    choice_decision=deepcopy(decision)
    choice_decision['profile'].update(discussion_route='peach_9d_2027',everest_preference='undecided')
    assert choice_error(choice_decision,choice_context) is None
    choice_decision['profile']['everest_preference']='avoid'
    assert choice_error(choice_decision,choice_context)
    materials=[{'key':'map','routes':['nine'],'content_type':'image','content_group_key':'itinerary_overview'}]
    with session_factory() as db:
        row=setup(db,monkeypatch);row.controls={**row.controls,'route_variant':''}
        service.save(row,context['state'])
        monkeypatch.setattr(service,'candidate_materials',lambda *a:materials)
        monkeypatch.setattr(service,'resolve_materials',lambda *a:[{'content_type':'image','asset_key':a[1][0]}])
        route,parts=service.parts_for(db,row,rules,decision,followup=True)
        assert not route and len(parts)==2
        decision['messages'][0]['asset_keys']=['unknown']
        with pytest.raises(ValueError,match='invalid_silence'):service.parts_for(db,row,rules,decision,followup=True)
        decision['start_introduction']=True
        assert not route_followup.reference_preview_allowed(context,decision)
