import json
from concurrent.futures import ThreadPoolExecutor, Future
from datetime import timedelta
from threading import Event

import httpx
import pytest
import respx
from sqlalchemy import func, select

from app import live_reply as live
from app import live_reply_worker as worker
from app import notification_dispatch as dispatch
from app.chatwoot import ChatwootClient, ChatwootError
from app.config import settings
from app.models import (AppSetting, ChatwootConnection, ConversationState, HandoffTask,
                        MessageEvent, Notification, NotificationDelivery, WorkerHeartbeat, utcnow)
from app.operations import ensure_handoff, save_setting
from app.runtime_health import runtime_health
from test_live_reply import setup
from app.live_reply_models import LiveReplyJob
from app.models import WebhookEvent


def test_readiness_requires_live_workers_not_playground(session_factory, monkeypatch, client):
    setup(session_factory, monkeypatch)
    with session_factory() as db:
        db.add_all([WorkerHeartbeat(worker_id="live-reply-worker", last_seen_at=live.iso(live.dt(utcnow()) - timedelta(hours=2))),
                    WorkerHeartbeat(worker_id="playground-worker", last_seen_at=utcnow())])
        db.commit()
        result = runtime_health(db)
        assert result["status"] == "degraded" and not result["reactive_live_enabled"]
        assert result["worker_type"] == "live-reply-worker"
    assert client.get("/v1/health").status_code == 200
    assert client.get("/v1/health/ready").status_code == 503
    with session_factory() as db:
        db.get(WorkerHeartbeat, "live-reply-worker").last_seen_at = utcnow()
        db.add(WorkerHeartbeat(worker_id="live-maintenance-worker", last_seen_at=utcnow()))
        db.commit()
        assert runtime_health(db)["reactive_live_enabled"]
        save_setting(db, "global_message_sending", {"enabled": False})
        db.commit()
        assert not runtime_health(db)["reactive_live_enabled"]
    assert client.get("/v1/health/ready").status_code == 200


def notification_setup(factory, monkeypatch, channel="chatwoot"):
    setup(factory, monkeypatch)
    monkeypatch.setattr(worker, "SessionLocal", factory)
    with factory() as db:
        db.get(ChatwootConnection, 1).base_url = "https://chatwoot.invalid"
        save_setting(db, "global_message_sending", {"enabled": False})
        save_setting(db, "notification_settings", {"enabled": True, "channel": channel,
            "agent_id": 7, "bot_id": 8, "url": "https://notify.invalid/hook",
            "event_types": ["handoff.created", "handoff.overdue"]})
        ensure_handoff(db, db.get(ConversationState, 1), "test", "Needs an advisor")
        db.commit()


def mock_chatwoot(router, response=None, error=None):
    base = "https://chatwoot.invalid/api/v1/accounts/180474"
    router.get(f"{base}/conversations/26").respond(200, json={"id": 26, "inbox_id": 128859})
    router.get(f"{base}/inbox_members/128859").respond(200, json={"payload": [{"id": 7}]})
    route = router.post(f"{base}/conversations/26/messages")
    if error:
        route.mock(side_effect=error)
    else:
        route.respond(200, json=response or {"id": 99, "private": True, "sender": {"id": 8, "type": "agent_bot"}})
    return route


def test_handoff_to_private_notification_while_public_sending_paused(session_factory, monkeypatch):
    notification_setup(session_factory, monkeypatch)
    monkeypatch.setattr(worker, "reconcile_live_delivery", lambda _: None)
    with respx.mock() as router:
        sent = mock_chatwoot(router)
        worker.maintenance_tick({}, {})
        assert sent.call_count == 1
        payload = json.loads(sent.calls[0].request.content)
        assert payload["private"] is True and payload["sender_type"] == "AgentBot"
        assert "mention://user/7/" in payload["content"]
        assert not dispatch.process_notification_delivery(session_factory)
    with session_factory() as db:
        assert db.scalar(select(NotificationDelivery)).status == "delivered"
        assert db.get(WorkerHeartbeat, "live-maintenance-worker") is not None


def test_uncertain_private_note_never_blindly_retried(session_factory, monkeypatch):
    notification_setup(session_factory, monkeypatch)
    with respx.mock() as router:
        sent = mock_chatwoot(router, error=httpx.ReadTimeout("uncertain"))
        assert dispatch.process_notification_delivery(session_factory)
        assert not dispatch.process_notification_delivery(session_factory)
        assert sent.call_count == 1
    with session_factory() as db:
        assert db.scalar(select(NotificationDelivery)).status == "submission_unknown"
        assert runtime_health(db)["failed_tasks"]["notifications"] == 1


def test_evaluation_never_dispatches_copied_notification_queue(session_factory, monkeypatch):
    notification_setup(session_factory, monkeypatch)
    monkeypatch.setattr(settings, "app_profile", "evaluation")
    with respx.mock(assert_all_mocked=True):
        assert not dispatch.process_notification_delivery(session_factory)
    with session_factory() as db:
        assert db.scalar(select(NotificationDelivery)).attempts == 0


