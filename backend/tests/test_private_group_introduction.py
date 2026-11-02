from copy import deepcopy
import pytest
from app.reception_v3 import service, large_party
from app.reception_v3.runtime import Decision
from test_large_party_reception import setup, configured, drain
from test_reception_v3 import step, delivered, answer


def private_config(monkeypatch):
    spec=configured()
    policy=spec['routes']['nine']['large_party']
    policy.update(include_explicit_private=True,opening_text='您們{party_description}想自己一團，先看行程',
                  completed_text='您們{party_description}想自己一團，繼續確認安排')
    monkeypatch.setattr(service,'compile_skills',lambda db:deepcopy(spec))
    return spec


@pytest.mark.parametrize('profile',[{'party_size':4,'group_type':'private'},
    {'party_size':5,'group_type':'private'},{'party_size':6,'group_type':'private'},
    {'party_size_min':4,'group_type':'private'},{'group_type':'private'}, {'party_size':8}])
def test_private_intro_then_handoff(session_factory,monkeypatch,profile):
    with session_factory() as db:
        row=setup(db,monkeypatch,profile)
        private_config(monkeypatch)
        step(db,row)
        assert service.state(row)['delivery_kind']=='large_party'
        assert not service.state(row).get('handoff')
        drain(db,row)
        assert service.state(row)['handoff']
        text='\n'.join(delivered(row))
        assert '完整行程图' in text and '普通6人團9座車' not in text and 'None' not in text
        if not large_party.count(profile):assert '7位' not in text


@pytest.mark.parametrize('profile',[{'party_size':4},{'party_size':6,'group_type':'shared'},
                                  {'party_size':4,'group_type':'undecided'}])
def test_number_alone_does_not_choose_private(session_factory,monkeypatch,profile):
    with session_factory() as db:
        row=setup(db,monkeypatch,profile);private_config(monkeypatch)
        step(db,row)
        assert service.state(row)['delivery_kind']=='introduction'
        assert not service.state(row).get('handoff_after_plan')


def test_unknown_route_preserves_reference_reply_and_answer(session_factory,monkeypatch):
    with session_factory() as db:
        row=setup(db,monkeypatch,{'party_size_min':4,'group_type':'private'});private_config(monkeypatch)
        row.controls={**row.controls,'route_variant':''}
        monkeypatch.setattr(service,'run_agent',lambda c: answer(messages=[{'text':'獨立一團可以先參考這條路線，機票另計。'}]))
        step(db,row);step(db,row)
        assert delivered(row)==['獨立一團可以先參考這條路線，機票另計。']
        assert row.controls['route_variant']=='' and not service.state(row).get('handoff')


def test_private_correction_to_shared_cancels_handoff_without_map_replay(session_factory,monkeypatch):
    with session_factory() as db:
        row=setup(db,monkeypatch,{'party_size':4,'group_type':'private'});private_config(monkeypatch)
        step(db,row);step(db,row);step(db,row)
        service.add_message(db,row,'我們改拼團，不用包團了','shared');db.commit()
        monkeypatch.setattr(service,'run_agent',lambda c: answer(action='queue',profile={'group_type':'shared'}))
        step(db,row)
        assert service.state(row)['delivery_kind']=='introduction'
        assert not service.state(row).get('handoff_after_plan')
        step(db,row);step(db,row)
        assert delivered(row).count('完整行程图')==1 and '普通6人團9座車' in delivered(row)


def test_completed_intro_only_acknowledges_private_request(session_factory,monkeypatch):
    with session_factory() as db:
        row=setup(db,monkeypatch,{'party_size':4,'group_type':'private'},completed=True);private_config(monkeypatch)
        step(db,row);drain(db,row)
        assert '完整行程图' not in delivered(row)
        assert service.state(row)['handoff']


def test_answer_to_question_survives_intake_and_private_intro(session_factory,monkeypatch):
    with session_factory() as db:
        row=setup(db,monkeypatch,{'party_size':4,'group_type':'private'});private_config(monkeypatch)
        value=service.state(row);value.update(intake_answered=True,intake_field='party_size');service.save(row,value)
        monkeypatch.setattr(service,'run_agent',lambda c: answer(route_variant='nine',
            answer_before_introduction=True,messages=[{'text':'機票不包含在團費內。'}]))
        step(db,row);drain(db,row)
        assert delivered(row)[0]=='機票不包含在團費內。'
        assert delivered(row).count('機票不包含在團費內。')==1


def test_existing_contact_continues_answering_without_sales_handoff(session_factory,monkeypatch):
    with session_factory() as db:
        row=setup(db,monkeypatch,{'party_size':4,'group_type':'private','contact_value':'test@example.com'},completed=True)
        spec=private_config(monkeypatch)
        decision=Decision(route_variant='nine',messages=[{'text':'團費包含早餐。'}]).model_dump()
        assert large_party.reply_contract_error(decision,{'skills':spec,'profile':row.memory}) is None
        monkeypatch.setattr(service,'run_agent',lambda c: (decision,[],None))
        step(db,row);step(db,row)
        assert delivered(row)==['團費包含早餐。']
        assert not service.state(row).get('handoff_after_plan')


def test_private_flag_validates_and_installer_preserves_existing_content():
    from app.route_packages import load_route_packages, _validate
    from scripts.configure_large_party_20261027 import update_package as initial
    from scripts.configure_private_groups_20261102 import update_package
    from pathlib import Path
    for package in load_route_packages().values():
        original=initial(package)
        updated=update_package(original)
        assert update_package(updated)==updated
        assert updated['content_groups']==original['content_groups']
        assert updated.get('party_pricing')==original.get('party_pricing')
        _validate(deepcopy(updated),Path('test.json'))
    policy=configured()['routes']['nine']['large_party']
    policy['include_explicit_private']='true'
    with pytest.raises(ValueError,match='private_flag'):
        large_party.validate_policy(policy,configured()['routes']['nine']['groups'])


@pytest.mark.parametrize('value,expected',[('4位以上','4位以上'),('4人以上','4人以上'),('4～6','4～6位')])
def test_range_preserves_existing_unit(value,expected):
    assert large_party.party_description({'party_size_range':value})==expected
