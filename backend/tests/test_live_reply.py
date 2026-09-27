from datetime import timedelta
from pathlib import Path
import os
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select, func

import app.live_reply as live
from app.automation_models import LiveSopEnrollment, LiveSopJob
from app.automation_service import dt, iso, sop_snapshot
from app.chatwoot import ChatwootClient, ChatwootError
from app.config import settings
from app.config import Settings
from app.conversation_policy import compute_state
from app.deepseek_evaluation import EvaluationDecision
from app.live_reply_models import LiveReplyJob
from app.lead_capture import CONTACT_REQUEST, model_contacts
from app.lead_capture_models import LeadCaptureState
from app.models import (AppSetting, ChatwootConnection, Contact, ConversationJourney, ConversationState, HandoffTask,
                        InboxBinding, MessageEvent, Notification, OutboundMessage, SopDefinition, Tenant, WebhookEvent, utcnow)
from app.route_packages import ROUTES, UNCLASSIFIED_SOP_NAME, UNCLASSIFIED_SOP_NODES
from app.outbound_control import global_message_sending_enabled
from app.security import encrypt_secret
from app.material_library import candidate_materials
from app.route_reply import bind_new_route_snapshot, ROUTE_SNAPSHOTS_KEY, make_route_snapshot


def test_handoff_summary_contains_customer_context_and_progress():
    journey = SimpleNamespace(
        route_variant="peach_11d_2027",
        slots={"party_size": 8, "departure_window": "未確定"},
        sent_groups=["itinerary_overview", "hotel_reference"],
    )
    summary = live._handoff_summary(journey, "large_group_custom_quote", "我們大概8位，日期還沒定")
    assert "線路：peach_11d_2027" in summary
    assert "人數：8" in summary
    assert "客戶本輪原話：我們大概8位" in summary
    assert "已提供內容：itinerary_overview、hotel_reference" in summary


class FakeClient:
    def __init__(self, at, labels=None):
        self.labels = labels if labels is not None else ["AI"]
        self.contact_labels = []
        self.can_reply = True
        self.messages = [{"id": 100, "created_at": at, "message_type": 0, "private": False,
                          "content": "想看桃花9日行程", "attachments": []}]
        self.sent = []
        self.operations = []
        self.send_label_snapshots = []
        self.remove_ai_after_text = False
        self.remove_ai_after_image = False
        self.fail_ai_removal = False
        self.label_catalog = [{"id": 1, "title": str(label), "color": "#64748B"} for label in self.labels]
        self.fail_after_submission = False

    def get_conversation(self, _id):
        return {"id": 26, "inbox_id": 128859, "can_reply": self.can_reply, "labels": self.labels,
                "meta": {"sender": {"id": 55}, "assignee": None}}

    def get_conversation_labels(self, _id):
        return {"payload": self.labels}

    def get_contact_labels(self, _id):
        return {"payload": self.contact_labels}

    def list_labels(self):
        return {"payload": self.label_catalog}

    def create_label(self, title, description, color, show_on_sidebar):
        row = {"id": len(self.label_catalog) + 1, "title": title, "description": description,
               "color": color, "show_on_sidebar": show_on_sidebar}
        self.label_catalog.append(row)
        return row

    def set_conversation_labels(self, _id, labels):
        self.operations.append("set_labels")
        if self.fail_ai_removal and not any(label.casefold() == "ai" for label in labels):
            raise ChatwootError("label_write_failed", "failed")
        self.labels = list(labels)
        return {"payload": self.labels}

    def get_messages(self, _id, before=None):
        return {"payload": [m for m in self.messages if before is None or m["id"] < before]}

    def list_contact_conversations(self, _id):
        return {"payload": [{"id": 26, "inbox_id": 128859}]}

    def create_text_message(self, _id, content):
        self.send_label_snapshots.append(list(self.labels))
        self.operations.append("send_text")
        self.sent.append(("text", content))
        if self.remove_ai_after_text:
            self.labels = [label for label in self.labels if label.casefold() != "ai"]
        if self.fail_after_submission:
            raise httpx.ReadTimeout("unknown")
        mid = 1000 + len(self.sent)
        self.messages.append({"id": mid, "created_at": utcnow(), "message_type": 1, "content": content})
        return {"id": mid}

    def create_input_select_message(self, _id, content, options):
        self.send_label_snapshots.append(list(self.labels))
        self.operations.append("send_options")
        self.sent.append(("input_select", content, list(options)))
        if self.fail_after_submission:
            raise httpx.ReadTimeout("unknown")
        mid = 1000 + len(self.sent)
        self.messages.append({
            "id": mid,
            "created_at": utcnow(),
            "message_type": 1,
            "content": content,
            "content_type": "input_select",
            "content_attributes": {"items": [{"title": item, "value": item} for item in options]},
        })
        return {"id": mid}

    def create_attachment_message(self, _id, content, path, mime_type):
        assert Path(path).is_file()
        self.send_label_snapshots.append(list(self.labels))
        self.operations.append("send_image")
        self.sent.append(("image", content))
        if self.remove_ai_after_image:
            self.labels = [label for label in self.labels if label.casefold() != "ai"]
        mid = 1000 + len(self.sent)
        self.messages.append({"id": mid, "created_at": utcnow(), "message_type": 1})
        return {"id": mid, "attachments": [{"file_type": "image", "data_url": "https://example.invalid/image.jpg"}]}

    def close(self):
        pass


def setup(session_factory, monkeypatch, labels=None):
    now = utcnow()
    fake = FakeClient(now, labels)
    monkeypatch.setattr(settings, "app_profile", "live_reply")
    monkeypatch.setattr(settings, "outbound_mode", "live")
    monkeypatch.setattr(settings, "chatwoot_write_enabled", True)
    monkeypatch.setattr(settings, "live_sop_conversation_ids", "26")
    monkeypatch.setattr(live, "SessionLocal", session_factory)
    monkeypatch.setattr(live, "client_for", lambda _: fake)
    monkeypatch.setattr(live, "generate_decision", lambda _: (
        EvaluationDecision(
            "reply",
            "unclassified",
            "other",
            reply="您好，請問想了解哪條路線？",
            reply_options=["桃花9日", "桃花+珠峰11日"],
            journey_stage="route_selection",
        ),
        [],
        "hash",
        {},
    ))
    monkeypatch.setattr(live.time, "sleep", lambda _seconds: None)
    with session_factory() as db:
        db.add(ChatwootConnection(id=1, tenant_id=1, account_id=180474, encrypted_api_token=encrypt_secret("fake"), connection_key="fake"))
        db.add(InboxBinding(id=1, tenant_id=1, chatwoot_inbox_id=128859, name="test", channel_type="Channel::FacebookPage", ai_enabled=True))
        db.add(Contact(id=1, tenant_id=1, chatwoot_contact_id=55, name="Test"))
        db.flush()
        db.add(ConversationState(id=1, tenant_id=1, inbox_binding_id=1, contact_id=1, chatwoot_conversation_id=26))
        db.flush()
        db.add(MessageEvent(id=1, conversation_state_id=1, chatwoot_message_id=100, direction="incoming", content="test", created_at=now))
        db.add(AppSetting(key="live_reply", value={"armed_at": iso(dt(now) - timedelta(seconds=30))}))
        db.add(AppSetting(key="global_message_sending", value={"enabled": True}))
        db.flush()
        db.add(LiveReplyJob(id=1, conversation_state_id=1, trigger_message_id=1, input_ids=[100], due_at=now))
        db.commit()
    return fake


def test_default_state_is_human_even_with_inbox_on(session_factory):
    with session_factory() as db:
        tenant = db.get(Tenant, 1)
        inbox = InboxBinding(ai_enabled=True)
        assert compute_state(tenant, inbox, [], [], True)[0] == "AI_PAUSED_CONVERSATION"
        assert compute_state(tenant, inbox, ["AI"], [], True, "enabled", "synced", True)[0] == "AI_ACTIVE"


def test_ai_label_is_required_in_every_scope(session_factory):
    with session_factory() as db:
        tenant = db.get(Tenant, 1)
        inbox = InboxBinding(ai_enabled=True)
        assert compute_state(tenant, inbox, [], [], True)[0] == "AI_PAUSED_CONVERSATION"
        assert compute_state(tenant, inbox, ["AI关闭"], [], True)[0] == "AI_PAUSED_LABEL"
        assert compute_state(tenant, inbox, [], ["拒绝联系"], True)[0] == "AI_PAUSED_LABEL"
        assert compute_state(tenant, inbox, [], [], False)[0] == "AI_PAUSED_CONVERSATION"


def test_all_eligible_runtime_scope_is_rejected():
    value = Settings(live_sop_scope="all_eligible", live_sop_enabled=False)
    with pytest.raises(ValueError, match="invalid_live_sop_scope"):
        value.validate_runtime()


def test_missing_global_message_setting_fails_closed(session_factory):
    with session_factory() as db:
        assert global_message_sending_enabled(db) is False


