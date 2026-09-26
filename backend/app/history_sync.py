from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select

from app.chatwoot import ChatwootClient, ReadOnlyChatwootClient
from app.config import settings
from app.db import SessionLocal
from app.models import ChatwootConnection, Contact, ConversationState, MessageEvent, OutboundMessage
from app.security import decrypt_secret

PAGE_SIZE = 20
MAX_HISTORY_PAGES = 250


class IncompleteHistoryError(RuntimeError):
    pass


@dataclass
class HistorySyncResult:
    fetched: int
    inserted: int
    updated: int


def unpack_messages(response: object) -> tuple[list[dict], dict]:
    if not isinstance(response, dict):
        return (response if isinstance(response, list) else []), {}
    payload = response.get("payload", response)
    if isinstance(payload, list):
        return payload, response.get("meta") or {}
    if isinstance(payload, dict):
        return payload.get("messages") or [], response.get("meta") or payload.get("meta") or {}
    return [], {}


def fetch_message_history(client: ChatwootClient, conversation_id: int, max_pages: int = MAX_HISTORY_PAGES) -> tuple[list[dict], dict]:
    before: int | None = None
    collected: dict[int, dict] = {}
    metadata: dict = {}
    for _ in range(max_pages):
        response = client.get_messages(conversation_id, before=before)
        raw = response.get("payload", response) if isinstance(response, dict) else response
        if isinstance(raw, dict):
            raw = raw.get("messages")
        if not isinstance(raw, list):
            raise IncompleteHistoryError("history_response_shape_unknown")
        batch, page_metadata = unpack_messages(response)
        if not metadata and page_metadata:
            metadata = page_metadata
        ids = [int(item["id"]) for item in batch if isinstance(item, dict) and item.get("id")]
        if len(ids) != len(batch):
            raise IncompleteHistoryError("history_message_identity_unknown")
        for item in batch:
            if isinstance(item, dict) and item.get("id"):
                collected[int(item["id"])] = item
        if not ids:
            return sorted(collected.values(), key=lambda item: int(item["id"])), metadata
        oldest = min(ids)
        if before is not None and oldest >= before:
            raise IncompleteHistoryError("history_cursor_not_advancing")
        before = oldest
    raise IncompleteHistoryError("history_page_limit_reached")


def message_direction(value: object) -> str:
    if value in (0, "0", "incoming"):
        return "incoming"
    if value in (1, "1", "outgoing"):
        return "outgoing"
    if value in (2, "2", "activity"):
        return "activity"
    return str(value or "unknown")


def attachment_placeholder(attachments: list[dict]) -> tuple[str, str]:
    if not attachments:
        return "", "text"
    first = attachments[0]
    value = str(first.get("file_type") or first.get("extension") or "file").lower()
    if "image" in value or value in {"jpg", "jpeg", "png", "gif", "webp"}:
        return "[图片]", "image"
    if "audio" in value or value in {"mp3", "wav", "ogg", "m4a"}:
        return "[音频]", "audio"
    if "video" in value or value in {"mp4", "mov", "webm"}:
        return "[视频]", "video"
    return "[文件]", "file"


def message_timestamp(value: object) -> str:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc).isoformat()
    return str(value or datetime.now(timezone.utc).isoformat())


def sync_conversation_history(conversation_state_id: int) -> HistorySyncResult:
    with SessionLocal() as db:
        state = db.get(ConversationState, conversation_state_id)
        if not state:
            return HistorySyncResult(0, 0, 0)
        connection = db.scalar(select(ChatwootConnection).where(ChatwootConnection.tenant_id == state.tenant_id))
        if not connection:
            return HistorySyncResult(0, 0, 0)
        conversation_id = state.chatwoot_conversation_id
        client_config = (connection.base_url, connection.account_id, decrypt_secret(connection.encrypted_api_token))

    client = ReadOnlyChatwootClient(*client_config, timeout=settings.chatwoot_request_timeout_seconds)
    try:
        messages, metadata = fetch_message_history(client, conversation_id)
    finally:
        client.close()

    inserted = 0
    updated = 0
    with SessionLocal() as db:
        state = db.get(ConversationState, conversation_state_id)
        if not state:
            return HistorySyncResult(len(messages), 0, 0)
        contact_data = metadata.get("contact") if isinstance(metadata, dict) else None
        if state.contact_id and isinstance(contact_data, dict) and contact_data.get("name"):
            contact = db.get(Contact, state.contact_id)
            if contact:
                contact.name = str(contact_data["name"])
                contact.email = contact_data.get("email") or contact.email
                contact.phone_number = contact_data.get("phone_number") or contact.phone_number
                contact.custom_attributes = contact_data.get("custom_attributes") or contact.custom_attributes
        latest_public: tuple[str, str] | None = None
        for item in messages:
            attributes = item.get("content_attributes") or {}
            if attributes.get("deleted"):
                continue
            message_id = int(item["id"])
            row = db.scalar(select(MessageEvent).where(
                MessageEvent.conversation_state_id == state.id,
                MessageEvent.chatwoot_message_id == message_id,
            ))
            if row:
                updated += 1
            else:
                row = MessageEvent(
                    conversation_state_id=state.id,
                    chatwoot_message_id=message_id,
                    direction=message_direction(item.get("message_type")),
                )
                db.add(row)
                inserted += 1
            attachments = item.get("attachments") if isinstance(item.get("attachments"), list) else []
            placeholder, attachment_type = attachment_placeholder(attachments)
            row.direction = message_direction(item.get("message_type"))
            row.private = bool(item.get("private", False))
            row.content_type = attachment_type if attachments and not item.get("content") else str(item.get("content_type") or attachment_type)
            row.content = str(item.get("content") or placeholder)
            row.status = item.get("status") or row.status
            row.sender_id = int(item["sender_id"]) if item.get("sender_id") else None
            row.attachments = attachments
            row.content_attributes = attributes
            if row.direction == "incoming":
                row.attribution = "customer"
            elif row.direction == "outgoing":
                linked = db.scalar(select(OutboundMessage).where(OutboundMessage.chatwoot_message_id == message_id))
                row.attribution = linked.source_type if linked else "inferred_human"
            else:
                row.attribution = "system"
            row.created_at = message_timestamp(item.get("created_at"))
            if row.direction in ("incoming", "outgoing") and not row.private and row.content:
                candidate = (row.created_at, row.content)
                if latest_public is None or candidate[0] > latest_public[0]:
                    latest_public = candidate
        if latest_public:
            state.last_message = latest_public[1]
        db.commit()
    return HistorySyncResult(len(messages), inserted, updated)
