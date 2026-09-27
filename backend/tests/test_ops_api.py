from sqlalchemy import select

from app.models import AppSetting, AuditLog, ChatwootConnection, ChatwootLabel, Contact, ConversationState, HandoffTask, InboxBinding, SopDefinition, SopEnrollment, SopJob, SyncJob, User, UserInboxScope
from app.live_reply_models import LiveReplyJob
from app.models import MessageEvent, utcnow
from app.security import encrypt_secret, hash_password
from app.worker_main import enroll_matching_sops


class FakeChatwoot:
    def __init__(self):
        self.labels = []
        self.assignee = None

    def close(self):
        pass

    def get_conversation_labels(self, _conversation_id):
        return {"payload": self.labels}

    def set_conversation_labels(self, _conversation_id, labels):
        self.labels = labels
        return {"payload": labels}

    def assign_conversation(self, _conversation_id, assignee_id):
        self.assignee = assignee_id
        return {"id": assignee_id}


def seed(session_factory):
    with session_factory() as db:
        db.add(ChatwootConnection(tenant_id=1, account_id=180474, encrypted_api_token=encrypt_secret("test-token"), connection_key="ops-test"))
        inbox = InboxBinding(tenant_id=1, chatwoot_inbox_id=128859, name="Facebook", channel_type="Channel::FacebookPage")
        contact = Contact(tenant_id=1, chatwoot_contact_id=99, name="Customer", email="customer@example.com", phone_number="13800138000")
        db.add_all([inbox, contact])
        db.flush()
        db.add(ConversationState(tenant_id=1, inbox_binding_id=inbox.id, contact_id=contact.id, chatwoot_conversation_id=10, labels=[], can_reply=True))
        db.commit()
        return inbox.id


def test_global_message_sending_switch_defaults_off_and_can_be_managed(authenticated, session_factory):
    client, csrf = authenticated
    initial = client.get("/v1/settings/global-message-sending")
    assert initial.status_code == 200
    assert initial.json()["enabled"] is False

    started = client.patch(
        "/v1/settings/global-message-sending",
        headers={"X-CSRF-Token": csrf},
        json={"enabled": True},
    )
    assert started.status_code == 200
    assert started.json()["enabled"] is True

    stopped = client.patch(
        "/v1/settings/global-message-sending",
        headers={"X-CSRF-Token": csrf},
        json={"enabled": False},
    )
    assert stopped.status_code == 200
    assert stopped.json()["enabled"] is False
    assert stopped.json()["effective_enabled"] is False

    with session_factory() as db:
        assert db.get(AppSetting, "global_message_sending").value == {"enabled": False}
        log = db.scalar(select(AuditLog).where(
            AuditLog.action == "settings.global_message_sending"
        ).order_by(AuditLog.id.desc()))
        assert log is not None and log.details == {"enabled": False}


def test_ai_reception_rollout_can_manage_allowlist_without_disabling_ai_label_gate(
    authenticated, session_factory, monkeypatch
):
    from app.config import settings

    monkeypatch.setattr(settings, "live_sop_scope", "allowlist")
    monkeypatch.setattr(settings, "live_sop_conversation_ids", "26")
    client, csrf = authenticated

    initial = client.get("/v1/settings/ai-reception-rollout")
    assert initial.status_code == 200
    assert initial.json()["allowlist_enabled"] is True
    assert initial.json()["conversation_ids"] == [26]
    assert initial.json()["require_ai_label"] is True

    saved = client.patch(
        "/v1/settings/ai-reception-rollout",
        headers={"X-CSRF-Token": csrf},
        json={
            "allowlist_enabled": True,
            "conversation_ids": [42, 26, 42],
        },
    )
    assert saved.status_code == 200
    assert saved.json()["conversation_ids"] == [26, 42]
    assert saved.json()["scope"] == "allowlist"
    assert saved.json()["require_ai_label"] is True

    missing_confirmation = client.patch(
        "/v1/settings/ai-reception-rollout",
        headers={"X-CSRF-Token": csrf},
        json={"allowlist_enabled": False, "conversation_ids": [26, 42]},
    )
    assert missing_confirmation.status_code == 422

    open_scope = client.patch(
        "/v1/settings/ai-reception-rollout",
        headers={"X-CSRF-Token": csrf},
        json={
            "allowlist_enabled": False,
            "conversation_ids": [26, 42],
            "confirm_ai_label_scope": True,
        },
    )
    assert open_scope.status_code == 200
    assert open_scope.json()["scope"] == "ai_label"
    assert open_scope.json()["require_ai_label"] is True

    with session_factory() as db:
        row = db.get(AppSetting, "ai_reception_rollout")
        assert row.value == {"allowlist_enabled": False, "conversation_ids": [26, 42]}
        log = db.scalar(select(AuditLog).where(
            AuditLog.action == "settings.ai_reception_rollout"
        ).order_by(AuditLog.id.desc()))
        assert log is not None


