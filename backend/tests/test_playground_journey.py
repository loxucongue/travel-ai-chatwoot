from datetime import timedelta
import hashlib

import pytest
from sqlalchemy import func, select

from app.automation_models import (AutomationRun, AutomationSession, RehearsalEnrollment,
                                   RehearsalJob, SilenceCycle, SopVersion)
from app.automation_service import (
    add_customer_message,
    advance_sops,
    advance_running_playgrounds,
    confirm_draft,
    dt,
    enroll_rehearsal,
    gate,
    iso,
    process_automation_run,
    queue_passive,
    simulation_state,
    sop_snapshot,
    start_journey,
    start_open_journey,
)
from app.deepseek_evaluation import EvaluationCallError, EvaluationDecision
from app.models import InboxBinding, OutboundMessage, SopDefinition, utcnow, StoredMedia, MaterialAsset, KnowledgeVersion
from app.material_library import CATALOG_VERSION, candidate_materials
from app.route_packages import ROUTES, UNCLASSIFIED_SOP_NAME, UNCLASSIFIED_SOP_NODES
from app.route_reply import prepare_route_reply_values, route_snapshot_from_values, make_route_snapshot
from app.route_packages import route_catalog_context


ROUTE = "peach_9d_2027"


def setup_journey(db):
    inbox = InboxBinding(id=1, tenant_id=1, chatwoot_inbox_id=128859, name="Facebook", channel_type="facebook")
    sop = SopDefinition(
        tenant_id=1,
        name="桃花9日演练",
        status="running",
        version=1,
        route_variant=ROUTE,
        inbox_ids=[128859],
        created_by=1,
        nodes=[
            {"key": "first", "schedule_type": "relative", "basis": "enrollment", "delay_minutes": 10,
             "content_group_key": "itinerary_overview", "content_type": "text", "content": "第一条 SOP"},
            {"key": "second", "schedule_type": "relative", "basis": "previous_node", "delay_minutes": 10,
             "content_type": "text", "content": "第二条 SOP"},
        ],
    )
    db.add_all([inbox, sop])
    db.flush()
    version = sop_snapshot(db, sop, 1)
    session = AutomationSession(
        owner_id=1,
        inbox_binding_id=inbox.id,
        mode="journey",
        environment="playground",
        virtual_now="2026-08-28T02:00:00+00:00",
        controls={
            "can_reply": True,
            "ai_enabled": True,
            "channel": "facebook",
            "labels": [],
            "human": False,
            "history_complete": True,
            "route_variant": ROUTE,
        },
    )
    db.add(session)
    db.flush()
    start_journey(db, session, version, duration_minutes=60, speed_multiplier=60, entry_message="想了解桃花9日")
    db.commit()
    return session, version


def decision():
    return EvaluationDecision(
        action="reply",
        branch="peach_9d",
        intent="other",
        reply="AI 演练回复",
        route_variant=ROUTE,
        route_evidence="桃花9日",
    )


def bind_new_route_snapshot(session):
    _, journey = prepare_route_reply_values(decision())
    session.controls = {**session.controls, "journey": journey}
    return route_snapshot_from_values(ROUTE, journey["slots"])


def test_v2_rehearsal_uses_sixty_second_first_silence_without_static_photos(session_factory):
    from app.reception_config import v2_silence_intervals
    with session_factory() as db:
        session, version = setup_journey(db)
        bind_new_route_snapshot(session)
        session.engine_version = "v2"
        for old in db.scalars(select(RehearsalEnrollment).where(RehearsalEnrollment.session_id == session.id)):
            old.status = "completed"
        db.flush()
        enrollment = enroll_rehearsal(db, session, version, source="model_route", reenroll=True,
            request_key="v2-feedback-sixty-seconds", schedule_intervals=v2_silence_intervals())
        jobs = db.scalars(select(RehearsalJob).where(RehearsalJob.enrollment_id == enrollment.id)
                          .order_by(RehearsalJob.id)).all()
        assert jobs[0].node_key == "silence_mainline"
        assert (dt(jobs[0].scheduled_at) - dt(enrollment.enrolled_at)).total_seconds() == 60
        assert all(job.payload.get("journey_trigger") for job in jobs)
        assert db.scalar(select(OutboundMessage)) is None


def test_customer_pause_cancels_followups_and_only_inquiry_resumes(session_factory, monkeypatch):
    with session_factory() as db:
        session, _ = setup_journey(db)
        session.due_at = utcnow()
        db.commit()
        assert queue_passive(db, environment="playground")
        paused = EvaluationDecision(action="no_action", branch="unclassified", intent="other",
                                    safety_flags=["stop_automation", "customer_paused_proactive"])
        monkeypatch.setattr("app.automation_service.generate_decision", lambda _: (paused, [], "hash", {}))
        assert process_automation_run(db, environment="playground")
        db.refresh(session)
        assert session.controls["proactive_pause"]
        assert gate(session, session.virtual_now, True, db) == "customer_paused_proactive"
        assert gate(session, session.virtual_now, False, db) is None
        assert not db.scalar(select(RehearsalEnrollment.id).where(
            RehearsalEnrollment.session_id == session.id, RehearsalEnrollment.status == "active"))
        add_customer_message(db, session, "想再問價格", "resume-question")
        session.due_at = utcnow()
        db.commit()
        assert queue_passive(db, environment="playground")
        monkeypatch.setattr("app.automation_service.generate_decision", lambda _: (decision(), [], "hash", {"customer_questions": ["price"]}))
        resumed_run = db.scalar(select(AutomationRun).where(AutomationRun.session_id == session.id).order_by(AutomationRun.id.desc()))
        assert resumed_run.input_snapshot['customer_text'] == "想再問價格"
        assert process_automation_run(db, environment="playground")
        db.refresh(session)
        assert not session.controls.get("proactive_pause")


def test_playground_handoff_notice_has_persisted_simulated_task(session_factory, monkeypatch):
    with session_factory() as db:
        session, _ = setup_journey(db)
        session.due_at = utcnow()
        db.commit()
        assert queue_passive(db, environment="playground")
        answer = EvaluationDecision(action="handoff", branch="unclassified", intent="price",
            reply="稍等一下，安排專項顧問確認。", handoff_reason="knowledge_confirmation_required")
        monkeypatch.setattr("app.automation_service.generate_decision", lambda _: (
            answer, [], "hash", {"confirmation_questions": ["兩人優惠"]}))
        assert process_automation_run(db, environment="playground")
        db.refresh(session)
        task = session.controls["handoff_tasks"][0]
        assert task["simulated"] and task["questions"] == ["兩人優惠"] and task["status"] == "pending"
        assert session.controls["human"]
        assert any(m.get("content") == answer.reply for m in session.messages)
        assert db.scalar(select(func.count()).select_from(OutboundMessage)) == 0


