from copy import deepcopy
import json
from pathlib import Path
import runpy

from app.reception_v3 import service, skills, runtime
from app.reception_config import get_reception_configuration
from app.route_packages import PACKAGE_ROOT, _validate
from test_reception_v3 import create, step, answer
from test_reception_v3_skills import context, model, final, tool


def test_service_facts_and_original_script_reach_generation_and_delivery(session_factory, monkeypatch):
    with session_factory() as db:
        compiled = skills.compile_skills(db)
        c = context('peach_9d_2027')
        c['skills'] = compiled
        requests = model(monkeypatch, [
            {'tool_calls': [tool('get_service_facts', {})]},
            final(messages=[{'script_id': 'tour_doctor_service'}])])
        decision, _, _ = runtime.run_agent(c)
        first = json.loads(requests[0]['messages'][-1]['content'])
        assert 'service.medical_support' in {f.get('id') for f in first['service_knowledge']['facts']}
        tool_result = next(m for m in requests[1]['messages'] if m['role'] == 'tool')
        assert 'service.medical_support' in tool_result['content']
        row = create(db, monkeypatch)
        _, parts = service.parts_for(db, row, compiled, decision)
        assert len(parts) == 1
        assert parts[0]['content'].startswith('行程沒有安排隨隊醫師')
        assert '正規醫院' in parts[0]['content']


def test_operator_can_override_or_disable_common_service_script(session_factory, monkeypatch):
    with session_factory() as db:
        config = get_reception_configuration(db)
        config['common_scripts'] = [dict(id='tour_doctor_service', name='医师',
                                        text='业务更新原文', scenario='医师咨询', enabled=False)]
        monkeypatch.setattr(skills, 'get_reception_configuration', lambda db: deepcopy(config))
        compiled = skills.compile_skills(db)
        assert 'tour_doctor_service' not in service.scripts(compiled, '')
        assert '业务更新原文' not in skills.SkillRegistry(compiled).load('tibet-reception')['instructions']
        config['common_scripts'][0]['enabled'] = True
        compiled = skills.compile_skills(db)
        assert service.scripts(compiled, '')['tour_doctor_service']['text'] == '业务更新原文'


def test_package_corrections_preserve_operator_text_and_materials():
    correct = runpy.run_path(str(Path(__file__).parents[1] / 'scripts/update_business_wording.py'))['corrected_package']
    for path in PACKAGE_ROOT.glob('*/route-package.json'):
        current = json.loads(path.read_text(encoding='utf-8'))
        _validate(deepcopy(current), path)
        assert correct(current) == current
        encoded = json.dumps(current, ensure_ascii=False)
        assert '目前75歲以上長輩申請入藏函是申請不下來的' not in encoded
        assert '2人1標間' not in encoded
        assert '除了地區條件有限以外' not in encoded
        assert '含75歲' in encoded
        edited = deepcopy(current)
        edited['content_groups']['hotel_reference']['approved_text'] = '运营最新自定义住宿'
        after = correct(edited)
        assert after['content_groups'] == edited['content_groups']
        assert after['fixed_answers'] == edited['fixed_answers']
        legacy = deepcopy(current)
        old_hotel = '以及除了地區條件有限以外，我們全面升級都住國際品牌希爾頓飯店哦！'
        legacy['content_groups']['hotel_reference']['approved_text'] = old_hotel
        next(f for f in legacy['knowledge_facts'] if f['id'] == 'route.shared.hotel_reference')['text'] = old_hotel
        next(s for s in legacy['fixed_answers'] if s['id'] == 'hotel')['answer_text'] = old_hotel
        next(s for s in legacy['fixed_answers'] if s['id'] == 'age_75_entry_claim')['answer_text'] = '目前75歲以上長輩申請入藏函是申請不下來的'
        fixed = correct(legacy)
        assert '波密' in fixed['content_groups']['hotel_reference']['approved_text']
        assert old_hotel not in json.dumps(fixed, ensure_ascii=False)
        assert '含75歲' in next(s for s in fixed['fixed_answers'] if s['id'] == 'age_75_entry_claim')['answer_text']
        assert fixed['content_groups']['hotel_reference']['asset_keys'] == legacy['content_groups']['hotel_reference']['asset_keys']
        _validate(fixed, path)