def test_webhook_retries_with_stable_idempotency_key(session_factory, monkeypatch):
    notification_setup(session_factory, monkeypatch, "webhook")
    with respx.mock() as router:
        route = router.post("https://notify.invalid/hook").respond(503)
        assert dispatch.process_notification_delivery(session_factory)
        first = route.calls[0].request.headers["Idempotency-Key"]
        with session_factory() as db:
            row = db.scalar(select(NotificationDelivery))
            assert row.status == "retry"
            row.available_at = utcnow()
            db.commit()
        route.respond(200)
        assert dispatch.process_notification_delivery(session_factory)
        assert route.calls[-1].request.headers["Idempotency-Key"] == first
    with session_factory() as db:
        assert db.scalar(select(NotificationDelivery)).status == "delivered"


def test_overdue_reminders_are_deduplicated(session_factory, monkeypatch):
    notification_setup(session_factory, monkeypatch)
    with session_factory() as db:
        db.scalar(select(HandoffTask)).sla_due_at = live.iso(live.dt(utcnow()) - timedelta(minutes=1))
        db.commit()
    assert dispatch.process_handoff_overdue(session_factory)
    assert not dispatch.process_handoff_overdue(session_factory)
    with session_factory() as db:
        assert db.scalar(select(func.count()).select_from(Notification).where(Notification.event_type == "handoff.overdue")) == 1


@pytest.mark.parametrize("private", [False, "true", None])
def test_public_send_switch_cannot_be_bypassed_by_non_boolean_private(monkeypatch, private):
    monkeypatch.setattr(settings, "outbound_mode", "live")
    monkeypatch.setattr(settings, "chatwoot_write_enabled", True)
    monkeypatch.setattr("app.chatwoot.global_message_sending_enabled", lambda: False)
    client = ChatwootClient("https://chatwoot.invalid", 180474, "test")
    try:
        with pytest.raises(ChatwootError, match="Global customer"):
            client._guard_request(httpx.Request("POST", client._url("conversations/26/messages"), json={"private": private}))
    finally:
        client.close()


@pytest.mark.parametrize("case", ["overdue", "before_activation", "answered", "newer", "paused", "inbox_off"])
def test_stale_messages_escalate_only_unanswered_post_activation(session_factory, monkeypatch, case):
    setup(session_factory, monkeypatch)
    old = live.iso(live.dt(utcnow()) - timedelta(minutes=10))
    with session_factory() as db:
        state = db.get(ConversationState, 1)
        state.labels = ["ai"]
        if case == "paused":
            state.ai_mode, state.ai_mode_source = "disabled", "platform"
        if case == "inbox_off":
            state.inbox.ai_enabled = False
        message = db.get(MessageEvent, 1)
        message.created_at = old
        armed = live.iso(live.dt(old) - timedelta(minutes=1)) if case != "before_activation" else utcnow()
        if case in {"answered", "newer"}:
            db.add(MessageEvent(conversation_state_id=1, chatwoot_message_id=101,
                                direction="outgoing" if case == "answered" else "incoming", content="reply"))
            db.flush()
        result = live.handoff_stale_message(db, state, message, armed_at=armed)
        assert result is (case == "overdue")
        if result:
            live.handoff_stale_message(db, state, message, armed_at=armed)
            assert db.scalar(select(func.count()).select_from(HandoffTask)) == 1


def test_slow_customer_does_not_block_other_customer_and_same_conversation_is_serial():
    release = Event()
    started = Event()
    second = Event()
    def slow(_):
        started.set()
        assert release.wait(5)
    with ThreadPoolExecutor(max_workers=2) as pool:
        scheduler = worker.ConversationScheduler(pool, 2)
        try:
            assert scheduler.submit(1, slow, 1)
            assert started.wait(5)
            assert not scheduler.submit(1, lambda _: None, 2)
            assert scheduler.submit(2, lambda _: second.set(), 3)
            assert second.wait(5)
            assert not scheduler.submit(3, lambda _: None, 4)  # Capacity reclaimed only on reap.
        finally:
            release.set()
    scheduler.reap()
    assert not scheduler.active


def test_maintenance_runs_all_tasks_even_when_one_fails(monkeypatch):
    calls = []
    monkeypatch.setattr(dispatch, "process_handoff_overdue", lambda _: (_ for _ in ()).throw(RuntimeError()))
    monkeypatch.setattr(dispatch, "process_notification_delivery", lambda _: calls.append("notify"))
    monkeypatch.setattr(worker, "reconcile_live_delivery", lambda _: calls.append("receipts"))
    monkeypatch.setattr(worker, "heartbeat", lambda _: calls.append("heartbeat"))
    worker.maintenance_tick({}, {})
    assert calls == ["notify", "receipts"]


def test_receipt_failure_remains_unhealthy_between_scheduled_checks(monkeypatch):
    beats = []
    monkeypatch.setattr(dispatch, "process_handoff_overdue", lambda _: False)
    monkeypatch.setattr(dispatch, "process_notification_delivery", lambda _: False)
    monkeypatch.setattr(worker, "reconcile_live_delivery", lambda _: (_ for _ in ()).throw(RuntimeError()))
    monkeypatch.setattr(worker, "heartbeat", lambda _: beats.append(1))
    deadlines = {}
    worker.maintenance_tick({}, deadlines)
    worker.maintenance_tick({}, deadlines)
    assert not beats
    monkeypatch.setattr(worker, "reconcile_live_delivery", lambda _: None)
    deadlines["receipts"] = 0
    worker.maintenance_tick({}, deadlines)
    assert beats == [1]


