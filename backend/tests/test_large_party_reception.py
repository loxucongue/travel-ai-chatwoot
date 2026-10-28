from copy import deepcopy

import pytest
from sqlalchemy import select

from app.automation_models import AutomationRun, AutomationSession
from app.reception_v3 import service, large_party
from app.reception_v3.runtime import Decision
from test_reception_v3 import bundle, create, step, delivered, answer


def configured():
    spec = bundle()
    route = spec['routes']['nine']
    route['groups']['vehicle'] = {'text':'普通6人團9座車', 'assets':[], 'delivery_mode':'text_only'}
    route['introduction_sequence'].append('vehicle')
    route['large_party'] = {'enabled':True, 'minimum':7,
        'introduction_sequence':['map','hotel'], 'brief_sequence':['map'],
        'group_text_overrides':{}, 'opening_text':'您們{party_size}位先看行程',
        'completed_text':'您們{party_size}位可獨立成團', 'contact_text':'請提供聯絡方式',
        'unknown_route_text':'請問想走幾天？'}
    return spec


def setup(db, monkeypatch, profile=None, completed=False):
    row = create(db, monkeypatch)
    row.messages = []
    row.memory = profile or {'party_size':7}
    row.controls = {**row.controls, 'route_variant':'nine'}
    service.save(row, {'pending_event':'customer_message','opening_started':True,
        'intake_status':'done', 'completed_introductions':['nine'] if completed else []})
    monkeypatch.setattr(service, 'compile_skills', lambda db: deepcopy(configured()))
    monkeypatch.setattr(service, 'run_agent', lambda c: answer(route_variant='nine', start_introduction=True))
    db.commit()
    return row


def drain(db, row):
    for _ in range(12):
        step(db, row)


def test_first_intro_and_restart_handoff_without_extra_model_or_wash(session_factory, monkeypatch):
    with session_factory() as db:
        row = setup(db,monkeypatch)
        step(db,row)
        assert not service.state(row).get('handoff')
        assert [m['content'] for m in row.messages if m['status']=='draft'] == [
            '您們7位先看行程','完整行程图','住宿原文','請提供聯絡方式']
        sid=row.id
    with session_factory() as db:
        row=db.get(AutomationSession,sid)
        monkeypatch.setattr(service,'run_agent',lambda c: pytest.fail('delivery/handoff called the model'))
        drain(db,row)
        assert service.state(row)['handoff'] and row.controls['human']
        assert not service.state(row).get('next_check_at')
        assert not service.state(row).get('handoff_after_plan')
        before=deepcopy(row.messages)
        step(db,row,86400)
        assert row.messages==before


@pytest.mark.parametrize('profile', [{'party_size':4,'group_type':'private'}, {'party_size':6},
    {'party_size_min':6,'party_size_range':'6–8'}, {'trip_days':7}, {'party_size':True}])
def test_not_explicit_seven_keeps_existing_intro(session_factory, monkeypatch, profile):
    with session_factory() as db:
        row=setup(db,monkeypatch,profile)
        step(db,row)
        assert service.state(row)['delivery_kind']=='introduction'
        assert not service.state(row).get('handoff_after_plan')


def test_mid_intro_reclassifies_before_vehicle_without_replay(session_factory, monkeypatch):
    with session_factory() as db:
        row=setup(db,monkeypatch,{'party_size':4})
        step(db,row);step(db,row)
        assert delivered(row)==['完整行程图']
        service.add_message(db,row,'另外三位也來，現在7人','seven');db.commit()
        monkeypatch.setattr(service,'run_agent',lambda c: answer(action='queue',profile={'party_size':7}))
        step(db,row)
        assert '住宿原文' not in delivered(row)
        assert service.state(row)['delivery_kind']=='large_party'
        drain(db,row)
        assert delivered(row).count('完整行程图')==1
        assert '普通6人團9座車' not in delivered(row)
        assert service.state(row)['handoff']


def test_after_intro_only_closing_and_exact_count_correction(session_factory, monkeypatch):
    with session_factory() as db:
        row=setup(db,monkeypatch,{'party_size_min':7},completed=True)
        step(db,row)
        assert [m['content'] for m in row.messages]==['您們7位可獨立成團','請提供聯絡方式']
        service.add_message(db,row,'更正是6位，不是7位','six');db.commit()
        monkeypatch.setattr(service,'run_agent',lambda c: answer(profile={'party_size':6},messages=[{'text':'收到，改為6位。'}]))
        step(db,row);step(db,row)
        assert not service.state(row).get('handoff_after_plan')
        assert not service.state(row).get('handoff')
        assert delivered(row)==['收到，改為6位。']


@pytest.mark.parametrize('decision', [dict(action='handoff',handoff_type='requested'),
    dict(action='handoff',handoff_type='custom'),dict(opt_out=True,messages=[{'text':'好的'}])])
def test_human_custom_quote_and_optout_preempt_intro(session_factory,monkeypatch,decision):
    with session_factory() as db:
        row=setup(db,monkeypatch)
        monkeypatch.setattr(service,'run_agent',lambda c: answer(**decision))
        step(db,row)
        assert not service.state(row).get('handoff_after_plan')
        assert not any(m.get('content')=='完整行程图' for m in row.messages)
        assert service.state(row).get('handoff') if decision.get('action')=='handoff' else service.state(row).get('opt_out')