@pytest.mark.parametrize("condition", ["no_tag", "human_tag", "contact_block", "can_reply", "handoff_task", "new_input", "human_reply", "old_input", "global_off", "message_sending_off", "unknown_submission", "timestamp_unknown"])
def test_live_gates_zero_sends(session_factory, monkeypatch, condition):
    fake = setup(session_factory, monkeypatch)
    with session_factory() as db:
        if condition == "no_tag": fake.labels = []
        if condition == "human_tag": fake.labels.append("人工接管")
        if condition == "contact_block": fake.contact_labels = ["黑名单"]
        if condition == "can_reply": fake.can_reply = False
        if condition == "handoff_task": db.add(HandoffTask(conversation_state_id=1, reason_code="test", sla_due_at=utcnow()))
        if condition == "new_input": fake.messages.append({**fake.messages[0], "id": 101})
        if condition == "human_reply": fake.messages.append({"id": 101, "message_type": 1, "content": "human"})
        if condition == "old_input": fake.messages[0]["created_at"] = iso(dt(utcnow()) - timedelta(hours=25))
        if condition == "timestamp_unknown": fake.messages[0].pop("created_at")
        if condition == "global_off": db.get(Tenant, 1).ai_enabled = False
        if condition == "message_sending_off":
            db.get(AppSetting, "global_message_sending").value = {"enabled": False}
        if condition == "unknown_submission": db.add(OutboundMessage(conversation_state_id=1, idempotency_key="unknown", content="", status="submission_unknown"))
        db.commit()
    live.process_job(1)
    assert fake.sent == []
    with session_factory() as db:
        assert db.get(LiveReplyJob, 1).status == ("submission_unknown" if condition == "unknown_submission" else "blocked")


def test_pending_contact_cannot_queue_without_ai_label(session_factory, monkeypatch):
    fake = setup(session_factory, monkeypatch, labels=[])
    with session_factory() as db:
        db.query(LiveReplyJob).delete()
        db.add(LeadCaptureState(
            conversation_state_id=1,
            status="asked",
            request_count=1,
        ))
        event = WebhookEvent(
            connection_id=1,
            event="message_created",
            account_id=180474,
            resource_id="101",
            idempotency_key="pending-contact-without-ai",
            received_at=utcnow(),
            payload={
                "event": "message_created",
                "id": 101,
                "account": {"id": 180474},
                "inbox": {"id": 128859},
                "message_type": "incoming",
                "private": False,
                "content_type": "text",
                "content": "微信: wx_test_2027",
                "created_at": utcnow(),
                "conversation": {
                    "id": 26,
                    "inbox_id": 128859,
                    "can_reply": True,
                    "labels": [],
                    "contact_inbox": {"contact_id": 55},
                    "meta": {"sender": {"id": 55, "name": "Test", "type": "contact"}},
                },
            },
        )
        db.add(event)
        db.flush()
        live.mirror_event(db, event)
        assert db.scalar(select(func.count()).select_from(LiveReplyJob)) == 0


def test_runtime_allowlist_blocks_existing_live_reply_job(session_factory, monkeypatch):
    fake = setup(session_factory, monkeypatch)
    with session_factory() as db:
        db.add(AppSetting(
            key="ai_reception_rollout",
            value={"allowlist_enabled": True, "conversation_ids": [99]},
        ))
        db.commit()
    live.process_job(1)
    assert fake.sent == []
    with session_factory() as db:
        job = db.get(LiveReplyJob, 1)
        assert job.status == "blocked"
        assert job.error_code == "test_conversation_required"


def test_runtime_allowlist_blocks_live_reply_job_creation(session_factory, monkeypatch):
    setup(session_factory, monkeypatch, labels=["ai"])
    with session_factory() as db:
        db.query(LiveReplyJob).delete()
        db.add(AppSetting(
            key="ai_reception_rollout",
            value={"allowlist_enabled": True, "conversation_ids": [99]},
        ))
        event = WebhookEvent(
            connection_id=1,
            event="message_created",
            account_id=180474,
            resource_id="101",
            idempotency_key="outside-runtime-allowlist",
            received_at=utcnow(),
            payload={
                "event": "message_created",
                "id": 101,
                "account": {"id": 180474},
                "inbox": {"id": 128859},
                "message_type": "incoming",
                "private": False,
                "content_type": "text",
                "content": "想了解行程",
                "created_at": utcnow(),
                "conversation": {
                    "id": 26,
                    "inbox_id": 128859,
                    "can_reply": True,
                    "labels": ["ai"],
                    "contact_inbox": {"contact_id": 55},
                    "meta": {"sender": {"id": 55, "name": "Test", "type": "contact"}},
                },
            },
        )
        db.add(event)
        db.flush()
        live.mirror_event(db, event)
        assert db.scalar(select(func.count()).select_from(LiveReplyJob)) == 0


def test_disabled_runtime_allowlist_still_requires_ai_label(session_factory, monkeypatch):
    fake = setup(session_factory, monkeypatch, labels=[])
    with session_factory() as db:
        db.add(AppSetting(
            key="ai_reception_rollout",
            value={"allowlist_enabled": False, "conversation_ids": [99]},
        ))
        db.commit()
    live.process_job(1)
    assert fake.sent == []
    with session_factory() as db:
        job = db.get(LiveReplyJob, 1)
        assert job.status == "blocked"
        assert job.error_code == "ai_opt_in_required"


def test_disabled_runtime_allowlist_allows_labeled_conversation(session_factory, monkeypatch):
    fake = setup(session_factory, monkeypatch, labels=["ai"])
    monkeypatch.setattr(settings, "live_sop_conversation_ids", "99")
    with session_factory() as db:
        db.add(AppSetting(
            key="ai_reception_rollout",
            value={"allowlist_enabled": False, "conversation_ids": [99]},
        ))
        db.commit()
    live.process_job(1)
    assert len(fake.sent) == 1

def test_live_text_is_idempotent(session_factory, monkeypatch):
    fake = setup(session_factory, monkeypatch)
    live.process_job(1)
    live.process_job(1)
    assert len(fake.sent) == 1
    assert fake.sent[0] == (
        "input_select",
        "您好，請問想了解哪條路線？",
        ["桃花9日", "桃花+珠峰11日"],
    )
    with session_factory() as db:
        assert db.get(LiveReplyJob, 1).status == "submitted"
        outbound = db.scalar(select(OutboundMessage))
        assert outbound.status == "submitted"
        assert outbound.content_type == "input_select"
        assert outbound.content_attributes["items"][0]["title"] == "桃花9日"


@pytest.mark.parametrize("remove_label", [False, True])
def test_opening_group_only_last_message_has_options(session_factory, monkeypatch, remove_label):
    fake = setup(session_factory, monkeypatch)
    fake.remove_ai_after_text = remove_label
    messages = ["您好～", "這裡是 China2Go。", "想了解哪條行程呢？"]
    monkeypatch.setattr(live, "generate_decision", lambda _: (
        EvaluationDecision("reply", "unclassified", "other", reply="\n".join(messages),
                           opening_messages=messages, opening_interval_seconds=2,
                           reply_options=["桃花9日", "桃花+珠峰11日"], journey_stage="route_selection"),
        [], "hash", {},
    ))
    live.process_job(1)
    assert fake.sent[0] == ("text", messages[0])
    if remove_label:
        assert len(fake.sent) == 1
    else:
        assert fake.sent[1] == ("text", messages[1])
        assert fake.sent[2] == ("input_select", messages[2], ["桃花9日", "桃花+珠峰11日"])
        live.process_job(1)
        assert len(fake.sent) == 3


def test_customer_stop_decision_only_disables_proactive_automation(session_factory, monkeypatch):
    fake = setup(session_factory, monkeypatch)
    monkeypatch.setattr(live, "generate_decision", lambda _: (
        EvaluationDecision(
            "no_action",
            "unclassified",
            "other",
            safety_flags=["customer_requested_stop", "stop_automation"],
        ),
        [],
        "hash",
        {"pipeline": "split_realtime_reply"},
    ))
    live.process_job(1)
    assert fake.sent == []
    with session_factory() as db:
        conversation = db.get(ConversationState, 1)
        assert conversation.ai_mode == "enabled"
        journey = db.scalar(select(ConversationJourney).where(ConversationJourney.conversation_state_id == 1))
        assert journey.slots['_v2_state']['proactive_opt_out'] is True


def test_opt_out_candidate_queues_current_reply_for_event_validation(session_factory, monkeypatch):
    fake = setup(session_factory, monkeypatch, labels=["ai"])
    with session_factory() as db:
        state = db.get(ConversationState, 1)
        state.ai_mode = "enabled"
        db.add(WebhookEvent(
            connection_id=1, event="message_created", account_id=180474,
            resource_id="101", idempotency_key="explicit-opt-out",
            received_at=utcnow(), payload={
                "event": "message_created", "id": 101,
                "account": {"id": 180474}, "inbox": {"id": 128859},
                "message_type": "incoming", "private": False,
                "content_type": "text", "content": "請不要再聯繫我",
                "created_at": utcnow(),
                "conversation": {
                    "id": 26, "inbox_id": 128859, "can_reply": True,
                    "labels": ["ai"], "contact_inbox": {"contact_id": 55},
                    "meta": {"sender": {"id": 55, "name": "Test", "type": "contact"}},
                },
            },
        ))
        db.flush()
        event = db.scalar(select(WebhookEvent).where(WebhookEvent.idempotency_key == "explicit-opt-out"))
        live.mirror_event(db, event)
        assert db.get(ConversationState, 1).ai_mode == "enabled"
        assert not (live.journey_for(db, db.get(ConversationState, 1)).slots or {}).get('_v2_state', {}).get('proactive_opt_out')
        assert db.get(LiveReplyJob, 1).status == "cancelled"
        assert db.scalar(select(func.count()).select_from(LiveReplyJob)) == 2
    live.process_job(1)
    assert fake.sent == []


