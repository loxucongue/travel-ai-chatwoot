from sqlalchemy import select

import app.worker_main as worker
from app.models import AppSetting, AiRun, ChatwootConnection, Contact, InboxBinding, OutboundMessage, WebhookEvent
from app.security import encrypt_secret


class FakeChatwoot:
    def get_conversation(self, _conversation_id):
        return {"id": 10, "labels": ["ai"], "can_reply": True}

    def create_text_message(self, _conversation_id, content):
        assert content == "测试人员回复消息"
        return {"id": 999}

    def close(self):
        pass


def seed_event(session_factory, *, message_type="incoming", labels=None, private=False):
    with session_factory() as db:
        db.add(ChatwootConnection(id=1, tenant_id=1, base_url="https://app.chatwoot.com", account_id=180474, encrypted_api_token=encrypt_secret("test-token"), token_last4="oken", connection_key="test"))
        db.add(InboxBinding(id=1, tenant_id=1, chatwoot_inbox_id=128859, name="Facebook", channel_type="Channel::FacebookPage", ai_enabled=True))
        db.add(AppSetting(key="global_message_sending", value={"enabled": True}))
        db.add(AppSetting(
            key="ai_reception_rollout",
            value={"allowlist_enabled": True, "conversation_ids": [10]},
        ))
        event = WebhookEvent(connection_id=1, event="message_created", account_id=180474, inbox_id=128859, resource_id="123", idempotency_key=f"test-{message_type}-{private}", payload={"event": "message_created", "id": 123, "account": {"id": 180474}, "inbox": {"id": 128859}, "sender": {"id": 99, "name": "Support Agent", "type": "user"} if message_type == "outgoing" else {"id": 88, "name": "Customer"}, "message_type": message_type, "private": private, "content_type": "text", "content": "测试人员触发消息", "conversation": {"id": 10, "inbox_id": 128859, "can_reply": True, "labels": labels or [], "contact_inbox": {"contact_id": 88}, "meta": {"sender": {"id": 88, "name": "Customer", "type": "contact"}}}})
        db.add(event)
        db.commit()
        return event.id


def test_incoming_creates_one_mock_reply(session_factory, monkeypatch):
    event_id = seed_event(session_factory, labels=["ai"])
    monkeypatch.setattr(worker, "SessionLocal", session_factory)
    monkeypatch.setattr(worker, "client_for", lambda _connection: FakeChatwoot())
    monkeypatch.setattr(worker.settings, "outbound_mode", "live")
    monkeypatch.setattr(worker.settings, "chatwoot_write_enabled", True)
    worker.process_event(event_id)
    worker.process_event(event_id)
    with session_factory() as db:
        runs = db.scalars(select(AiRun)).all()
        outbound = db.scalars(select(OutboundMessage)).all()
        assert len(runs) == len(outbound) == 1
        assert outbound[0].status == "submitted"
        assert outbound[0].chatwoot_message_id == 999


def test_incoming_does_not_create_ai_or_outbound_by_default(session_factory, monkeypatch):
    event_id = seed_event(session_factory)
    monkeypatch.setattr(worker, "SessionLocal", session_factory)
    monkeypatch.setattr(worker.settings, "outbound_mode", "disabled")
    monkeypatch.setattr(worker.settings, "chatwoot_write_enabled", False)
    worker.process_event(event_id)
    with session_factory() as db:
        assert db.scalar(select(AiRun)) is None
        assert db.scalar(select(OutboundMessage)) is None


def test_outgoing_and_blocking_label_do_not_reply(session_factory, monkeypatch):
    outgoing = seed_event(session_factory, message_type="outgoing")
    monkeypatch.setattr(worker, "SessionLocal", session_factory)
    worker.process_event(outgoing)
    with session_factory() as db:
        assert db.scalar(select(AiRun)) is None
        assert db.scalar(select(Contact)).name == "Customer"


def test_state_priority():
    tenant = type("T", (), {"ai_enabled": True})()
    inbox = type("I", (), {"ai_enabled": True})()
    assert worker.compute_state(tenant, inbox, ["人工接管"], [], True)[0] == "HUMAN_HANDOFF"
    assert worker.compute_state(tenant, inbox, [], ["拒绝联系"], True)[0] == "AI_PAUSED_LABEL"
    assert worker.compute_state(tenant, inbox, ["ai"], [], False, "enabled", "synced", True)[0] == "CHANNEL_BLOCKED"
    assert worker.compute_state(tenant, inbox, [], [], True, "disabled", "synced", False)[0] == "AI_PAUSED_CONVERSATION"
    assert worker.compute_state(tenant, inbox, [], [], True, "enabled", "conflict", False)[0] == "AI_PAUSED_SYNC"
    assert worker.compute_state(tenant, inbox, ["ai"], [], True, "enabled", "synced", True)[0] == "AI_ACTIVE"