def test_opening_group_drafts_keep_order_interval_and_last_options(session_factory, monkeypatch):
    with session_factory() as db:
        session, _ = setup_journey(db)
        session.due_at = utcnow()
        db.commit()
        assert queue_passive(db, environment="playground")
        reply = decision()
        reply.opening_messages = ["您好～", "想了解哪條行程呢？"]
        reply.opening_interval_seconds = 3
        reply.reply_options = ["桃花9日", "桃花+珠峰11日"]
        monkeypatch.setattr("app.automation_service.generate_decision", lambda _: (reply, [], "hash", {}))
        assert process_automation_run(db, environment="playground")
        db.refresh(session)
        drafts = [item for item in session.messages if item.get("status") == "draft"]
        assert [item["content"] for item in drafts] == reply.opening_messages
        assert [item["content_type"] for item in drafts] == ["text", "input_select"]
        assert dt(drafts[1]["created_at"]) - dt(drafts[0]["created_at"]) == timedelta(seconds=3)
        assert not drafts[0]["content_attributes"]
        assert len(drafts[-1]["content_attributes"]["items"]) == 2


def test_deferred_question_is_not_marked_by_first_reply(session_factory, monkeypatch):
    with session_factory() as db:
        session, _ = setup_journey(db)
        bind_new_route_snapshot(session)
        session.due_at = utcnow()
        db.commit()
        assert queue_passive(db, environment="playground")
        reply = decision()
        reply.reply = "先把行程傳給您。這次幾位呢？"
        reply.reply_body = "先把行程傳給您。"
        reply.follow_up_type = "slot"
        reply.follow_up_field = "party_size"
        reply.follow_up_question = "這次幾位呢？"
        reply.covered_content_groups = ["advisor_greeting", "party_question"]
        monkeypatch.setattr("app.automation_service.generate_decision", lambda _: (reply, [], "hash", {}))
        assert process_automation_run(db, environment="playground")
        db.refresh(session)
        draft = next(item for item in session.messages if item.get("status") == "draft")
        assert draft["content"] == reply.reply_body
        confirm_draft(db, session, str(draft["id"]))
        covered = session.controls["journey"]["sent_content_groups"]
        assert "advisor_greeting" not in covered
        assert "party_question" not in covered
        progress = session.controls["journey"]["slots"]["_content_progress"][ROUTE]
        assert progress["advisor_greeting"]["topic_covered"]
        assert not progress["advisor_greeting"]["text_delivered"]
        assert "party_question" not in progress


def test_passive_batch_claim_rejects_stale_worker(session_factory, monkeypatch):
    with session_factory() as db:
        session, _ = setup_journey(db)
        session.due_at = utcnow()
        db.commit()
        sid = session.id
        with session_factory() as contender:
            stale = contender.get(AutomationSession, sid)
            assert queue_passive(db, environment="playground")
            original_scalar = contender.scalar

            def stale_selection(statement, *args, **kwargs):
                monkeypatch.setattr(contender, "scalar", original_scalar)
                return stale

            monkeypatch.setattr(contender, "scalar", stale_selection)
            assert not queue_passive(contender, environment="playground")
        assert db.scalar(select(func.count()).select_from(AutomationRun)) == 1


def test_scoped_playground_driver_does_not_touch_other_sessions(session_factory):
    with session_factory() as db:
        session, _ = setup_journey(db)
        session.due_at = utcnow()
        db.commit()
        original = (session.due_at, session.virtual_now, dict(session.controls))
        assert not queue_passive(db, environment="playground", session_id=session.id + 1)
        assert not advance_running_playgrounds(db, session_id=session.id + 1)
        db.refresh(session)
        assert (session.due_at, session.virtual_now, session.controls) == original
        assert db.scalar(select(func.count()).select_from(AutomationRun)) == 0


def test_playground_sop_does_not_die_when_accelerated_clock_crosses_contact_hours(session_factory):
    with session_factory() as db:
        session, _ = setup_journey(db)
        after_hours = "2026-08-28T18:00:00+00:00"
        session.virtual_now = after_hours
        session.due_at = None

        assert gate(session, after_hours, proactive=True, db=db) is None

        session.environment = "shadow"
        assert gate(session, after_hours, proactive=True, db=db) == "outside_contact_hours"