def test_v2_job_with_unavailable_release_cannot_send(session_factory, monkeypatch):
    fake = setup(session_factory, monkeypatch, labels=["ai"])
    with session_factory() as db:
        state = db.get(ConversationState, 1)
        state.ai_engine_version = "v2"
        state.ai_engine_release_id = "reception-v2-preview-2-missing"
        job = db.get(LiveReplyJob, 1)
        job.engine_version = "v2"
        job.engine_release_id = state.ai_engine_release_id
        db.commit()
    live.process_job(1)
    assert fake.sent == []
    with session_factory() as db:
        assert db.get(LiveReplyJob, 1).error_code == "engine_release_unavailable"


def test_route_choice_reply_enrolls_unclassified_silence_journey(
    session_factory, monkeypatch
):
    monkeypatch.setattr(settings, "live_sop_enabled", True)
    monkeypatch.setattr(settings, "live_sop_scope", "allowlist")
    fake = setup(session_factory, monkeypatch, labels=["ai"])
    with session_factory() as db:
        sop = SopDefinition(
            tenant_id=1,
            created_by=1,
            name=UNCLASSIFIED_SOP_NAME,
                status="running",
                route_variant="",
                inbox_ids=[128859],
                test_conversation_ids=[26],
                nodes=UNCLASSIFIED_SOP_NODES,
        )
        db.add(sop)
        db.flush()
        sop_snapshot(db, sop, 1)
        db.commit()

    live.process_job(1)

    assert fake.sent[0][0] == "input_select"
    with session_factory() as db:
        job = db.get(LiveReplyJob, 1)
        enrollment = db.scalar(select(LiveSopEnrollment))
        first = db.scalar(select(LiveSopJob).where(
            LiveSopJob.enrollment_id == enrollment.id,
        ).order_by(LiveSopJob.id))
        assert job.trace["sop_enrollment"]["status"] == "enrolled"
        assert enrollment.status == "active"
        assert first.node_key == "route_selection_silence"
        assert first.payload["delay_minutes"] == 30


def test_sop_enrollment_failure_never_changes_a_submitted_reply(session_factory, monkeypatch):
    import app.live_sop as live_sop

    fake = setup(session_factory, monkeypatch)
    monkeypatch.setattr(live, "generate_decision", lambda _: (
        EvaluationDecision(
            "reply",
            "peach_9d",
            "route_intro",
            reply="route reply",
            route_variant="peach_9d_2027",
            content_group_key="entry_question",
        ),
        [],
        "hash",
        {"validator_version": "model-owned-contract-v2"},
    ))
    monkeypatch.setattr(
        live_sop,
        "enroll_model_route_sop",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("sop unavailable")),
    )
    live.process_job(1)
    assert fake.sent == [("text", "route reply")]
    with session_factory() as db:
        job = db.get(LiveReplyJob, 1)
        assert job.status == "submitted"
        assert job.trace["sop_enrollment"] == {
            "status": "error",
            "reason": "RuntimeError",
            "route_variant": "peach_9d_2027",
        }


def test_model_contact_values_require_verbatim_customer_evidence():
    decision = EvaluationDecision("no_action", "unclassified", "contact", lead_action="captured",
        contact_values={"line": "travel_2027", "wechat": "wx-trip88", "phone": "+886 912-345-678",
                        "email": "abc@example.com"})
    matches = model_contacts(decision, "LINE: travel_2027，微信 wx-trip88，電話 +886 912-345-678，Email abc@example.com")
    assert {item.kind for item in matches} == {"line", "wechat", "phone", "email"}
    assert all("travel_2027" not in item.masked and "912-345" not in item.masked for item in matches)
    assert model_contacts(decision, "想看2027桃花9日行程，兩位，9月出發") == []

    generic = EvaluationDecision("reply", "peach_9d", "contact", reply="好的", route_variant="peach_9d_2027",
        lead_action="captured", contact_values={"line": "已加你line喔"})
    assert model_contacts(generic, "已加你line喔") == []

    channel_only = EvaluationDecision("reply", "peach_9d", "contact", reply="好的", route_variant="peach_9d_2027",
        lead_action="captured", contact_values={"line": "line"})
    assert model_contacts(channel_only, "已加你line喔") == []

    line_link = EvaluationDecision("no_action", "unclassified", "contact",
        lead_action="captured", contact_values={"line": "https://line.me/ti/p/Uyfeasb797"})
    assert model_contacts(line_link, "https://line.me/ti/p/Uyfeasb797")[0].kind == "line"

    model_missed_link = EvaluationDecision("handoff", "other_destination", "other", lead_action="none")
    assert model_contacts(model_missed_link, "https://line.me/ti/p/Uyfeasb797") == []


def test_text_with_attachment_is_exposed_to_model_without_attachment_content():
    text, attachments = live.input_payload([
        {
            "content": "请介绍这条9日线路",
            "attachments": [{
                "file_type": "image",
                "extension": "jpg",
                "data_url": "https://example.test/private.jpg",
            }],
        }
    ])
    assert text == "请介绍这条9日线路"
    assert attachments == [{"file_type": "image", "extension": "jpg"}]
    assert "data_url" not in attachments[0]


def test_live_contact_capture_acknowledges_once_and_hands_off(session_factory, monkeypatch):
    fake = setup(session_factory, monkeypatch)
    fake.messages[0]["content"] = "我的 LINE 是 travel_2027"
    monkeypatch.setattr(live, "generate_decision", lambda _: (
        EvaluationDecision(
            "handoff",
            "unclassified",
            "contact",
            reply="收到您的 LINE，旅遊顧問會接續協助。",
            handoff_reason="lead_captured",
            lead_action="captured",
            contact_values={"line": "travel_2027"},
            journey_stage="completed",
        ),
        [],
        "hash",
        {},
    ))
    live.process_job(1)
    live.process_job(1)
    assert len(fake.sent) == 1
    assert fake.sent[0][0] == "text" and "travel_2027" not in fake.sent[0][1]
    assert "ai" in {label.casefold() for label in fake.send_label_snapshots[0]}
    assert {"已留资", "人工接管"}.issubset(set(fake.send_label_snapshots[0]))
    assert "ai" not in {label.casefold() for label in fake.labels}
    assert {"已留资", "人工接管"}.issubset(set(fake.labels))
    assert fake.operations.index("set_labels") < fake.operations.index("send_text")
    with session_factory() as db:
        capture = db.scalar(select(LeadCaptureState))
        state = db.get(ConversationState, 1)
        assert capture.status == "captured" and capture.request_count == 0
        assert capture.masked_values["line"] != "travel_2027"
        assert capture.label_sync_status == "synced"
        assert state.ai_mode == "disabled" and state.effective_ai_state == "HUMAN_HANDOFF"
        assert db.scalar(select(HandoffTask)).reason_code == "lead_captured"
        assert state.contact.custom_attributes["lead_contacts"]["line"] == "travel_2027"


def test_contact_request_requires_complete_needs_and_is_not_repeated(session_factory, monkeypatch):
    fake = setup(session_factory, monkeypatch)
    earlier = iso(dt(utcnow()) - timedelta(minutes=2))
    fake.messages = [
        {"id": 97, "created_at": earlier, "message_type": 0, "private": False, "content": "想了解2027桃花9日", "attachments": []},
        {"id": 98, "created_at": earlier, "message_type": 0, "private": False, "content": "我們兩位", "attachments": []},
        {"id": 99, "created_at": earlier, "message_type": 0, "private": False, "content": "想參加", "attachments": []},
        fake.messages[0],
    ]
    with session_factory() as db:
        db.add(ConversationJourney(
            conversation_state_id=1,
            route_variant="peach_9d_2027",
            stage="contact_ready",
            slots=bind_new_route_snapshot(
                "peach_9d_2027", {"party_size": 2, "departure_window": "3月25日"},
                available_materials=candidate_materials(db, 1),
            ),
            sent_groups=[],
        ))
        db.commit()
    calls = []
    def lead_model(_):
        calls.append(1)
        if len(calls) == 1:
            return EvaluationDecision(
                "reply", "peach_9d", "contact",
                reply=f"已記錄您的需求。{CONTACT_REQUEST}",
                slots={"party_size": 2, "departure_window": "3月25日"},
                route_variant="peach_9d_2027",
                lead_action="ask",
                journey_stage="contact_requested",
            ), [], "hash", {}
        return EvaluationDecision(
            "reply", "peach_9d", "other", reply="沒問題，您可以再看看。",
            route_variant="peach_9d_2027", journey_stage="answering",
        ), [], "hash-2", {}
    monkeypatch.setattr(live, "generate_decision", lead_model)
    live.process_job(1)
    assert len(fake.sent) == 1 and fake.sent[0][1].count(CONTACT_REQUEST) == 1
    with session_factory() as db:
        capture = db.scalar(select(LeadCaptureState))
        assert capture.status == "asked" and capture.request_count == 1
        trigger = MessageEvent(conversation_state_id=1, chatwoot_message_id=101, direction="incoming",
                               content="我再看看", created_at=utcnow())
        db.add(trigger); db.flush()
        db.add(LiveReplyJob(conversation_state_id=1, trigger_message_id=trigger.id, input_ids=[101], due_at=utcnow()))
        db.commit()
        second_job_id = db.scalar(select(LiveReplyJob.id).where(LiveReplyJob.trigger_message_id == trigger.id))
    fake.messages.append({"id": 101, "created_at": utcnow(), "message_type": 0, "private": False,
                          "content": "我再看看", "attachments": []})
    live.process_job(second_job_id)
    assert len(fake.sent) == 2
    assert sum(content.count(CONTACT_REQUEST) for _, content in fake.sent) == 1
    with session_factory() as db:
        assert db.scalar(select(LeadCaptureState)).request_count == 1


