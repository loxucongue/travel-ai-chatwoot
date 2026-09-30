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