def test_notification_settings_validate_and_preserve_destination(authenticated):
    client, csrf = authenticated
    headers = {"X-CSRF-Token": csrf}
    assert client.patch("/v1/settings/notifications", headers=headers, json={"enabled": True, "channel": "chatwoot"}).status_code == 422
    response = client.patch("/v1/settings/notifications", headers=headers,
                            json={"enabled": True, "channel": "chatwoot", "agent_id": 7, "bot_id": 8})
    assert response.status_code == 200
    result = client.get("/v1/settings/notifications").json()
    assert result["channel"] == "chatwoot" and result["agent_id"] == 7 and result["bot_id"] == 8


def test_delayed_webhook_creates_handoff_without_automatic_reply(session_factory, monkeypatch):
    setup(session_factory, monkeypatch)
    old = live.iso(live.dt(utcnow()) - timedelta(minutes=10))
    with session_factory() as db:
        save_setting(db, "live_reply", {"armed_at": live.iso(live.dt(old) - timedelta(minutes=1))})
        event = WebhookEvent(connection_id=1, event="message_created", account_id=180474,
            resource_id="101", idempotency_key="delayed", received_at=old, payload={
                "event": "message_created", "id": 101, "message_type": "incoming", "content": "Help",
                "created_at": old, "inbox": {"id": 128859},
                "conversation": {"id": 26, "inbox_id": 128859, "labels": ["AI"], "can_reply": True}})
        db.add(event)
        db.flush()
        live.mirror_event(db, event)
        assert db.scalar(select(HandoffTask)).reason_code == "reply_queue_overdue"
        assert db.scalar(select(func.count()).select_from(LiveReplyJob)) == 1  # Only the fixture's existing job.
        assert event.status == "live_observed"


def test_queued_reply_that_expires_creates_handoff_without_sending(session_factory, monkeypatch):
    fake = setup(session_factory, monkeypatch)
    old = live.iso(live.dt(utcnow()) - timedelta(minutes=10))
    fake.messages[0]["created_at"] = old
    with session_factory() as db:
        db.get(MessageEvent, 1).created_at = old
        save_setting(db, "live_reply", {"armed_at": live.iso(live.dt(old) - timedelta(minutes=1))})
        db.commit()
    live.process_job(1)
    assert not fake.sent
    with session_factory() as db:
        assert db.scalar(select(HandoffTask)).reason_code == "reply_queue_overdue"
        assert db.get(LiveReplyJob, 1).trace["manual_followup_created"] is True


def test_dispatch_skips_busy_conversation_and_limits_capacity(session_factory, monkeypatch):
    setup(session_factory, monkeypatch)
    monkeypatch.setattr(worker, "SessionLocal", session_factory)
    class Executor:
        def __init__(self):
            self.calls = []
        def submit(self, fn, job_id):
            self.calls.append(job_id)
            return Future()
    with session_factory() as db:
        db.add(ConversationState(id=2, tenant_id=1, inbox_binding_id=1, chatwoot_conversation_id=27))
        db.flush()
        db.add_all([MessageEvent(id=2, conversation_state_id=1, chatwoot_message_id=101, direction="incoming"),
                    MessageEvent(id=3, conversation_state_id=2, chatwoot_message_id=102, direction="incoming")])
        db.flush()
        db.add_all([LiveReplyJob(id=2, conversation_state_id=1, trigger_message_id=2, due_at=utcnow()),
                    LiveReplyJob(id=3, conversation_state_id=2, trigger_message_id=3, due_at=utcnow())])
        db.commit()
    executor = Executor()
    scheduler = worker.ConversationScheduler(executor, 2)
    worker.dispatch_due(scheduler)
    assert executor.calls == [1, 3]
    worker.dispatch_due(scheduler)
    assert executor.calls == [1, 3]


def test_private_notification_webhook_does_not_reactivate_handoff(session_factory, monkeypatch):
    setup(session_factory, monkeypatch)
    with session_factory() as db:
        state = db.get(ConversationState, 1)
        ensure_handoff(db, state, "manual", "Needs advisor")
        event = WebhookEvent(connection_id=1, event="message_created", account_id=180474,
            resource_id="105", idempotency_key="private-note", received_at=utcnow(), payload={
                "event": "message_created", "id": 105, "message_type": "outgoing", "private": True,
                "content": "Advisor notification", "created_at": utcnow(), "inbox": {"id": 128859},
                "conversation": {"id": 26, "inbox_id": 128859, "labels": ["AI"], "can_reply": True}})
        db.add(event)
        db.flush()
        live.mirror_event(db, event)
        assert state.effective_ai_state == "HUMAN_HANDOFF"
        assert db.scalar(select(func.count()).select_from(HandoffTask)) == 1