def test_remove_label_during_model_cancels(session_factory, monkeypatch):
    fake = setup(session_factory, monkeypatch)
    def model(_):
        fake.labels = []
        return EvaluationDecision("reply", "unclassified", "other", reply="draft"), [], "hash", {}
    monkeypatch.setattr(live, "generate_decision", model)
    live.process_job(1)
    assert fake.sent == []


def test_planned_contact_request_is_not_marked_asked_when_final_guard_blocks(session_factory, monkeypatch):
    fake = setup(session_factory, monkeypatch)
    earlier = iso(dt(utcnow()) - timedelta(minutes=2))
    fake.messages = [
        {"id": 98, "created_at": earlier, "message_type": 0, "private": False, "content": "我們兩位", "attachments": []},
        {"id": 99, "created_at": earlier, "message_type": 0, "private": False, "content": "想參加9日行程", "attachments": []},
        fake.messages[0],
    ]
    def model(_):
        fake.labels = []
        return EvaluationDecision("reply", "peach_9d", "other", reply="已記錄。",
            slots={"party_size": 2, "departure_window": "3月"}, route_variant="peach_9d_2027",
            lead_action="ask"), [], "hash", {}
    monkeypatch.setattr(live, "generate_decision", model)
    live.process_job(1)
    assert fake.sent == []
    with session_factory() as db:
        capture = db.scalar(select(LeadCaptureState))
        assert capture is not None and capture.status == "not_started" and capture.request_count == 0


@pytest.mark.parametrize("change", ["none", "remove_label", "customer_reply", "human_reply"])
def test_persistent_timeout_retry_rechecks_controls_and_sends_at_most_once(session_factory, monkeypatch, change):
    from app.deepseek_evaluation import EvaluationCallError
    fake = setup(session_factory, monkeypatch)
    attempts = []
    def model(_):
        attempts.append(1)
        if len(attempts) == 1:
            raise EvaluationCallError("ReadTimeout", [{"attempt": 1, "duration_ms": 20000,
                "status": "retry", "error_code": "ReadTimeout"}], "test")
        return EvaluationDecision("reply", "unclassified", "other", reply="draft"), [], "test", {}
    monkeypatch.setattr(live, "generate_decision", model)
    live.process_job(1)
    with session_factory() as db:
        assert db.get(LiveReplyJob, 1).status == "queued"
        assert not db.scalar(select(OutboundMessage))
        live.recover_jobs(db)
    if change == "remove_label": fake.labels = []
    if change == "customer_reply": fake.messages.append({**fake.messages[0], "id": 101})
    if change == "human_reply": fake.messages.append({"id": 101, "created_at": utcnow(), "message_type": 1, "content": "human"})
    live.process_job(1)
    live.process_job(1)
    assert len(fake.sent) == (1 if change == "none" else 0)
    assert len(attempts) == (2 if change == "none" else 1)


def test_live_model_receives_complete_old_customer_context(session_factory, monkeypatch):
    fake = setup(session_factory, monkeypatch)
    at = iso(dt(utcnow()) - timedelta(days=2))
    fake.messages = [{"id": i, "created_at": at, "message_type": 0 if i == 1 else 1,
        "content": "我們兩位，3月出發" if i == 1 else "past public reply"} for i in range(1, 80)] + fake.messages
    def paginated(_id, before=None):
        return {"payload": [m for m in fake.messages if before is None or m["id"] < before][-20:]}
    fake.get_messages = paginated
    def model(context):
        assert context["context_complete"] is True
        assert len(context["context_messages"]) == 79
        assert context["context_messages"][0]["content"] == "我們兩位，3月出發"
        return EvaluationDecision("reply", "unclassified", "other", reply="draft"), [], "hash", {}
    monkeypatch.setattr(live, "generate_decision", model)
    live.process_job(1)
    assert len(fake.sent) == 1


def test_unknown_submission_is_not_retried(session_factory, monkeypatch):
    fake = setup(session_factory, monkeypatch)
    fake.fail_after_submission = True
    live.process_job(1)
    with session_factory() as db:
        live.recover_jobs(db)
    live.process_job(1)
    assert len(fake.sent) == 1
    with session_factory() as db:
        assert db.scalar(select(OutboundMessage)).status == "submission_unknown"


def test_tag_alone_or_backlog_does_not_create_job(session_factory, monkeypatch):
    setup(session_factory, monkeypatch)
    with session_factory() as db:
        for i, event in enumerate(["conversation_updated", "message_created"]):
            payload = {"event": event, "id": 200 + i, "account": {"id": 180474}, "message_type": "incoming", "created_at": "2020-01-01T00:00:00Z",
                       "conversation": {"id": 26, "inbox_id": 128859, "labels": ["AI"], "meta": {"sender": {"id": 55}}}}
            row = WebhookEvent(connection_id=1, event=event, account_id=180474, resource_id=str(i), idempotency_key=str(i), payload=payload)
            db.add(row)
            db.flush()
            live.mirror_event(db, row)
        assert db.scalar(select(func.count()).select_from(LiveReplyJob)) == 1


def test_live_worker_mirrors_label_to_sop_rehearsal_without_sending(session_factory, monkeypatch):
    from app.automation_models import RehearsalEnrollment, AutomationSession
    from app.automation_service import sop_snapshot
    from app.automation_shadow import advance_shadow_sops
    from app.models import SopDefinition, SopJob
    fake = setup(session_factory, monkeypatch)
    with session_factory() as db:
        sop = SopDefinition(tenant_id=1, created_by=1, name="isolated", status="running", trigger_type="label",
            trigger_labels=["start"], nodes=[{"key": "one", "schedule_type": "relative", "basis": "enrollment", "delay_minutes": 0,
                "messages": [{"key": "text", "content_type": "text", "content": "draft only"}]}])
        db.add(sop)
        db.flush()
        sop_snapshot(db, sop, 1)
        event = WebhookEvent(connection_id=1, event="conversation_updated", account_id=180474, resource_id="26", idempotency_key="current-label",
            payload={"event": "conversation_updated", "id": 26, "inbox_id": 128859, "account": {"id": 180474},
                     "labels": ["ai", "start"], "can_reply": True, "meta": {"sender": {"id": 55}}})
        db.add(event)
        db.flush()
        live.mirror_event(db, event)
        enrollment = db.scalar(select(RehearsalEnrollment))
        assert enrollment is not None
        assert db.get(AutomationSession, enrollment.session_id).environment == "shadow"
        advance_shadow_sops(db)
        assert not db.scalar(select(OutboundMessage)) and not db.scalar(select(SopJob))
    assert fake.sent == []


def test_live_transport_blocks_unrelated_writes(monkeypatch):
    monkeypatch.setattr(settings, "app_profile", "live_reply")
    monkeypatch.setattr(settings, "outbound_mode", "live")
    monkeypatch.setattr(settings, "chatwoot_write_enabled", True)
    client = ChatwootClient("https://example.invalid", 180474, "fake")
    with pytest.raises(ChatwootError, match="Only replies"):
        client._guard_request(httpx.Request("POST", "https://example.invalid/api/v1/accounts/180474/webhooks"))
    client._guard_request(httpx.Request("POST", "https://example.invalid/api/v1/accounts/180474/conversations/26/assignments"))
    client.close()


def test_global_message_switch_blocks_message_transport_but_not_control_writes(monkeypatch):
    monkeypatch.setattr(settings, "app_profile", "live_reply")
    monkeypatch.setattr(settings, "outbound_mode", "live")
    monkeypatch.setattr(settings, "chatwoot_write_enabled", True)
    monkeypatch.setattr("app.chatwoot.global_message_sending_enabled", lambda: False)
    client = ChatwootClient("https://example.invalid", 180474, "fake")
    with pytest.raises(ChatwootError) as exc:
        client._guard_request(httpx.Request(
            "POST",
            "https://example.invalid/api/v1/accounts/180474/conversations/26/messages",
        ))
    assert exc.value.code == "global_message_sending_disabled"
    client._guard_request(httpx.Request(
        "POST",
        "https://example.invalid/api/v1/accounts/180474/conversations/26/labels",
    ))
    client.close()