def test_ai_reception_rollout_rejects_empty_enabled_allowlist(authenticated):
    client, csrf = authenticated
    response = client.patch(
        "/v1/settings/ai-reception-rollout",
        headers={"X-CSRF-Token": csrf},
        json={"allowlist_enabled": True, "conversation_ids": []},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "allowlist_empty"


def test_narrowing_ai_reception_allowlist_cancels_outside_queued_replies(
    authenticated, session_factory
):
    client, csrf = authenticated
    seed(session_factory)
    with session_factory() as db:
        conversation = db.scalar(select(ConversationState).where(
            ConversationState.chatwoot_conversation_id == 10
        ))
        message = MessageEvent(
            conversation_state_id=conversation.id,
            chatwoot_message_id=100,
            direction="incoming",
            content="test",
        )
        db.add(message)
        db.flush()
        db.add(LiveReplyJob(
            conversation_state_id=conversation.id,
            trigger_message_id=message.id,
            due_at=utcnow(),
        ))
        db.commit()

    response = client.patch(
        "/v1/settings/ai-reception-rollout",
        headers={"X-CSRF-Token": csrf},
        json={"allowlist_enabled": True, "conversation_ids": [26]},
    )
    assert response.status_code == 200
    assert response.json()["cancelled_reply_jobs"] == 1
    with session_factory() as db:
        job = db.scalar(select(LiveReplyJob))
        assert job.status == "cancelled"
        assert job.error_code == "ai_reception_allowlist_changed"


def test_user_creation_returns_temporary_password_once(authenticated, session_factory):
    client, csrf = authenticated
    inbox_id = seed(session_factory)
    response = client.post("/v1/users", headers={"X-CSRF-Token": csrf}, json={"email": "agent@example.com", "display_name": "Agent", "role": "agent", "chatwoot_agent_id": 7, "inbox_binding_ids": [inbox_id]})
    assert response.status_code == 200
    assert response.json()["temporary_password"]
    assert response.json()["inbox_binding_ids"] == [inbox_id]
    listing = client.get("/v1/users").json()["items"]
    assert "temporary_password" not in listing[-1]


def test_manual_handoff_stops_ai_and_claim_assigns_chatwoot(authenticated, session_factory, monkeypatch):
    client, csrf = authenticated
    seed(session_factory)
    fake = FakeChatwoot()
    monkeypatch.setattr("app.ops_api.client_for", lambda _connection: fake)
    response = client.post("/v1/handoffs", headers={"X-CSRF-Token": csrf}, json={"conversation_id": 10, "reason_code": "manual", "reason_detail": "test", "priority": "P2"})
    assert response.status_code == 200
    assert "人工接管" in fake.labels
    with session_factory() as db:
        task = db.scalar(select(HandoffTask))
        state = db.scalar(select(ConversationState))
        admin = db.scalar(select(User))
        admin.chatwoot_agent_id = 7
        db.commit()
        version = task.version
        assert state.effective_ai_state == "HUMAN_HANDOFF"
    claim = client.post(f"/v1/handoffs/{response.json()['id']}/claim", headers={"X-CSRF-Token": csrf}, json={"version": version})
    assert claim.status_code == 200
    assert fake.assignee == 7
    conflict = client.post(f"/v1/handoffs/{response.json()['id']}/claim", headers={"X-CSRF-Token": csrf}, json={"version": version})
    assert conflict.status_code == 409




def test_first_login_password_change(authenticated, session_factory):
    client, csrf = authenticated
    with session_factory() as db:
        user = db.scalar(select(User))
        user.must_change_password = True
        db.commit()
    response = client.post("/v1/auth/change-password", headers={"X-CSRF-Token": csrf}, json={"current_password": "password123", "new_password": "replacement123"})
    assert response.status_code == 200
    assert response.json()["must_change_password"] is False


def test_agent_contact_data_is_masked_until_assigned(client, session_factory):
    inbox_id = seed(session_factory)
    with session_factory() as db:
        agent = User(email="agent@example.com", display_name="Agent", password_hash=hash_password("password123"), role="agent", chatwoot_agent_id=7)
        db.add(agent)
        db.flush()
        db.add(UserInboxScope(user_id=agent.id, inbox_binding_id=inbox_id))
        db.commit()
    csrf = client.post("/v1/auth/login", json={"email": "agent@example.com", "password": "password123"}).json()["csrf_token"]
    masked = client.get("/v1/conversations/10").json()["contact"]
    assert masked == {"name": "Customer", "email": "c***@example.com", "phone_number": "***8000", "pii_masked": True}
    with session_factory() as db:
        db.scalar(select(ConversationState)).assignee_id = 7
        db.commit()
    visible = client.get("/v1/conversations/10").json()["contact"]
    assert visible["email"] == "customer@example.com"
    assert visible["pii_masked"] is False


def test_label_trigger_enrolls_running_sop(session_factory):
    seed(session_factory)
    with session_factory() as db:
        conversation = db.scalar(select(ConversationState))
        sop = SopDefinition(tenant_id=1, created_by=1, name="Label SOP", status="running", trigger_type="label", trigger_labels=["意向客户"], nodes=[{"key": "step1", "delay_minutes": 0, "content": "hello"}])
        db.add(sop)
        db.flush()
        enroll_matching_sops(db, conversation, {"意向客户"})
        db.commit()
        assert db.scalar(select(SopEnrollment)).conversation_state_id == conversation.id
        assert db.scalar(select(SopJob)).status == "scheduled"


def test_history_sync_can_pause(authenticated, session_factory):
    client, csrf = authenticated
    with session_factory() as db:
        db.add(SyncJob(tenant_id=1, status="running"))
        db.commit()
    response = client.post("/v1/settings/history-sync/pause", headers={"X-CSRF-Token": csrf})
    assert response.status_code == 200
    assert response.json()["status"] == "paused"
