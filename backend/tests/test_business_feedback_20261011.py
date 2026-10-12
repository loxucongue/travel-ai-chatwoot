from copy import deepcopy
from datetime import datetime
from zoneinfo import ZoneInfo
from sqlalchemy import select

from app.automation_models import AutomationRun
from app.reception_v3 import service
from app.reception_v3.skills import effective_price_scripts
from scripts.update_business_feedback_20261011 import update_package, quotation
from test_v3_intake_followup import create, config
from test_reception_v3 import answer, step, delivered


def test_model_recognized_route_wait_still_sends_actual_intake(session_factory, monkeypatch):
    with session_factory() as db:
        row = create(db, monkeypatch, entry='西藏桃花節＋珠峰 11日',
                     model=lambda c: answer(action='wait', route_variant='eleven'))
        step(db, row, 0)
        assert service.state(row)['intake_status'] == 'sending'
        assert service.state(row).get('next_check_at') is None
        step(db, row, 1)
        assert delivered(row) == [config()['intake']['question']]
        step(db, row, 60)
        assert service.state(row)['delivery_kind'] == 'introduction'


def test_night_followup_defers_to_nine_am_then_runs_once(session_factory, monkeypatch):
    seen = []
    with session_factory() as db:
        row = create(db, monkeypatch, entry='先看看',
                     model=lambda c: (seen.append(c['event']) or answer(messages=[{'text':'慢慢看就好～'}])))
        spec = config(); spec['silence'].update(intervals_minutes=[360], active_start='09:00', active_end='00:00')
        monkeypatch.setattr(service, 'compile_skills', lambda db: spec)
        row.virtual_now = '2026-10-11T11:48:00+00:00'  # 19:48 Taiwan
        row.messages = [{'id':'c', 'direction':'incoming','content':'先看看','created_at':row.virtual_now}]
        value = {'silence_step':0, 'followup_intervals':[360], 'opening_started':True}
        service.schedule_followup(value, spec, row.virtual_now, reset=True)
        service.save(row, value); db.commit()
        step(db, row, 6*3600)
        assert not seen
        assert service.state(row)['next_check_at'] == '2026-10-12T09:00:00+08:00'
        step(db, row, 7*3600+12*60)
        assert seen == ['silence_due']
        assert 'followup_skip_reason' not in service.state(row)
        step(db, row, 2)
        assert delivered(row) == ['慢慢看就好～']
        assert service.state(row)['next_check_at'] is None


def test_new_message_cancels_deferred_followup(session_factory, monkeypatch):
    with session_factory() as db:
        row = create(db, monkeypatch, entry='先看看')
        service.save(row, {'next_check_at':'2026-10-12T09:00:00+08:00', 'followup_anchor':row.virtual_now})
        service.add_message(db, row, '我再問一下', 'new')
        assert service.state(row)['next_check_at'] is None
        assert service.state(row)['pending_event'] == 'customer_message'


def test_promotion_fixed_dates_and_fallback_do_not_roll_forward():
    package = {'route_variant':'peach_11d_2027','content_groups':{},
               'fixed_answers':[{'id':'price','answer_text':'old','status':'active'}]}
    scripts = update_package(package)['fixed_answers']
    for day, expected in [('2026-10-11', '12,180'), ('2026-10-31','12,180'),
                          ('2026-11-01','12,780'), ('2026-10-10','12,780')]:
        now = datetime.fromisoformat(day).replace(tzinfo=ZoneInfo('Asia/Taipei'))
        text = effective_price_scripts(scripts, now)[0]['answer_text']
        assert expected in text
        assert ('10/31' in text) == (expected == '12,180')
    assert scripts[0]['valid_until'] == '2026-10-31'
    assert package['fixed_answers'][0]['answer_text'] == 'old'


def test_nine_day_preserves_price_without_inventing_promotion():
    package = {'route_variant':'peach_9d_2027','content_groups':{},
               'fixed_answers':[{'id':'price','answer_text':'old'}]}
    text = update_package(package)['fixed_answers'][0]['answer_text']
    assert '10,780' in text and '包含' in text and '不含' in text
    assert '國慶' not in text and '10/31' not in text


def test_price_validity_survives_configuration_api_roundtrip():
    import json
    from app.automation_api import RouteFixedAnswerInput
    from app.route_packages import PACKAGE_ROOT
    package = json.loads((PACKAGE_ROOT/'peach-11d-2027/route-package.json').read_text(encoding='utf8'))
    price = next(s for s in update_package(package)['fixed_answers'] if s['id'] == 'price')
    saved = RouteFixedAnswerInput.model_validate(price).model_dump()
    assert saved['valid_until'] == '2026-10-31'
    saved['valid_from'] = None
    assert '12,180' in effective_price_scripts([saved], datetime(2026,10,11,tzinfo=ZoneInfo('Asia/Taipei')))[0]['answer_text']
    assert '12,780' in effective_price_scripts([saved], datetime(2026,11,1,tzinfo=ZoneInfo('Asia/Taipei')))[0]['answer_text']
