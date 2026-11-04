"""Replies are model-owned; execution/protocol errors are retried separately."""
from sqlalchemy import select
from app.automation_models import AutomationRun
from app.reception_v3 import runtime, service
from app.reception_v3.route_understanding import DecisionContractError
from test_reception_v3 import create, step
from test_reception_v3_skills import context, model, final


def test_mixed_winter_price_and_voice_survive_delivery(session_factory, monkeypatch):
    text = '普通團期每人9,980元；2027/2/5每人10,980元。您比較想哪天？我可以請顧問確認。'
    requests = model(monkeypatch, [final(route_variant='peach_9d_2027', messages=[{'text': text}])])
    c = context('peach_9d_2027')
    decision, _, _ = runtime.run_agent(c)
    with session_factory() as db:
        row = create(db, monkeypatch)
        _, parts = service.parts_for(db, row, c['skills'], decision)
        value = service.state(row)
        service.append_plan(row, value, parts, 'reply', 2, 123)
        assert [m['content'] for m in row.messages if m.get('run_id') == 123] == [text]
    assert len(requests) == 1


def test_protocol_failure_retries_then_recovers_without_handoff(session_factory, monkeypatch):
    calls = []
    def respond(c):
        calls.append(c)
        if len(calls) == 1:
            raise DecisionContractError('invalid_decision_protocol')
        return runtime.Decision(messages=[{'text':'兩位每人9,980元。'}]).model_dump(), [], 'test'
    monkeypatch.setattr(service, 'run_agent', respond)
    with session_factory() as db:
        row = create(db, monkeypatch)
        step(db, row); step(db, row)
        assert not service.state(row).get('handoff')
        assert service.state(row)['retry_at']
        # Retry clock uses wall time, independent of the simulator clock.
        value = service.state(row); value['retry_at'] = '2000-01-01T00:00:00+00:00'
        service.save(row, value); row.due_at = value['retry_at']; db.commit()
        step(db, row)
        assert len(calls) == 2 and not service.state(row).get('handoff')
        runs = list(db.scalars(select(AutomationRun).where(AutomationRun.session_id == row.id)))
        assert runs[-1].status == 'completed'
        assert calls[1]['state']['last_error'] == 'invalid_decision_protocol'


def test_optional_followup_is_not_a_second_model_review(monkeypatch):
    c = context('peach_9d_2027'); c['state'] = {'human_followup': True}
    text = '您們日期商量好了嗎？'
    requests = model(monkeypatch, [final(messages=[{'text':text}])])
    decision, _, _ = runtime.run_agent(c)
    assert len(requests) == 1 and decision['messages'][0]['text'] == text