def test_global_message_switch_does_not_prevent_worker_observation(session_factory, monkeypatch):
    monkeypatch.setattr(settings, "app_profile", "live_reply")
    monkeypatch.setattr(settings, "outbound_mode", "live")
    monkeypatch.setattr(settings, "chatwoot_write_enabled", True)
    with session_factory() as db:
        db.add(AppSetting(key="live_reply", value={"armed_at": iso(dt(utcnow()))}))
        db.add(AppSetting(key="global_message_sending", value={"enabled": False}))
        db.commit()
        assert live.assert_worker_ready(db)["armed_at"]
        with pytest.raises(live.ReplyBlocked, match="global_message_sending_disabled"):
            live.assert_armed(db)


def add_image(db, tmp_path):
    import hashlib
    from app.models import KnowledgeVersion, MaterialAsset, StoredMedia
    from app.material_library import CATALOG_VERSION
    path = tmp_path / "route.png"
    path.write_bytes(b"test-image")
    version = KnowledgeVersion(tenant_id=1, version_key=CATALOG_VERSION, title="test", content_hash="x")
    media = StoredMedia(tenant_id=1, created_by=1, original_name="route.png", media_type="image", mime_type="image/png", file_size=10, storage_path=str(path))
    db.add_all([version, media]); db.flush()
    asset = MaterialAsset(knowledge_version_id=version.id, asset_key="routes12-9d-itinerary", source_path=str(path), display_name="route", available=True,
        file_hash=hashlib.sha256(path.read_bytes()).hexdigest(), metadata_json={"stored_media_id": media.id, "review_state": "evaluation_ready",
            "live_approved": True, "route_variants": ["peach_9d_2027"], "content_family": "route9"})
    db.add(asset); db.commit()
    return asset.id


def add_itinerary_progress(db):
    db.add(ConversationJourney(
        conversation_state_id=1,
        route_variant="peach_9d_2027",
        stage="collecting_departure",
        slots=bind_new_route_snapshot(
            "peach_9d_2027", {"party_size": 2},
            available_materials=candidate_materials(db, 1),
        ),
        sent_groups=[],
    ))
    db.commit()


@pytest.mark.parametrize('interrupt', [False, True])
@pytest.mark.parametrize('terminal', ['reply', 'handoff', 'capture', 'pending_capture'])
def test_v2_full_introduction_uses_worker_and_stops_on_new_customer_message(
    session_factory, monkeypatch, tmp_path, interrupt, terminal
):
    import hashlib
    from app.models import MaterialAsset, StoredMedia
    from app.reception_v2 import ENGINE_RELEASE_ID
    from app.reception_v2.material_delivery import introduction_sections
    fake = setup(session_factory, monkeypatch)
    route = 'peach_9d_2027'
    keys = {asset for group in ROUTES[route]['groups'].values() if group.get('initial_delivery')
            for asset in group['assets']}
    with session_factory() as db:
        first_id = add_image(db, tmp_path)
        first = db.get(MaterialAsset, first_id)
        for index, key in enumerate(sorted(keys - {'routes12-9d-itinerary'})):
            path = tmp_path / f'{key}.png'
            path.write_bytes(f'fixture-{key}'.encode())
            media = StoredMedia(tenant_id=1, created_by=1, original_name=path.name,
                media_type='image', mime_type='image/png', file_size=path.stat().st_size, storage_path=str(path))
            db.add(media)
            db.flush()
            db.add(MaterialAsset(knowledge_version_id=first.knowledge_version_id, asset_key=key,
                source_path=str(path), display_name=key, available=True,
                file_hash=hashlib.sha256(path.read_bytes()).hexdigest(), metadata_json={
                    'stored_media_id': media.id, 'review_state': 'evaluation_ready',
                    'live_approved': True, 'route_variants': [route], 'content_family': 'route9'}))
        db.get(ConversationState, 1).ai_engine_version = 'v2'
        db.get(ConversationState, 1).ai_engine_release_id = ENGINE_RELEASE_ID
        db.get(LiveReplyJob, 1).engine_version = 'v2'
        db.get(LiveReplyJob, 1).engine_release_id = ENGINE_RELEASE_ID
        db.commit()
        add_itinerary_progress(db)
    sections = introduction_sections(ROUTES[route], keys, {})
    decision = EvaluationDecision('reply', 'peach_9d', 'itinerary', reply=sections[0]['text'],
        route_variant=route, content_group_key=sections[0]['group_key'],
        covered_content_groups=[s['group_key'] for s in sections],
        material_keys=list(keys), v2_delivery_sections=sections)
    if terminal != 'reply':
        decision.action = 'handoff'
        decision.handoff_reason = 'knowledge_confirmation_required'
        decision.v2_delivery_sections = [s for s in sections if s['group_key'] != 'party_question']
    if terminal in {'capture', 'pending_capture'}:
        fake.messages[0]['content'] = '請給我資料，微信abc12345請顧問聯絡我'
        decision.lead_action = 'captured'
        decision.contact_values = {'wechat': 'abc12345'}
        decision.handoff_reason = 'lead_captured'
        if terminal == 'pending_capture':
            with session_factory() as db:
                db.add(LeadCaptureState(conversation_state_id=1, status='asked'))
                db.get(ConversationState, 1).effective_ai_state = 'HUMAN_HANDOFF'
                db.commit()
    monkeypatch.setattr(live, 'generate_decision', lambda _: (decision, [], 'hash', {}))
    if interrupt:
        monkeypatch.setattr(live.time, 'sleep', lambda _: fake.messages.append({
            'id': 101, 'created_at': utcnow(), 'message_type': 0, 'private': False,
            'content': '先等等，在哪集合？', 'attachments': []}))
    live.process_job(1)
    with session_factory() as db:
        job = db.get(LiveReplyJob, 1)
        assert job.status == ('blocked' if interrupt else 'submitted' if terminal == 'reply' else 'handoff'), (job.error_code, job.trace)
        if interrupt:
            assert len(fake.sent) == 1
        else:
            assert [kind for kind, *_ in fake.sent].count('image') == len(keys)
            attempts = db.scalars(select(OutboundMessage)).all()
            assert all(len(row.content_attributes['_delivery_item']['group_keys']) == 1 for row in attempts)
            assert fake.sent[0][1] == sections[0]['text']
            if terminal == 'reply':
                assert fake.sent[-1][1] == sections[-1]['text']
            else:
                assert job.trace['terminal_handoff_plan']
                assert db.scalar(select(HandoffTask)) is not None


def test_checked_initial_visual_uses_the_route_configured_gap(
    session_factory, monkeypatch, tmp_path
):
    fake = setup(session_factory, monkeypatch)
    waits = []
    monkeypatch.setattr(live.time, "sleep", waits.append)
    with session_factory() as db:
        add_image(db, tmp_path)
        add_itinerary_progress(db)
        journey = db.scalar(select(ConversationJourney))
        entry = journey.slots[ROUTE_SNAPSHOTS_KEY]["peach_9d_2027"]
        spec = {**entry["spec"], "initial_delivery_interval_seconds": 6}
        journey.slots = {**journey.slots, ROUTE_SNAPSHOTS_KEY: {
            "peach_9d_2027": make_route_snapshot("peach_9d_2027", spec),
        }}
        db.commit()
    decision = EvaluationDecision(
        "reply", "peach_9d", "route_intro", reply="行程參考",
        route_variant="peach_9d_2027",
        content_group_key="itinerary_overview",
        covered_content_groups=["itinerary_overview"],
        material_keys=["routes12-9d-itinerary"],
    )
    monkeypatch.setattr(live, "generate_decision", lambda _: (decision, [], "hash", {}))
    live.process_job(1)
    assert [kind for kind, _ in fake.sent] == ["image", "text"]
    assert waits == [6]
    with session_factory() as db:
        assert db.get(LiveReplyJob, 1).status == "submitted"
        assert db.scalar(select(func.count()).select_from(OutboundMessage)) == 2


@pytest.mark.parametrize("change", ["not_approved", "route_mismatch", "file_missing"])
def test_material_fail_closed_before_text(session_factory, monkeypatch, tmp_path, change):
    from app.models import MaterialAsset
    fake = setup(session_factory, monkeypatch)
    with session_factory() as db:
        asset_id = add_image(db, tmp_path)
        add_itinerary_progress(db)
        asset = db.get(MaterialAsset, asset_id)
        if change == "not_approved": asset.metadata_json = {**asset.metadata_json, "live_approved": False}
        if change == "route_mismatch": asset.metadata_json = {**asset.metadata_json, "route_variants": ["peach_11d_2027"]}
        if change == "file_missing": Path(asset.source_path).unlink()
        db.commit()
    monkeypatch.setattr(live, "generate_decision", lambda _: (EvaluationDecision("reply", "peach_9d", "route_intro", reply="draft", route_variant="peach_9d_2027", material_keys=["route-image"]), [], "hash", {}))
    live.process_job(1)
    assert fake.sent == []


def test_model_handoff_uses_model_final_reply_then_blocks(session_factory, monkeypatch):
    fake = setup(session_factory, monkeypatch)
    monkeypatch.setattr(live, "generate_decision", lambda _: (
        EvaluationDecision(
            "handoff", "unclassified", "complaint",
            reply="已為您記錄，將由旅遊顧問接續處理。",
            handoff_reason="human_requested",
        ), [], "hash", {}))
    live.process_job(1)
    assert fake.sent == [("text", "已為您記錄，將由旅遊顧問接續處理。")] 
    assert "ai" in {label.casefold() for label in fake.send_label_snapshots[0]}
    assert "人工接管" in fake.send_label_snapshots[0]
    assert fake.operations.index("set_labels") < fake.operations.index("send_text")
    with session_factory() as db:
        assert db.scalar(select(HandoffTask)).status == "pending"
        assert db.get(ConversationState, 1).effective_ai_state == "HUMAN_HANDOFF"