def test_unclassified_new_customer_lets_model_choose_route_then_enrolls_sop(session_factory, monkeypatch, tmp_path):
    with session_factory() as db:
        inbox = InboxBinding(id=1, tenant_id=1, chatwoot_inbox_id=128859, name="Facebook", channel_type="facebook")
        sop = SopDefinition(
            tenant_id=1,
            name=ROUTES[ROUTE]["sop"]["name"],
            status="running",
            version=1,
            route_variant=ROUTE,
            inbox_ids=[128859],
            created_by=1,
            nodes=[{"key": "first", "schedule_type": "relative", "basis": "enrollment", "delay_minutes": 10,
                    "content_type": "text", "content": "自动绑定后的 SOP"}],
        )
        legacy_sop = SopDefinition(
            tenant_id=1,
            name="旧图文演练副本",
            status="running",
            version=99,
            route_variant=ROUTE,
            inbox_ids=[128859],
            created_by=1,
            nodes=[{"key": "legacy", "schedule_type": "relative", "basis": "enrollment",
                    "delay_minutes": 1, "content_type": "text", "content": "旧静态话术"}],
        )
        db.add_all([inbox, sop, legacy_sop])
        db.flush()
        sop_snapshot(db, sop, 1)
        sop_snapshot(db, legacy_sop, 1)
        session = AutomationSession(
            owner_id=1,
            inbox_binding_id=inbox.id,
            mode="journey",
            environment="playground",
            virtual_now="2026-08-28T02:00:00+00:00",
            controls={"can_reply": True, "ai_enabled": True, "channel": "facebook", "labels": [],
                      "human": False, "history_complete": True},
        )
        db.add(session)
        db.flush()
        start_open_journey(
            db,
            session,
            duration_minutes=60,
            speed_multiplier=60,
            entry_message="你好，我想咨询旅行行程",
        )
        assert db.scalar(select(RehearsalEnrollment)) is None

        knowledge = KnowledgeVersion(tenant_id=1, version_key=CATALOG_VERSION,
                                     title="Timing fixture", content_hash="a" * 64)
        db.add(knowledge)
        db.flush()
        asset_keys = sorted({key for group in ROUTES[ROUTE]["groups"].values() for key in group["assets"]})
        for index, key in enumerate(asset_keys):
            path = tmp_path / f"route-{index}.png"
            data = f"reviewed fixture {key}".encode()
            path.write_bytes(data)
            media = StoredMedia(tenant_id=1, original_name=path.name, media_type="image",
                                mime_type="image/png", file_size=len(data), storage_path=str(path), created_by=1)
            db.add(media)
            db.flush()
            db.add(MaterialAsset(knowledge_version_id=knowledge.id, asset_key=key,
                                 source_path=str(path), display_name=key, available=True,
                                 file_hash=hashlib.sha256(data).hexdigest(), metadata_json={
                                     "stored_media_id": media.id, "route_variants": [ROUTE],
                                     "content_family": key, "review_state": "evaluation_ready",
                                 }))
        db.flush()
        selected = decision()
        selected.opening_messages = ["Route selected.", "Here are the route details."]
        selected.opening_interval_seconds = 3
        with route_catalog_context(available_materials=candidate_materials(db, 1)):
            selected.bound_route_snapshot = make_route_snapshot(ROUTE, ROUTES[ROUTE])
        assert set(selected.bound_route_snapshot["spec"]["asset_bindings"]) == set(asset_keys)
        session.due_at = utcnow()
        db.commit()
        assert queue_passive(db, environment="playground")
        monkeypatch.setattr("app.automation_service.generate_decision", lambda payload: (selected, [], "hash", {}))
        before_generation = session.virtual_now
        assert process_automation_run(db, environment="playground")
        assert session.virtual_now == before_generation
        state = simulation_state(session)
        assert advance_running_playgrounds(db, iso(dt(state["last_wall_at"]) + timedelta(seconds=1)))
        db.refresh(session)
        parts = [item for item in session.messages if item.get("run_id")]
        assert [item["status"] for item in parts] == ["simulated_delivered", "draft"]
        assert db.scalar(select(RehearsalEnrollment).where(RehearsalEnrollment.session_id == session.id)) is None
        state = simulation_state(session)
        assert advance_running_playgrounds(db, iso(dt(state["last_wall_at"]) + timedelta(seconds=10)))
        assert session.virtual_now == parts[-1]["created_at"]
        state = simulation_state(session)
        assert advance_running_playgrounds(db, iso(dt(state["last_wall_at"]) + timedelta(seconds=1)))
        db.refresh(session)
        assert all(item["status"] == "simulated_delivered" for item in session.messages if item.get("run_id"))
        assert not any(event["event_type"] == "ai_blocked" for event in session.controls["timeline_events"])

        enrollment = db.scalar(select(RehearsalEnrollment).where(RehearsalEnrollment.session_id == session.id))
        assert enrollment is not None
        assert enrollment.status == "active"
        assert session.controls["route_variant"] == ROUTE
        assert simulation_state(session)["sop_name"] == ROUTES[ROUTE]["sop"]["name"]
        assert any(event["event_type"] == "sop_enrolled" for event in session.controls["timeline_events"])
        jobs = db.scalars(select(RehearsalJob).where(RehearsalJob.enrollment_id == enrollment.id)).all()
        silence_jobs = [job for job in jobs if job.payload.get("journey_trigger")]
        assert silence_jobs and silence_jobs[0].payload["delay_minutes"] == 1
        assert any(job.payload.get("initial_delivery") for job in jobs)
        assert db.scalar(select(OutboundMessage)) is None


def test_customer_interrupt_keeps_delivered_part_and_cancels_remaining_drafts(session_factory, monkeypatch):
    with session_factory() as db:
        session, _ = setup_journey(db)
        bind_new_route_snapshot(session)
        reply = decision()
        reply.opening_messages = ["First part.", "Second part.", "Last question?"]
        reply.opening_interval_seconds = 3
        monkeypatch.setattr("app.automation_service.generate_decision", lambda _: (reply, [], "hash", {}))
        session.due_at = utcnow()
        db.commit()
        assert queue_passive(db, environment="playground")
        initial_now = session.virtual_now
        assert process_automation_run(db, environment="playground")
        assert session.virtual_now == initial_now
        drafts = [item for item in session.messages if item.get("status") == "draft"]
        assert len(drafts) == 3
        state = simulation_state(session)
        assert advance_running_playgrounds(db, iso(dt(state["last_wall_at"]) + timedelta(seconds=1)))
        parts = [item for item in session.messages if item.get("run_id") == drafts[0]["run_id"]]
        assert [item["status"] for item in parts] == ["simulated_delivered", "draft", "draft"]
        delivered_first = dict(parts[0])
        pending = {item["id"]: dict(item) for item in parts[1:]}

        add_customer_message(db, session, "Please wait.", "interrupt-parts")
        db.commit()
        db.refresh(session)
        parts = [item for item in session.messages if item.get("run_id") == drafts[0]["run_id"]]
        assert parts[0] == delivered_first
        assert len(parts) == 3
        for item in parts[1:]:
            assert item == {**pending[item["id"]], "status": "cancelled", "reason": "customer_new_message"}
        # Advance beyond all original due times without generating a new reply.
        session.due_at = None
        db.commit()
        for _ in range(2):
            state = simulation_state(session)
            advance_running_playgrounds(db, iso(dt(state["last_wall_at"]) + timedelta(seconds=10)))
        db.refresh(session)
        final_parts = [item for item in session.messages if item.get("run_id") == drafts[0]["run_id"]]
        assert final_parts == parts
        assert dt(session.virtual_now) > dt(drafts[-1]["created_at"])
        assert not session.controls["journey"]["sent_content_groups"]
        assert db.scalar(select(OutboundMessage)) is None


