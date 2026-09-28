from datetime import timedelta


from pathlib import Path


import os


from types import SimpleNamespace


import httpx


import pytest


from sqlalchemy import select, func


import app.live_reply as live


from app.automation_models import LiveSopEnrollment, LiveSopJob


from app.chatwoot import ChatwootClient, ChatwootError


from app.config import settings


from app.config import Settings


from app.conversation_policy import compute_state


from app.live_reply_models import LiveReplyJob


from app.lead_capture_models import LeadCaptureState


from app.models import (AppSetting, ChatwootConnection, Contact, ConversationJourney, ConversationState, HandoffTask,
                        InboxBinding, MessageEvent, Notification, OutboundMessage, SopDefinition, Tenant, WebhookEvent, utcnow)


from app.route_packages import ROUTES, UNCLASSIFIED_SOP_NAME, UNCLASSIFIED_SOP_NODES


from app.outbound_control import global_message_sending_enabled


from app.security import encrypt_secret


from app.material_library import candidate_materials




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
