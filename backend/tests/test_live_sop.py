from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import func, select

import app.live_sop as live_sop
from app.automation_models import LiveSopEnrollment, LiveSopJob, SopVersion, TouchReservation
from app.automation_service import dt, iso, sop_snapshot
from app.config import settings
from app.deepseek_evaluation import EvaluationDecision
from app.live_reply_models import LiveReplyJob
from app.models import (AppSetting, ChatwootConnection, Contact, ConversationJourney, ConversationState, InboxBinding,
                        MessageEvent, OutboundMessage, SopDefinition, StoredMedia, utcnow)
from app.reception_config import SETTING_KEY, default_reception_configuration
from app.route_packages import ROUTES, UNCLASSIFIED_SOP_NAME, UNCLASSIFIED_SOP_NODES
from app.route_reply import resolve_journey_payload
from app.security import encrypt_secret


AT = "2026-08-27T02:00:00+00:00"


def test_dynamic_silence_node_selects_first_unsent_reviewed_group():
    journey = SimpleNamespace(sent_groups=["itinerary_overview"])
    job = SimpleNamespace(payload={
        "content_group_candidates": [
            {"content_group_key": "itinerary_overview", "messages": [{"content": "旧"}]},
            {"content_group_key": "peach_highlights", "messages": [{"content": "新"}]},
        ]
    })

    resolved = resolve_journey_payload(job.payload, journey.sent_groups)
    assert resolved["content_group_key"] == "peach_highlights"
    assert resolved["messages"] == [{"content": "新"}]
    assert "content_group_candidates" not in resolved


def test_dynamic_silence_node_stops_when_all_reviewed_groups_were_sent():
    journey = SimpleNamespace(sent_groups=["itinerary_overview"])
    job = SimpleNamespace(payload={
        "content_group_candidates": [
            {"content_group_key": "itinerary_overview", "messages": [{"content": "旧"}]},
        ]
    })

    assert resolve_journey_payload(job.payload, journey.sent_groups) is None


class FakeClient:
    def __init__(self):
        self.labels = ["ai"]
        self.contact_labels = []
        self.can_reply = True
        self.messages = [{"id": 100, "created_at": AT, "message_type": 0, "private": False,
                          "content": "想了解行程", "content_attributes": {}}]
        self.sent = []
        self.unknown = False

    def get_conversation(self, _id):
        return {"id": 26, "inbox_id": 128859, "can_reply": self.can_reply,
                "meta": {"sender": {"id": 55}, "assignee": None}}

    def get_conversation_labels(self, _id):
        return {"payload": self.labels}

    def get_contact_labels(self, _id):
        return {"payload": self.contact_labels}

    def get_messages(self, _id, before=None):
        return {"payload": [item for item in self.messages if before is None or item["id"] < before]}

    def create_text_message(self, _id, content):
        self.sent.append(("text", content))
        if self.unknown:
            raise httpx.ReadTimeout("unknown after submit")
        message = {"id": 1000 + len(self.sent), "created_at": AT, "message_type": 1,
                   "private": False, "content": content}
        self.messages.append(message)
        return message

    def create_attachment_message(self, *_args):
        content = _args[1]
        self.sent.append(("image", content))
        message = {"id": 1000 + len(self.sent), "created_at": AT, "message_type": 1,
                   "private": False, "content": content, "attachments": [{"file_type": "image"}]}
        self.messages.append(message)
        return message

    def close(self):
        pass