def test_unclassified_customer_silence_enrolls_and_delivers_route_selection_journey(session_factory, monkeypatch):
    with session_factory() as db:
        inbox = InboxBinding(id=1, tenant_id=1, chatwoot_inbox_id=128859, name="Facebook", channel_type="facebook")
        sop = SopDefinition(
            tenant_id=1,
            name=UNCLASSIFIED_SOP_NAME,
            status="running",
            version=1,
            route_variant="",
            inbox_ids=[128859],
            created_by=1,
            nodes=UNCLASSIFIED_SOP_NODES,
        )
        db.add_all([inbox, sop])
        db.flush()
        sop_snapshot(db, sop, 1)
        session = AutomationSession(
            owner_id=1,
            inbox_binding_id=inbox.id,
            mode="journey",
            environment="playground",
            virtual_now="2026-08-28T02:00:00+00:00",
            controls={"can_reply": True, "ai_enabled": True, "channel": "facebook", "labels": [],
                      "human": False, "history_complete": True},
        )
        db.add(session)
        db.flush()
        start_open_journey(db, session, duration_minutes=120, speed_multiplier=60,
                           entry_message="你好，我想咨询旅行行程")
        session.due_at = utcnow()
        db.commit()

        unclassified = EvaluationDecision(
            action="reply",
            branch="unclassified",
            intent="route_intro",
            reply="目前有9日和11日两条线路，您想先了解哪一条？",
            reply_options=["桃花9日", "桃花+珠峰11日"],
            route_variant="",
        )
        assert queue_passive(db, environment="playground")
        silence = EvaluationDecision(
            action="reply",
            branch="unclassified",
            intent="route_intro",
            reply="如果想轻松赏花可看9日，想连珠峰一起走可看11日。您更想先看哪一条？",
            reply_options=[],
            route_variant="",
            journey_stage="route_selection",
            touch_goal="route_choice",
            touch_reason="客户尚未选择线路，先提供差异帮助选择",
        )
        monkeypatch.setattr(
            "app.automation_service.generate_decision",
            lambda payload: ((silence if payload.get("module") == "silence_touch" else unclassified), [], "hash", {}),
        )
        assert process_automation_run(db, environment="playground")
        state = simulation_state(session)
        assert advance_running_playgrounds(db, iso(dt(state["last_wall_at"]) + timedelta(seconds=1)))
        db.refresh(session)

        enrollment = db.scalar(select(RehearsalEnrollment).where(RehearsalEnrollment.session_id == session.id))
        assert enrollment is not None
        assert db.get(SopDefinition, enrollment.sop_id).name == UNCLASSIFIED_SOP_NAME
        state = simulation_state(session)
        assert advance_running_playgrounds(db, iso(dt(state["last_wall_at"]) + timedelta(seconds=1)))
        db.refresh(session)
        first = db.scalar(select(RehearsalJob).where(
            RehearsalJob.enrollment_id == enrollment.id,
            RehearsalJob.node_key == "route_selection_silence",
        ))
        assert first.payload["delay_minutes"] == 30
        session.virtual_now = first.scheduled_at
        advance_sops(db, session)
        db.commit()
        db.refresh(first)
        assert first.status == "model_pending"
        assert process_automation_run(db, environment="playground")
        state = simulation_state(session)
        assert advance_running_playgrounds(db, iso(dt(state["last_wall_at"]) + timedelta(seconds=1)))
        db.refresh(first)
        db.refresh(session)
        assert first.status == "simulated_delivered"
        assert first.payload["model_decision"]["touch_goal"] == "route_choice"
        assert any(message.get("source") == "sop_ai" for message in session.messages)
        assert db.scalar(select(func.count()).select_from(RehearsalJob).where(
            RehearsalJob.enrollment_id == enrollment.id,
        )) == 1
        assert db.scalar(select(SilenceCycle).where(SilenceCycle.session_id == session.id)) is None
        assert db.scalar(select(OutboundMessage)) is None


def test_unclassified_new_customer_silence_journey_sends_one_low_pressure_touch(
    session_factory, monkeypatch
):
    """Unselected-route customers must not be pushed through the full route SOP."""
    with session_factory() as db:
        inbox = InboxBinding(
            id=1, tenant_id=1, chatwoot_inbox_id=128859,
            name="Facebook", channel_type="facebook",
        )
        sop = SopDefinition(
            tenant_id=1,
            name=UNCLASSIFIED_SOP_NAME,
            status="running",
            version=1,
            route_variant="",
            inbox_ids=[128859],
            created_by=1,
            nodes=UNCLASSIFIED_SOP_NODES,
        )
        db.add_all([inbox, sop])
        db.flush()
        sop_snapshot(db, sop, 1)
        session = AutomationSession(
            owner_id=1,
            inbox_binding_id=1,
            mode="journey",
            environment="playground",
            virtual_now="2026-08-28T02:00:00+00:00",
            controls={"can_reply": True, "ai_enabled": True, "channel": "facebook",
                      "labels": [], "human": False, "history_complete": True},
        )
        db.add(session)
        db.flush()
        start_open_journey(
            db, session, duration_minutes=240, speed_multiplier=1,
            entry_message="你好，我想先了解旅行行程",
        )
        session.due_at = utcnow()
        db.commit()

        def model(packet):
            if packet.get("module") == "reply":
                return EvaluationDecision(
                    action="reply", branch="unclassified", intent="route_intro",
                    reply="目前有两条线路，您想先了解哪条？",
                    reply_options=["桃花9日", "桃花+珠峰11日"],
                    journey_stage="route_selection",
                ), [], "initial", {}
            index = int(packet.get("touch_index") or 0)
            return EvaluationDecision(
                action="reply", branch="unclassified", intent="route_intro",
                reply=f"这是客户沉默后的第{index}次有效跟进。",
                journey_stage="route_selection",
                touch_goal="route_choice",
                touch_reason=f"第{index}次提供新的选择帮助",
            ), [], f"touch-{index}", {}

        monkeypatch.setattr("app.automation_service.generate_decision", model)
        assert queue_passive(db, environment="playground")
        assert process_automation_run(db, environment="playground")
        initial_draft = next(
            item for item in session.messages
            if item.get("direction") == "outgoing" and item.get("status") == "draft"
        )
        confirm_draft(db, session, str(initial_draft["id"]))
        db.commit()

        enrollment = db.scalar(select(RehearsalEnrollment).where(
            RehearsalEnrollment.session_id == session.id,
            RehearsalEnrollment.status == "active",
        ))
        jobs = db.scalars(select(RehearsalJob).where(
            RehearsalJob.enrollment_id == enrollment.id,
        ).order_by(RehearsalJob.id)).all()
        assert [job.payload["delay_minutes"] for job in jobs] == [30]
        scheduled_times = []
        for expected_index, job in enumerate(jobs, 1):
            db.refresh(job)
            if job.scheduled_at is None:
                advance_sops(db, session)
                db.flush()
                db.refresh(job)
            scheduled_times.append(job.scheduled_at)
            session.virtual_now = job.scheduled_at
            advance_sops(db, session)
            db.commit()
            assert job.status == "model_pending"
            assert process_automation_run(db, environment="playground")
            db.refresh(session)
            draft = next(
                item for item in session.messages
                if item.get("run_id")
                and item.get("status") == "draft"
                and item.get("source") == "sop_ai"
            )
            confirm_draft(db, session, str(draft["id"]))
            db.commit()
            db.refresh(job)
            assert job.status == "simulated_delivered"
            assert f"第{expected_index}次有效跟进" in draft["content"]

        assert all(
            dt(later) > dt(earlier)
            for earlier, later in zip(scheduled_times, scheduled_times[1:])
        )
        assert len([
            item for item in session.messages if item.get("source") == "sop_ai"
        ]) == 1
        assert db.scalar(select(OutboundMessage)) is None


