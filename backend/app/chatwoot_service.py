"""Shared Chatwoot construction and response normalization, independent of HTTP routes."""
from app.chatwoot import ChatwootClient
from app.config import settings
from app.models import ChatwootConnection
from app.security import decrypt_secret


def client_for(connection: ChatwootConnection) -> ChatwootClient:
    return ChatwootClient(connection.base_url, connection.account_id,
                          decrypt_secret(connection.encrypted_api_token),
                          settings.chatwoot_request_timeout_seconds)


def payload_dict(value) -> dict:
    if isinstance(value, dict) and isinstance(value.get("payload"), dict):
        return value["payload"]
    return value if isinstance(value, dict) else {}


def string_payload(value) -> list[str]:
    if isinstance(value, dict):
        value = value.get("payload", [])
    return [str(item) for item in value] if isinstance(value, list) else []
