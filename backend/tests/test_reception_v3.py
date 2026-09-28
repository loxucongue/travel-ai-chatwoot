from copy import deepcopy
from datetime import timedelta

from sqlalchemy import select

from app.automation_models import AutomationRun, AutomationSession
from app.reception_v3 import service
from app.reception_v3.runtime import Decision


def bundle():
    return {'digest': 'fixture', 'routes': {'nine': {'name': '九日', 'groups': {
        'map': {'text': '完整行程图', 'assets': [], 'delivery_mode': 'text_only'},
        'hotel': {'text': '住宿原文', 'assets': [], 'delivery_mode': 'text_only'}},
        'introduction_sequence': ['map', 'hotel'], 'interval_seconds': 2, 'scripts': []}},
        'common_scripts': [], 'reply': {'opening_items': [], 'opening_messages': ['配置开场一', '配置开场二'],
        'opening_interval_seconds': 2}, 'silence': {'enabled': True}}


def answer(**kwargs):
    return Decision(**kwargs).model_dump(), [], 'hash'


def create(db, monkeypatch):
    monkeypatch.setattr(service, 'compile_skills', lambda db: deepcopy(bundle()))
    monkeypatch.setattr(service, 'active_web_facts', lambda *a, **k: ([], 'test'))
    monkeypatch.setattr(service, 'candidate_materials', lambda *a: [])
    row = AutomationSession(owner_id=1, engine_version='v3', mode='journey', environment='playground',
        messages=[], memory={}, controls={}, virtual_now='2026-09-28T01:00:00+00:00')
    db.add(row); db.flush()
    service.start(db, row, '九日行程')
    db.commit()
    return row


def step(db, row, seconds=3):
    wall = service.later(row.controls['simulation']['last_wall_at'], seconds)
    service.tick(db, session_id=row.id, wall_now=wall)


def delivered(row):
    return [m['content'] for m in row.messages if m.get('status') == 'simulated_delivered']


def test_script_reference_never_implicitly_attaches_images(session_factory, monkeypatch):
    spec = bundle()
    spec['routes']['nine']['scripts'] = [{'id': 'hotel', 'status': 'active',
        'answer_text': '完整住宿原话', 'asset_ids': ['room']}]
    monkeypatch.setattr(service, 'resolve_materials', lambda *a: [{'content_type': 'image', 'asset_key': 'room'}])
    monkeypatch.setattr(service, 'tenant_for_session', lambda *a: None)
    with session_factory() as db:
        row = create(db, monkeypatch)
        for text in ('只答獨立衛浴。', ''):
            decision = Decision(route_variant='nine', messages=[{'text': text, 'script_id': 'hotel'}]).model_dump()
            _, parts = service.parts_for(db, row, spec, decision)
            assert [p['content_type'] for p in parts] == ['text']
            assert parts[0]['content'] == (text or '完整住宿原话')
        decision['messages'][0]['asset_keys'] = ['room']
        _, parts = service.parts_for(db, row, spec, decision)
        assert [p['content_type'] for p in parts] == ['text', 'image']


def test_complete_intro_then_answer_queued_and_ask_at_end(session_factory, monkeypatch):
    events = []
    def model(context):
        events.append(context)
        if context['event'] == 'introduction_completed':
            return answer(messages=[{'text': '有獨立衛浴。您們預計幾月出發呢？'}], next_check_minutes=3)
        if context['state'].get('delivery_kind') == 'introduction':
            return answer(action='queue', profile={'party_size': 2})
        return answer(route_variant='nine', start_introduction=True)
    monkeypatch.setattr(service, 'run_agent', model)
    with session_factory() as db:
        row = create(db, monkeypatch)
        step(db, row); assert not events
        step(db, row); assert len(events) == 1
        step(db, row)
        service.add_message(db, row, '我們兩位，住宿有獨立衛浴嗎？', 'q2'); db.commit()
        step(db, row, 0)
        assert row.memory['party_size'] == 2
        step(db, row)
        assert events[-1]['event'] == 'introduction_completed'
        assert events[-1]['buffered_questions'][0]['id'] == 'q2'
        step(db, row)
        assert delivered(row) == ['配置开场一', '配置开场二', '完整行程图', '住宿原文', '有獨立衛浴。您們預計幾月出發呢？']
        assert service.date(service.state(row)['next_check_at']) - service.date(row.virtual_now) == timedelta(minutes=3)
        assert not service.state(row)['buffered_questions']


def test_completion_event_even_without_questions_and_exact_timer(session_factory, monkeypatch):
    events = []
    def model(c):
        events.append(c['event'])
        if c['event'] == 'customer_message':
            return answer(route_variant='nine', start_introduction=True)
        if c['event'] == 'introduction_completed':
            return answer(messages=[{'text': '請問幾位同行呢？'}], next_check_minutes=3)
        return answer(action='wait', next_check_minutes=5, reason='留时间讨论')
    monkeypatch.setattr(service, 'run_agent', model)
    with session_factory() as db:
        row = create(db, monkeypatch)
        for _ in range(5): step(db, row)
        assert events == ['customer_message', 'introduction_completed']
        step(db, row, 179); assert len(events) == 2
        step(db, row, 1); assert events[-1] == 'silence_due'
        assert service.date(service.state(row)['next_check_at']) - service.date(row.virtual_now) == timedelta(minutes=5)
        step(db, row, 299); assert len(events) == 3
        step(db, row, 1); assert len(events) == 4


