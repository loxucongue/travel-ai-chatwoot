from sqlalchemy import select

from app.models import ChatwootConnection, ChatwootLabel, Contact, ConversationState, InboxBinding, MessageEvent, WebhookEvent
from app.security import encrypt_secret


def test_login_and_csrf(client):
    assert client.get("/v1/auth/me").status_code == 401
    response = client.post("/v1/auth/login", json={"email": "admin@example.com", "password": "password123"})
    assert response.status_code == 200
    assert "aiops_session" in response.cookies
    assert client.get("/v1/auth/me").json()["email"] == "admin@example.com"
    assert client.put("/v1/settings/ai", json={"trigger_text": "a", "reply_text": "b"}).status_code == 403
    token = response.json()["csrf_token"]
    assert client.put("/v1/settings/ai", headers={"X-CSRF-Token": token}, json={"trigger_text": "a", "reply_text": "b"}).status_code == 200


def test_chatwoot_token_is_not_returned(authenticated):
    client, csrf = authenticated
    response = client.put("/v1/settings/chatwoot", headers={"X-CSRF-Token": csrf}, json={"base_url": "https://app.chatwoot.com", "account_id": 180474, "api_token": "secret-token-value"})
    assert response.status_code == 200
    body = response.text
    assert "secret-token-value" not in body
    assert response.json()["token_last4"] == "alue"


def test_webhook_is_idempotent(client, session_factory):
    with session_factory() as db:
        db.add(ChatwootConnection(id=1, tenant_id=1, base_url="https://app.chatwoot.com", account_id=180474, encrypted_api_token=encrypt_secret("test-token"), token_last4="oken", connection_key="connection-test"))
        db.add(InboxBinding(tenant_id=1, chatwoot_inbox_id=128859, name="Facebook", channel_type="Channel::FacebookPage"))
        db.commit()
    payload = {"event": "message_created", "id": 123, "account": {"id": 180474}, "inbox": {"id": 128859}, "message_type": "outgoing", "private": False, "content": "hello", "conversation": {"id": 10, "inbox_id": 128859, "can_reply": True, "labels": []}}
    first = client.post("/v1/webhooks/chatwoot/connection-test", json=payload)
    second = client.post("/v1/webhooks/chatwoot/connection-test", json=payload)
    assert first.status_code == second.status_code == 202
    assert first.json()["duplicate"] is False
    assert second.json()["duplicate"] is True
    with session_factory() as db:
        assert len(db.scalars(select(WebhookEvent)).all()) == 1


def test_conversation_filters_use_real_fields(authenticated, session_factory):
    client, _ = authenticated
    with session_factory() as db:
        inbox = InboxBinding(tenant_id=1, chatwoot_inbox_id=128859, name="Facebook", channel_type="Channel::FacebookPage")
        contact = Contact(tenant_id=1, chatwoot_contact_id=88, name="Filter Customer")
        db.add_all([inbox, contact, ChatwootLabel(tenant_id=1, chatwoot_label_id=9, title="已留资", color="#10B981")])
        db.flush()
        conversation = ConversationState(
            tenant_id=1,
            inbox_binding_id=inbox.id,
            contact_id=contact.id,
            chatwoot_conversation_id=10,
            labels=["已留资"],
            effective_ai_state="AI_ACTIVE",
            last_message="hello",
        )
        db.add(conversation)
        db.flush()
        db.add(MessageEvent(
            conversation_state_id=conversation.id,
            chatwoot_message_id=99,
            direction="outgoing",
            content_type="text",
            content="",
            attribution="inferred_human",
            attachments=[{"file_type": "image", "data_url": "https://example.com/full.jpg", "thumb_url": "https://example.com/thumb.jpg"}],
        ))
        db.commit()

    options = client.get("/v1/conversations/filters")
    assert options.status_code == 200
    assert options.json()["labels"] == [{"title": "已留资", "color": "#10B981"}]
    response = client.get("/v1/conversations?label=已留资&ai_state=AI_ACTIVE&inbox_id=128859")
    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert response.json()["items"][0]["name"] == "Filter Customer"
    message = client.get("/v1/conversations/10/messages").json()["items"][0]
    assert message["content_type"] == "image"
    assert message["content"] == "[图片]"