def test_journey_auto_replies_and_advances_sop_without_outbound(session_factory, monkeypatch):
    with session_factory() as db:
        session, _ = setup_journey(db)
        bind_new_route_snapshot(session)
        assert simulation_state(session)["status"] == "running"
        assert session.messages[-1]["direction"] == "incoming"
        assert db.scalar(select(RehearsalEnrollment)).status == "active"

        session.due_at = utcnow()
        db.commit()
        assert queue_passive(db, environment="playground")
        monkeypatch.setattr("app.automation_service.generate_decision", lambda payload: (decision(), [], "hash", {}))
        assert process_automation_run(db, environment="playground")

        state = simulation_state(session)
        first_wall = iso(dt(state["last_wall_at"]) + timedelta(seconds=1))
        assert advance_running_playgrounds(db, first_wall)
        db.refresh(session)
        assert any(x.get("content") == "AI 演练回复" and x.get("status") == "simulated_delivered" for x in session.messages)

        state = simulation_state(session)
        sop_wall = iso(dt(state["last_wall_at"]) + timedelta(seconds=10))
        assert advance_running_playgrounds(db, sop_wall)
        db.refresh(session)
        assert db.scalar(select(RehearsalJob).where(RehearsalJob.node_key == "first")).status == "simulated_delivered"
        assert any(x.get("content") == "第一条 SOP" for x in session.messages)
        assert session.controls["journey"]["sent_content_groups"] == []
        progress = session.controls["journey"]["slots"]["_content_progress"][ROUTE]["itinerary_overview"]
        assert progress["topic_covered"]
        assert not progress["text_delivered"]
        assert progress["asset_keys"] == []
        assert db.scalar(select(OutboundMessage)) is None


@pytest.mark.parametrize("engine_version", ["v1", "v2"])
def test_checked_mainline_visual_is_drafted_before_reply_with_two_second_gap(
    session_factory, monkeypatch, engine_version
):
    with session_factory() as db:
        session, _ = setup_journey(db)
        session.engine_version = engine_version
        session.due_at = utcnow()
        db.commit()
        assert queue_passive(db, environment="playground")
        model_decision = EvaluationDecision(
            action="reply",
            branch="peach_9d",
            intent="route_intro",
            reply="這是行程圖的介紹文案。",
            route_variant=ROUTE,
            route_evidence="桃花9日",
            covered_content_groups=["itinerary_overview"],
            material_keys=["routes12-9d-itinerary"],
        )
        monkeypatch.setattr(
            "app.automation_service.generate_decision",
            lambda payload: (model_decision, [], "hash", {}),
        )
        monkeypatch.setattr(
            "app.automation_service.resolve_materials",
            lambda *_args, **_kwargs: [{
                "asset_key": "routes12-9d-itinerary",
                "media_id": 87,
                "media_hash": "route-image-hash",
                "content_family": "route-itinerary",
                "content_group_key": f"{ROUTE}:itinerary_overview",
                "name": "9日行程圖",
                "content_type": "image",
                "route_variant": ROUTE,
            }],
        )

        assert process_automation_run(db, environment="playground")
        db.refresh(session)
        drafted = [
            item for item in session.messages
            if item.get("run_id") and item.get("status") == "draft"
        ]
        assert [item["content_type"] for item in drafted] == ["image", "text"]
        assert dt(drafted[1]["created_at"]) - dt(drafted[0]["created_at"]) == timedelta(seconds=2)
        assert drafted[0]["timeline_sequence"] < drafted[1]["timeline_sequence"]
        monkeypatch.setattr(
            "app.automation_service.material_info",
            lambda _db, item, _route, _tenant: item,
        )
        monkeypatch.setattr("app.automation_service.previous_delivery", lambda *_args, **_kwargs: None)
        monkeypatch.setattr("app.automation_service.record_delivery", lambda *_args, **_kwargs: True)
        confirm_draft(db, session, str(drafted[1]["id"]))
        delivered = [
            item for item in session.messages
            if item.get("run_id") and item.get("status") == "simulated_delivered"
        ]
        assert [item["content_type"] for item in delivered] == ["image", "text"]
        assert dt(delivered[1]["created_at"]) - dt(delivered[0]["created_at"]) == timedelta(seconds=2)
        assert db.scalar(select(OutboundMessage)) is None


def test_ai_and_sop_share_reviewed_content_progress_without_duplicate(session_factory, monkeypatch):
    with session_factory() as db:
        session, _ = setup_journey(db)
        snapshot = bind_new_route_snapshot(session)
        reviewed = snapshot["groups"]["entry_question"]
        assert not reviewed["assets"]
        first = db.scalar(select(RehearsalJob).where(RehearsalJob.node_key == "first"))
        first.payload = {
            **first.payload,
            "content_group_key": "entry_question",
            "content": "这条重复 SOP 不应发送",
        }
        session.due_at = utcnow()
        db.commit()

        model_decision = EvaluationDecision(
            action="reply",
            branch="peach_9d",
            intent="route_intro",
            reply=reviewed["text"],
            route_variant=ROUTE,
            route_evidence="桃花9日",
            content_group_key="entry_question",
        )
        assert queue_passive(db, environment="playground")
        monkeypatch.setattr(
            "app.automation_service.generate_decision",
            lambda payload: (model_decision, [], "hash", {}),
        )
        assert process_automation_run(db, environment="playground")

        run = db.scalar(select(AutomationRun).where(AutomationRun.session_id == session.id))
        assert run.decision["reply"] == reviewed["text"]

        state = simulation_state(session)
        assert advance_running_playgrounds(
            db,
            iso(dt(state["last_wall_at"]) + timedelta(seconds=1)),
        )
        state = simulation_state(session)
        assert advance_running_playgrounds(
            db,
            iso(dt(state["last_wall_at"]) + timedelta(seconds=10)),
        )
        db.refresh(session)
        db.refresh(first)

        assert first.status == "already_provided"
        assert first.reason == "content_group_already_provided"
        assert not any(item.get("content") == "这条重复 SOP 不应发送" for item in session.messages)
        assert session.controls["journey"]["sent_content_groups"] == ["entry_question"]
        receipt = session.controls["journey"]["slots"]["_content_progress"][ROUTE]["entry_question"]
        assert receipt["text_delivered"]
        assert not receipt["history_unknown"]
        assert sum(item.get("content") == reviewed["text"] and item.get("status") == "simulated_delivered"
                   for item in session.messages) == 1
        assert db.scalar(select(OutboundMessage)) is None


