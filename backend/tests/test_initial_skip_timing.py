from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

import app.automation_service as rehearsal
import app.live_sop as live
from app.automation_models import AutomationSession, RehearsalEnrollment, RehearsalJob, SopVersion, LiveSopJob, LiveSopEnrollment
from app.models import ConversationState, OutboundMessage, StoredMedia
from test_live_sop import AT, setup, enroll


@pytest.mark.parametrize("engine", ["live", "rehearsal"])
@pytest.mark.parametrize("automatic,initial", [(True, True), (False, True), (True, False)])
@pytest.mark.parametrize("prior_age", [0, 10])
def test_first_fixed_node_waits_after_actual_reply_only(session_factory, monkeypatch, engine, automatic, initial, prior_age):
    from app.route_reply import route_snapshot_from_values, make_route_snapshot, ROUTE_SNAPSHOTS_KEY
    from app.models import ConversationJourney
    fake, _sop_id, version_id = setup(session_factory, monkeypatch)
    prior_at = rehearsal.iso(rehearsal.dt(AT) - timedelta(seconds=prior_age))
    with session_factory() as db:
        version = db.get(SopVersion, version_id)
        node = {"key": "first", "initial_delivery": initial, "schedule_type": "relative", "basis": "enrollment",
                "delay_minutes": 0 if initial else 1, "messages": [{"key": "text", "content_type": "text", "content": "fixed text"}]}
        version.config = {**version.config, "nodes": [node]}
        journey = db.scalar(select(ConversationJourney))
        spec = route_snapshot_from_values(journey.route_variant, journey.slots)
        spec = {**spec, "initial_delivery_interval_seconds": 2, "sop": {**spec["sop"], "nodes": [node]}}
        journey.slots = {ROUTE_SNAPSHOTS_KEY: {journey.route_variant: make_route_snapshot(journey.route_variant, spec)}}
        if engine == "live":
            db.add(OutboundMessage(conversation_state_id=1, idempotency_key="actual-answer", status="submitted",
                                   content="direct answer", submitted_at=prior_at))
            db.commit()
            row = live.enroll_live_sop(db, db.get(ConversationState, 1), version, request_key="first-gap",
                                      trigger_source="passive_route" if automatic else "manual_test")
            job = db.scalar(select(LiveSopJob).where(LiveSopJob.enrollment_id == row.id))
        else:
            session = AutomationSession(owner_id=1, inbox_binding_id=1, virtual_now=AT,
                controls={"route_variant": journey.route_variant, "journey": {"slots": journey.slots}}, messages=[
                    {"id": "direct", "direction": "outgoing", "status": "simulated_delivered", "content": "direct answer", "created_at": prior_at}])
            db.add(session)
            db.commit()
            row = rehearsal.enroll_rehearsal(db, session, version, source="model_route" if automatic else "manual")
            job = db.scalar(select(RehearsalJob).where(RehearsalJob.enrollment_id == row.id))
        expected = 60 if not initial else 2 if automatic and prior_age == 0 else 0
        assert rehearsal.dt(job.scheduled_at) - rehearsal.dt(AT) == timedelta(seconds=expected)
        db.commit()
        if initial and automatic and prior_age == 0:
            if engine == "live":
                assert not live.process_due_live_sop()
                monkeypatch.setattr(live, "utcnow", lambda: rehearsal.iso(rehearsal.dt(AT) + timedelta(seconds=2)))
                assert live.process_due_live_sop()
                assert fake.sent == [("text", "fixed text")]
            else:
                monkeypatch.setattr(rehearsal, "gate", lambda *_args: None)
                monkeypatch.setattr(rehearsal, "reserve_touch", lambda *_args, **_kwargs: True)
                rehearsal.advance_sops(db, session)
                assert len(session.messages) == 1
                session.virtual_now = rehearsal.iso(rehearsal.dt(AT) + timedelta(seconds=2))
                rehearsal.advance_sops(db, session)
                assert session.messages[-1]["content"] == "fixed text"
                assert rehearsal.dt(session.messages[-1]["created_at"]) - rehearsal.dt(prior_at) == timedelta(seconds=2)