def test_last_submit_preserves_new_customer_event_and_failed_part_blocks_handoff(session_factory,monkeypatch):
    with session_factory() as db:
        row=setup(db,monkeypatch,completed=True)
        step(db,row)
        parts=list(row.messages)
        service.complete_delivery(row,parts[0]['id'],'submitted')
        service.add_message(db,row,'等一下，可能改六位','pending');db.commit()
        service.complete_delivery(row,parts[1]['id'],'submitted')
        assert service.state(row)['pending_event']=='customer_message'
        assert not large_party.finish(db,row)
        value=service.state(row);value['pending_event']='handoff_after_delivery';service.save(row,value)
        row.messages=[{**m,'status':'failed'} if m['id']==parts[1]['id'] else m for m in row.messages]
        assert not large_party.finish(db,row)
        assert not service.state(row).get('handoff')


def test_all_closing_already_delivered_still_finishes_empty_plan(session_factory,monkeypatch):
    with session_factory() as db:
        row=setup(db,monkeypatch,completed=True)
        row.messages=[{'id':str(i),'direction':'outgoing','status':'sent','content':t}
            for i,t in enumerate(['您們7位可獨立成團','請提供聯絡方式'])]
        step(db,row)
        assert service.state(row)['pending_event']=='handoff_after_delivery'
        step(db,row)
        assert service.state(row)['handoff']


def test_unknown_route_asks_days_then_uses_brief_sequence(session_factory,monkeypatch):
    with session_factory() as db:
        row=setup(db,monkeypatch)
        row.controls={**row.controls,'route_variant':''}
        monkeypatch.setattr(service,'run_agent',lambda c: answer(action='handoff',handoff_type='party_size'))
        step(db,row);step(db,row)
        assert delivered(row)==['請問想走幾天？']
        assert not service.state(row).get('handoff')
        service.add_message(db,row,'想走九日這條','route');db.commit()
        monkeypatch.setattr(service,'run_agent',lambda c: answer(route_variant='nine',start_introduction=True))
        step(db,row);drain(db,row)
        assert '完整行程图' in delivered(row) and '住宿原文' not in delivered(row)
        assert service.state(row)['handoff']


def test_live_handoff_and_business_label_are_durable_and_idempotent(session_factory,monkeypatch):
    from live_fixture import setup as setup_live
    from app.models import HandoffTask, ConversationLabelAddition
    setup_live(session_factory,monkeypatch)
    with session_factory() as db:
        row=setup(db,monkeypatch,completed=True)
        row.conversation_state_id=1
        step(db,row)
        for part in list(row.messages):service.complete_delivery(row,part['id'],'submitted')
        row.environment='live'
        assert large_party.finish(db,row)
        db.commit()
        assert not large_party.finish(db,row)
        assert len(db.scalars(select(HandoffTask)).all())==1
        assert db.get(ConversationLabelAddition,(1,'高價值・獨立成團'))
        assert row.controls['human_followup']=={'enabled':False}
        assert row.controls['simulation']['status']=='stopped'


def test_migration_is_idempotent_and_preserves_ordinary_route():
    import json
    from pathlib import Path
    from scripts.configure_large_party_20261027 import update_package, EXCLUDED
    source=Path(__file__).parents[2]/'data/knowledge/china2go/route-packages'
    packages=[json.loads(p.read_text(encoding='utf8')) for p in source.rglob('route-package.json')]
    assert packages
    for package in packages:
        result=update_package(package)
        if 'large_party' not in result:continue
        large_party.validate_policy(result['large_party'],result['content_groups'])
        assert result==update_package(result)
        assert result['content_sequence']==package['content_sequence']
        assert result['content_groups']==package['content_groups']
        assert not EXCLUDED.intersection(result['large_party']['introduction_sequence'])


def test_model_quoting_config_does_not_duplicate_closing(session_factory,monkeypatch):
    with session_factory() as db:
        row=setup(db,monkeypatch,completed=True)
        monkeypatch.setattr(service,'run_agent',lambda c: answer(messages=[
            {'text':'您們7位可獨立成團'},{'text':'請提供聯絡方式'}]))
        step(db,row);drain(db,row)
        assert delivered(row)==['您們7位可獨立成團','請提供聯絡方式']


def test_actual_question_is_answered_but_acknowledgement_is_owned_by_config(session_factory,monkeypatch):
    with session_factory() as db:
        row=setup(db,monkeypatch,completed=True)
        monkeypatch.setattr(service,'run_agent',lambda c: answer(answer_before_introduction=True,
            messages=[{'text':'行程不包含機票。'}]))
        step(db,row);drain(db,row)
        assert delivered(row)==['行程不包含機票。','您們7位可獨立成團','請提供聯絡方式']


