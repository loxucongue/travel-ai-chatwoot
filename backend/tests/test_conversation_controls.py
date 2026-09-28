from sqlalchemy import select


from app.config import settings


from app.models import (AppSetting, ChatwootConnection, Contact, ConversationState, InboxBinding,
                        MessageEvent, OutboundMessage, utcnow)


from app.security import encrypt_secret


class FakeChatwoot:
    def __init__(self):
        self.current_labels = ["ai", "外部新增"]
        self.calls = []
        self.assignee_id = None
        self.can_reply = True
        self.messages = [{
            "id": 501,
            "message_type": 0,
            "private": False,
            "content": "你好",
            "content_attributes": {},
            "created_at": utcnow(),
        }]

    def close(self):
        pass

    def list_inbox_agents(self, _inbox_id):
        return {"payload": [{"id": 7, "name": "Agent Seven", "availability_status": "available", "role": "agent"}]}

    def list_labels(self):
        return {"payload": [
            {"id": 1, "title": "ai", "color": "#111111"},
            {"id": 2, "title": "人工接管", "color": "#DF0808"},
            {"id": 3, "title": "外部新增", "color": "#333333"},
        ]}

    def get_conversation_labels(self, _conversation_id):
        return {"payload": list(self.current_labels)}

    def set_conversation_labels(self, _conversation_id, labels):
        self.calls.append(("labels", list(labels)))
        self.current_labels = list(labels)
        return {"payload": list(labels)}

    def create_label(self, title, description, color, show_on_sidebar):
        self.calls.append(("create_label", title))
        return {"id": 99, "title": title, "description": description, "color": color,
                "show_on_sidebar": show_on_sidebar}

    def assign_conversation(self, _conversation_id, assignee_id):
        self.calls.append(("assignment", assignee_id))
        self.assignee_id = assignee_id
        return {"id": assignee_id} if assignee_id else None

    def get_conversation(self, _conversation_id):
        assignee = {"id": 7, "name": "Agent Seven", "availability_status": "available"} if self.assignee_id else None
        return {"id": 10, "can_reply": self.can_reply, "meta": {"assignee": assignee, "team": None}}

    def get_messages(self, _conversation_id):
        return {"payload": list(self.messages)}

    def create_input_select_message(self, _conversation_id, content, options):
        self.calls.append(("quick_replies", content, list(options)))
        return {"id": 777, "status": "sent"}


def seed_conversation(session_factory):
    with session_factory() as db:
        db.add(ChatwootConnection(tenant_id=1, account_id=180474, encrypted_api_token=encrypt_secret("test-token"), connection_key="control-test"))
        inbox = InboxBinding(tenant_id=1, chatwoot_inbox_id=128859, name="Facebook", channel_type="Channel::FacebookPage")
        contact = Contact(tenant_id=1, chatwoot_contact_id=99, name="Customer")
        db.add_all([inbox, contact])
        db.flush()
        db.add(ConversationState(
            tenant_id=1,
            inbox_binding_id=inbox.id,
            contact_id=contact.id,
            chatwoot_conversation_id=10,
            labels=["ai"],
            ai_mode="enabled",
            ai_label_present=True,
        ))
        db.add(AppSetting(key="global_message_sending", value={"enabled": True}))
        db.commit()


def test_label_update_merges_changes_from_chatwoot(authenticated, session_factory, monkeypatch):
    client, csrf = authenticated
    seed_conversation(session_factory)
    fake = FakeChatwoot()
    monkeypatch.setattr("app.api.client_for", lambda _connection: fake)

    response = client.put(
        "/v1/conversations/10/labels",
        headers={"X-CSRF-Token": csrf},
        json={"base_labels": ["ai"], "labels": ["人工接管"]},
    )

    assert response.status_code == 200
    assert response.json()["labels"] == ["外部新增", "人工接管"]
    assert response.json()["ai_state"] == "HUMAN_HANDOFF"
    with session_factory() as db:
        row = db.scalar(select(ConversationState).where(ConversationState.chatwoot_conversation_id == 10))
        assert row.labels == ["外部新增", "人工接管"]


def test_assignment_pauses_ai_before_assigning_agent(authenticated, session_factory, monkeypatch):
    client, csrf = authenticated
    seed_conversation(session_factory)
    fake = FakeChatwoot()
    fake.current_labels = ["ai"]
    monkeypatch.setattr("app.api.client_for", lambda _connection: fake)

    response = client.put(
        "/v1/conversations/10/assignment",
        headers={"X-CSRF-Token": csrf},
        json={"assignee_id": 7, "pause_ai": True},
    )

    assert response.status_code == 200
    assert response.json()["assignee"]["id"] == 7
    assert fake.calls == [("labels", ["ai", "人工接管"]), ("assignment", 7)]


def test_ai_mode_toggle_updates_chatwoot_label_and_local_state(authenticated, session_factory, monkeypatch):
    client, csrf = authenticated
    seed_conversation(session_factory)
    fake = FakeChatwoot()
    monkeypatch.setattr("app.api.client_for", lambda _connection: fake)

    response = client.put(
        "/v1/conversations/10/ai-mode",
        headers={"X-CSRF-Token": csrf},
        json={"enabled": False},
    )

    assert response.status_code == 200
    assert response.json()["ai_mode"] == "disabled"
    assert response.json()["ai_sync_status"] == "synced"
    assert response.json()["ai_state"] == "AI_PAUSED_CONVERSATION"
    assert fake.calls == [("labels", ["外部新增"])]
    with session_factory() as db:
        row = db.scalar(select(ConversationState).where(ConversationState.chatwoot_conversation_id == 10))
        assert row.ai_mode == "disabled"
        assert row.ai_label_present is False