def test_playground_persists_every_group_covered_by_one_ai_reply(session_factory, monkeypatch):
    with session_factory() as db:
        session, _ = setup_journey(db)
        bind_new_route_snapshot(session)
        session.due_at = utcnow()
        db.commit()
        multi_group = EvaluationDecision(
            action="reply",
            branch="peach_9d",
            intent="price",
            reply="价格与住宿都已说明。",
            route_variant=ROUTE,
            route_evidence="桃花9日",
            content_group_key="price_reference",
            covered_content_groups=["price_reference", "hotel_reference", "contact_request"],
        )
        assert queue_passive(db, environment="playground")
        monkeypatch.setattr(
            "app.automation_service.generate_decision",
            lambda payload: (multi_group, [], "hash", {}),
        )
        assert process_automation_run(db, environment="playground")

        state = simulation_state(session)
        assert advance_running_playgrounds(
            db,
            iso(dt(state["last_wall_at"]) + timedelta(seconds=1)),
        )
        db.refresh(session)

        assert session.controls["journey"]["sent_content_groups"] == []
        progress = session.controls["journey"]["slots"]["_content_progress"][ROUTE]
        for key in multi_group.covered_content_groups:
            assert progress[key]["topic_covered"]
            assert not progress[key]["text_delivered"]
            assert progress[key]["asset_keys"] == []
            assert not progress[key]["history_unknown"]


def test_due_sop_waits_for_passive_reply_instead_of_becoming_terminal(session_factory):
    with session_factory() as db:
        session, _ = setup_journey(db)
        first = db.scalar(select(RehearsalJob).where(RehearsalJob.node_key == "first"))
        session.virtual_now = first.scheduled_at
        session.due_at = utcnow()

        advance_sops(db, session)
        db.flush()

        assert first.status == "scheduled"
        assert first.reason == "passive_reply_pending"
        second = db.scalar(select(RehearsalJob).where(RehearsalJob.node_key == "second"))
        assert second.status == "waiting_dependency"


def test_configured_silence_values_are_gaps_after_each_previous_touch(session_factory):
    """1/3/5 are relative gaps, never absolute offsets from enrollment."""
    with session_factory() as db:
        inbox = InboxBinding(id=1, tenant_id=1, chatwoot_inbox_id=128859, name="Facebook", channel_type="facebook")
        sop = SopDefinition(
            tenant_id=1, name="legacy absolute silence snapshot", status="running", version=1,
            route_variant=ROUTE, inbox_ids=[128859], created_by=1,
            nodes=[
                {"key": "one", "schedule_type": "relative", "basis": "enrollment", "delay_minutes": 10,
                 "journey_trigger": "silence_mainline", "content_type": "text", "content": "one"},
                {"key": "two", "schedule_type": "relative", "basis": "enrollment", "delay_minutes": 10,
                 "journey_trigger": "wakeup", "content_type": "text", "content": "two"},
            ],
        )
        db.add_all([inbox, sop])
        db.flush()
        version = sop_snapshot(db, sop, 1)
        session = AutomationSession(
            owner_id=1, inbox_binding_id=1, mode="journey", environment="playground",
            virtual_now="2026-08-28T02:00:00+00:00",
            controls={"can_reply": True, "ai_enabled": True, "channel": "facebook", "labels": [],
                      "human": False, "history_complete": True, "route_variant": ROUTE},
        )
        db.add(session)
        db.flush()

        enrollment = enroll_rehearsal(
            db, session, version, source="journey", schedule_intervals=[1, 3]
        )
        first, second = db.scalars(select(RehearsalJob).where(
            RehearsalJob.enrollment_id == enrollment.id
        ).order_by(RehearsalJob.id)).all()
        assert first.status == "scheduled"
        assert first.scheduled_at == iso(dt(session.virtual_now) + timedelta(minutes=1))
        assert second.status == "waiting_dependency"
        assert second.scheduled_at is None
        assert second.predecessor_id == first.id

        first.status = "skipped_model_failure"
        first.confirmed_at = first.scheduled_at
        advance_sops(db, session)
        assert second.status == "scheduled"
        assert second.scheduled_at == iso(dt(first.confirmed_at) + timedelta(minutes=3))


def test_configured_silence_timeline_expands_beyond_published_template(session_factory):
    intervals = [1, 3, 5, 10, 30, 60, 120, 240]
    with session_factory() as db:
        inbox = InboxBinding(id=1, tenant_id=1, chatwoot_inbox_id=128859, name="Facebook", channel_type="facebook")
        sop = SopDefinition(
            tenant_id=1, name="short published template", status="running", version=1,
            route_variant=ROUTE, inbox_ids=[128859], created_by=1,
            nodes=[
                {"key": "one", "schedule_type": "relative", "basis": "enrollment", "delay_minutes": 10,
                 "journey_trigger": "silence_mainline", "content_type": "text", "content": "one"},
                {"key": "two", "schedule_type": "relative", "basis": "previous_node", "delay_minutes": 10,
                 "journey_trigger": "wakeup", "content_type": "text", "content": "two"},
            ],
        )
        db.add_all([inbox, sop])
        db.flush()
        version = sop_snapshot(db, sop, 1)
        session = AutomationSession(
            owner_id=1, inbox_binding_id=1, mode="journey", environment="playground",
            virtual_now="2026-08-28T02:00:00+00:00",
            controls={"can_reply": True, "ai_enabled": True, "channel": "facebook", "labels": [],
                      "human": False, "history_complete": True, "route_variant": ROUTE},
        )
        db.add(session)
        db.flush()
        enrollment = enroll_rehearsal(
            db, session, version, source="journey", schedule_intervals=intervals
        )
        jobs = db.scalars(select(RehearsalJob).where(
            RehearsalJob.enrollment_id == enrollment.id
        ).order_by(RehearsalJob.id)).all()

        assert len(jobs) == 8
        assert [job.node_key for job in jobs] == [
            "silence_mainline", "wakeup_1", "wakeup_2", "wakeup_3",
            "wakeup_4", "wakeup_5", "wakeup_6", "wakeup_7",
        ]
        assert [job.payload["delay_minutes"] for job in jobs] == intervals
        assert jobs[0].status == "scheduled"
        assert all(job.status == "waiting_dependency" for job in jobs[1:])
        assert all(job.predecessor_id == jobs[index - 1].id for index, job in enumerate(jobs[1:], 1))


def test_silence_generation_failure_skips_only_current_touch(session_factory, monkeypatch):
    with session_factory() as db:
        session, _ = setup_journey(db)
        jobs = db.scalars(select(RehearsalJob).order_by(RehearsalJob.id)).all()
        first, second = jobs
        first.payload = {**first.payload, "journey_trigger": "silence_mainline"}
        second.payload = {**second.payload, "journey_trigger": "wakeup"}
        session.due_at = None
        session.virtual_now = first.scheduled_at
        advance_sops(db, session)
        db.commit()

        assert first.status == "model_pending"
        # advance_sops only queued the model run; process it after replacing the
        # network boundary with a deterministic generation failure.
        monkeypatch.setattr(
            "app.automation_service.generate_decision",
            lambda _payload: (_ for _ in ()).throw(EvaluationCallError(
                "deepseek_failed", [{"status": "failed"}] * 3, "hash"
            )),
        )
        assert process_automation_run(db, environment="playground")
        db.refresh(first)
        db.refresh(session)
        assert first.status == "skipped_model_failure"
        assert first.reason == "model_generation_or_verification_failed"
        assert session.controls["route_variant"] == ROUTE

        advance_sops(db, session)
        db.refresh(second)
        assert second.status == "scheduled"
        assert second.scheduled_at is not None
        assert any(
            event["event_type"] == "silence_model_warning"
            for event in session.controls["timeline_events"]
        )
        assert db.scalar(select(OutboundMessage)) is None


