import httpx
import pytest
from sqlalchemy import select
from app.automation_models import AutomationRun
from app.reception_v3 import service
from test_reception_v3 import create, step, answer


def outage(kind):
    if kind == 'timeout':
        return TimeoutError('provider_timeout')
    request = httpx.Request('POST', 'https://provider.invalid/chat/completions')
    return httpx.HTTPStatusError('unavailable', request=request, response=httpx.Response(503, request=request))


@pytest.mark.parametrize('kind', ['timeout', 'http503'])
def test_optional_followup_outage_defers_without_handoff_and_new_question_works(session_factory, monkeypatch, kind):
    def broken(context):
        raise outage(kind)
    monkeypatch.setattr(service, 'run_agent', broken)
    with session_factory() as db:
        row = create(db, monkeypatch)
        row.messages = [m for m in row.messages if m.get('direction') == 'incoming']
        value = service.state(row)
        value.update(opening_started=True, delivery_kind=None, delivery_due_at=None, model_not_before=None,
                     pending_event='silence_due', buffered_questions=[])
        service.save(row, value)
        db.commit()
        step(db, row, 0)
        value = service.state(row)
        assert not value.get('handoff') and not row.controls.get('human')
        assert value['pending_event'] is None and not value.get('retry_at')
        assert value['followup_skip_reason'] == 'model_transport_unavailable'
        assert row.controls['simulation']['status'] == 'running'
        assert not value.get('attempts') and not value.get('failed_event')
        assert value['next_check_at'] is None or service.date(value['next_check_at']) > service.date(row.virtual_now)
        assert db.scalar(select(AutomationRun)).trace['failure_kind'] == 'optional_outreach_transport'
        service.add_message(db, row, 'How much for two people?', 'new-customer')
        db.commit()
        monkeypatch.setattr(service, 'run_agent', lambda c: answer(messages=[{'text': 'Here is the answer.'}], stop_followup=True))
        step(db, row)
        step(db, row)
        assert any(m.get('content') == 'Here is the answer.' for m in row.messages)
        assert not service.state(row).get('handoff')


@pytest.mark.parametrize('event', ['customer_message', 'silence_due', 'introduction_completed'])
def test_outage_does_not_silently_drop_buffered_customer_questions(session_factory, monkeypatch, event):
    def broken(context):
        raise outage('http503')
    monkeypatch.setattr(service, 'run_agent', broken)
    with session_factory() as db:
        row = create(db, monkeypatch)
        row.messages = [m for m in row.messages if m.get('direction') == 'incoming']
        value = service.state(row)
        value.update(opening_started=True, delivery_kind=None, delivery_due_at=None, model_not_before=None,
                     pending_event=event, buffered_questions=[row.messages[0]['id']])
        service.save(row, value)
        db.commit()
        step(db, row, 0)
        value = service.state(row)
        assert value['buffered_questions'] and value['pending_event'] == event
        assert value['attempts'] == 1 and value['retry_at']
        assert value.get('followup_skip_reason') != 'model_transport_unavailable'


def test_live_followup_outage_creates_no_handoff_or_advisor_notification(session_factory, monkeypatch):
    from copy import deepcopy
    from live_fixture import setup
    from test_live_audit_fixes import session
    from test_reception_v3 import bundle
    from app.models import HandoffTask, Notification, OutboundMessage, utcnow
    from app.reception_v3 import live
    setup(session_factory, monkeypatch)
    spec = bundle()
    spec['silence'].update(live_enabled=True, daily_at='09:00')
    monkeypatch.setattr(service, 'compile_skills', lambda db: deepcopy(spec))
    monkeypatch.setattr(service, 'active_web_facts', lambda *a, **k: ([], 'test'))
    monkeypatch.setattr(service, 'candidate_materials', lambda *a: [])
    monkeypatch.setattr(live, 'permitted', lambda *a, **k: True)
    def broken(context):
        raise outage('http503')
    monkeypatch.setattr(service, 'run_agent', broken)
    with session_factory() as db:
        row = session(db)
        value = service.state(row)
        value.update(pending_event='silence_due', buffered_questions=[], followup_entry_at=utcnow())
        service.save(row, value)
        db.commit()
        assert service.tick(db, session_id=row.id, environment='live', advance_clock=False)
        assert db.scalar(select(HandoffTask)) is None
        assert db.scalar(select(Notification)) is None
        assert db.scalar(select(OutboundMessage)) is None
        assert row.controls['simulation']['status'] == 'running'
        assert service.state(row)['next_check_at'] > row.virtual_now
