import pytest
from sqlalchemy import select
from app.automation_models import AutomationRun
from app.model_transport import EvaluationCallError
from app.reception_v3 import runtime, service
from test_reception_v3 import create, step
from test_reception_v3_skills import context

def test_overflow_stops_before_transport_and_records_sizes(monkeypatch):
    monkeypatch.setattr(runtime.settings, 'deepseek_max_input_characters', 1)
    monkeypatch.setattr(runtime, '_post_with_deadline', lambda *a: pytest.fail('must not call provider'))
    with pytest.raises(EvaluationCallError, match='v3_context_too_large') as error:
        runtime.run_agent(context('peach_9d_2027'))
    budget = next(log for log in error.value.logs if log.get('node') == 'context_budget')
    assert budget['input_characters'] > budget['limit'] == 1
    assert budget['task_field_characters'] and budget['message_characters']

def test_oversized_task_has_one_terminal_attempt(session_factory, monkeypatch):
    def oversized(ctx):
        raise EvaluationCallError('v3_context_too_large', [], 'test')
    monkeypatch.setattr(service, 'run_agent', oversized)
    with session_factory() as db:
        row = create(db, monkeypatch)
        step(db, row); step(db, row); step(db, row)
        runs = list(db.scalars(select(AutomationRun).where(AutomationRun.session_id == row.id)))
        assert len(runs) == 1
        assert runs[0].error_code == 'v3_context_too_large'
        assert service.state(row)['handoff']
        assert service.state(row)['attempts'] == 1