def test_sales_handoff_sets_label_before_text_and_helpful_image(session_factory, monkeypatch, tmp_path):
    fake = setup(session_factory, monkeypatch)
    with session_factory() as db:
        add_image(db, tmp_path)
        add_itinerary_progress(db)
    monkeypatch.setattr(live, "generate_decision", lambda _: (
        EvaluationDecision(
            "handoff", "peach_9d", "other",
            reply="稍等一下，我這邊安排專項顧問給您跟進，您可以先看看行程圖。",
            handoff_reason="large_group_custom_quote",
            route_variant="peach_9d_2027",
            material_keys=["routes12-9d-itinerary"],
            covered_content_groups=["itinerary_overview"],
        ), [], "hash", {}))
    live.process_job(1)
    assert [kind for kind, _ in fake.sent] == ["text", "image"]
    assert fake.operations.index("set_labels") < fake.operations.index("send_text")
    assert fake.operations.index("send_text") < fake.operations.index("send_image")
    assert all("ai" in {label.casefold() for label in labels} for labels in fake.send_label_snapshots)
    assert all("人工接管" in labels for labels in fake.send_label_snapshots)
    assert "ai" not in {label.casefold() for label in fake.labels}
    assert "人工接管" in fake.labels
    with session_factory() as db:
        assert db.get(LiveReplyJob, 1).status == "handoff"
        assert db.scalar(select(func.count()).select_from(OutboundMessage)) == 2


def test_ai_label_removed_between_image_and_text_stops_remaining_send(
    session_factory, monkeypatch, tmp_path
):
    fake = setup(session_factory, monkeypatch)
    fake.remove_ai_after_image = True
    with session_factory() as db:
        add_image(db, tmp_path)
        add_itinerary_progress(db)
    monkeypatch.setattr(live, "generate_decision", lambda _: (
        EvaluationDecision(
            "reply", "peach_9d", "route_intro",
            reply="先把9日行程重点发您。",
            route_variant="peach_9d_2027",
            material_keys=["routes12-9d-itinerary"],
            covered_content_groups=["itinerary_overview"],
        ), [], "hash", {}
    ))

    live.process_job(1)

    assert fake.sent == [("image", "")]
    with session_factory() as db:
        job = db.get(LiveReplyJob, 1)
        assert job.status == "blocked"
        assert job.error_code == "ai_opt_in_required"
        assert db.scalar(select(func.count()).select_from(OutboundMessage)) == 1


def test_customer_reply_between_image_and_text_stops_remaining_send(
    session_factory, monkeypatch, tmp_path
):
    fake = setup(session_factory, monkeypatch)
    with session_factory() as db:
        add_image(db, tmp_path)
        add_itinerary_progress(db)

    def customer_replies(_seconds):
        fake.messages.append({
            "id": 101,
            "created_at": utcnow(),
            "message_type": 0,
            "private": False,
            "content": "我想先問價格",
            "attachments": [],
        })

    monkeypatch.setattr(live.time, "sleep", customer_replies)
    monkeypatch.setattr(live, "generate_decision", lambda _: (
        EvaluationDecision(
            "reply", "peach_9d", "route_intro",
            reply="這是9日行程重點。",
            route_variant="peach_9d_2027",
            material_keys=["routes12-9d-itinerary"],
            covered_content_groups=["itinerary_overview"],
        ), [], "hash", {}
    ))

    live.process_job(1)

    assert fake.sent == [("image", "")]
    with session_factory() as db:
        job = db.get(LiveReplyJob, 1)
        assert job.status == "blocked"
        assert job.error_code == "new_customer_message"


def test_handoff_ai_label_removal_failure_keeps_local_block_and_alerts(
    session_factory, monkeypatch
):
    fake = setup(session_factory, monkeypatch)
    fake.fail_ai_removal = True
    monkeypatch.setattr(live, "generate_decision", lambda _: (
        EvaluationDecision(
            "handoff", "unclassified", "other",
            reply="稍等一下，我這邊安排專項顧問給您跟進。",
            handoff_reason="human_requested",
        ), [], "hash", {}
    ))

    live.process_job(1)

    assert fake.sent == [("text", "稍等一下，我這邊安排專項顧問給您跟進。")] 
    assert "ai" in {label.casefold() for label in fake.labels}
    assert "人工接管" in fake.labels
    with session_factory() as db:
        state = db.get(ConversationState, 1)
        assert state.effective_ai_state == "HUMAN_HANDOFF"
        assert db.scalar(select(HandoffTask)).status == "pending"
        assert db.scalar(select(Notification).where(
            Notification.event_type == "ai.handoff_label_finalize_failed"
        )) is not None


def test_model_lead_request_then_capture_hands_off(session_factory, monkeypatch):
    fake = setup(session_factory, monkeypatch)
    decision = EvaluationDecision(
        "reply", "peach_9d", "contact",
        reply=f"我可以再協助確認細節。{CONTACT_REQUEST}",
        route_variant="peach_9d_2027",
        lead_action="ask",
        journey_stage="contact_requested",
    )
    def model(context):
        if "微信" in context.get("customer_text", ""):
            return EvaluationDecision(
                "handoff", "peach_9d", "contact",
                reply="收到您的微信，旅遊顧問會接續協助。",
                handoff_reason="lead_captured",
                route_variant="peach_9d_2027",
                lead_action="captured",
                contact_values={"wechat": "wx_test_2027"},
                journey_stage="completed",
            ), [], "hash-2", {}
        return decision, [], "hash", {}
    monkeypatch.setattr(live, "generate_decision", model)
    live.process_job(1)
    assert len(fake.sent) == 1 and CONTACT_REQUEST in fake.sent[0][1]
    assert "ai" in {label.casefold() for label in fake.labels}
    fake.messages.append({"id": 1002, "created_at": utcnow(), "message_type": 0, "private": False,
                          "content": "微信: wx_test_2027", "attachments": []})
    with session_factory() as db:
        capture = db.scalar(select(LeadCaptureState))
        assert capture.status == "asked" and capture.request_count == 1
        event = WebhookEvent(connection_id=1, event="message_created", account_id=180474,
            resource_id="1002", idempotency_key="pending-lead-contact", received_at=utcnow(), payload={
                "event": "message_created", "id": 1002, "account": {"id": 180474},
                "inbox": {"id": 128859}, "message_type": "incoming", "private": False,
                "content_type": "text", "content": "微信: wx_test_2027", "created_at": utcnow(),
                "conversation": {"id": 26, "inbox_id": 128859, "can_reply": True,
                    "labels": ["AI"], "contact_inbox": {"contact_id": 55},
                    "meta": {"sender": {"id": 55, "name": "Test", "type": "contact"}}}})
        db.add(event); db.flush()
        live.mirror_event(db, event)
        job_id = db.scalar(select(LiveReplyJob.id).where(LiveReplyJob.trigger_message_id != 1)
                           .order_by(LiveReplyJob.id.desc()))
        assert job_id is not None
    live.process_job(job_id)
    assert len(fake.sent) == 2
    assert "收到您的微信" in fake.sent[-1][1]
    assert {"人工接管", "已留资"}.issubset(set(fake.labels))
    with session_factory() as db:
        capture = db.scalar(select(LeadCaptureState))
        assert capture.status == "captured" and capture.captured_kinds == ["wechat"]
        assert capture.label_sync_status == "synced"


def test_message_updated_tracks_delivery_without_reply(session_factory, monkeypatch):
    setup(session_factory, monkeypatch)
    live.process_job(1)
    with session_factory() as db:
        event = WebhookEvent(connection_id=1, event="message_updated", account_id=180474, resource_id="1001", idempotency_key="delivered",
            payload={"id": 1001, "event": "message_updated", "status": "delivered", "message_type": "outgoing", "conversation": {"id": 26, "inbox_id": 128859}})
        db.add(event); db.flush()
        live.mirror_event(db, event)
        assert db.scalar(select(OutboundMessage)).status == "delivered"
        assert db.scalar(select(func.count()).select_from(LiveReplyJob)) == 1


def test_timeout_keeps_attempt_diagnostics_without_sending(session_factory, monkeypatch):
    from app.deepseek_evaluation import EvaluationCallError
    fake = setup(session_factory, monkeypatch)
    def timeout(_):
        raise EvaluationCallError("TimeoutError", [
            {"attempt": 1, "duration_ms": 20000, "status": "retry", "error_code": "ReadTimeout"},
            {"attempt": 2, "duration_ms": 9500, "status": "retry", "error_code": "TimeoutError"}], "request-digest")
    monkeypatch.setattr(live, "generate_decision", timeout)
    live.process_job(1)
    live.process_job(1)
    with session_factory() as db:
        job = db.get(LiveReplyJob, 1)
        assert job.status == "failed" and job.error_code == "TimeoutError"
        assert job.trace["request_count"] == 4 and job.trace["model_ms"] == 59000
        assert job.trace["outbound"] is False
        assert db.scalar(select(func.count()).select_from(OutboundMessage)) == 0
    assert fake.sent == []


