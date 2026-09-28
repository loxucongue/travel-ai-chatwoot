from app.runtime_settings import iso
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
from live_fixture import setup
from app.live_reply_models import LiveReplyJob
from app.models import WebhookEvent


def test_readiness_requires_live_workers_not_playground(session_factory, monkeypatch, client):
    setup(session_factory, monkeypatch)
    with session_factory() as db:
        db.add_all([WorkerHeartbeat(worker_id="live-reply-worker", last_seen_at=iso(live.dt(utcnow()) - timedelta(hours=2))),
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
        db.scalar(select(HandoffTask)).sla_due_at = iso(live.dt(utcnow()) - timedelta(minutes=1))
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






def test_maintenance_runs_all_tasks_even_when_one_fails(monkeypatch, session_factory):
    monkeypatch.setattr(worker, "SessionLocal", session_factory)
    calls = []
    monkeypatch.setattr(dispatch, "process_handoff_overdue", lambda _: (_ for _ in ()).throw(RuntimeError()))
    monkeypatch.setattr(dispatch, "process_notification_delivery", lambda _: calls.append("notify"))
    monkeypatch.setattr(worker, "reconcile_live_delivery", lambda _: calls.append("receipts"))
    monkeypatch.setattr(worker, "heartbeat", lambda _: calls.append("heartbeat"))
    worker.maintenance_tick({}, {})
    assert calls == ["notify", "receipts"]


def test_receipt_failure_remains_unhealthy_between_scheduled_checks(monkeypatch, session_factory):
    monkeypatch.setattr(worker, "SessionLocal", session_factory)
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
    old = iso(live.dt(utcnow()) - timedelta(minutes=10))
    with session_factory() as db:
        save_setting(db, "live_reply", {"armed_at": iso(live.dt(old) - timedelta(minutes=1))})
        event = WebhookEvent(connection_id=1, event="message_created", account_id=180474,
            resource_id="101", idempotency_key="delayed", received_at=old, payload={
                "event": "message_created", "id": 101, "message_type": "incoming", "content": "Help",
                "created_at": old, "inbox": {"id": 128859},
                "conversation": {"id": 26, "inbox_id": 128859, "labels": ["AI"], "can_reply": True}})
        db.add(event)
        db.flush()
        live.mirror_event(db, event)
        assert db.scalar(select(HandoffTask)).reason_code == "reply_queue_overdue"
        assert db.scalar(select(func.count()).select_from(LiveReplyJob)) == 0  # Retired engine jobs are never created.
        assert event.status == "live_observed"






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