def test_model_repairs_answer_that_executor_would_discard(monkeypatch):
    from test_reception_v3_skills import context, model, final
    from app.reception_v3.runtime import run_agent
    c=context('peach_9d_2027')
    c['skills']['routes']['peach_9d_2027']['large_party']=configured()['routes']['nine']['large_party']
    c['profile']={'party_size':7}
    c['messages']=[{'direction':'incoming','content':'我們7位，機票含嗎？'}]
    responses=[final(action='handoff',handoff_type='party_size'),final(messages=[{'text':'機票不含。'}]),
        final(answer_before_introduction=True,messages=[{'text':'機票不含。'},
            {'text':'請加LINE','purpose':'contact_invitation'}])]
    # Both forms must be repaired: an omitted answer flag, or an extra invitation.
    for first in responses:
        requests=model(monkeypatch,[first,final(answer_before_introduction=True,messages=[{'text':'機票不含。'}])])
        monkeypatch.setattr('app.reception_v3.repetition.review',lambda d,*a:(d,[{'node':'repetition_review','status':'completed'}],None))
        result,logs,_=run_agent(c)
        assert result['answer_before_introduction'] and len(result['messages'])==1
        assert len(requests)==2 and logs[0]['status']=='large_party_reply_rejected'


def test_group_reply_contract_leaves_small_groups_unchanged():
    c={'skills':configured(),'route_variant':'nine','profile':{'party_size':6}}
    assert large_party.reply_contract_error(Decision(messages=[{'text':'行程不含機票。'}]).model_dump(),c) is None


def test_group_handoff_respects_prior_contact_refusal(session_factory,monkeypatch):
    with session_factory() as db:
        row=setup(db,monkeypatch,{'party_size':7,'contact_capture_declined':True},completed=True)
        step(db,row);drain(db,row)
        assert delivered(row)==['您們7位可獨立成團']
        assert service.state(row)['handoff']


def test_disabling_contact_after_group_intro_does_not_strand_handoff(session_factory,monkeypatch):
    with session_factory() as db:
        row=setup(db,monkeypatch,completed=True)
        step(db,row);step(db,row)
        assert delivered(row)==['您們7位可獨立成團']
        config=configured();config['lead_capture']={'enabled':False}
        monkeypatch.setattr(service,'compile_skills',lambda db:deepcopy(config))
        drain(db,row)
        assert delivered(row)==['您們7位可獨立成團']
        assert service.state(row)['handoff']
        assert any(m.get('cancel_reason')=='contact_permission_revoked' for m in row.messages)


def test_unknown_route_cannot_send_model_selected_intro_assets(session_factory,monkeypatch):
    with session_factory() as db:
        row=setup(db,monkeypatch)
        row.controls={**row.controls,'route_variant':''}
        monkeypatch.setattr(service,'run_agent',lambda c: answer(messages=[
            {'text':'先看這條路線','asset_keys':['unselected-map']}]))
        step(db,row);step(db,row)
        assert delivered(row)==['請問想走幾天？']


def test_bare_count_pauses_before_old_vehicle_is_sent(session_factory,monkeypatch):
    with session_factory() as db:
        row=setup(db,monkeypatch,{'party_size':4})
        step(db,row);step(db,row);step(db,row)
        assert delivered(row)==['完整行程图','住宿原文']
        service.add_message(db,row,'7','bare-seven');db.commit()
        monkeypatch.setattr(service,'run_agent',lambda c: answer(profile={'party_size':7},action='queue'))
        step(db,row)
        assert '普通6人團9座車' not in delivered(row)
        drain(db,row)
        assert '普通6人團9座車' not in delivered(row)
        assert service.state(row)['handoff']


@pytest.mark.parametrize('profile,description', [({'party_size':7},'7位'),
    ({'party_size_min':7,'party_size_range':'7～10位'},'7～10位'),({'party_size_min':7},'7位以上')])
def test_description_never_turns_lower_bound_into_exact_count(profile,description):
    assert large_party.party_description(profile)==description


def test_rejected_route_cancels_deferred_group_intro(session_factory,monkeypatch):
    with session_factory() as db:
        row=setup(db,monkeypatch)
        step(db,row)
        monkeypatch.setattr('app.reception_v3.route_choice.rejects_current_route',lambda *args:True)
        run=db.scalar(select(AutomationRun))
        assert not large_party.begin(db,row,run,configured(),Decision(messages=[{'text':'了解'}]).model_dump())
        assert not any(m.get('status')=='draft' for m in row.messages)
        assert not service.state(row).get('handoff_after_plan')


def test_mid_intro_reduction_resumes_ordinary_remaining_parts(session_factory,monkeypatch):
    with session_factory() as db:
        row=setup(db,monkeypatch)
        step(db,row);step(db,row);step(db,row)
        assert '完整行程图' in delivered(row)
        service.add_message(db,row,'改成6位','six');db.commit()
        monkeypatch.setattr(service,'run_agent',lambda c: answer(action='queue',profile={'party_size':6}))
        step(db,row)
        assert service.state(row)['delivery_kind']=='introduction'
        assert not service.state(row).get('handoff_after_plan')
        step(db,row);step(db,row)
        assert delivered(row).count('完整行程图')==1
        assert '住宿原文' in delivered(row) and '普通6人團9座車' in delivered(row)
        assert not service.state(row).get('handoff')