def test_interrupt_cancels_remaining_intro_and_handoff_is_terminal(session_factory, monkeypatch):
    monkeypatch.setattr(service, 'run_agent', lambda c: answer(route_variant='nine', start_introduction=True))
    with session_factory() as db:
        row = create(db, monkeypatch)
        step(db, row); step(db, row); step(db, row)
        monkeypatch.setattr(service, 'run_agent', lambda c: answer(action='handoff', interrupt=True, handoff_reason='客户要求真人'))
        service.add_message(db, row, '請真人接手', 'human'); db.commit()
        step(db, row, 0)
        assert '住宿原文' not in delivered(row)
        assert not any(m.get('status') == 'draft' for m in row.messages)
        assert service.state(row)['handoff']
        service.add_message(db, row, '还有问题', 'after'); db.commit()
        assert not service.state(row)['pending_event']


def test_new_message_supersedes_model_output(session_factory, monkeypatch):
    with session_factory() as db:
        row = create(db, monkeypatch)
        def model(c):
            service.add_message(db, row, '改成十一日', 'new'); db.commit()
            return answer(messages=[{'text': '过期回复'}])
        monkeypatch.setattr(service, 'run_agent', model)
        step(db, row); step(db, row)
        assert not any(m.get('content') == '过期回复' for m in row.messages)
        run = db.scalar(select(AutomationRun))
        assert run.status == 'cancelled'
        assert service.state(row)['pending_event'] == 'customer_message'


def test_optout_allows_passive_question_without_restoring_timer(session_factory, monkeypatch):
    monkeypatch.setattr(service, 'run_agent', lambda c: answer(opt_out=True, next_check_minutes=3, messages=[{'text': '好的'}]))
    with session_factory() as db:
        row = create(db, monkeypatch)
        for _ in range(3): step(db, row)
        monkeypatch.setattr(service, 'run_agent', lambda c: answer(opt_out=False, next_check_minutes=3, messages=[{'text': '机票不包含'}]))
        service.add_message(db, row, '机票包含吗', 'passive'); db.commit()
        step(db, row); step(db, row)
        assert '机票不包含' in delivered(row)
        assert service.state(row)['opt_out']
        assert not service.state(row).get('next_check_at')


def test_restart_pause_and_no_burst(session_factory, monkeypatch):
    monkeypatch.setattr(service, 'run_agent', lambda c: answer(route_variant='nine', start_introduction=True))
    with session_factory() as db:
        row = create(db, monkeypatch)
        step(db, row); step(db, row)
        sid = row.id
    with session_factory() as db:
        row = db.get(AutomationSession, sid)
        service.control(row, 'pause'); db.commit()
        step(db, row, 180); assert delivered(row) == ['配置开场一', '配置开场二']
        service.control(row, 'resume'); db.commit()
        step(db, row, 180); assert delivered(row)[-1] == '完整行程图'
        assert '住宿原文' not in delivered(row)


def test_api_v3_isolated_and_legacy_worker_does_not_consume(authenticated, session_factory, monkeypatch):
    from app.automation_service import queue_passive, process_automation_run, advance_running_playgrounds
    client, csrf = authenticated
    monkeypatch.setattr(service, 'compile_skills', lambda db: bundle())
    response = client.post('/v1/playground/sessions', json={'engine_version': 'v3', 'mode': 'journey'}, headers={'X-CSRF-Token': csrf})
    assert response.status_code == 201, response.text
    data = response.json()
    assert data['engine_release_id'].startswith('reception-v3-')
    with session_factory() as db:
        assert not queue_passive(db)
        assert not process_automation_run(db)
        assert not advance_running_playgrounds(db)
    bad = client.post('/v1/playground/sessions', json={'engine_version': 'v3', 'mode': 'reply'}, headers={'X-CSRF-Token': csrf})
    assert bad.status_code == 422


def test_default_configured_timer_runs_without_model_delay(session_factory, monkeypatch):
    config = bundle()
    config['silence']['intervals_minutes'] = [3, 5]
    events = []
    def model(c):
        events.append(c['event'])
        return answer(action='wait' if c['event'] == 'silence_due' else 'reply',
                      messages=[] if c['event'] == 'silence_due' else [{'text': '請問幾位呢？'}])
    monkeypatch.setattr(service, 'run_agent', model)
    with session_factory() as db:
        row = create(db, monkeypatch)
        monkeypatch.setattr(service, 'compile_skills', lambda db: deepcopy(config))
        for _ in range(3): step(db, row)
        step(db, row, 179); assert events == ['customer_message']
        step(db, row, 1); assert events[-1] == 'silence_due'
        assert service.date(service.state(row)['next_check_at']) - service.date(row.virtual_now) == timedelta(minutes=5)
        step(db, row, 300)
        assert events == ['customer_message', 'silence_due', 'silence_due']
        assert not service.state(row)['next_check_at']


def test_explicit_model_stop_ends_configured_timers(session_factory, monkeypatch):
    config = bundle()
    config['silence']['intervals_minutes'] = [3, 5]
    monkeypatch.setattr(service, 'run_agent', lambda c: answer(action='wait', stop_followup=True))
    with session_factory() as db:
        row = create(db, monkeypatch)
        monkeypatch.setattr(service, 'compile_skills', lambda db: deepcopy(config))
        step(db, row); step(db, row)
        assert not service.state(row)['next_check_at']


def test_model_text_is_unchanged_even_when_reference_is_only_a_hint(session_factory, monkeypatch):
    monkeypatch.setattr(service, 'run_agent', lambda c: answer(messages=[{'text': '完整原稿，不裁剪。', 'script_id': 'not-a-script'}]))
    with session_factory() as db:
        row = create(db, monkeypatch)
        for _ in range(3): step(db, row)
        assert delivered(row)[-1] == '完整原稿，不裁剪。'
