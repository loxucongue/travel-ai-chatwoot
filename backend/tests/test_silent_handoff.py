"""Handoff must never send a customer-facing acknowledgement, even this turn."""
import pytest
from sqlalchemy import select

from app.automation_models import AutomationRun
from app.models import ConversationState, HandoffTask, Notification, OutboundMessage
from app.reception_v3 import live, service
from app.reception_v3.runtime import Decision
from live_fixture import setup
from test_live_audit_fixes import session
from test_reception_v3 import bundle, create


def run_for(db, row):
    run = AutomationRun(session_id=row.id, module='reception_v3', generation=row.generation,
                        idempotency_key=f'test:{row.id}', input_snapshot={'event': 'customer_message'})
    db.add(run)
    db.flush()
    return run


@pytest.mark.parametrize('reason', ['knowledge', 'requested', 'party_size', 'trip_days',
                                   'custom', 'contact', 'contact_image', 'contact_reported', 'other'])
def test_live_handoff_is_silent_immediate_and_idempotent(session_factory, monkeypatch, reason):
    setup(session_factory, monkeypatch)
    monkeypatch.setattr(live, 'client_for', lambda *a: pytest.fail('no public channel call allowed'))
    with session_factory() as db:
        row = session(db)
        value = service.state(row)
        service.append_plan(row, value, [{'content': '旧介绍', 'content_type': 'text'}], 'introduction', 0)
        value.update(next_check_at=service.later(row.virtual_now, 600), intake_deadline=row.virtual_now)
        service.save(row, value)
        run = run_for(db, row)
        decision = Decision(action='handoff', handoff_type=reason, handoff_reason='需要顾问查看',
            start_introduction=True, messages=[{'text': '圖片我收到了', 'script_id': 'missing',
                                              'asset_keys': ['missing-image']}]).model_dump()
        # Invalid outgoing references must not prevent the internal handoff.
        service.apply_decision(db, row, run, bundle(), decision)
        db.commit()
        task = db.scalar(select(HandoffTask))
        assert task is not None and task.reason_code == reason
        assert db.scalar(select(Notification)).event_type == 'handoff.created'
        assert live.human_owned(db, db.get(ConversationState, 1))
        assert row.controls['simulation']['status'] == 'stopped'
        assert row.controls['human'] and service.state(row)['handoff_created']
        assert not any(m.get('status') in ('draft', 'submitted') for m in row.messages)
        assert all(service.state(row).get(k) is None for k in
                   ['pending_event', 'intake_deadline', 'next_check_at', 'delivery_due_at'])
        assert run.trace['suppressed_handoff_messages'] == 1
        live.deliver_one(db, row)
        live.deliver_one(db, row)
        assert len(db.scalars(select(HandoffTask)).all()) == 1
        assert len(db.scalars(select(Notification)).all()) == 1
        assert db.scalar(select(OutboundMessage)) is None


@pytest.mark.parametrize('status', ['running', 'stopped'])
def test_pre_release_handoff_draft_never_sends(session_factory, monkeypatch, status):
    setup(session_factory, monkeypatch)
    monkeypatch.setattr(live, 'client_for', lambda *a: pytest.fail('must not contact customer'))
    with session_factory() as db:
        row = session(db, status)
        value = service.state(row)
        service.append_plan(row, value, [{'content': '旧转接话术', 'content_type': 'text'}], 'reply', 0)
        value.update(handoff=True, handoff_type='knowledge', handoff_reason='查看图片')
        row.controls = {**row.controls, 'human': True}
        service.save(row, value)
        db.commit()
        assert live.human_owned(db, db.get(ConversationState, 1))
        live.deliver_one(db, row)
        assert row.messages[0]['status'] == 'cancelled'
        assert db.scalar(select(HandoffTask)).reason_code == 'knowledge'
        assert db.scalar(select(OutboundMessage)) is None


def test_playground_same_turn_handoff_suppresses_text_and_images(session_factory, monkeypatch):
    with session_factory() as db:
        row = create(db, monkeypatch)
        service.apply_decision(db, row, run_for(db, row), bundle(), Decision(action='handoff',
            messages=[{'text': '稍等', 'asset_keys': ['missing']}]).model_dump())
        service.advance(row, wall_now=service.later(row.controls['simulation']['last_wall_at'], 3600))
        assert not any(m.get('status') in ('draft', 'simulated_delivered') for m in row.messages)
        assert service.state(row)['handoff']


def test_system_handoff_cancels_queued_messages(session_factory, monkeypatch):
    setup(session_factory, monkeypatch)
    with session_factory() as db:
        row = session(db)
        value = service.state(row)
        service.append_plan(row, value, [{'content': '待发内容', 'content_type': 'text'}], 'reply', 0)
        service.save(row, value)
        live.handoff(db, row, 'model_failed', '模型失败')
        assert row.messages[0]['status'] == 'cancelled'
        assert row.controls['simulation']['status'] == 'stopped'
        assert service.state(row)['handoff_created']