def test_contact_script_migration_keeps_opening_and_custom_authoring():
    correct = runpy.run_path(str(Path(__file__).parents[1] / 'scripts/update_business_wording.py'))['corrected_configuration']
    before = {'reply': {'opening_message': '运营开场'}, 'common_scripts': [
        {'id': 'contact_email', 'text': '可以呀，留 Email 就好。方便提供您的信箱嗎？我請顧問用郵件和您聯絡。'},
        {'id': 'custom', 'text': '运营自定义原话'},
    ]}
    after = correct(before)
    assert after['reply'] == before['reply']
    assert after['common_scripts'][0]['text'].endswith('方便提供您的信箱嗎？')
    assert after['common_scripts'][1] == before['common_scripts'][1]
    assert correct(after) == after


def test_family_wording_upgrade_preserves_custom_script_and_switches():
    from scripts.update_business_wording import corrected_configuration
    from scripts.update_contact_reception import FAMILY_TEXT, FAMILY_SCENARIO, PREVIOUS_FAMILY_SCENARIO
    before = {'reply': {'opening_items': [{'content': '业务开场'}]},
              'silence': {'enabled': False, 'intervals_minutes': [3, 5]},
              'common_scripts': [dict(id='contact_family', enabled=False,
                 text='您先和家人討論，時間還不用急著決定。之後有想調整的地方，可以請顧問接著協助您。方便留一下您的 LINE ID 嗎？',
                 scenario=PREVIOUS_FAMILY_SCENARIO)]}
    after = corrected_configuration(before)
    assert after['common_scripts'][0] == dict(id='contact_family', enabled=False,
                                              text=FAMILY_TEXT, scenario=FAMILY_SCENARIO)
    assert after['reply'] == before['reply'] and after['silence'] == before['silence']
    assert corrected_configuration(after) == after
    custom = deepcopy(before)
    custom['common_scripts'][0].update(text='运营专属邀请', scenario='运营专属场景')
    assert corrected_configuration(custom) == custom


def test_invitation_scenario_upgrade_is_narrow_and_idempotent():
    from scripts.update_contact_reception import DEPLOYED_SCENARIOS, update_invitation_scenarios
    before = {'reply': {'opening_items': [{'content': '业务开场'}]},
              'silence': {'intervals_minutes': [1, 3, 5]}, 'common_scripts': [
                  dict(id=key, scenario=value, text='保留运营正文', enabled=False)
                  for key, value in DEPLOYED_SCENARIOS.items()]}
    after = update_invitation_scenarios(before)
    assert after['reply'] == before['reply'] and after['silence'] == before['silence']
    for old, new in zip(before['common_scripts'], after['common_scripts']):
        assert new['scenario'] != old['scenario']
        assert {k: v for k, v in new.items() if k != 'scenario'} == {k: v for k, v in old.items() if k != 'scenario'}
    assert update_invitation_scenarios(after) == after
    custom = deepcopy(before)
    for script in custom['common_scripts']:
        script['scenario'] = '运营自定义适用场景'
    assert update_invitation_scenarios(custom) == custom


def test_route_invitation_upgrade_preserves_sop_facts_and_script_text():
    from scripts.update_business_wording import corrected_package, PREVIOUS_CONTACT_NOTE, CONTACT_NOTE
    for path in PACKAGE_ROOT.glob('*/route-package.json'):
        current = json.loads(path.read_text(encoding='utf8'))
        legacy = deepcopy(current)
        for script in legacy['fixed_answers']:
            if script['id'] in ('contact_request', 'contact_after_read'):
                script['usage_note'] = PREVIOUS_CONTACT_NOTE
        after = corrected_package(legacy)
        assert after['content_groups'] == legacy['content_groups']
        assert after['knowledge_facts'] == legacy['knowledge_facts']
        assert after['content_sequence'] == legacy['content_sequence']
        for old, new in zip(legacy['fixed_answers'], after['fixed_answers']):
            assert old['answer_text'] == new['answer_text'] and old['asset_ids'] == new['asset_ids']
            if old['id'] in ('contact_request', 'contact_after_read'):
                assert new['usage_note'] == CONTACT_NOTE
                old['usage_note'] = '运营自己的邀请时机'
        custom = corrected_package(legacy)
        assert all(s['usage_note'] == '运营自己的邀请时机' for s in custom['fixed_answers']
                   if s['id'] in ('contact_request', 'contact_after_read'))


