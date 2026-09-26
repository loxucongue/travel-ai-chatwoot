from sqlalchemy import select

import app.relay as relay
from app.models import ChatwootConnection, WebhookEvent
from app.security import encrypt_secret


class FakeRelayClient:
    acked: list[int] = []
    retried: list[int] = []
    closed = False

    def close(self):
        self.closed = True

    def lease(self):
        return {
            "lease_token": "lease-test",
            "events": [
                {
                    "id": 41,
                    "payload": {
                        "event": "message_created",
                        "id": 123,
                        "account": {"id": 180474},
                        "inbox": {"id": 128859},
                        "message_type": "incoming",
                        "content": "测试人员触发消息",
                        "conversation": {"id": 10, "inbox_id": 128859},
                    },
                }
            ],
        }

    def ack(self, _lease_token, ids):
        self.__class__.acked.extend(ids)

    def retry(self, _lease_token, ids, _error):
        self.__class__.retried.extend(ids)


def test_relay_event_is_persisted_before_ack(session_factory, monkeypatch):
    with session_factory() as db:
        db.add(ChatwootConnection(tenant_id=1, account_id=180474, encrypted_api_token=encrypt_secret("test"), token_last4="test", connection_key="key"))
        db.commit()
    FakeRelayClient.acked = []
    FakeRelayClient.retried = []
    monkeypatch.setattr(relay, "SessionLocal", session_factory)
    monkeypatch.setattr(relay, "RelayClient", FakeRelayClient)
    monkeypatch.setattr(relay.settings, "relay_base_url", "https://relay.example.com")
    monkeypatch.setattr(relay.settings, "relay_api_token", "relay-token")

    assert relay.poll_relay_once() == 1
    assert relay.poll_relay_once() == 1
    assert FakeRelayClient.acked == [41, 41]
    assert FakeRelayClient.retried == []
    with session_factory() as db:
        rows = db.scalars(select(WebhookEvent)).all()
        assert len(rows) == 1
        assert rows[0].resource_id == "123"


def test_shared_relay_connection_survives_empty_polls(monkeypatch):
    monkeypatch.setattr(relay.settings, "relay_base_url", "https://relay.example.com")
    monkeypatch.setattr(relay.settings, "relay_api_token", "relay-token")
    client = FakeRelayClient()
    client.lease = lambda: {"events": []}
    for _ in range(3):
        assert relay.poll_relay_once(client) == 0
        assert not client.closed
    client.close()
    assert client.closed