def setup(session_factory, monkeypatch):
    fake = FakeClient()
    monkeypatch.setattr(settings, "app_profile", "live_reply")
    monkeypatch.setattr(settings, "outbound_mode", "live")
    monkeypatch.setattr(settings, "chatwoot_write_enabled", True)
    monkeypatch.setattr(settings, "live_sop_enabled", True)
    monkeypatch.setattr(settings, "live_sop_scope", "allowlist")
    monkeypatch.setattr(settings, "live_sop_conversation_ids", "26")
    monkeypatch.setattr(live_sop, "SessionLocal", session_factory)
    monkeypatch.setattr(live_sop, "client_for", lambda _connection: fake)
    monkeypatch.setattr(live_sop, "utcnow", lambda: AT)
    monkeypatch.setattr(live_sop.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(live_sop, "pin_route_assets", lambda *_args: {})
    with session_factory() as db:
        db.add(ChatwootConnection(id=1, tenant_id=1, account_id=180474,
                                  encrypted_api_token=encrypt_secret("fake"), connection_key="fake"))
        db.add(InboxBinding(id=1, tenant_id=1, chatwoot_inbox_id=128859, name="Facebook",
                            channel_type="Channel::FacebookPage", ai_enabled=True))
        db.add(Contact(id=1, tenant_id=1, chatwoot_contact_id=55, name="Test"))
        db.flush()
        state = ConversationState(id=1, tenant_id=1, inbox_binding_id=1, contact_id=1,
                                  chatwoot_conversation_id=26, labels=["ai"], ai_mode="enabled",
                                  ai_label_present=True, ai_sync_status="synced", can_reply=True)
        db.add(state)
        db.flush()
        live_sop.prepare_route_reply(db, state, EvaluationDecision(
            action="no_action", branch="peach_9d", intent="other", route_variant="peach_9d_2027",
        ), None)
        db.add(MessageEvent(id=1, conversation_state_id=1, chatwoot_message_id=100,
                            direction="incoming", content="想了解行程", created_at=AT))
        db.add(AppSetting(key="global_message_sending", value={"enabled": True}))
        sop = SopDefinition(tenant_id=1, created_by=1, name="live test", status="running",
            route_variant="peach_9d_2027", inbox_ids=[128859], test_conversation_ids=[26], nodes=[{
                "key": "first", "schedule_type": "relative", "basis": "enrollment", "delay_minutes": 0,
                "content_group_key": "itinerary_overview",
                "messages": [{"key": "text", "content_type": "text", "content": "真实测试消息"}]}])
        db.add(sop)
        db.flush()
        version = sop_snapshot(db, sop, 1)
        from app.route_reply import make_route_snapshot, route_snapshot_from_values, ROUTE_SNAPSHOTS_KEY
        journey = db.scalar(select(ConversationJourney))
        spec = route_snapshot_from_values(journey.route_variant, journey.slots)
        spec = {**spec, "sop": {**spec["sop"], "nodes": sop.nodes}}
        journey.slots = {**journey.slots, ROUTE_SNAPSHOTS_KEY: {
            journey.route_variant: make_route_snapshot(journey.route_variant, spec),
        }}
        db.commit()
        return fake, sop.id, version.id


def enroll(session_factory, version_id, request_key="request-1", reenroll=False, allow_repeat_delivery=False):
    from app.automation_models import SopVersion
    with session_factory() as db:
        row = live_sop.enroll_live_sop(db, db.get(ConversationState, 1), db.get(SopVersion, version_id),
                                       request_key=request_key, reenroll=reenroll,
                                       allow_repeat_delivery=allow_repeat_delivery)
        db.commit()
        return row.id


def test_static_initial_delivery_sends_image_then_copy_with_guarded_gap(
    session_factory, monkeypatch, tmp_path
):
    fake, _sop_id, version_id = setup(session_factory, monkeypatch)
    image = tmp_path / "initial.jpg"
    image.write_bytes(b"initial-image")
    with session_factory() as db:
        media = StoredMedia(
            tenant_id=1,
            original_name="initial.jpg",
            media_type="image",
            mime_type="image/jpeg",
            file_size=image.stat().st_size,
            storage_path=str(image),
            created_by=1,
        )
        db.add(media)
        db.commit()
        media_id = media.id
    enrollment_id = enroll(session_factory, version_id, request_key="initial-image-first")
    with session_factory() as db:
        job = db.scalar(select(LiveSopJob).where(LiveSopJob.enrollment_id == enrollment_id))
        job.payload = {
            **job.payload,
            "delivery_interval_seconds": 3,
            "skip_if_materials_provided": False,
            "messages": [
                {"key": "photo", "content_type": "image", "content": "", "media_id": media_id},
                {"key": "copy", "content_type": "text", "content": "這是行程重點。"},
            ],
        }
        db.commit()

    waits = []
    info = {"media_id": media_id, "content_family": "initial", "content_type": "image"}
    monkeypatch.setattr(
        live_sop,
        "_material",
        lambda db, *_args: (info, db.get(StoredMedia, media_id)),
    )
    monkeypatch.setattr(live_sop, "_media_previously_sent", lambda *_args: False)
    monkeypatch.setattr(live_sop.time, "sleep", waits.append)

    assert live_sop.process_due_live_sop()
    assert fake.sent == [("image", ""), ("text", "這是行程重點。")]
    assert waits == [3]


def test_static_initial_delivery_stops_when_customer_replies_during_gap(
    session_factory, monkeypatch, tmp_path
):
    fake, _sop_id, version_id = setup(session_factory, monkeypatch)
    image = tmp_path / "interrupted.jpg"
    image.write_bytes(b"interrupted-image")
    with session_factory() as db:
        media = StoredMedia(
            tenant_id=1,
            original_name="interrupted.jpg",
            media_type="image",
            mime_type="image/jpeg",
            file_size=image.stat().st_size,
            storage_path=str(image),
            created_by=1,
        )
        db.add(media)
        db.commit()
        media_id = media.id
    enrollment_id = enroll(session_factory, version_id, request_key="initial-interrupted")
    with session_factory() as db:
        job = db.scalar(select(LiveSopJob).where(LiveSopJob.enrollment_id == enrollment_id))
        job.payload = {
            **job.payload,
            "delivery_interval_seconds": 3,
            "skip_if_materials_provided": False,
            "messages": [
                {"key": "photo", "content_type": "image", "content": "", "media_id": media_id},
                {"key": "copy", "content_type": "text", "content": "這段不應送出。"},
            ],
        }
        db.commit()

    info = {"media_id": media_id, "content_family": "interrupted", "content_type": "image"}
    monkeypatch.setattr(
        live_sop,
        "_material",
        lambda db, *_args: (info, db.get(StoredMedia, media_id)),
    )
    monkeypatch.setattr(live_sop, "_media_previously_sent", lambda *_args: False)

    def customer_replies(_seconds):
        fake.messages.append({
            "id": 101,
            "created_at": "2026-08-27T02:00:05+00:00",
            "message_type": 0,
            "private": False,
            "content": "我想先問價格",
            "content_attributes": {},
        })

    monkeypatch.setattr(live_sop.time, "sleep", customer_replies)

    assert live_sop.process_due_live_sop()
    assert fake.sent == [("image", "")]
    with session_factory() as db:
        job = db.scalar(select(LiveSopJob).where(LiveSopJob.enrollment_id == enrollment_id))
        assert job.status == "blocked"
        assert job.reason == "customer_new_message"


def test_passive_live_sop_expands_to_operator_timeline(session_factory, monkeypatch):
    _fake, _sop_id, version_id = setup(session_factory, monkeypatch)
    intervals = [1, 3, 5, 10, 30, 60, 120, 240]
    with session_factory() as db:
        config = default_reception_configuration()
        config["silence"]["intervals_minutes"] = intervals
        config["silence"]["max_proactive_messages_per_day"] = 8
        db.add(AppSetting(key=SETTING_KEY, value=config))
        db.commit()

        enrollment = live_sop.enroll_live_sop(
            db,
            db.get(ConversationState, 1),
            db.get(SopVersion, version_id),
            request_key="dynamic-passive-route",
            trigger_source="passive_route",
        )
        jobs = db.scalars(select(LiveSopJob).where(
            LiveSopJob.enrollment_id == enrollment.id
        ).order_by(LiveSopJob.id)).all()

        assert len(jobs) == 8
        assert [job.node_key for job in jobs] == [
            "silence_mainline", "wakeup_1", "wakeup_2", "wakeup_3",
            "wakeup_4", "wakeup_5", "wakeup_6", "wakeup_7",
        ]
        assert [job.payload["delay_minutes"] for job in jobs] == intervals
        assert jobs[0].status == "scheduled"
        assert all(job.status == "waiting_dependency" for job in jobs[1:])


@pytest.mark.parametrize("has_setting", [False, True])
def test_v2_passive_first_silence_waits_sixty_seconds(session_factory, monkeypatch, has_setting):
    fake, _, version_id = setup(session_factory, monkeypatch)
    from app.reception_v2 import ENGINE_RELEASE_ID
    from app.route_reply import make_route_snapshot, ROUTE_SNAPSHOTS_KEY
    with session_factory() as db:
        state = db.get(ConversationState, 1)
        state.ai_engine_version = "v2"
        state.ai_engine_release_id = ENGINE_RELEASE_ID
        journey = db.scalar(select(ConversationJourney))
        journey.slots = {ROUTE_SNAPSHOTS_KEY: {journey.route_variant: make_route_snapshot(journey.route_variant, ROUTES[journey.route_variant])}}
        if has_setting:
            db.add(AppSetting(key=SETTING_KEY, value=default_reception_configuration()))
        db.commit()
        enrollment = live_sop.enroll_live_sop(db, state, db.get(SopVersion, version_id),
                                             request_key="v2-sixty-seconds", trigger_source="passive_route")
        jobs = db.scalars(select(LiveSopJob).where(LiveSopJob.enrollment_id == enrollment.id)
                          .order_by(LiveSopJob.id)).all()
        assert jobs[0].node_key == "silence_mainline"
        assert (dt(jobs[0].scheduled_at) - dt(enrollment.enrolled_at)).total_seconds() == 60
        assert all(job.payload.get("journey_trigger") for job in jobs)
        assert fake.sent == []


def test_allowlisted_live_sop_sends_once_and_persists_chatwoot_id(session_factory, monkeypatch):
    fake, _sop_id, version_id = setup(session_factory, monkeypatch)
    enrollment_id = enroll(session_factory, version_id)
    assert live_sop.process_due_live_sop()
    assert not live_sop.process_due_live_sop()
    assert fake.sent == [("text", "真实测试消息")]
    with session_factory() as db:
        enrollment = db.get(LiveSopEnrollment, enrollment_id)
        job = db.scalar(select(LiveSopJob))
        out = db.scalar(select(OutboundMessage))
        assert enrollment.status == "completed"
        assert job.status == "submitted"
        assert out.status == "submitted" and out.chatwoot_message_id == 1001
        message = db.scalar(select(MessageEvent).where(MessageEvent.chatwoot_message_id == 1001))
        assert message.content_attributes["delivery_item"] == out.content_attributes["delivery_item"]
        assert message.content_attributes["delivery_item"]["item_id"] == out.idempotency_key
        assert not any(key.startswith("_") for key in message.content_attributes)
        journey = db.scalar(select(ConversationJourney))
        assert journey.route_variant == "peach_9d_2027"
        assert journey.sent_groups == []
        assert journey.last_group_key is None


def test_stage_driven_live_silence_touch_uses_planned_model_content(session_factory, monkeypatch):
    fake, _sop_id, version_id = setup(session_factory, monkeypatch)
    enrollment_id = enroll(session_factory, version_id, request_key="stage-driven")
    with session_factory() as db:
        job = db.scalar(select(LiveSopJob).where(LiveSopJob.enrollment_id == enrollment_id))
        job.payload = {**job.payload, "journey_trigger": "silence_mainline"}
        db.commit()

    monkeypatch.setattr(
        live_sop,
        "fetch_complete_customer_history",
        lambda _client, _remote: ([{
            "id": 100,
            "direction": "incoming",
            "content": "我和家人商量一下",
            "created_at": AT,
        }], {"context_complete": True, "context_messages": 1}),
    )
    model_decision = EvaluationDecision(
        action="reply",
        branch="peach_9d",
        intent="other",
        reply="您可以先把桃花9日的重点转给家人参考，我会继续协助您。",
        route_variant="peach_9d_2027",
        journey_stage="considering",
        touch_goal="soft_nurture",
        touch_reason="客户正在和家人讨论，适合低压力跟进",
    )
    monkeypatch.setattr(
        live_sop,
        "generate_decision",
        lambda _context: (model_decision, [{"status": "completed"}], "hash", {}),
    )

    assert live_sop.process_due_live_sop()
    assert fake.sent == [("text", model_decision.reply)]
    with session_factory() as db:
        job = db.scalar(select(LiveSopJob).where(LiveSopJob.enrollment_id == enrollment_id))
        assert job.status == "submitted"
        assert job.payload["model_decision"]["touch_goal"] == "soft_nurture"
        assert job.payload["model_attempts"] == 1
        assert db.scalar(select(OutboundMessage)).chatwoot_message_id == 1001


def test_live_silence_touch_skips_without_reserving_or_sending_when_no_value_remains(session_factory, monkeypatch):
    fake, _sop_id, version_id = setup(session_factory, monkeypatch)
    enrollment_id = enroll(session_factory, version_id, request_key="no-relevant-value")
    with session_factory() as db:
        job = db.scalar(select(LiveSopJob).where(LiveSopJob.enrollment_id == enrollment_id))
        job.payload = {**job.payload, "journey_trigger": "silence_mainline"}
        db.commit()

    monkeypatch.setattr(
        live_sop,
        "fetch_complete_customer_history",
        lambda _client, _remote: ([{
            "id": 100,
            "direction": "incoming",
            "content": "我先看看",
            "created_at": AT,
        }], {"context_complete": True, "context_messages": 1}),
    )
    no_action = EvaluationDecision(
        action="no_action",
        branch="peach_9d",
        intent="other",
        route_variant="peach_9d_2027",
        journey_stage="considering",
        touch_reason="当前没有尚未覆盖且与客户阶段相关的内容",
        safety_flags=["silence_no_relevant_content"],
    )
    monkeypatch.setattr(
        live_sop,
        "generate_decision",
        lambda _context: (no_action, [], "hash", {"pipeline": "split_silence_touch"}),
    )

    assert live_sop.process_due_live_sop()
    assert fake.sent == []
    with session_factory() as db:
        job = db.scalar(select(LiveSopJob).where(LiveSopJob.enrollment_id == enrollment_id))
        assert job.status == "skipped"
        assert job.reason == "silence_no_relevant_content"
        assert db.scalar(select(TouchReservation)) is None
        assert db.scalar(select(OutboundMessage)) is None


def test_live_configured_silence_values_are_previous_touch_gaps(session_factory, monkeypatch):
    _fake, _sop_id, version_id = setup(session_factory, monkeypatch)
    with session_factory() as db:
        version = db.get(SopVersion, version_id)
        first = dict(version.config["nodes"][0])
        second = {**first, "key": "second", "basis": "enrollment", "delay_minutes": 10}
        version.config = {**version.config, "nodes": [first, second]}
        config = default_reception_configuration()
        config["silence"]["intervals_minutes"] = [1, 3]
        config["silence"]["max_proactive_messages_per_day"] = 2
        db.add(AppSetting(key=SETTING_KEY, value=config))
        db.commit()

        enrollment = live_sop.enroll_live_sop(
            db,
            db.get(ConversationState, 1),
            version,
            request_key="relative-gaps",
            trigger_source="passive_route",
        )
        first_job, second_job = db.scalars(select(LiveSopJob).where(
            LiveSopJob.enrollment_id == enrollment.id
        ).order_by(LiveSopJob.id)).all()
        assert first_job.status == "scheduled"
        assert second_job.status == "waiting_dependency"
        assert second_job.scheduled_at is None
        assert second_job.predecessor_id == first_job.id
        assert [first_job.payload["delay_minutes"], second_job.payload["delay_minutes"]] == [1, 3]
        assert [first_job.payload["basis"], second_job.payload["basis"]] == ["enrollment", "previous_node"]


def test_distinct_images_in_the_same_content_group_are_not_duplicates(session_factory, monkeypatch, tmp_path):
    setup(session_factory, monkeypatch)
    first_path = tmp_path / "first.jpg"
    second_path = tmp_path / "second.jpg"
    first_path.write_bytes(b"first")
    second_path.write_bytes(b"second")
    with session_factory() as db:
        first = StoredMedia(tenant_id=1, original_name="first.jpg", media_type="image",
                            mime_type="image/jpeg", file_size=5, storage_path=str(first_path), created_by=1)
        second = StoredMedia(tenant_id=1, original_name="second.jpg", media_type="image",
                             mime_type="image/jpeg", file_size=6, storage_path=str(second_path), created_by=1)
        db.add_all([first, second])
        db.flush()
        db.add(OutboundMessage(
            conversation_state_id=1, idempotency_key="first-image", content="",
            content_type="image", media_id=first.id, status="sent",
        ))
        db.commit()
        first_id, second_id = first.id, second.id

    def asset_for(_db, media):
        family = "attraction:pabongka" if media.id == first_id else "attraction:xiuba"
        return SimpleNamespace(metadata_json={
            "stored_media_id": media.id,
            "content_family": family,
            "content_group_key": "peach_9d_2027:peach_highlights",
        })

    monkeypatch.setattr(live_sop, "by_media", asset_for)
    with session_factory() as db:
        state = db.get(ConversationState, 1)
        assert live_sop._media_previously_sent(db, state, {
            "media_id": second_id,
            "content_family": "attraction:xiuba",
            "content_group_key": "peach_9d_2027:peach_highlights",
        }) is False


def test_live_sop_skips_question_when_model_memory_already_has_required_slot(session_factory, monkeypatch):
    fake, _sop_id, version_id = setup(session_factory, monkeypatch)
    enrollment_id = enroll(session_factory, version_id, request_key="known-party")
    with session_factory() as db:
        job = db.scalar(select(LiveSopJob).where(LiveSopJob.enrollment_id == enrollment_id))
        job.payload = {**job.payload, "skip_if_slots_present": ["party_size"]}
        journey = db.scalar(select(ConversationJourney))
        journey.slots = {**journey.slots, "party_size": 2}
        db.commit()

    assert live_sop.process_due_live_sop()
    assert fake.sent == []
    with session_factory() as db:
        job = db.scalar(select(LiveSopJob).where(LiveSopJob.enrollment_id == enrollment_id))
        assert job.status == "already_provided"
        assert job.reason == "required_slots_already_known"
        assert db.get(LiveSopEnrollment, enrollment_id).status == "completed"


def test_live_sop_uses_runtime_allowlist_instead_of_published_test_ids(session_factory, monkeypatch):
    _fake, _sop_id, version_id = setup(session_factory, monkeypatch)
    with session_factory() as db:
        from app.automation_models import SopVersion
        version = db.get(SopVersion, version_id)
        version.config = {**version.config, "test_conversation_ids": [99]}
        db.add(AppSetting(
            key="ai_reception_rollout",
            value={"allowlist_enabled": True, "conversation_ids": [26]},
        ))
        db.commit()
        enrollment = live_sop.enroll_live_sop(
            db, db.get(ConversationState, 1), version, request_key="x"
        )
        assert enrollment.status == "active"


def test_live_sop_runtime_allowlist_blocks_unlisted_conversation(session_factory, monkeypatch):
    _fake, _sop_id, version_id = setup(session_factory, monkeypatch)
    with session_factory() as db:
        from app.automation_models import SopVersion
        db.add(AppSetting(
            key="ai_reception_rollout",
            value={"allowlist_enabled": True, "conversation_ids": [99]},
        ))
        db.commit()
        with pytest.raises(live_sop.ReplyBlocked, match="test_conversation_required"):
            live_sop.enroll_live_sop(
                db, db.get(ConversationState, 1), db.get(SopVersion, version_id), request_key="x"
            )


def test_live_sop_ai_label_scope_allows_labeled_conversation_without_allowlist(session_factory, monkeypatch):
    fake, _sop_id, version_id = setup(session_factory, monkeypatch)
    monkeypatch.setattr(settings, "live_sop_scope", "ai_label")
    monkeypatch.setattr(settings, "live_sop_conversation_ids", "")
    enrollment_id = enroll(session_factory, version_id, request_key="ai-label-scope")
    assert live_sop.process_due_live_sop()
    assert fake.sent == [("text", "真实测试消息")]
    with session_factory() as db:
        assert db.get(LiveSopEnrollment, enrollment_id).status == "completed"


def test_live_sop_ai_label_scope_rejects_unlabeled_conversation(session_factory, monkeypatch):
    _fake, _sop_id, version_id = setup(session_factory, monkeypatch)
    monkeypatch.setattr(settings, "live_sop_scope", "ai_label")
    monkeypatch.setattr(settings, "live_sop_conversation_ids", "")
    with session_factory() as db:
        state = db.get(ConversationState, 1)
        state.labels = []
        state.ai_label_present = False
        db.commit()
        from app.automation_models import SopVersion
        with pytest.raises(live_sop.ReplyBlocked, match="ai_opt_in_required"):
            live_sop.enroll_live_sop(
                db, state, db.get(SopVersion, version_id), request_key="not-labeled"
            )


@pytest.mark.parametrize("change", ["label", "incoming"])
def test_removed_ai_label_or_new_customer_message_cancels_without_send(session_factory, monkeypatch, change):
    fake, _sop_id, version_id = setup(session_factory, monkeypatch)
    enrollment_id = enroll(session_factory, version_id, request_key=f"request-{change}")
    if change == "label":
        fake.labels = []
    else:
        fake.messages.append({"id": 101, "created_at": iso(dt(AT) + timedelta(minutes=1)),
                              "message_type": 0, "private": False, "content": "先不用", "content_attributes": {}})
    assert live_sop.process_due_live_sop()
    assert fake.sent == []
    with session_factory() as db:
        assert db.get(LiveSopEnrollment, enrollment_id).status == "cancelled"
        assert db.scalar(select(func.count()).select_from(OutboundMessage)) == 0


@pytest.mark.parametrize("attachment", [False, True])
def test_immediate_failed_part_preserves_failure_and_pauses(session_factory, monkeypatch, attachment, tmp_path):
    fake, _sop_id, version_id = setup(session_factory, monkeypatch)
    enrollment_id = enroll(session_factory, version_id)
    method = "create_attachment_message" if attachment else "create_text_message"
    monkeypatch.setattr(fake, method, lambda *_args: {"id": 1001, "status": "failed"})
    with session_factory() as db:
        media = None
        if attachment:
            from test_live_reply import add_image
            from app.models import MaterialAsset
            asset = db.get(MaterialAsset, add_image(db, tmp_path))
            media = db.get(StoredMedia, asset.metadata_json["stored_media_id"])
        job = db.scalar(select(LiveSopJob))
        with pytest.raises(live_sop.ReplyBlocked, match="^channel_send_failed$"):
            live_sop._submit_part(
                db, fake, db.get(ConversationState, 1), db.get(LiveSopEnrollment, enrollment_id),
                job, {"key": "failed", "content": "failed part"}, media, db.get(SopVersion, version_id),
            )
        db.rollback()
    with session_factory() as db:
        out = db.scalar(select(OutboundMessage))
        assert out.status == "failed"
        assert out.error_code == "channel_send_failed"
        assert out.chatwoot_message_id == 1001
        message = db.scalar(select(MessageEvent).where(MessageEvent.chatwoot_message_id == 1001))
        assert message.status == "failed"
        assert db.get(LiveSopEnrollment, enrollment_id).status == "attention_required"
        assert db.scalar(select(LiveSopJob)).status == "blocked"


def test_immediate_failure_stops_remaining_parts_without_progress(session_factory, monkeypatch):
    fake, _sop_id, version_id = setup(session_factory, monkeypatch)
    enrollment_id = enroll(session_factory, version_id)
    with session_factory() as db:
        job = db.scalar(select(LiveSopJob))
        job.payload = {**job.payload, "messages": [
            {"key": "first", "content_type": "text", "content": "fails"},
            {"key": "second", "content_type": "text", "content": "must not send"},
        ]}
        db.commit()
    original = fake.create_text_message

    def fail_immediately(*args):
        return {**original(*args), "status": "failed"}

    monkeypatch.setattr(fake, "create_text_message", fail_immediately)
    assert live_sop.process_due_live_sop()
    assert not live_sop.process_due_live_sop()
    assert fake.sent == [("text", "fails")]
    with session_factory() as db:
        enrollment = db.get(LiveSopEnrollment, enrollment_id)
        assert enrollment.status == "attention_required"
        assert enrollment.exit_reason == "channel_send_failed"
        job = db.scalar(select(LiveSopJob))
        assert job.status == "blocked"
        assert job.reason == "channel_send_failed"
        assert job.confirmed_at is None
        out = db.scalar(select(OutboundMessage))
        assert out.status == "failed"
        assert out.error_code == "channel_send_failed"
        journey = db.scalar(select(ConversationJourney))
        assert journey is None or not journey.sent_groups
        reservation = db.scalar(select(TouchReservation))
        assert reservation.status != "submitted"
        assert reservation.confirmed_at is None


def setup_dynamic_delivery(session_factory, monkeypatch, decision):
    fake, _sop_id, version_id = setup(session_factory, monkeypatch)
    enrollment_id = enroll(session_factory, version_id)
    with session_factory() as db:
        job = db.scalar(select(LiveSopJob))
        job.payload = {**job.payload, "journey_trigger": "silence_mainline"}
        db.commit()
    monkeypatch.setattr(live_sop, "fetch_complete_customer_history", lambda *_args: (
        [{"id": 100, "direction": "incoming", "content": "details", "created_at": AT}], {}
    ))
    monkeypatch.setattr(live_sop, "generate_decision", lambda _context: (decision, [], "", {}))
    monkeypatch.setattr(live_sop, "prepare_route_reply", lambda db, state, value, *_args: (
        value, live_sop.journey_for(db, state)
    ))
    monkeypatch.setattr(live_sop, "apply_model_policy", lambda _db, _state, value, *_args: (
        value, None, False, None
    ))
    return fake, enrollment_id, version_id


@pytest.mark.parametrize("interval,route_interval,expected_interval", [(None, None, 2), (None, 3, 3), (5, 3, 3), (0, 3, 3), (5, 0, 0)])
@pytest.mark.parametrize("interrupt", [False, True])
def test_dynamic_delivery_orders_assets_body_question_and_gates_each_part(
    session_factory, monkeypatch, tmp_path, interval, route_interval, expected_interval, interrupt
):
    import hashlib
    from app.route_reply import make_route_snapshot, ROUTE_SNAPSHOTS_KEY
    path = tmp_path / "dynamic.jpg"
    path.write_bytes(b"dynamic")
    route = {"groups": {"itinerary_overview": {"text": "body", "assets": ["asset"], "delivery_mode": "assets_then_text"}},
             "asset_hashes": {"asset": hashlib.sha256(b"dynamic").hexdigest()}}
    if route_interval is not None:
        route["initial_delivery_interval_seconds"] = route_interval
    decision = EvaluationDecision(
        action="reply", branch="peach_9d", intent="other", reply="legacy combined",
        reply_body="body", follow_up_question="party size?", follow_up_type="slot",
        follow_up_field="party_size", route_variant="peach_9d_2027", material_keys=["asset"],
        covered_content_groups=["itinerary_overview"],
    )
    fake, enrollment_id, version_id = setup_dynamic_delivery(session_factory, monkeypatch, decision)
    with session_factory() as db:
        media = StoredMedia(tenant_id=1, original_name="dynamic.jpg", media_type="image",
                            mime_type="image/jpeg", file_size=7, storage_path=str(path), created_by=1)
        db.add(media)
        journey = db.scalar(select(ConversationJourney))
        journey.slots = {ROUTE_SNAPSHOTS_KEY: {journey.route_variant: make_route_snapshot(journey.route_variant, route)}}
        version = db.get(SopVersion, version_id)
        version.config = {**version.config}
        version.config.pop("initial_delivery_interval_seconds", None)
        if interval is not None:
            version.config = {**version.config, "initial_delivery_interval_seconds": interval}
        db.commit()
        media_id = media.id
    info = {"asset_key": "asset", "content_type": "image", "media_id": media_id}
    monkeypatch.setattr(live_sop, "resolve_materials", lambda *_args, **_kwargs: [info])
    monkeypatch.setattr(live_sop, "_material", lambda db, *_args: (info, db.get(StoredMedia, media_id)))
    monkeypatch.setattr(live_sop, "_media_previously_sent", lambda *_args: False)
    monkeypatch.setattr(live_sop, "ROUTES", {"peach_9d_2027": {"initial_delivery_interval_seconds": 99}})
    waits = []

    def wait(seconds):
        waits.append(seconds)
        if interrupt:
            fake.labels = []

    monkeypatch.setattr(live_sop.time, "sleep", wait)
    assert live_sop.process_due_live_sop()
    interrupted = interrupt and expected_interval > 0
    assert fake.sent == ([("image", "")] if interrupted else [
        ("image", ""), ("text", "body"), ("text", "party size?")
    ])
    assert waits == ([expected_interval] * (1 if interrupted else 2) if expected_interval else [])
    with session_factory() as db:
        job = db.scalar(select(LiveSopJob))
        assert job.status == ("blocked" if interrupted else "submitted")
        assert db.get(LiveSopEnrollment, enrollment_id).status != "active"


@pytest.mark.parametrize("verification", [False, True])
def test_dynamic_no_action_is_not_delivery(session_factory, monkeypatch, verification):
    decision = EvaluationDecision(action="no_action", branch="peach_9d", intent="other", reply="",
        safety_flags=["silence_verification_failed_no_action"] if verification else [])
    fake, _enrollment_id, _version_id = setup_dynamic_delivery(session_factory, monkeypatch, decision)
    assert live_sop.process_due_live_sop()
    assert fake.sent == []
    with session_factory() as db:
        assert db.scalar(select(LiveSopJob)).status == ("verification_blocked" if verification else "skipped")
        journey = db.scalar(select(ConversationJourney))
        assert journey is None or not journey.sent_groups


@pytest.mark.parametrize("prior_status", ["submitted", "already_provided"])
def test_dynamic_context_does_not_infer_delivery_from_prior_job(session_factory, monkeypatch, prior_status):
    decision = EvaluationDecision(action="no_action", branch="peach_9d", intent="other")
    _fake, enrollment_id, _version_id = setup_dynamic_delivery(session_factory, monkeypatch, decision)
    with session_factory() as db:
        prior = db.scalar(select(LiveSopJob))
        prior.status = prior_status
        db.add(LiveSopJob(enrollment_id=enrollment_id, node_key="next", scheduled_at=AT,
                          payload={"journey_trigger": "silence_mainline"}))
        db.commit()
    contexts = []

    def generate(context):
        contexts.append(context)
        return decision, [], "", {}

    monkeypatch.setattr(live_sop, "generate_decision", generate)
    assert live_sop.process_due_live_sop()
    assert "itinerary_overview" not in contexts[0]["journey"]["sent_content_groups"]


@pytest.mark.parametrize("status", ["submitted", "sent"])
@pytest.mark.parametrize("exact", [False, True])
def test_static_progress_requires_confirmed_exact_approved_text(session_factory, monkeypatch, status, exact):
    from app.route_reply import route_snapshot_from_values
    fake, _sop_id, version_id = setup(session_factory, monkeypatch)
    enrollment_id = enroll(session_factory, version_id)
    with session_factory() as db:
        _, journey = live_sop.prepare_route_reply(db, db.get(ConversationState, 1), EvaluationDecision(
            action="no_action", branch="peach_9d", intent="other", route_variant="peach_9d_2027",
        ), None)
        group, spec = next((key, value) for key, value in route_snapshot_from_values(
            journey.route_variant, journey.slots)["groups"].items() if value.get("text") and not value.get("assets"))
        content = spec["text"] if exact else spec["text"] + " extra"
        job = db.scalar(select(LiveSopJob))
        job.payload = {**job.payload, "content_group_key": group,
                       "messages": [{"key": "approved", "content_type": "text", "content": content}]}
        db.commit()
    original = fake.create_text_message

    def submit(*args):
        with session_factory() as db:
            out = db.scalar(select(OutboundMessage))
            assert out.status == "submission_unknown"
            assert out.content_attributes["_delivery_item"]["item_id"] == out.idempotency_key
            assert out.content_attributes["_delivery_item"]["group_keys"] == [group]
        return {**original(*args), "status": status}

    monkeypatch.setattr(fake, "create_text_message", submit)
    assert live_sop.process_due_live_sop()
    with session_factory() as db:
        journey = db.scalar(select(ConversationJourney))
        assert (group in journey.sent_groups) is (exact and status == "sent")
        assert db.scalar(select(OutboundMessage)).status == status
        assert db.get(LiveSopEnrollment, enrollment_id).status == "completed"


@pytest.mark.parametrize("dynamic", [False, True])
def test_missing_old_snapshot_pauses_before_model_or_static_send(session_factory, monkeypatch, dynamic):
    from app.models import HandoffTask
    fake, _sop_id, version_id = setup(session_factory, monkeypatch)
    enrollment_id = enroll(session_factory, version_id)
    with session_factory() as db:
        journey = db.scalar(select(ConversationJourney))
        journey.slots = {}
        if dynamic:
            job = db.scalar(select(LiveSopJob))
            job.payload = {**job.payload, "journey_trigger": "silence_mainline"}
        db.commit()
    monkeypatch.setattr(live_sop, "generate_decision", lambda *_args: pytest.fail("must not call model"))
    assert live_sop.process_due_live_sop()
    assert fake.sent == []
    with session_factory() as db:
        assert db.get(LiveSopEnrollment, enrollment_id).status == "attention_required"
        assert db.scalar(select(LiveSopJob)).reason == "automatic_delivery_paused"
        assert db.scalar(select(HandoffTask)) is not None
        assert db.scalar(select(OutboundMessage)) is None


def test_duplicate_image_does_not_skip_unsent_static_text(session_factory, monkeypatch, tmp_path):
    fake, _sop_id, version_id = setup(session_factory, monkeypatch)
    enroll(session_factory, version_id)
    path = tmp_path / "duplicate.jpg"
    path.write_bytes(b"duplicate")
    with session_factory() as db:
        media = StoredMedia(tenant_id=1, original_name=path.name, media_type="image", mime_type="image/jpeg",
                            file_size=9, storage_path=str(path), created_by=1)
        db.add(media)
        db.flush()
        media_id = media.id
        job = db.scalar(select(LiveSopJob))
        job.payload = {**job.payload, "skip_if_materials_provided": True, "messages": [
            {"key": "image", "content_type": "image", "media_id": media_id},
            {"key": "copy", "content_type": "text", "content": "unsent copy"},
        ]}
        db.commit()
    monkeypatch.setattr(live_sop, "_material", lambda db, *_args: (
        {"media_id": media_id}, db.get(StoredMedia, media_id)))
    monkeypatch.setattr(live_sop, "_media_previously_sent", lambda *_args: True)
    assert live_sop.process_due_live_sop()
    assert fake.sent == [("text", "unsent copy")]
    with session_factory() as db:
        assert db.scalar(select(ConversationJourney)).sent_groups == []


def test_submitted_approved_text_is_reused_across_sop_rounds(session_factory, monkeypatch):
    from app.route_reply import route_snapshot_from_values
    fake, _sop_id, version_id = setup(session_factory, monkeypatch)
    enrollment_id = enroll(session_factory, version_id)
    with session_factory() as db:
        journey = db.scalar(select(ConversationJourney))
        group, spec = next((key, value) for key, value in route_snapshot_from_values(
            journey.route_variant, journey.slots)["groups"].items() if value.get("text") and not value.get("assets"))
        job = db.scalar(select(LiveSopJob))
        job.payload = {**job.payload, "content_group_key": group,
                       "messages": [{"key": "fixed", "content": spec["text"]}]}
        db.commit()
    assert live_sop.process_due_live_sop()
    second_id = enroll(session_factory, version_id, "round-two", reenroll=True, allow_repeat_delivery=True)
    with session_factory() as db:
        first = db.scalar(select(LiveSopJob).where(LiveSopJob.enrollment_id == enrollment_id))
        second = db.scalar(select(LiveSopJob).where(LiveSopJob.enrollment_id == second_id))
        second.payload = dict(first.payload)
        db.commit()
    assert live_sop.process_due_live_sop()
    assert len(fake.sent) == 1
    with session_factory() as db:
        assert db.scalar(select(func.count()).select_from(OutboundMessage)) == 1
        assert group not in db.scalar(select(ConversationJourney)).sent_groups


def test_static_without_any_bound_route_pauses_instead_of_binding_latest(session_factory, monkeypatch):
    fake, _sop_id, version_id = setup(session_factory, monkeypatch)
    enrollment_id = enroll(session_factory, version_id)
    with session_factory() as db:
        journey = db.scalar(select(ConversationJourney))
        journey.route_variant, journey.slots = "", {}
        db.commit()
    assert live_sop.process_due_live_sop()
    assert not fake.sent
    with session_factory() as db:
        assert db.scalar(select(LiveSopJob)).reason == "delivery_snapshot_unknown"
        assert db.get(LiveSopEnrollment, enrollment_id).status == "attention_required"


def test_unapproved_route_assets_pause_before_model(session_factory, monkeypatch):
    from app.delivery_tracking import pin_route_assets
    decision = EvaluationDecision(action="no_action", branch="peach_9d", intent="other")
    fake, enrollment_id, _version_id = setup_dynamic_delivery(session_factory, monkeypatch, decision)
    monkeypatch.setattr(live_sop, "pin_route_assets", pin_route_assets)
    monkeypatch.setattr(live_sop, "generate_decision", lambda *_args: pytest.fail("must pin before model"))
    assert live_sop.process_due_live_sop()
    assert not fake.sent
    with session_factory() as db:
        assert db.get(LiveSopEnrollment, enrollment_id).status == "attention_required"
        assert db.scalar(select(LiveSopJob)).reason == "delivery_asset_not_approved"


@pytest.mark.parametrize("recorded_hash,expected", [("same", True), ("other", False), (None, False)])
def test_duplicate_images_require_historical_exact_hash(session_factory, monkeypatch, recorded_hash, expected):
    setup(session_factory, monkeypatch)
    with session_factory() as db:
        media = StoredMedia(tenant_id=1, original_name="old.jpg", media_type="image", mime_type="image/jpeg",
                            file_size=1, storage_path="unused.jpg", created_by=1)
        db.add(media)
        db.flush()
        db.add(OutboundMessage(conversation_state_id=1, idempotency_key="old-image", content="",
                               media_id=media.id, status="submitted", content_attributes={
                                   "_delivery_item": {"asset_hash": recorded_hash}}))
        db.commit()
        assert live_sop._media_previously_sent(db, db.get(ConversationState, 1), {
            "media_id": media.id, "media_hash": "same", "content_family": "same-family",
        }) is expected


@pytest.mark.parametrize("trigger_source", ["passive_route", "manual_test"])
def test_enrollment_uses_snapshot_sequence_only_for_passive_route(session_factory, monkeypatch, trigger_source):
    from app.route_reply import make_route_snapshot, ROUTE_SNAPSHOTS_KEY
    _fake, _sop_id, version_id = setup(session_factory, monkeypatch)
    old_nodes = [
        {"key": "old-photo", "content_group_key": "overview", "initial_delivery": True,
         "schedule_type": "relative", "basis": "enrollment", "delay_minutes": 0,
         "messages": [{"key": "photo", "asset_key": "old-asset", "content_type": "image"}]},
        {"key": "old-copy", "content_group_key": "overview", "initial_delivery": True,
         "schedule_type": "relative", "basis": "previous_node", "delay_minutes": 0,
         "messages": [{"key": "copy", "content_type": "text", "content": "old approved copy"}]},
    ]
    spec = {"groups": {"overview": {"text": "old approved copy", "assets": ["old-asset"]},
                       "old-question": {"text": "party size?", "assets": []}},
            "policies": {"party_question_group": "old-question"},
            "initial_delivery_interval_seconds": 7,
            "sop": {"nodes": old_nodes},
            "asset_hashes": {"old-asset": "old-hash"},
            "asset_bindings": {"old-asset": {"asset_key": "old-asset", "media_id": 321,
                                             "media_hash": "old-hash", "content_type": "image"}}}
    with session_factory() as db:
        journey = db.scalar(select(ConversationJourney))
        journey.slots = {ROUTE_SNAPSHOTS_KEY: {journey.route_variant: make_route_snapshot(journey.route_variant, spec)}}
        version = db.get(SopVersion, version_id)
        custom = {"key": "new-custom", "schedule_type": "relative", "basis": "enrollment", "delay_minutes": 0,
                  "messages": [{"key": "new", "content_type": "text", "content": "new version copy"}]}
        version.config = {**version.config, "nodes": [custom]}
        db.commit()
        row = live_sop.enroll_live_sop(db, db.get(ConversationState, 1), version,
            request_key="frozen-sequence", trigger_source=trigger_source,
            deferred_follow_up={"type": "slot", "field": "party_size", "question": "party size?"})
        jobs = db.scalars(select(LiveSopJob).where(LiveSopJob.enrollment_id == row.id).order_by(LiveSopJob.id)).all()
        if trigger_source == "manual_test":
            assert [job.node_key for job in jobs] == ["new-custom"]
            assert jobs[0].payload == custom
        else:
            assert [job.node_key for job in jobs] == ["old-photo", "old-copy", "initial_delivery_follow_up"]
            assert jobs[0].payload["messages"][0]["media_id"] == 321
            assert jobs[0].payload["messages"][0]["media_hash"] == "old-hash"
            assert jobs[1].payload["messages"][0]["content"] == "old approved copy"
            assert jobs[2].payload["content_group_key"] == "old-question"
            assert jobs[2].payload["delay_seconds"] == 7
            assert jobs[1].predecessor_id == jobs[0].id
            assert jobs[2].predecessor_id == jobs[1].id
            assert "media_id" not in spec["sop"]["nodes"][0]["messages"][0]


def test_passive_enrollment_missing_snapshot_does_not_use_new_version(session_factory, monkeypatch):
    _fake, _sop_id, version_id = setup(session_factory, monkeypatch)
    with session_factory() as db:
        journey = db.scalar(select(ConversationJourney))
        journey.slots = {}
        db.commit()
        with pytest.raises(ValueError, match="delivery_snapshot_unknown"):
            live_sop.enroll_live_sop(db, db.get(ConversationState, 1), db.get(SopVersion, version_id),
                request_key="missing-snapshot", trigger_source="passive_route")
        assert db.scalar(select(func.count()).select_from(LiveSopEnrollment)) == 0


def enroll_multimessage_snapshot(session_factory, monkeypatch, trigger_source="passive_route"):
    from app.route_reply import make_route_snapshot, ROUTE_SNAPSHOTS_KEY
    fake, _sop_id, version_id = setup(session_factory, monkeypatch)
    node = {"key": "mainline", "content_group_key": "overview", "initial_delivery": True,
            "schedule_type": "relative", "basis": "enrollment", "delay_minutes": 0,
            "messages": [{"key": "first", "content_type": "text", "content": "first"},
                         {"key": "second", "content_type": "text", "content": "second"}]}
    spec = {"groups": {"overview": {"text": "complete approved overview", "assets": []},
                       "question": {"text": "party size?", "assets": []}},
            "policies": {"party_question_group": "question"},
            "initial_delivery_interval_seconds": 2, "sop": {"nodes": [node]}}
    with session_factory() as db:
        journey = db.scalar(select(ConversationJourney))
        journey.slots = {ROUTE_SNAPSHOTS_KEY: {journey.route_variant: make_route_snapshot(journey.route_variant, spec)}}
        version = db.get(SopVersion, version_id)
        version.config = {**version.config, "nodes": [node]}
        db.commit()
        enrollment = live_sop.enroll_live_sop(db, db.get(ConversationState, 1), version,
            request_key="split-mainline", trigger_source=trigger_source,
            deferred_follow_up={"type": "slot", "field": "party_size", "question": "party size?"})
        db.commit()
        return fake, enrollment.id


@pytest.mark.parametrize("trigger_source", ["passive_route", "manual_test"])
def test_only_passive_static_nodes_expand_after_deferred_question(session_factory, monkeypatch, trigger_source):
    _fake, enrollment_id = enroll_multimessage_snapshot(session_factory, monkeypatch, trigger_source)
    with session_factory() as db:
        jobs = db.scalars(select(LiveSopJob).where(LiveSopJob.enrollment_id == enrollment_id).order_by(LiveSopJob.id)).all()
        if trigger_source == "manual_test":
            assert len(jobs) == 2
            assert len(jobs[0].payload["messages"]) == 2
        else:
            assert len(jobs) == 3
            assert [job.payload["messages"][0]["content"] for job in jobs] == ["first", "second", "party size?"]
            assert all(len(job.payload["messages"]) == 1 for job in jobs)
            assert jobs[1].payload["delay_seconds"] == 2
            assert jobs[1].predecessor_id == jobs[0].id
            assert jobs[2].predecessor_id == jobs[1].id
            assert [job.status for job in jobs] == ["scheduled", "waiting_dependency", "waiting_dependency"]
        assert jobs[-1].node_key == "initial_delivery_follow_up"


@pytest.mark.parametrize("interrupt", [False, True])
def test_expanded_static_delivery_waits_and_checks_customer_before_next_part(session_factory, monkeypatch, interrupt):
    fake, enrollment_id = enroll_multimessage_snapshot(session_factory, monkeypatch)
    assert live_sop.process_due_live_sop()
    assert fake.sent == [("text", "first")]
    assert not live_sop.process_due_live_sop()
    with session_factory() as db:
        jobs = db.scalars(select(LiveSopJob).where(LiveSopJob.enrollment_id == enrollment_id).order_by(LiveSopJob.id)).all()
        assert dt(jobs[1].scheduled_at) == dt(AT) + timedelta(seconds=2)
        assert db.scalar(select(ConversationJourney)).sent_groups == []
    monkeypatch.setattr(live_sop, "utcnow", lambda: iso(dt(AT) + timedelta(seconds=2)))
    if interrupt:
        fake.messages.append({"id": 101, "created_at": iso(dt(AT) + timedelta(seconds=1)),
                              "message_type": 0, "private": False, "content": "wait", "content_attributes": {}})
    assert live_sop.process_due_live_sop()
    assert fake.sent == ([("text", "first")] if interrupt else [("text", "first"), ("text", "second")])
    with session_factory() as db:
        jobs = db.scalars(select(LiveSopJob).where(LiveSopJob.enrollment_id == enrollment_id).order_by(LiveSopJob.id)).all()
        assert jobs[1].status == ("blocked" if interrupt else "submitted")
        if interrupt:
            assert jobs[1].reason == "customer_new_message"
            assert jobs[2].status == "cancelled"
        else:
            assert dt(jobs[2].scheduled_at) == dt(AT) + timedelta(seconds=4)


@pytest.mark.parametrize("change", [None, "conversation_state_id", "source_id", "source_type", "content", "media_id",
                                  "content_type", "group_keys", "item_id", "snapshot_digest", "missing_metadata"])
def test_existing_part_requires_identical_delivery_identity(session_factory, monkeypatch, change):
    from copy import deepcopy
    fake, _sop_id, version_id = setup(session_factory, monkeypatch)
    enrollment_id = enroll(session_factory, version_id)
    with session_factory() as db:
        state = db.get(ConversationState, 1)
        enrollment = db.get(LiveSopEnrollment, enrollment_id)
        job = db.scalar(select(LiveSopJob))
        version = db.get(SopVersion, version_id)
        item = {"key": "identity", "content": "original text"}
        out = live_sop._submit_part(db, fake, state, enrollment, job, item, None, version)
        if change in {"conversation_state_id", "source_id", "media_id"}:
            setattr(out, change, 999)
        elif change in {"source_type", "content", "content_type"}:
            setattr(out, change, "different")
        elif change:
            attrs = deepcopy(out.content_attributes)
            if change == "missing_metadata":
                attrs = {}
            else:
                attrs["_delivery_item"][change] = ["different"] if change == "group_keys" else "different"
            out.content_attributes = attrs
        db.commit()
        monkeypatch.setattr(live_sop, "_verified_remote", lambda *_args: None)
        if change:
            with pytest.raises(live_sop.ReplyBlocked, match="^delivery_item_identity_mismatch$"):
                live_sop._submit_part(db, fake, state, enrollment, job, item, None, version)
        else:
            assert live_sop._submit_part(db, fake, state, enrollment, job, item, None, version).id == out.id
        assert len(fake.sent) == 1


def test_media_approval_revoked_after_reservation_blocks_actual_send(session_factory, monkeypatch, tmp_path):
    from test_live_reply import add_image
    from app.models import MaterialAsset
    fake, _sop_id, version_id = setup(session_factory, monkeypatch)
    enrollment_id = enroll(session_factory, version_id)
    with session_factory() as db:
        asset = db.get(MaterialAsset, add_image(db, tmp_path))
        media = db.get(StoredMedia, asset.metadata_json["stored_media_id"])
        item = {"key": "revoked", "media_id": media.id, "content_type": "image", "content": ""}
        version = db.get(SopVersion, version_id)
        live_sop._material(db, item, version, 1)
        commit = db.commit
        revoked = False

        def commit_then_revoke():
            nonlocal revoked
            commit()
            if not revoked and db.scalar(select(OutboundMessage)) is not None:
                revoked = True
                with session_factory() as other:
                    current = other.get(MaterialAsset, asset.id)
                    current.metadata_json = {**current.metadata_json, "live_approved": False}
                    other.commit()

        monkeypatch.setattr(db, "commit", commit_then_revoke)
        with pytest.raises(live_sop.ReplyBlocked, match="^material_not_approved_for_live$"):
            live_sop._submit_part(db, fake, db.get(ConversationState, 1),
                db.get(LiveSopEnrollment, enrollment_id), db.scalar(select(LiveSopJob)), item, media, version)
        assert revoked
        assert fake.sent == []
        out = db.scalar(select(OutboundMessage))
        assert out.status == "blocked"
        assert out.error_code == "material_not_approved_for_live"
        assert out.chatwoot_message_id is None


def test_dynamic_restart_reuses_saved_plan_and_sends_only_remaining_media(session_factory, monkeypatch, tmp_path):
    from copy import deepcopy
    from test_live_reply import add_image
    from app.models import MaterialAsset
    from app.route_reply import make_route_snapshot, ROUTE_SNAPSHOTS_KEY
    decision = EvaluationDecision(action="reply", branch="peach_9d", intent="other", reply="frozen body",
        reply_body="frozen body", route_variant="peach_9d_2027", material_keys=["routes12-9d-itinerary"],
        covered_content_groups=["itinerary_overview"])
    fake, enrollment_id, version_id = setup_dynamic_delivery(session_factory, monkeypatch, decision)
    with session_factory() as db:
        asset = db.get(MaterialAsset, add_image(db, tmp_path))
        info = {"asset_key": asset.asset_key, "media_id": asset.metadata_json["stored_media_id"],
                "media_hash": asset.file_hash, "content_type": "image"}
        journey = db.scalar(select(ConversationJourney))
        spec = {"groups": {"itinerary_overview": {"text": "frozen body", "assets": [asset.asset_key],
                                                   "delivery_mode": "text_then_assets"}},
                "asset_hashes": {asset.asset_key: asset.file_hash}, "asset_bindings": {asset.asset_key: info},
                "initial_delivery_interval_seconds": 2}
        journey.slots = {ROUTE_SNAPSHOTS_KEY: {journey.route_variant: make_route_snapshot(journey.route_variant, spec)}}
        db.commit()
    monkeypatch.setattr(live_sop, "resolve_materials", lambda *_args, **_kwargs: [info])
    original = fake.create_text_message

    def text_with_saved_plan(*args):
        with session_factory() as db:
            plan = db.scalar(select(LiveSopJob)).payload["dynamic_delivery_plan"]
            assert [part["content_type"] for part in plan["items"]] == ["text", "image"]
            assert plan["items"][0]["content"] == "frozen body"
            assert plan["interval_seconds"] == 2
            assert plan["decision"]["reply_body"] == "frozen body"
        return original(*args)

    def crash(_seconds):
        raise SystemExit("simulated worker restart")

    monkeypatch.setattr(fake, "create_text_message", text_with_saved_plan)
    monkeypatch.setattr(live_sop.time, "sleep", crash)
    with pytest.raises(SystemExit, match="simulated worker restart"):
        live_sop.process_due_live_sop()
    with session_factory() as db:
        job = db.scalar(select(LiveSopJob))
        assert job.status == "processing"
        saved = deepcopy(job.payload["dynamic_delivery_plan"])
        assert db.scalar(select(OutboundMessage)).status == "submitted"
        version = db.get(SopVersion, version_id)
        version.config = {**version.config, "initial_delivery_interval_seconds": 59}
        db.commit()
        live_sop.recover_live_sop_jobs(db)
        assert job.status == "scheduled"
    monkeypatch.setattr(live_sop, "generate_decision", lambda *_args: pytest.fail("must reuse saved model decision"))
    monkeypatch.setattr(live_sop, "resolve_materials", lambda *_args, **_kwargs: pytest.fail("must reuse frozen media"))
    waits = []
    monkeypatch.setattr(live_sop.time, "sleep", waits.append)
    assert live_sop.process_due_live_sop()
    assert fake.sent == [("text", "frozen body"), ("image", "")]
    assert waits == [2]
    with session_factory() as db:
        assert db.scalar(select(LiveSopJob)).payload["dynamic_delivery_plan"] == saved
        assert db.get(LiveSopEnrollment, enrollment_id).status == "completed"
        assert db.scalar(select(func.count()).select_from(OutboundMessage)) == 2


@pytest.mark.parametrize("change", ["global", "approval"])
def test_next_part_observes_independent_session_safety_change(session_factory, monkeypatch, tmp_path, change):
    from test_live_reply import add_image
    from app.models import MaterialAsset
    fake, _sop_id, version_id = setup(session_factory, monkeypatch)
    enrollment_id = enroll(session_factory, version_id)
    with session_factory() as db:
        asset = db.get(MaterialAsset, add_image(db, tmp_path))
        asset_id = asset.id
        media = db.get(StoredMedia, asset.metadata_json["stored_media_id"])
        state = db.get(ConversationState, 1)
        enrollment = db.get(LiveSopEnrollment, enrollment_id)
        job = db.scalar(select(LiveSopJob))
        version = db.get(SopVersion, version_id)
        item = {"key": "image", "media_id": media.id, "content_type": "image"}
        live_sop._material(db, item, version, 1)
        switch = db.get(AppSetting, "global_message_sending")
        live_sop._submit_part(db, fake, state, enrollment, job, {"key": "body", "content": "first"}, None, version)
        assert switch.value == {"enabled": True}
        with session_factory() as other:
            if change == "global":
                other.get(AppSetting, "global_message_sending").value = {"enabled": False}
            else:
                approved = other.get(MaterialAsset, asset_id)
                approved.metadata_json = {**approved.metadata_json, "live_approved": False}
            other.commit()
        job.payload = {**job.payload, "pending_marker": "must survive refresh"}
        with pytest.raises(live_sop.ReplyBlocked):
            live_sop._submit_part(db, fake, state, enrollment, job, item, media, version)
        assert fake.sent == [("text", "first")]
    with session_factory() as db:
        assert db.scalar(select(LiveSopJob)).payload["pending_marker"] == "must survive refresh"


@pytest.mark.parametrize("old_approved", [False, True])
def test_historical_revision_requires_its_own_live_approval(session_factory, monkeypatch, tmp_path, old_approved):
    from test_live_reply import add_image
    from app.models import MaterialAsset
    from app.material_library import replace_asset_binding
    fake, _sop_id, version_id = setup(session_factory, monkeypatch)
    enrollment_id = enroll(session_factory, version_id)
    with session_factory() as db:
        asset = db.get(MaterialAsset, add_image(db, tmp_path))
        old_media = db.get(StoredMedia, asset.metadata_json["stored_media_id"])
        old_hash = asset.file_hash
        asset.metadata_json = {**asset.metadata_json, "live_approved": old_approved}
        db.commit()
        path = tmp_path / "replacement.png"
        path.write_bytes(b"new approved revision")
        replacement = StoredMedia(tenant_id=1, original_name=path.name, media_type="image", mime_type="image/png",
                                  storage_path=str(path), file_size=21, created_by=1)
        db.add(replacement)
        db.flush()
        replace_asset_binding(db, asset, replacement)
        asset.metadata_json = {**asset.metadata_json, "live_approved": True}
        db.commit()
        item = {"key": "old-revision", "media_id": old_media.id, "media_hash": old_hash, "content_type": "image"}
        version = db.get(SopVersion, version_id)
        args = (db, fake, db.get(ConversationState, 1), db.get(LiveSopEnrollment, enrollment_id),
                db.scalar(select(LiveSopJob)), item, old_media, version)
        if old_approved:
            info, checked = live_sop._material(db, item, version, 1)
            assert info["media_hash"] == old_hash and checked.id == old_media.id
            assert live_sop._submit_part(*args).status == "submitted"
            assert fake.sent == [("image", "")]
        else:
            with pytest.raises(live_sop.ReplyBlocked, match="material_not_approved_for_live"):
                live_sop._material(db, item, version, 1)
            with pytest.raises(live_sop.ReplyBlocked, match="material_not_approved_for_live"):
                live_sop._submit_part(*args)
            assert fake.sent == []
            assert db.scalar(select(OutboundMessage)).error_code == "material_not_approved_for_live"


def test_unknown_submission_is_never_retried(session_factory, monkeypatch):
    fake, _sop_id, version_id = setup(session_factory, monkeypatch)
    enrollment_id = enroll(session_factory, version_id)
    fake.unknown = True
    assert live_sop.process_due_live_sop()
    assert not live_sop.process_due_live_sop()
    assert len(fake.sent) == 1
    with session_factory() as db:
        assert db.get(LiveSopEnrollment, enrollment_id).status == "attention_required"
        assert db.scalar(select(LiveSopJob)).status == "submission_unknown"
        assert db.scalar(select(OutboundMessage)).status == "submission_unknown"
        from app.automation_models import SopVersion
        with pytest.raises(ValueError, match="sop_round_reconciliation_required"):
            live_sop.enroll_live_sop(db, db.get(ConversationState, 1), db.get(SopVersion, version_id),
                                     request_key="unsafe-retry", reenroll=True)


def test_same_request_is_idempotent_and_completed_round_requires_explicit_reenroll(session_factory, monkeypatch):
    _fake, _sop_id, version_id = setup(session_factory, monkeypatch)
    first = enroll(session_factory, version_id, "same")
    assert enroll(session_factory, version_id, "same") == first
    assert live_sop.process_due_live_sop()
    with session_factory() as db:
        from app.automation_models import SopVersion
        try:
            live_sop.enroll_live_sop(db, db.get(ConversationState, 1), db.get(SopVersion, version_id), request_key="new")
            assert False, "explicit reenrollment required"
        except ValueError as exc:
            assert str(exc) == "sop_reenrollment_required"




def test_explicit_repeat_test_bypasses_only_test_touch_cooldown(session_factory, monkeypatch):
    fake, _sop_id, version_id = setup(session_factory, monkeypatch)
    with session_factory() as db:
        db.add(TouchReservation(contact_key="live:1:1", owner_key="older-sop", status="confirmed",
                                reserved_at=AT, expires_at=iso(dt(AT) + timedelta(hours=24)),
                                confirmed_at=AT))
        db.commit()
    enrollment_id = enroll(session_factory, version_id, request_key="repeat-test",
                           allow_repeat_delivery=True)
    assert live_sop.process_due_live_sop()
    assert fake.sent == [("text", "真实测试消息")]
    with session_factory() as db:
        enrollment = db.get(LiveSopEnrollment, enrollment_id)
        reservation = db.scalar(select(TouchReservation))
        assert enrollment.allow_repeat_delivery is True
        assert enrollment.status == "completed"
        assert reservation.owner_key == f"live-sop:{enrollment_id}"




def make_canonical_sop(session_factory, sop_id):
    with session_factory() as db:
        sop = db.get(SopDefinition, sop_id)
        sop.name = ROUTES["peach_9d_2027"]["sop"]["name"]
        db.commit()


def test_model_route_enrolls_only_the_canonical_sop_and_is_idempotent(session_factory, monkeypatch):
    _fake, sop_id, _version_id = setup(session_factory, monkeypatch)
    make_canonical_sop(session_factory, sop_id)
    with session_factory() as db:
        state = db.get(ConversationState, 1)
        first = live_sop.enroll_model_route_sop(
            db, state, "peach_9d_2027", request_key="passive-route:1:peach_9d_2027"
        )
        second = live_sop.enroll_model_route_sop(
            db, state, "peach_9d_2027", request_key="passive-route:1:peach_9d_2027"
        )
    assert first["status"] == "enrolled"
    assert second["status"] == "existing"
    assert first["enrollment_id"] == second["enrollment_id"]
    with session_factory() as db:
        row = db.get(LiveSopEnrollment, first["enrollment_id"])
        assert row.trigger_source == "passive_route"
        assert db.scalar(select(func.count()).select_from(LiveSopEnrollment)) == 1


def test_unclassified_model_reply_enrolls_route_selection_silence_sop(session_factory, monkeypatch):
    _fake, _sop_id, _version_id = setup(session_factory, monkeypatch)
    with session_factory() as db:
        generic = SopDefinition(
            tenant_id=1,
            created_by=1,
            name=UNCLASSIFIED_SOP_NAME,
            status="running",
            route_variant="",
            inbox_ids=[128859],
            test_conversation_ids=[26],
            nodes=UNCLASSIFIED_SOP_NODES,
        )
        db.add(generic)
        db.flush()
        sop_snapshot(db, generic, 1)
        db.commit()
        result = live_sop.enroll_model_route_sop(
            db, db.get(ConversationState, 1), "", request_key="passive-route:1:unclassified"
        )
    assert result["status"] == "enrolled"
    assert result["route_variant"] == ""
    with session_factory() as db:
        enrollment = db.get(LiveSopEnrollment, result["enrollment_id"])
        first = db.scalar(select(LiveSopJob).where(
            LiveSopJob.enrollment_id == enrollment.id,
            LiveSopJob.node_key == "route_selection_silence",
        ))
        assert first.payload["delay_minutes"] == 30


def test_model_route_restarts_after_customer_returns_from_completed_sop_round(session_factory, monkeypatch):
    _fake, sop_id, _version_id = setup(session_factory, monkeypatch)
    make_canonical_sop(session_factory, sop_id)
    with session_factory() as db:
        first = live_sop.enroll_model_route_sop(
            db, db.get(ConversationState, 1), "peach_9d_2027",
            request_key="passive-route:1:peach_9d_2027",
        )
    assert live_sop.process_due_live_sop()
    with session_factory() as db:
        second = live_sop.enroll_model_route_sop(
            db, db.get(ConversationState, 1), "peach_9d_2027",
            request_key="passive-route:2:peach_9d_2027",
        )
    assert first["status"] == "enrolled"
    assert second["status"] == "enrolled"
    assert second["route_variant"] == "peach_9d_2027"
    with session_factory() as db:
        rows = db.scalars(select(LiveSopEnrollment).order_by(LiveSopEnrollment.id)).all()
        assert [(row.round_number, row.status) for row in rows] == [
            (1, "completed"),
            (2, "active"),
        ]


def test_customer_message_cancels_active_round_then_model_reply_reenrolls(session_factory, monkeypatch):
    _fake, sop_id, _version_id = setup(session_factory, monkeypatch)
    make_canonical_sop(session_factory, sop_id)
    with session_factory() as db:
        first = live_sop.enroll_model_route_sop(
            db, db.get(ConversationState, 1), "peach_9d_2027",
            request_key="passive-route:1:peach_9d_2027",
        )
        customer_at = iso(dt(AT) + timedelta(minutes=1))
        message = MessageEvent(
            conversation_state_id=1,
            chatwoot_message_id=101,
            direction="incoming",
            private=False,
            content="new question",
            created_at=customer_at,
        )
        db.add(message)
        db.flush()
        assert live_sop.cancel_live_sop_on_customer_message(
            db, db.get(ConversationState, 1), message
        ) == 1
        db.commit()
    monkeypatch.setattr(live_sop, "utcnow", lambda: iso(dt(AT) + timedelta(minutes=2)))
    with session_factory() as db:
        second = live_sop.enroll_model_route_sop(
            db, db.get(ConversationState, 1), "peach_9d_2027",
            request_key="passive-route:2:peach_9d_2027",
        )
    assert first["status"] == "enrolled"
    assert second["status"] == "enrolled"
    with session_factory() as db:
        rows = db.scalars(select(LiveSopEnrollment).order_by(LiveSopEnrollment.id)).all()
        assert [(row.round_number, row.status, row.exit_reason) for row in rows] == [
            (1, "cancelled", "customer_new_message"),
            (2, "active", None),
        ]


def test_passive_route_round_is_a_continuation_after_customer_engagement(session_factory, monkeypatch):
    fake, sop_id, _version_id = setup(session_factory, monkeypatch)
    make_canonical_sop(session_factory, sop_id)
    with session_factory() as db:
        db.add(TouchReservation(
            contact_key="live:1:1",
            owner_key="prior-touch",
            status="confirmed",
            reserved_at=AT,
            expires_at=iso(dt(AT) + timedelta(hours=24)),
            confirmed_at=AT,
        ))
        result = live_sop.enroll_model_route_sop(
            db, db.get(ConversationState, 1), "peach_9d_2027",
            request_key="passive-route:1:peach_9d_2027",
        )
    assert result["status"] == "enrolled"
    assert live_sop.process_due_live_sop()
    assert fake.sent


def test_reconciler_recovers_submitted_model_owned_reply(session_factory, monkeypatch):
    _fake, sop_id, _version_id = setup(session_factory, monkeypatch)
    make_canonical_sop(session_factory, sop_id)
    with session_factory() as db:
        db.add(LiveReplyJob(
            conversation_state_id=1,
            trigger_message_id=1,
            status="submitted",
            input_ids=[100],
            decision={"action": "reply", "route_variant": "peach_9d_2027"},
            trace={"validator_version": "model-owned-contract-v2"},
            due_at=AT,
            completed_at=AT,
        ))
        db.commit()
    assert live_sop.reconcile_model_route_sops() == 1
    assert live_sop.reconcile_model_route_sops() == 0
    with session_factory() as db:
        job = db.scalar(select(LiveReplyJob))
        assert job.trace["sop_enrollment"]["status"] == "enrolled"
        assert db.scalar(select(func.count()).select_from(LiveSopEnrollment)) == 1


def test_explicit_repeat_test_resends_previously_delivered_material(session_factory, monkeypatch, tmp_path):
    fake, _sop_id, version_id = setup(session_factory, monkeypatch)
    image = tmp_path / "repeat.jpg"
    image.write_bytes(b"repeat-test-image")
    with session_factory() as db:
        media = StoredMedia(tenant_id=1, original_name="repeat.jpg", media_type="image",
                            mime_type="image/jpeg", file_size=image.stat().st_size,
                            storage_path=str(image), created_by=1)
        db.add(media)
        db.commit()
        media_id = media.id
    enrollment_id = enroll(session_factory, version_id, request_key="repeat-material",
                           allow_repeat_delivery=True)
    with session_factory() as db:
        job = db.scalar(select(LiveSopJob).where(LiveSopJob.enrollment_id == enrollment_id))
        job.payload = {**job.payload, "skip_if_materials_provided": True, "messages": [{
            "key": "photo", "content_type": "image", "content": "重复素材测试", "media_id": media_id,
        }]}
        db.commit()
    info = {"media_id": media_id, "content_family": "already-sent", "content_type": "image"}
    monkeypatch.setattr(live_sop, "_material", lambda db, *_args: (info, db.get(StoredMedia, media_id)))
    monkeypatch.setattr(live_sop, "_media_previously_sent", lambda *_args: True)
    assert live_sop.process_due_live_sop()
    assert fake.sent == [("image", "重复素材测试")]
    with session_factory() as db:
        out = db.scalar(select(OutboundMessage).where(OutboundMessage.source_id == enrollment_id))
        assert out.media_id == media_id and out.status == "submitted"