def test_wording_cli_updates_saved_configuration_and_loaded_skill(session_factory, monkeypatch, tmp_path):
    import sys
    from app import db as database, route_packages
    from app.reception_config import ReceptionConfiguration, put_reception_configuration
    from scripts import update_business_wording
    from scripts.update_contact_reception import PREVIOUS_FAMILY_SCENARIO, FAMILY_TEXT
    with session_factory() as db:
        before = get_reception_configuration(db)
        before['common_scripts'] = [dict(id='contact_family', name='家庭讨论', enabled=True,
            scenario=PREVIOUS_FAMILY_SCENARIO,
            text='您先和家人討論，時間還不用急著決定。之後有想調整的地方，可以請顧問接著協助您。方便留一下您的 LINE ID 嗎？')]
        put_reception_configuration(db, ReceptionConfiguration.model_validate(before)); db.commit()
        before = get_reception_configuration(db)
    monkeypatch.setattr(database, 'SessionLocal', session_factory)
    monkeypatch.setattr(route_packages, 'load_route_packages', lambda: {})
    monkeypatch.setattr(sys, 'argv', ['update', '--apply', '--backup', str(tmp_path)])
    update_business_wording.main()
    assert json.loads((tmp_path / 'reception-config.json').read_text(encoding='utf8')) == before
    with session_factory() as db:
        after = get_reception_configuration(db)
        assert after['reply'] == before['reply'] and after['silence'] == before['silence']
        assert after['common_scripts'][0]['text'] == FAMILY_TEXT
        loaded = skills.SkillRegistry(skills.compile_skills(db)).load('tibet-reception')['instructions']
        assert FAMILY_TEXT in loaded
    update_business_wording.main()  # idempotent; existing backup is not overwritten


def test_contact_capture_refusal_survives_into_silence_without_global_optout(session_factory, monkeypatch):
    with session_factory() as db:
        row = create(db, monkeypatch)
        monkeypatch.setattr(service, 'run_agent', lambda c: answer(
            profile={'contact_capture_declined': True, 'contact_preference': 'current_channel_only'},
            messages=[{'text': '可以，我們就在這裡聊。'}], next_check_minutes=1))
        step(db, row); step(db, row); step(db, row)
        assert not service.state(row)['opt_out']
        assert service.state(row)['next_check_at']
        seen = []
        def followup(c):
            seen.append(c)
            return answer(action='wait', next_check_minutes=3)
        monkeypatch.setattr(service, 'run_agent', followup)
        step(db, row, 65)
        assert seen[-1]['event'] == 'silence_due'
        assert seen[-1]['profile']['contact_capture_declined'] is True
        assert seen[-1]['profile']['contact_preference'] == 'current_channel_only'
        assert not service.state(row)['opt_out']
        assert service.state(row)['next_check_at']


def test_self_reported_contact_handoffs_without_fabricated_id(session_factory, monkeypatch):
    with session_factory() as db:
        row = create(db, monkeypatch)
        monkeypatch.setattr(service, 'run_agent', lambda c: answer(
            action='handoff', profile={'contact_status': 'self_reported_added',
            'contact_channel': 'LINE', 'contact_evidence': '我已經加你們LINE了'},
            handoff_reason='客户自报已添加，待顾问核实', messages=[{'text': '請顧問幫您確認訊息。'}]))
        step(db, row); step(db, row); step(db, row)
        assert row.memory['contact_status'] == 'self_reported_added'
        assert 'contact_value' not in row.memory
        assert service.state(row)['handoff']
        assert service.state(row)['next_check_at'] is None


def test_wait_timer_uses_completion_and_exposes_last_configured_round(session_factory, monkeypatch):
    with session_factory() as db:
        row = create(db, monkeypatch)
        service.advance(row)
        service.advance(row, service.later(row.controls['simulation']['last_wall_at'], 3))
        value = service.state(row)
        value.update(pending_event='silence_due', silence_step=1)
        service.save(row, value)
        base_clock = row.controls['simulation']['last_wall_at']
        original_bundle = service.compile_skills(db)
        original_bundle['silence']['intervals_minutes'] = [3, 5]
        monkeypatch.setattr(service, 'compile_skills', lambda db: deepcopy(original_bundle))
        completed = service.later(base_clock, 20)
        def respond(c):
            assert c['followup_schedule']['remaining_intervals_minutes'] == []
            assert c['followup_schedule']['next_configured_minutes'] is None
            return answer(action='wait', next_check_minutes=3)
        monkeypatch.setattr(service, 'run_agent', respond)
        monkeypatch.setattr(service, 'utcnow', lambda: completed)
        before = row.virtual_now
        db.commit()
        service.tick(db, session_id=row.id, advance_clock=False)
        assert row.virtual_now == service.later(before, 20)
        assert service.state(row)['next_check_at'] == service.later(before, 20 + 180)