def test_ai_mode_toggle_readds_missing_control_label(authenticated, session_factory, monkeypatch):
    client, csrf = authenticated
    seed_conversation(session_factory)
    fake = FakeChatwoot()
    fake.current_labels = []
    monkeypatch.setattr("app.api.client_for", lambda _connection: fake)

    response = client.put(
        "/v1/conversations/10/ai-mode",
        headers={"X-CSRF-Token": csrf},
        json={"enabled": True},
    )

    assert response.status_code == 200
    assert response.json()["labels"] == ["ai"]
    assert response.json()["ai_state"] == "AI_ACTIVE"
    assert fake.calls == [("labels", ["ai"])]


def test_ai_mode_always_uses_exact_ai_label(authenticated, session_factory, monkeypatch):
    client, csrf = authenticated
    seed_conversation(session_factory)
    fake = FakeChatwoot()
    monkeypatch.setattr("app.api.client_for", lambda _connection: fake)

    stopped = client.put(
        "/v1/conversations/10/ai-mode",
        headers={"X-CSRF-Token": csrf},
        json={"enabled": False},
    )
    assert stopped.status_code == 200
    assert stopped.json()["labels"] == ["外部新增"]
    assert stopped.json()["ai_state"] == "AI_PAUSED_CONVERSATION"

    resumed = client.put(
        "/v1/conversations/10/ai-mode",
        headers={"X-CSRF-Token": csrf},
        json={"enabled": True},
    )
    assert resumed.status_code == 200
    assert resumed.json()["labels"] == ["外部新增", "ai"]
    assert resumed.json()["ai_state"] == "AI_ACTIVE"


def test_completed_handoff_restores_explicit_disabled_mode(authenticated, session_factory, monkeypatch):
    from app.models import HandoffTask, utcnow
    client, csrf = authenticated
    seed_conversation(session_factory)
    fake = FakeChatwoot()
    monkeypatch.setattr("app.ops_api.client_for", lambda _: fake)
    with session_factory() as db:
        row = db.scalar(select(ConversationState))
        row.ai_mode = "disabled"
        task = HandoffTask(conversation_state_id=row.id, status="completed", reason_code="test", sla_due_at=utcnow())
        db.add(task); db.commit()
        task_id, version = task.id, task.version
    response = client.post(f"/v1/handoffs/{task_id}/restore-ai", headers={"X-CSRF-Token": csrf}, json={"version": version})
    assert response.status_code == 200
    assert response.json()["ai_state"] == "AI_ACTIVE"


def test_active_handoff_cannot_restore_ai(authenticated, session_factory, monkeypatch):
    from app.models import HandoffTask, utcnow
    client, csrf = authenticated
    seed_conversation(session_factory)
    fake = FakeChatwoot()
    monkeypatch.setattr("app.ops_api.client_for", lambda _: fake)
    with session_factory() as db:
        row = db.scalar(select(ConversationState))
        task = HandoffTask(conversation_state_id=row.id, status="pending", reason_code="test", sla_due_at=utcnow())
        db.add(task); db.commit()
        task_id, version = task.id, task.version
    response = client.post(f"/v1/handoffs/{task_id}/restore-ai", headers={"X-CSRF-Token": csrf}, json={"version": version})
    assert response.status_code == 409
    assert fake.calls == []


def test_unknown_delivery_blocks_restore_before_remote_write(authenticated, session_factory, monkeypatch):
    from app.models import HandoffTask, OutboundMessage, utcnow
    client, csrf = authenticated
    seed_conversation(session_factory)
    fake = FakeChatwoot()
    monkeypatch.setattr("app.ops_api.client_for", lambda _: fake)
    with session_factory() as db:
        row = db.scalar(select(ConversationState))
        task = HandoffTask(conversation_state_id=row.id, status="completed", reason_code="test", sla_due_at=utcnow())
        db.add(task)
        db.add(OutboundMessage(conversation_state_id=row.id, idempotency_key="unknown-test", source_type="ai", content="test", status="submission_unknown"))
        db.commit()
        task_id, version = task.id, task.version
    response = client.post(f"/v1/handoffs/{task_id}/restore-ai", headers={"X-CSRF-Token": csrf}, json={"version": version})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "delivery_reconciliation_required"
    assert fake.calls == []


def test_facebook_quick_reply_is_sent_once_and_persisted(authenticated, session_factory, monkeypatch):
    client, csrf = authenticated
    seed_conversation(session_factory)
    fake = FakeChatwoot()
    monkeypatch.setattr("app.api.client_for", lambda _connection: fake)
    payload = {
        "content": "请问您想咨询哪条线路？",
        "options": ["桃花9日", "桃花+珠峰11日"],
        "request_key": "route-selector-test-1",
    }

    first = client.post("/v1/conversations/10/quick-replies", headers={"X-CSRF-Token": csrf}, json=payload)
    second = client.post("/v1/conversations/10/quick-replies", headers={"X-CSRF-Token": csrf}, json=payload)

    assert first.status_code == 200
    assert first.json()["content_type"] == "input_select"
    assert second.status_code == 200 and second.json()["duplicate"] is True
    assert fake.calls == [("quick_replies", payload["content"], payload["options"])]
    with session_factory() as db:
        outbound = db.scalar(select(OutboundMessage))
        message = db.scalar(select(MessageEvent))
        assert outbound.content_type == "input_select"
        assert outbound.content_attributes["items"][1]["title"] == "桃花+珠峰11日"
        assert message.content_attributes == outbound.content_attributes


def test_route_quick_reply_options_are_bounded_for_messenger(authenticated):
    client, _ = authenticated
    response = client.get("/v1/route-quick-replies")
    assert response.status_code == 200
    data = response.json()
    assert data["options"] == ["桃花9日", "桃花+珠峰11日"]
    assert all(len(title) <= 20 for title in data["options"])
