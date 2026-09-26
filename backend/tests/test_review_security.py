from unittest.mock import Mock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import app
from app.chatwoot import ChatwootError
from app.models import (
    AppSession, ConversationState, HandoffTask, InboxBinding, LabelEvent,
    Notification, NotificationRead, Tenant, User, UserInboxScope,
)
from app.operations import ensure_handoff
from app.ops_api import claim_for
from app.security import hash_password


def login(client, email, password="password123"):
    response = client.post("/v1/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200
    return {"X-CSRF-Token": response.json()["csrf_token"]}


@pytest.fixture
def scoped_data(session_factory):
    with session_factory() as db:
        db.add_all([
            User(id=2, email="agent@example.com", display_name="Agent", role="agent", password_hash=hash_password("password123"), chatwoot_agent_id=102),
            User(id=3, email="supervisor@example.com", display_name="Supervisor", role="supervisor", password_hash=hash_password("password123"), chatwoot_agent_id=103),
            InboxBinding(id=1, tenant_id=1, chatwoot_inbox_id=10, name="allowed"),
            InboxBinding(id=2, tenant_id=1, chatwoot_inbox_id=20, name="restricted"),
        ])
        db.flush()
        db.add_all([
            UserInboxScope(user_id=2, inbox_binding_id=1),
            UserInboxScope(user_id=3, inbox_binding_id=1),
            ConversationState(id=1, tenant_id=1, inbox_binding_id=1, chatwoot_conversation_id=100),
            ConversationState(id=2, tenant_id=1, inbox_binding_id=2, chatwoot_conversation_id=200),
        ])
        db.commit()


def test_notifications_enforce_scope_for_listing_and_read(client, session_factory, scoped_data):
    with session_factory() as db:
        ensure_handoff(db, db.get(ConversationState, 1), "manual", "allowed detail")
        ensure_handoff(db, db.get(ConversationState, 2), "manual", "restricted detail")
        db.add(Notification(tenant_id=1, event_type="system", title="admin only", body="operational detail"))
        db.add(Notification(tenant_id=1, user_id=2, event_type="system", title="personal"))
        db.add(Notification(tenant_id=1, user_id=3, event_type="system", title="someone else"))
        db.add(Notification(tenant_id=1, conversation_id=999, event_type="handoff", title="orphan"))
        db.commit()
        ids = {n.title + n.body: n.id for n in db.scalars(select(Notification))}
        forbidden = db.scalar(select(Notification.id).where(Notification.conversation_id == 200))
    headers = login(client, "agent@example.com")
    assert client.get("/v1/conversations/200").status_code == 404
    response = client.get("/v1/notifications").json()
    assert len(response["items"]) == 2
    assert {item["body"] for item in response["items"]} == {"allowed detail", ""}
    assert client.post(f"/v1/notifications/{forbidden}/read", headers=headers).status_code == 404
    assert client.post(f"/v1/notifications/{ids['orphan']}/read", headers=headers).status_code == 404


def test_notification_conversation_match_includes_tenant(client, session_factory, scoped_data):
    with session_factory() as db:
        db.add(Tenant(id=2, name="other")); db.flush()
        db.add(Notification(tenant_id=2, conversation_id=100, event_type="handoff", title="other tenant"))
        db.commit()
    login(client, "agent@example.com")
    assert client.get("/v1/notifications").json()["items"] == []


def test_broadcast_reads_are_personal_and_idempotent(client, session_factory, scoped_data):
    with session_factory() as db:
        row = Notification(tenant_id=1, conversation_id=100, event_type="handoff", title="shared")
        db.add(row); db.commit(); notification_id = row.id
    headers = login(client, "agent@example.com")
    for _ in range(2):
        assert client.post(f"/v1/notifications/{notification_id}/read", headers=headers).status_code == 200
    assert client.get("/v1/notifications?unread_only=true").json()["items"] == []
    login(client, "supervisor@example.com")
    response = client.get("/v1/notifications").json()
    assert response["unread"] == 1
    assert response["items"][0]["read_at"] is None
    with session_factory() as db:
        assert len(db.scalars(select(NotificationRead)).all()) == 1
        assert db.get(Notification, notification_id).read_at is None


def test_notification_unread_filter_is_applied_before_limit(client, session_factory, scoped_data):
    with session_factory() as db:
        for i in range(60):
            row = Notification(tenant_id=1, conversation_id=100, event_type="handoff", title=str(i))
            db.add(row); db.flush()
            if i >= 5:
                db.add(NotificationRead(notification_id=row.id, user_id=2))
        db.commit()
    login(client, "agent@example.com")
    response = client.get("/v1/notifications?unread_only=true").json()
    assert response["unread"] == 5
    assert {item["title"] for item in response["items"]} == {str(i) for i in range(5)}


def test_password_reset_revokes_all_sessions_and_requires_change(client, session_factory, scoped_data):
    old = TestClient(app)
    old_headers = login(old, "agent@example.com")
    admin_headers = login(client, "admin@example.com")
    result = client.post("/v1/users/2/reset-password", headers=admin_headers)
    assert result.status_code == 200
    assert old.get("/v1/auth/me").status_code == 401
    assert old.post("/v1/notifications/1/read", headers=old_headers).status_code == 401
    with session_factory() as db:
        assert all(row.revoked_at for row in db.scalars(select(AppSession).where(AppSession.user_id == 2)))
    headers = login(old, "agent@example.com", result.json()["temporary_password"])
    assert old.get("/v1/auth/me").json()["must_change_password"] is True
    assert old.get("/v1/auth/csrf").status_code == 200
    assert old.get("/v1/notifications").json()["error"]["code"] == "password_change_required"
    assert old.post("/v1/handoffs/1/claim", headers=headers, json={"version": 1}).status_code == 403
    changed = old.post("/v1/auth/change-password", headers=headers, json={
        "current_password": result.json()["temporary_password"], "new_password": "replacement123",
    })
    assert changed.status_code == 200
    assert old.get("/v1/notifications").status_code == 200


def test_password_change_preserves_current_session_but_revokes_others(client, scoped_data):
    other = TestClient(app)
    login(other, "agent@example.com")
    headers = login(client, "agent@example.com")
    result = client.post("/v1/auth/change-password", headers=headers, json={"current_password": "password123", "new_password": "replacement123"})
    assert result.status_code == 200
    assert client.get("/v1/auth/me").status_code == 200
    assert other.get("/v1/auth/me").status_code == 401


def test_temporary_password_can_logout(client, session_factory, scoped_data):
    with session_factory() as db:
        db.get(User, 2).must_change_password = True; db.commit()
    headers = login(client, "agent@example.com")
    assert client.post("/v1/auth/logout", headers=headers).status_code == 204


def test_bi_conversion_scope_and_event_time(client, session_factory, scoped_data):
    with session_factory() as db:
        db.get(Tenant, 1).analytics_cutover_at = "2000-01-01T00:00:00+00:00"
        # A recent conversion on an older mirror must still be counted.
        db.get(ConversationState, 1).updated_at = "2000-01-01T00:00:00+00:00"
        for conversation_id in (1, 2):
            for label in ("已留资", "已成交"):
                db.add(LabelEvent(conversation_state_id=conversation_id, label=label, action="added"))
        db.commit()
    login(client, "supervisor@example.com")
    result = client.get("/v1/bi/overview").json()
    assert result["conversations"] == 0
    assert result["leads"] == result["conversions"] == 1
    result = client.get("/v1/bi/overview?inbox_id=20").json()
    assert result["leads"] == result["conversions"] == 0
    with session_factory() as db:
        for scope in db.scalars(select(UserInboxScope).where(UserInboxScope.user_id == 3)):
            db.delete(scope)
        db.commit()
    result = client.get("/v1/bi/overview").json()
    assert result["leads"] == result["conversions"] == 0
    login(client, "admin@example.com")
    assert client.get("/v1/bi/overview").json()["leads"] == 2
    assert client.get("/v1/bi/overview?inbox_id=10").json()["leads"] == 1


@pytest.fixture
def assignment_client(session_factory, scoped_data, monkeypatch):
    with session_factory() as db:
        db.add(HandoffTask(id=1, conversation_state_id=1, reason_code="manual")); db.commit()
    remote = Mock()
    monkeypatch.setattr("app.ops_api.get_connection", lambda db: None)
    monkeypatch.setattr("app.ops_api.client_for", lambda connection: remote)
    return remote


def test_stale_concurrent_claim_only_assigns_once(session_factory, assignment_client):
    with session_factory() as a, session_factory() as b:
        task_a, task_b = a.get(HandoffTask, 1), b.get(HandoffTask, 1)
        conversation_a, conversation_b = a.get(ConversationState, 1), b.get(ConversationState, 1)
        first, second = a.get(User, 2), b.get(User, 3)
        claim_for(a, task_a, conversation_a, first, first, 1)
        with pytest.raises(HTTPException) as error:
            claim_for(b, task_b, conversation_b, second, second, 1)
        assert error.value.status_code == 409
    assignment_client.assign_conversation.assert_called_once_with(100, 102)


def test_inflight_assignment_blocks_fresh_version_claim(session_factory, assignment_client):
    def concurrent_claim(*args):
        with session_factory() as other:
            task = other.get(HandoffTask, 1)
            assert task.assignment_target_user_id == 2
            target = other.get(User, 3)
            with pytest.raises(HTTPException) as error:
                claim_for(other, task, other.get(ConversationState, 1), target, target, task.version)
            assert error.value.status_code == 409
    assignment_client.assign_conversation.side_effect = concurrent_claim
    with session_factory() as db:
        target = db.get(User, 2)
        claim_for(db, db.get(HandoffTask, 1), db.get(ConversationState, 1), target, target, 1)
    assert assignment_client.assign_conversation.call_count == 1


def test_unknown_assignment_is_reconciled_without_resending(client, session_factory, assignment_client):
    headers = login(client, "agent@example.com")
    assignment_client.assign_conversation.side_effect = ChatwootError("chatwoot_unreachable", "timeout")
    result = client.post("/v1/handoffs/1/claim", headers=headers, json={"version": 1})
    assert result.status_code == 502
    task = client.get("/v1/handoffs").json()["items"][0]
    assert task["assignment_pending"] is True
    assert client.post("/v1/handoffs/1/claim", headers=headers, json={"version": task["version"]}).status_code == 409
    assignment_client.get_conversation.return_value = {"id": 100, "meta": {"assignee": {"id": 103}}}
    assert client.post("/v1/handoffs/1/reconcile-assignment", headers=headers, json={"version": task["version"]}).status_code == 409
    assignment_client.get_conversation.return_value = {"id": 100, "meta": {"assignee": {"id": 102}}}
    result = client.post("/v1/handoffs/1/reconcile-assignment", headers=headers, json={"version": task["version"]})
    assert result.status_code == 200
    assert result.json()["assignment_pending"] is False
    assert result.json()["assignee_user_id"] == 2
    assert assignment_client.assign_conversation.call_count == 1


def test_explicit_assignment_rejection_releases_reservation(client, session_factory, assignment_client):
    headers = login(client, "agent@example.com")
    assignment_client.assign_conversation.side_effect = ChatwootError("chatwoot_request_failed", "invalid", 422)
    assert client.post("/v1/handoffs/1/claim", headers=headers, json={"version": 1}).status_code == 502
    task = client.get("/v1/handoffs").json()["items"][0]
    assert task["assignment_pending"] is False
    assignment_client.assign_conversation.side_effect = None
    assert client.post("/v1/handoffs/1/claim", headers=headers, json={"version": task["version"]}).status_code == 200


def test_pending_reassignment_cannot_be_completed(client, session_factory, assignment_client):
    with session_factory() as db:
        task = db.get(HandoffTask, 1)
        task.status, task.assignee_user_id = "claimed", 2
        task.assignment_target_user_id, task.assignment_target_agent_id = 3, 103
        db.commit()
    headers = login(client, "agent@example.com")
    assert client.post("/v1/handoffs/1/complete", headers=headers, json={"version": 1}).status_code == 409


@pytest.mark.parametrize("scopes", [{}, {"inbox_binding_ids": [1]}])
def test_user_binding_conflicts_return_409_on_create_and_update(client, session_factory, scoped_data, scopes):
    headers = login(client, "admin@example.com")
    result = client.post("/v1/users", headers=headers, json={
        "email": "duplicate@example.com", "display_name": "Duplicate", "role": "agent", "chatwoot_agent_id": 102,
    })
    assert result.status_code == 409
    result = client.patch("/v1/users/3", headers=headers, json={"chatwoot_agent_id": 102, **scopes})
    assert result.status_code == 409
    with session_factory() as db:
        assert db.get(User, 3).chatwoot_agent_id == 103
