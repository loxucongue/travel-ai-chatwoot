from copy import deepcopy
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select

from scripts import migrate_delivery_consistency as migration
from app.automation_models import LiveSopEnrollment, LiveSopJob
from app.live_reply_models import LiveReplyJob
from app.models import (
    AppSetting, ConversationJourney, ConversationState, HandoffTask, MessageEvent,
    OutboundMessage, utcnow,
)
from app.outbound_control import GLOBAL_MESSAGE_SETTING_KEY
from app.route_reply import CONTENT_PROGRESS_KEY, prepare_route_reply_values


@pytest.fixture
def legacy(session_factory):
    with session_factory() as db:
        db.add(AppSetting(key=GLOBAL_MESSAGE_SETTING_KEY, value={"enabled": False}))
        state = ConversationState(tenant_id=1, inbox_binding_id=1, chatwoot_conversation_id=123,
                                  last_message="private customer text")
        db.add(state)
        db.flush()
        journey = ConversationJourney(conversation_state_id=state.id, route_variant="peach_9d_2027",
            slots={"customer": "private", "legacy": [1]}, sent_groups=["hotel_reference"])
        message = MessageEvent(conversation_state_id=state.id, chatwoot_message_id=456,
                               direction="incoming", content="private history")
        db.add_all([journey, message])
        db.flush()
        reply = LiveReplyJob(conversation_state_id=state.id, trigger_message_id=message.id,
                             status="processing", due_at=utcnow(), decision={"text": "private draft"})
        enrollment = LiveSopEnrollment(sop_id=1, sop_version_id=1, conversation_state_id=state.id,
            subject_key="private subject", request_key="request", enrolled_at=utcnow(), expires_at=utcnow())
        out = OutboundMessage(conversation_state_id=state.id, idempotency_key="legacy",
                              content="private outbound", status="submitted", content_attributes={"old": 1})
        db.add_all([reply, enrollment, out])
        db.flush()
        jobs = [LiveSopJob(enrollment_id=enrollment.id, node_key=status, status=status,
                          payload={"text": "private sop"})
                for status in ["scheduled", "waiting_dependency", "processing", "sent"]]
        db.add_all(jobs)
        db.commit()
        yield db, journey, state, reply, enrollment, jobs, message, out


def test_default_dry_run_is_select_only(legacy):
    db, journey, state, reply, enrollment, jobs, message, out = legacy
    db.get(AppSetting, GLOBAL_MESSAGE_SETTING_KEY).value = {"enabled": True}
    db.commit()
    statements = []
    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    event.listen(db.bind, "before_cursor_execute", capture)
    try:
        report = migration.migrate_delivery_consistency(db)
    finally:
        event.remove(db.bind, "before_cursor_execute", capture)
    assert report == {"active_journeys": 1, "review_journeys": 1, "reply_jobs": 1,
                      "sop_enrollments": 1, "sop_jobs": 3, "handoffs_created": 1, "handoffs_reused": 0}
    assert all(statement.lstrip().upper().startswith("SELECT") for statement in statements)
    assert not db.new and not db.dirty
    assert not db.scalars(select(HandoffTask)).all()


def test_apply_requires_disabled_send(legacy):
    db = legacy[0]
    db.get(AppSetting, GLOBAL_MESSAGE_SETTING_KEY).value = {"enabled": True}
    db.commit()
    with pytest.raises(ValueError, match="global_message_sending_must_be_disabled"):
        migration.migrate_delivery_consistency(db, apply=True)
    assert not db.dirty and not db.new


def test_apply_preserves_history_and_is_idempotent(legacy):
    db, journey, state, reply, enrollment, jobs, message, out = legacy
    def values(row):
        return {column.name: deepcopy(getattr(row, column.name)) for column in row.__table__.columns}
    preserved = [journey, message, out]
    before = [values(row) for row in preserved]
    report = migration.migrate_delivery_consistency(db, apply=True)
    db.commit()
    assert report["review_journeys"] == 1
    assert [values(row) for row in preserved] == before
    assert state.last_message == "private customer text"
    assert reply.status == "blocked" and reply.error_code == migration.REASON
    assert reply.decision == {"text": "private draft"}
    assert enrollment.status == "attention_required"
    assert [job.status for job in jobs] == ["blocked", "blocked", "blocked", "sent"]
    assert all(job.payload == {"text": "private sop"} for job in jobs)
    handoff = db.scalar(select(HandoffTask))
    assert handoff.reason_code == migration.REASON
    assert state.effective_ai_state == "HUMAN_HANDOFF"
    again = migration.migrate_delivery_consistency(db, apply=True)
    assert all(count == 0 for count in again.values())
    assert len(db.scalars(select(HandoffTask)).all()) == 1
    assert not db.dirty and not db.new


@pytest.mark.parametrize("kind", ["verified", "unknown", "corrupt", "unselected", "inactive"])
def test_scope_and_verified_history(legacy, kind):
    db, journey, state, reply, enrollment, *_ = legacy
    _, prepared = prepare_route_reply_values(SimpleNamespace(route_variant=journey.route_variant))
    journey.slots = prepared["slots"]
    journey.sent_groups = []
    if kind == "unknown":
        journey.slots = {**journey.slots, CONTENT_PROGRESS_KEY: {journey.route_variant: {
            "hotel_reference": {"schema_version": 2, "history_unknown": True}}}}
    elif kind == "corrupt":
        journey.slots = {"_route_snapshots": []}
    elif kind == "unselected":
        journey.route_variant = ""
    elif kind == "inactive":
        journey.slots = {}
        reply.status = "sent"
        enrollment.status = "completed"
    db.commit()
    report = migration.migrate_delivery_consistency(db, apply=True)
    assert report["review_journeys"] == (1 if kind in {"unknown", "corrupt"} else 0)


def test_existing_handoff_notes_preserved(legacy):
    db, journey, state, *_ = legacy
    handoff = HandoffTask(conversation_state_id=state.id, status="claimed",
                          reason_code="manual", reason_detail="operator notes")
    db.add(handoff)
    db.commit()
    report = migration.migrate_delivery_consistency(db, apply=True)
    db.commit()
    assert report["handoffs_reused"] == 1 and not report["handoffs_created"]
    assert handoff.reason_detail == "operator notes" and handoff.reason_code == "manual"
    assert state.effective_ai_state == "HUMAN_HANDOFF"


def test_cli_counts_only_and_apply_persists(legacy, monkeypatch, capsys):
    db, _, _, reply, *_ = legacy
    from sqlalchemy.orm import sessionmaker
    monkeypatch.setattr(migration, "SessionLocal", sessionmaker(bind=db.bind))
    assert migration.main([]) == 0
    report = json.loads(capsys.readouterr().out)
    assert all(type(value) is int for value in report.values())
    db.refresh(reply)
    assert reply.status == "processing"
    assert migration.main(["--apply"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["review_journeys"] == 1
    db.refresh(reply)
    assert reply.status == "blocked"


def test_cli_errors_do_not_print_database_details(monkeypatch, capsys):
    def fail():
        raise RuntimeError("private database contents")
    monkeypatch.setattr(migration, "SessionLocal", fail)
    assert migration.main([]) == 1
    assert capsys.readouterr().out.strip() == '{"errors": 1}'