def test_terminal_fact_verification_failure_blocks_send_and_creates_manual_followup(session_factory, monkeypatch):
    from app.deepseek_evaluation import EvaluationCallError
    fake = setup(session_factory, monkeypatch)

    def factual_failure(_):
        raise EvaluationCallError(
            "reply_factual_verification_failed",
            [{
                "node": "reply_fact_verification",
                "status": "failed",
                "error_code": "reply_factual_verification_failed",
                "unsupported_claims": ["不受事实支持的承诺"],
            }],
            "fact-failure-digest",
        )

    monkeypatch.setattr(live, "generate_decision", factual_failure)
    live.process_job(1)
    with session_factory() as db:
        job = db.get(LiveReplyJob, 1)
        task = db.scalar(select(HandoffTask))
        assert job.status == "failed"
        assert job.trace["manual_followup_created"] is True
        assert task is not None
        assert task.reason_code == "ai_factual_verification_failed"
        assert db.get(ConversationState, 1).effective_ai_state == "HUMAN_HANDOFF"
        assert db.scalar(select(func.count()).select_from(OutboundMessage)) == 0
    assert fake.sent == []


def test_receipt_reconciliation_only_reads_and_does_not_downgrade(session_factory, monkeypatch):
    from app import delivery_status as delivery
    fake = setup(session_factory, monkeypatch)
    live.process_job(1)
    monkeypatch.setattr(delivery, "SessionLocal", session_factory)
    monkeypatch.setattr(delivery, "ReadOnlyChatwootClient", lambda *args, **kwargs: fake)
    fake.messages[-1]["status"] = "sent"
    assert delivery.reconcile_live_delivery() == 1
    with session_factory() as db:
        assert db.scalar(select(OutboundMessage)).status == "sent"
        assert db.scalar(select(MessageEvent).where(MessageEvent.direction == "outgoing")).status == "sent"
    fake.messages[-1]["status"] = "delivered"
    assert delivery.reconcile_live_delivery() == 1
    fake.messages[-1]["status"] = "sent"
    assert delivery.reconcile_live_delivery() == 0
    with session_factory() as db:
        message = db.scalar(select(MessageEvent).where(MessageEvent.direction == "outgoing"))
        message.attachments = [{"file_type": "image", "data_url": "https://example.invalid/photo.png"}]
        message.created_at = "2026-08-01T00:00:00+00:00"
        db.commit()
        event = WebhookEvent(connection_id=1, event="message_updated", account_id=180474, resource_id="1001", idempotency_key="late-sent",
            payload={"id": 1001, "event": "message_updated", "status": "sent", "message_type": "outgoing", "conversation": {"id": 26, "inbox_id": 128859}})
        db.add(event); db.flush()
        live.mirror_event(db, event)
        assert db.scalar(select(OutboundMessage)).status == "delivered"
        assert db.scalar(select(MessageEvent).where(MessageEvent.direction == "outgoing")).status == "delivered"
        assert message.attachments and message.created_at == "2026-08-01T00:00:00+00:00"
        assert db.scalar(select(func.count()).select_from(OutboundMessage)) == 1
    assert len(fake.sent) == 1


def test_create_response_explicit_sent_is_preserved(session_factory, monkeypatch):
    fake = setup(session_factory, monkeypatch)
    create = fake.create_input_select_message
    monkeypatch.setattr(fake, "create_input_select_message", lambda *args: {**create(*args), "status": "sent"})
    live.process_job(1)
    with session_factory() as db:
        assert db.scalar(select(OutboundMessage)).status == "sent"
        assert db.scalar(select(MessageEvent).where(MessageEvent.direction == "outgoing")).status == "sent"


def test_explicit_failed_text_stops_remaining_images(session_factory, monkeypatch, tmp_path):
    fake = setup(session_factory, monkeypatch)
    with session_factory() as db:
        add_image(db, tmp_path)
        slots = bind_new_route_snapshot(
            "peach_9d_2027", {}, available_materials=candidate_materials(db, 1),
        )
        spec = slots[ROUTE_SNAPSHOTS_KEY]["peach_9d_2027"]["spec"]
        spec["groups"]["itinerary_overview"]["delivery_mode"] = "text_then_assets"
        bound_snapshot = make_route_snapshot("peach_9d_2027", spec)
    create = fake.create_text_message
    monkeypatch.setattr(fake, "create_text_message", lambda *args: {**create(*args), "status": "failed"})
    monkeypatch.setattr(live, "generate_decision", lambda _: (
        EvaluationDecision(
            "reply", "peach_9d", "route_intro", reply="draft",
            bound_route_snapshot=bound_snapshot,
            route_variant="peach_9d_2027",
            content_group_key="itinerary_overview",
            material_keys=["routes12-9d-itinerary"],
        ), [], "hash", {}))
    live.process_job(1)
    with session_factory() as db:
        assert db.get(LiveReplyJob, 1).error_code == "channel_send_failed"
        assert db.scalar(select(OutboundMessage)).status == "failed"
    assert [kind for kind, _ in fake.sent] == ["text"]


def test_latest_reply_api_exposes_only_operational_fields(authenticated, session_factory, monkeypatch):
    setup(session_factory, monkeypatch)
    with session_factory() as db:
        job = db.get(LiveReplyJob, 1)
        job.status, job.error_code = "failed", "TimeoutError"
        job.trace = {"model_ms": 30000, "request_count": 2, "secret": "must-not-leak"}
        job.decision = {"reply": "private model content"}
        db.commit()
    client, _ = authenticated
    response = client.get("/v1/conversations/26")
    assert response.status_code == 200
    result = response.json()["latest_ai_reply"]
    assert result["status"] == "failed" and result["failure_kind"] == "model_timeout"
    assert result["model_ms"] == 30000
    assert set(result) == {"status", "failure_kind", "created_at", "completed_at", "model_ms", "request_count", "model_http_request_count", "engine_version", "engine_release_id"}
    from app.models import User
    with session_factory() as db:
        user = db.scalar(select(User))
        user.role = "agent"
        db.commit()
    assert client.get("/v1/conversations/26").status_code == 404


def test_attachment_transport_uses_native_multipart(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "app_profile", "live_reply")
    monkeypatch.setattr(settings, "outbound_mode", "live")
    monkeypatch.setattr(settings, "chatwoot_write_enabled", True)
    monkeypatch.setattr("app.chatwoot.global_message_sending_enabled", lambda: True)
    image = tmp_path / "tour.jpg"
    image.write_bytes(b"local-image-bytes")
    captured = []
    def transport(request):
        body = request.read()
        assert b'name="attachments[]"' in body and b"local-image-bytes" in body
        assert b'name="private"' in body and b"false" in body
        captured.append(request.url.path)
        return httpx.Response(200, json={"id": 123})
    client = ChatwootClient("https://example.invalid", 180474, "fake")
    client.client.close()
    client.client = httpx.Client(transport=httpx.MockTransport(transport), event_hooks={"request": [client._guard_request]})
    client.create_attachment_message(26, "", str(image), "image/jpeg")
    client.close()
    assert captured == ["/api/v1/accounts/180474/conversations/26/messages"]


def test_quick_reply_transport_uses_chatwoot_input_select(monkeypatch):
    monkeypatch.setattr(settings, "app_profile", "live_reply")
    monkeypatch.setattr(settings, "outbound_mode", "live")
    monkeypatch.setattr(settings, "chatwoot_write_enabled", True)
    monkeypatch.setattr("app.chatwoot.global_message_sending_enabled", lambda: True)
    captured = []

    def transport(request):
        captured.append((request.url.path, __import__("json").loads(request.read())))
        return httpx.Response(200, json={"id": 456, "status": "sent"})

    client = ChatwootClient("https://example.invalid", 180474, "fake")
    client.client.close()
    client.client = httpx.Client(
        transport=httpx.MockTransport(transport),
        event_hooks={"request": [client._guard_request]},
    )
    result = client.create_input_select_message(
        26,
        "请问您想咨询哪条线路？",
        ["桃花9日", "桃花+珠峰11日"],
    )
    client.close()

    assert result["id"] == 456
    path, payload = captured[0]
    assert path == "/api/v1/accounts/180474/conversations/26/messages"
    assert payload == {
        "content": "请问您想咨询哪条线路？",
        "message_type": "outgoing",
        "private": False,
        "content_type": "input_select",
        "content_attributes": {
            "items": [
                {"title": "桃花9日", "value": "桃花9日"},
                {"title": "桃花+珠峰11日", "value": "桃花+珠峰11日"},
            ]
        },
    }