@pytest.mark.parametrize("flag,status,skip_reason", [
    ("silence_no_relevant_content", "skipped", "silence_no_relevant_content"),
    ("silence_verification_failed_no_action", "verification_blocked", "fact_verification_failed_no_action"),
])
def test_playground_silence_no_action_creates_no_draft_and_advances(
    session_factory, monkeypatch, flag, status, skip_reason,
):
    with session_factory() as db:
        session, _ = setup_journey(db)
        first, second = db.scalars(select(RehearsalJob).order_by(RehearsalJob.id)).all()
        first.payload = {**first.payload, "journey_trigger": "silence_mainline"}
        second.payload = {**second.payload, "journey_trigger": "wakeup"}
        session.due_at = None
        session.virtual_now = first.scheduled_at
        advance_sops(db, session)
        db.commit()

        no_action = EvaluationDecision(
            action="no_action",
            branch="peach_9d",
            intent="other",
            route_variant=ROUTE,
            journey_stage="considering",
            touch_reason="当前没有新的相关内容",
            safety_flags=[flag],
        )
        monkeypatch.setattr(
            "app.automation_service.generate_decision",
            lambda _payload: (no_action, [], "hash", {
                "pipeline": "split_silence_touch", "skip_reason": skip_reason,
                "fact_verification_passed": False if status == "verification_blocked" else None,
            }),
        )
        assert process_automation_run(db, environment="playground")
        db.refresh(session)
        db.refresh(first)

        assert first.status == status
        assert first.reason == flag
        assert not any(item.get("direction") == "outgoing" for item in session.messages)
        assert not session.controls["journey"]["sent_content_groups"]
        assert not session.controls["journey"]["slots"].get("_content_progress")
        run = db.scalar(select(AutomationRun).where(AutomationRun.session_id == session.id))
        assert run.status == "completed"
        assert run.trace["skip_reason"] == skip_reason
        assert any(
            event["event_type"] == "silence_touch_skipped"
            for event in session.controls["timeline_events"]
        )

        advance_sops(db, session)
        db.refresh(second)
        assert second.status == "scheduled"
        assert db.scalar(select(OutboundMessage)) is None


def test_silence_follow_up_is_independent_last_delivery(session_factory, monkeypatch):
    with session_factory() as db:
        session, _ = setup_journey(db)
        snapshot = bind_new_route_snapshot(session)
        first = db.scalar(select(RehearsalJob).where(RehearsalJob.node_key == "first"))
        first.payload = {**first.payload, "journey_trigger": "silence_mainline"}
        session.due_at = None
        session.virtual_now = first.scheduled_at
        advance_sops(db, session)
        db.commit()

        body, question = "Room photos for comparison.", "How many guests are travelling?"
        reply = decision()
        reply.reply = f"{body} {question}"
        reply.reply_body = body
        reply.follow_up_type = "slot"
        reply.follow_up_field = "party_size"
        reply.follow_up_question = question
        reply.covered_content_groups = ["hotel_reference"]
        reply.material_keys = snapshot["groups"]["hotel_reference"]["assets"]
        materials = [{
            "asset_key": key, "media_id": 87 + index, "media_hash": f"room-{index}",
            "content_family": "hotel", "content_group_key": f"{ROUTE}:hotel_reference",
            "content_type": "image", "route_variant": ROUTE,
        } for index, key in enumerate(reply.material_keys)]
        assert len(materials) >= 2
        monkeypatch.setattr("app.automation_service.generate_decision",
                            lambda _: (reply, [], "hash", {"pipeline": "split_silence_touch"}))
        monkeypatch.setattr("app.automation_service.resolve_materials", lambda *args, **kwargs: materials)
        monkeypatch.setattr("app.automation_service.material_info", lambda _db, item, _route, _tenant: item)
        monkeypatch.setattr("app.automation_service.previous_delivery", lambda *args, **kwargs: None)
        monkeypatch.setattr("app.automation_service.record_delivery", lambda *args, **kwargs: True)
        assert process_automation_run(db, environment="playground")
        db.refresh(session)
        drafts = [item for item in session.messages if item.get("status") == "draft"]
        assert len(drafts) == len(materials) + 2
        assert len({item["id"] for item in drafts}) == len(drafts)
        assert drafts[-1]["content"] == question
        assert drafts[-1]["content_attributes"]["delivery_item"]["is_follow_up"]
        assert sum(item.get("content") == body for item in drafts) == 1
        assert all(question not in item.get("content", "") for item in drafts[:-1])
        assert {item["asset_key"] for item in drafts if item.get("media_id")} == set(reply.material_keys)
        before = [(item["id"], item["created_at"], item["timeline_sequence"]) for item in drafts]
        gap = timedelta(seconds=snapshot["initial_delivery_interval_seconds"])
        for earlier, later in zip(drafts, drafts[1:]):
            assert earlier["timeline_sequence"] < later["timeline_sequence"]
            assert dt(later["created_at"]) - dt(earlier["created_at"]) >= gap

        confirm_draft(db, session, str(drafts[-1]["id"]))
        db.commit()
        db.refresh(session)
        delivered = [item for item in session.messages if item.get("status") == "simulated_delivered"]
        assert [(item["id"], item["created_at"], item["timeline_sequence"]) for item in delivered] == before
        assert delivered[-1]["content"] == question
        assert not session.controls["journey"]["sent_content_groups"]
        receipt = session.controls["journey"]["slots"]["_content_progress"][ROUTE]["hotel_reference"]
        assert receipt["topic_covered"] and not receipt["text_delivered"]
        assert set(receipt["asset_keys"]) == set(reply.material_keys)
        assert db.scalar(select(OutboundMessage)) is None