@pytest.mark.parametrize("source,initial", [("model_route", True), ("manual", True), ("model_route", False)])
def test_rehearsal_duplicate_chain_has_no_empty_initial_gaps(session_factory, monkeypatch, source, initial):
    _fake, sop_id, version_id = setup(session_factory, monkeypatch)
    monkeypatch.setattr(rehearsal, "gate", lambda *_args: None)
    monkeypatch.setattr(rehearsal, "media_error", lambda *_args: None)
    monkeypatch.setattr(rehearsal, "reserve_touch", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(rehearsal, "record_session_content_delivery", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(rehearsal, "material_info", lambda _db, item, *_args: {
        "media_id": item["media_id"], "media_hash": str(item["media_id"]), "asset_key": str(item["media_id"]),
    })
    monkeypatch.setattr(rehearsal, "previous_delivery", lambda _db, _subject, info, **_kwargs: SimpleNamespace(id=info["media_id"]))
    with session_factory() as db:
        session = AutomationSession(owner_id=1, inbox_binding_id=1, virtual_now=AT, controls={}, messages=[
            {"id": "prior-text", "direction": "outgoing", "status": "simulated_delivered", "created_at": AT, "content": "previous"}])
        db.add(session)
        db.flush()
        enrollment = RehearsalEnrollment(session_id=session.id, sop_id=sop_id, sop_version_id=version_id,
            subject_key="test", generation=0, trigger_source=source, enrolled_at=AT,
            expires_at=rehearsal.iso(rehearsal.dt(AT) + timedelta(hours=24)))
        db.add(enrollment)
        db.flush()
        previous = RehearsalJob(enrollment_id=enrollment.id, node_key="prior", status="simulated_delivered",
            confirmed_at=AT, scheduled_at=AT, payload={"initial_delivery": initial})
        db.add(previous)
        db.flush()
        jobs = []
        for index in range(3):
            item = {"key": "image", "content_type": "image", "media_id": index + 11} if index < 2 else {
                "key": "text", "content_type": "text", "content": "next visible"}
            job = RehearsalJob(enrollment_id=enrollment.id, node_key=f"part-{index}", predecessor_id=previous.id,
                status="waiting_dependency", payload={"initial_delivery": initial, "schedule_type": "relative",
                    "basis": "previous_node", "delay_seconds": 2, "messages": [item]})
            db.add(job)
            db.flush()
            jobs.append(job)
            previous = job
        session.virtual_now = rehearsal.iso(rehearsal.dt(AT) + timedelta(seconds=2))
        rehearsal.advance_sops(db, session)
        compact = source == "model_route" and initial
        if not compact:
            for seconds in (4, 6):
                session.virtual_now = rehearsal.iso(rehearsal.dt(AT) + timedelta(seconds=seconds))
                rehearsal.advance_sops(db, session)
        assert [j.status for j in jobs] == ["already_provided", "already_provided", "simulated_delivered"]
        assert jobs[0].payload["reused_delivery_ids"] == [11]
        assert jobs[1].payload["reused_delivery_ids"] == [12]
        assert len(session.messages) == 2
        assert rehearsal.dt(session.messages[-1]["created_at"]) - rehearsal.dt(AT) == timedelta(seconds=2 if compact else 6)


@pytest.mark.parametrize("source,initial", [("passive_route", True), ("manual_test", True), ("passive_route", False)])
def test_live_duplicate_chain_anchors_to_last_visible_message(session_factory, monkeypatch, source, initial):
    fake, _sop_id, version_id = setup(session_factory, monkeypatch)
    enrollment_id = enroll(session_factory, version_id)
    with session_factory() as db:
        enrollment = db.get(LiveSopEnrollment, enrollment_id)
        enrollment.trigger_source = source
        first = db.scalar(select(LiveSopJob))
        first.status, first.confirmed_at = "submitted", AT
        db.add(OutboundMessage(conversation_state_id=1, idempotency_key="visible", status="submitted", content="previous", submitted_at=AT))
        media = StoredMedia(tenant_id=1, original_name="old.jpg", media_type="image", mime_type="image/jpeg",
                            file_size=1, storage_path="unused.jpg", created_by=1)
        db.add(media)
        db.flush()
        old = OutboundMessage(conversation_state_id=1, idempotency_key="old-image", status="sent", content="",
            media_id=media.id, submitted_at=rehearsal.iso(rehearsal.dt(AT) - timedelta(seconds=20)),
            content_attributes={"_delivery_item": {"asset_hash": "old"}})
        db.add(old)
        previous = first
        jobs = []
        for index in range(3):
            item = {"key": "image", "content_type": "image", "media_id": media.id} if index < 2 else {
                "key": "text", "content_type": "text", "content": "next visible"}
            job = LiveSopJob(enrollment_id=enrollment.id, node_key=f"skip-{index}", predecessor_id=previous.id,
                status="waiting_dependency", payload={"initial_delivery": initial, "schedule_type": "relative",
                    "basis": "previous_node", "delay_seconds": 2, "messages": [item]})
            db.add(job)
            db.flush()
            jobs.append(job)
            previous = job
        db.commit()
        live._advance_after_terminal(db, first, enrollment)
        monkeypatch.setattr(live, "_material", lambda *_args: ({"media_id": media.id, "media_hash": "old"}, media))
        state = db.get(ConversationState, 1)
        version = db.get(SopVersion, version_id)
        compact = source == "passive_route" and initial
        for index, job in enumerate(jobs):
            now = rehearsal.iso(rehearsal.dt(AT) + timedelta(seconds=2 if compact else (index + 1) * 2))
            monkeypatch.setattr(live, "utcnow", lambda at=now: at)
            assert rehearsal.dt(job.scheduled_at) <= rehearsal.dt(now)
            live._deliver_static_live_job(db, fake, state, enrollment, version, job)
        assert [job.status for job in jobs] == ["already_provided", "already_provided", "submitted"]
        assert jobs[0].payload["reused_outbound_ids"] == [old.id]
        assert fake.sent == [("text", "next visible")]
        assert rehearsal.dt(jobs[-1].confirmed_at) - rehearsal.dt(AT) == timedelta(seconds=2 if compact else 6)