@pytest.mark.skipif(os.environ.get("VERIFY_LIVE_DEEPSEEK") != "1", reason="Explicit paid-model check; Chatwoot remains mocked")
def test_real_deepseek_to_mocked_text_and_image(session_factory, monkeypatch, tmp_path):
    from app.decision_service import generate_decision
    from app.models import MaterialAsset, StoredMedia
    from app.delivery_tracking import attach_delivery_item, record_delivery_progress
    from app.material_library import CATALOG_VERSION
    from app.route_reply import route_snapshot_from_values
    from app.opening_messages import opening_media_info
    from test_opening_media import media as uploaded_media
    from email import policy
    from email.parser import BytesParser
    import app.live_sop as live_sop
    import json
    fake = setup(session_factory, monkeypatch)
    route = "peach_9d_2027"
    fake.messages[0]["content"] = "想看2027年桃花9日的行程圖片，不上珠峰。"
    monkeypatch.setattr(settings, "live_sop_enabled", True)
    monkeypatch.setattr(settings, "live_sop_scope", "allowlist")
    monkeypatch.setattr(live_sop, "SessionLocal", session_factory)
    monkeypatch.setattr(live_sop, "client_for", lambda _: fake)
    monkeypatch.setattr("app.chatwoot.global_message_sending_enabled", lambda: True)
    assets_by_hash = {}
    with session_factory() as db:
        from app.models import KnowledgeVersion
        version = KnowledgeVersion(tenant_id=1, version_key=CATALOG_VERSION, title="paid fixture", content_hash="paid")
        db.add(version)
        db.flush()
        for index, key in enumerate(dict.fromkeys(
            key for group in ROUTES[route]["groups"].values() for key in group["assets"]
        )):
            directory = tmp_path / str(index)
            directory.mkdir()
            item = uploaded_media(db, directory)
            # Distinct valid bytes are required for the real duplicate checks.
            from PIL import Image
            row = db.get(StoredMedia, item["media_id"])
            Image.new("RGB", (32, 32), (index * 13 % 255, 80, 150)).save(row.storage_path, format="PNG")
            item.pop("media_hash")
            info = opening_media_info(db, item, 1)
            group_key = next(name for name, group in ROUTES[route]["groups"].items() if key in group["assets"])
            db.add(MaterialAsset(
                knowledge_version_id=version.id, asset_key=key, source_path=row.storage_path,
                display_name="2027林芝桃花9日行程圖" if key == "routes12-9d-itinerary" else key,
                usage="route_itinerary" if key == "routes12-9d-itinerary" else group_key,
                available=True, media_type="image", file_hash=info["media_hash"],
                metadata_json={"stored_media_id": row.id, "review_state": "evaluation_ready",
                               "live_approved": True, "route_variants": [route],
                               "content_family": key, "content_group_key": group_key},
            ))
            assets_by_hash[info["media_hash"]] = (key, row.storage_path)
        db.commit()
        add_itinerary_progress(db)
        journey = db.scalar(select(ConversationJourney))
        spec = route_snapshot_from_values(route, journey.slots)
        greeting = spec["groups"]["advisor_greeting"]["text"]
        prior = OutboundMessage(conversation_state_id=1, source_type="ai", source_id=0,
                                idempotency_key="paid:prior:greeting", content=greeting, content_type="text",
                                status="sent", chatwoot_message_id=98)
        attach_delivery_item(db, prior, journey, ["advisor_greeting"], item_id="paid:prior:greeting")
        db.add(prior)
        db.flush()
        record_delivery_progress(db, prior)
        fake.messages.insert(0, {"id": 98, "created_at": iso(dt(utcnow()) - timedelta(minutes=1)),
                                 "message_type": 1, "content": greeting})
        sop = SopDefinition(tenant_id=1, created_by=1, name=spec["sop"]["name"], status="running",
                            route_variant=route, inbox_ids=[128859], test_conversation_ids=[26],
                            nodes=spec["sop"]["nodes"])
        db.add(sop)
        db.flush()
        sop_snapshot(db, sop, 1)
        db.commit()
    requests = []
    original_text, original_image = fake.create_text_message, fake.create_attachment_message

    def transport(request):
        assert request.method == "POST"
        assert request.url.path == "/api/v1/accounts/180474/conversations/26/messages"
        if request.headers["content-type"].startswith("multipart/"):
            message = BytesParser(policy=policy.default).parsebytes(
                f"Content-Type: {request.headers['content-type']}\r\nMIME-Version: 1.0\r\n\r\n".encode() + request.read())
            fields = {part.get_param("name", header="content-disposition"): part for part in message.iter_parts()}
            data = fields["attachments[]"].get_payload(decode=True)
            digest = __import__("hashlib").sha256(data).hexdigest()
            key, path = assets_by_hash[digest]
            assert fields["attachments[]"].get_content_type() == "image/png"
            assert fields["private"].get_content().strip() == "false"
            content = fields["content"].get_content()
            requests.append({"kind": "image", "asset_key": key, "content": content, "hash": digest})
            result = original_image(26, content, path, "image/png")
        else:
            payload = json.loads(request.read())
            assert payload["message_type"] == "outgoing" and payload["private"] is False
            requests.append({"kind": "text", "content": payload["content"]})
            result = original_text(26, payload["content"])
        return httpx.Response(200, json={**result, "status": "sent"})

    adapter = ChatwootClient("https://example.invalid", 180474, "fake")
    adapter.client.close()
    adapter.client = httpx.Client(transport=httpx.MockTransport(transport),
                                 event_hooks={"request": [adapter._guard_request]})
    monkeypatch.setattr(fake, "create_text_message", adapter.create_text_message)
    monkeypatch.setattr(fake, "create_attachment_message", adapter.create_attachment_message)
    monkeypatch.setattr(live, "generate_decision", generate_decision)
    try:
        live.process_job(1)
        with session_factory() as db:
            job = db.get(LiveReplyJob, 1)
            print({"status": job.status, "reason": job.error_code, "route": job.decision.get("route_variant"),
                   "body": job.decision.get("reply_body"), "question": job.decision.get("follow_up_question"),
                   "model_ms": job.trace.get("model_ms"), "sop_enrollment": job.trace.get("sop_enrollment"),
                   "real_chatwoot_writes": 0})
            assert job.status == "submitted"
            assert job.decision["route_variant"] == route
            assert job.decision["material_keys"] == ["routes12-9d-itinerary"]
            body = job.decision["reply_body"]
            assert body and "行程" in body and any(word in body for word in ("圖", "图", "海報", "海报"))
            assert any(item["kind"] == "text" and item["content"] == body for item in requests)
            question = job.decision["follow_up_question"]
            assert question
            deferred = job.trace.get("deferred_follow_up")
            assert job.trace["sop_enrollment"]["status"] == "enrolled"
        if deferred:
            for _ in range(60):
                with session_factory() as db:
                    follow = db.scalar(select(LiveSopJob).where(LiveSopJob.node_key == "initial_delivery_follow_up"))
                    assert follow is not None
                    if follow.status == "submitted":
                        break
                    pending = db.scalar(select(LiveSopJob).where(LiveSopJob.status == "scheduled").order_by(LiveSopJob.scheduled_at))
                    assert pending is not None, [(row.node_key, row.status, row.reason) for row in db.scalars(select(LiveSopJob))]
                    now = max(utcnow(), pending.scheduled_at)
                monkeypatch.setattr(live_sop, "utcnow", lambda: now)
                assert live_sop.process_due_live_sop()
            else:
                pytest.fail("initial delivery did not reach final question")
        assert any(item.get("asset_key") == "routes12-9d-itinerary" for item in requests)
        assert requests[-1] == {"kind": "text", "content": question}
        assert sum(question in item["content"] for item in requests) == 1
        assert sum(item["content"].count("?") + item["content"].count("？") for item in requests) == 1
        print({"adapter_submissions": requests, "real_chatwoot_writes": 0})
    finally:
        adapter.close()


@pytest.mark.skipif(os.environ.get("VERIFY_LIVE_DEEPSEEK") != "1", reason="Explicit paid-model check; no Chatwoot client")
def test_real_deepseek_followup_month():
    from app.decision_service import generate_decision
    import re
    decision, logs, _, trace = generate_decision({"module": "reply", "customer_text": "9月啊",
        "route_variant": "peach_9d_2027", "available_materials": [], "context_messages": [
            {"direction": "incoming", "content": "想看2027年桃花9日的行程圖片，不上珠峰。"},
            {"direction": "outgoing", "content": "好的，這是2027桃花9日的行程海報，供您參考。請問您預計幾月出發？"}]})
    print({"action": decision.action, "branch": decision.branch, "reply": decision.reply,
           "slots": decision.slots, "handoff_reason": decision.handoff_reason,
           "model_ms": trace["model_ms"], "request_count": len(logs), "real_chatwoot_writes": 0})
    assert decision.slots.get("departure_window") == "9月"
    assert "party_size" not in decision.slots
    assert decision.slot_evidence.get("departure_window") in {"9月", "9月啊"}
    assert decision.material_keys == []
    assert decision.action == "reply" and not decision.handoff_reason
    assert decision.route_variant == "peach_9d_2027"
    assert "route.9.departure" in decision.evidence_refs
    reply = (decision.reply or "").replace("九月", "9月")
    assert "9月" in reply
    assert re.search(r"3月|三月", reply) and re.search(r"4月|四月", reply)
    assert re.search(r"不在|未公布|未公佈|尚未公布|尚未公佈|沒有.{0,8}(?:團期|出發)|需要.{0,8}(?:確認|核對)|需另.{0,8}(?:確認|核對)", reply)
    assert not re.search(r"9月.{0,12}(?:保證|保留|已安排|確定出團|可以出團|有團|有出發)|(?:安排|保留).{0,8}9月", reply)
    assert not re.search(r"(?:9|九)\s*(?:位|人)", reply)
    assert reply.count("?") + reply.count("？") <= 1