def test_journey_handoff_stops_remaining_sop(session_factory, monkeypatch):
    with session_factory() as db:
        session, _ = setup_journey(db)
        session.due_at = utcnow()
        db.commit()
        assert queue_passive(db, environment="playground")
        handoff = EvaluationDecision(
            action="handoff",
            branch="peach_9d",
            intent="complaint",
            reply="需要由旅游顾问继续处理。",
            handoff_reason="customer_requested_human",
            route_variant=ROUTE,
        )
        monkeypatch.setattr("app.automation_service.generate_decision", lambda payload: (handoff, [], "hash", {}))

        assert process_automation_run(db, environment="playground")
        db.refresh(session)

        assert session.controls["human"] is True
        assert "人工接管" in session.controls["labels"]
        assert db.scalar(select(RehearsalEnrollment)).status == "cancelled"
        assert all(job.status == "cancelled" for job in db.scalars(select(RehearsalJob)).all())
        event = next(event for event in session.controls["timeline_events"] if event["event_type"] == "ai_handoff")
        reply = next(message for message in session.messages if message.get("run_id"))
        assert event["timeline_sequence"] < reply["timeline_sequence"]
        assert db.scalar(select(OutboundMessage)) is None


def test_customer_reply_exits_sop_and_keeps_ai_reply_chain(session_factory, monkeypatch):
    with session_factory() as db:
        session, _ = setup_journey(db)
        add_customer_message(db, session, "请问价格？", "customer-2")
        db.commit()
        enrollment = db.scalar(select(RehearsalEnrollment))
        assert enrollment.status == "cancelled"
        assert any(x["event_type"] == "sop_exited" for x in session.controls["timeline_events"])

        session.due_at = utcnow()
        db.commit()
        assert queue_passive(db, environment="playground")
        monkeypatch.setattr("app.automation_service.generate_decision", lambda payload: (decision(), [], "hash", {}))
        assert process_automation_run(db, environment="playground")
        state = simulation_state(session)
        assert advance_running_playgrounds(db, iso(dt(state["last_wall_at"]) + timedelta(seconds=1)))
        db.refresh(session)
        assert any(x.get("content") == "AI 演练回复" and x.get("status") == "simulated_delivered" for x in session.messages)
        assert db.scalar(select(OutboundMessage)) is None


def test_environment_scoped_worker_does_not_take_shadow_run(session_factory):
    with session_factory() as db:
        playground, _ = setup_journey(db)
        shadow = AutomationSession(
            owner_id=1,
            mode="reply",
            environment="shadow",
            virtual_now=playground.virtual_now,
            due_at=utcnow(),
            controls={"can_reply": True, "ai_enabled": True, "channel": "facebook", "history_complete": True},
            messages=[{"direction": "incoming", "content": "shadow", "created_at": playground.virtual_now}],
        )
        db.add(shadow)
        playground.due_at = utcnow()
        db.commit()
        assert queue_passive(db, environment="playground")
        assert db.scalar(select(AutomationRun).where(AutomationRun.session_id == playground.id)) is not None
        assert db.scalar(select(AutomationRun).where(AutomationRun.session_id == shadow.id)) is None


def test_journey_api_auto_binds_route_sop_and_controls_run(authenticated, session_factory):
    client, csrf = authenticated
    with session_factory() as db:
        inbox = InboxBinding(id=1, tenant_id=1, chatwoot_inbox_id=128859, name="Facebook", channel_type="facebook")
        sop = SopDefinition(
            tenant_id=1,
            name="桃花9日 API 演练",
            status="running",
            version=1,
            route_variant=ROUTE,
            inbox_ids=[128859],
            created_by=1,
            nodes=[{"key": "first", "schedule_type": "relative", "basis": "enrollment", "delay_minutes": 10,
                    "content_type": "text", "content": "SOP API"}],
        )
        db.add_all([inbox, sop])
        db.flush()
        version = sop_snapshot(db, sop, 1)
        version_id = version.id
        db.commit()

    options = client.get("/v1/playground/options?inbox_binding_id=1")
    assert options.status_code == 200
    assert options.json()["strategies"][0]["version_id"] == version_id

    response = client.post("/v1/playground/sessions", headers={"X-CSRF-Token": csrf}, json={
        "mode": "journey",
        "inbox_binding_id": 1,
        "route_variant": ROUTE,
        "virtual_now": "2026-08-28T10:00:00+08:00",
        "duration_minutes": 60,
        "speed_multiplier": 300,
        "entry_message": "想了解桃花9日",
        "sop_version_id": version_id,
    })
    assert response.status_code == 201
    body = response.json()
    assert body["mode"] == "journey"
    assert body["simulation"]["status"] == "running"
    assert body["simulation"]["speed_multiplier"] == 1
    assert body["simulation"]["sop_version_id"] == version_id
    assert body["messages"][-1]["content"] == "想了解桃花9日"

    paused = client.post(f"/v1/playground/sessions/{body['id']}/control/pause", headers={"X-CSRF-Token": csrf})
    assert paused.status_code == 200
    assert paused.json()["simulation"]["status"] == "paused"


def test_journey_api_can_advance_only_to_next_sandbox_touch(authenticated, session_factory):
    client, csrf = authenticated
    with session_factory() as db:
        inbox = InboxBinding(id=1, tenant_id=1, chatwoot_inbox_id=128859, name="Facebook", channel_type="facebook")
        sop = SopDefinition(
            tenant_id=1,
            name="快进演练",
            status="running",
            version=1,
            route_variant=ROUTE,
            inbox_ids=[128859],
            created_by=1,
            nodes=[{"key": "first", "schedule_type": "relative", "basis": "enrollment", "delay_minutes": 10,
                    "content_type": "text", "content": "SOP API"}],
        )
        db.add_all([inbox, sop])
        db.flush()
        version_id = sop_snapshot(db, sop, 1).id
        db.commit()

    created = client.post("/v1/playground/sessions", headers={"X-CSRF-Token": csrf}, json={
        "mode": "journey",
        "inbox_binding_id": 1,
        "route_variant": ROUTE,
        "virtual_now": "2026-08-28T10:00:00+08:00",
        "duration_minutes": 60,
        "entry_message": "想了解桃花9日",
        "sop_version_id": version_id,
    })
    assert created.status_code == 201
    session_id = created.json()["id"]

    busy = client.post(f"/v1/playground/sessions/{session_id}/advance-next", headers={"X-CSRF-Token": csrf})
    assert busy.status_code == 409
    assert busy.json()["error"]["code"] == "journey_busy"

    with session_factory() as db:
        row = db.get(AutomationSession, session_id)
        row.due_at = None
        for run in db.scalars(select(AutomationRun).where(AutomationRun.session_id == session_id)).all():
            run.status = "cancelled"
        db.commit()

    advanced = client.post(f"/v1/playground/sessions/{session_id}/advance-next", headers={"X-CSRF-Token": csrf})
    assert advanced.status_code == 200
    result = advanced.json()
    assert result["virtual_now"] == "2026-08-28T02:10:00+00:00"
    assert result["jobs"][0]["status"] == "simulated_delivered"
