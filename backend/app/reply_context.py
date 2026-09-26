"""Complete, past-only public context shared by live replies and replay."""
import hashlib
import json
from datetime import datetime, timezone

from app.history_sync import fetch_message_history, message_direction, message_timestamp, IncompleteHistoryError

CONTEXT_VERSION = "full-public-customer-history-v1"


def message_key(message):
    if message.get("created_at") is None or not message.get("id"):
        raise IncompleteHistoryError("history_message_identity_or_timestamp_unknown")
    at = datetime.fromisoformat(message_timestamp(message.get("created_at")).replace("Z", "+00:00"))
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return at, int(message.get("id") or 0)


def public_context(messages, target):
    cutoff = message_key(target)
    result = []
    for item in sorted(messages, key=message_key):
        direction = message_direction(item.get("message_type", item.get("direction")))
        if (message_key(item) >= cutoff or item.get("private") or direction not in ("incoming", "outgoing")
                or (item.get("content_attributes") or {}).get("deleted")):
            continue
        text = str(item.get("content") or "")
        if not text and item.get("attachments"):
            text = "[attachment: " + ", ".join(str(a.get("file_type") or "file") for a in item["attachments"]) + "]"
        result.append({"id": item["id"], "conversation_id": item.get("conversation_id"),
                       "direction": direction, "content": text, "created_at": message_timestamp(item.get("created_at"))})
    return result


def context_trace(history):
    encoded = json.dumps(history, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return {"context_version": CONTEXT_VERSION, "context_complete": True, "context_messages": len(history),
            "context_characters": sum(len(m["content"]) for m in history),
            "context_hash": hashlib.sha256(encoded).hexdigest(),
            "context_first_id": history[0]["id"] if history else None,
            "context_last_id": history[-1]["id"] if history else None}


def fetch_customer_context(client, remote, input_ids):
    contact_id = ((remote.get("meta") or {}).get("sender") or {}).get("id") or (remote.get("contact_inbox") or {}).get("contact_id")
    response = client.list_contact_conversations(int(contact_id))
    conversations = response.get("payload") if isinstance(response, dict) else response
    if not isinstance(conversations, list):
        raise IncompleteHistoryError("contact_conversation_list_unknown")
    allowed = {int(item["id"]) for item in conversations if item.get("inbox_id") == remote["inbox_id"]}
    allowed.add(remote["id"])
    messages = []
    for conversation_id in sorted(allowed):
        batch, _ = fetch_message_history(client, conversation_id)
        messages.extend({**item, "conversation_id": conversation_id} for item in batch)
    inputs = [m for m in messages if m["conversation_id"] == remote["id"] and m["id"] in input_ids]
    if len(inputs) != len(set(input_ids)):
        raise IncompleteHistoryError("trigger_not_in_complete_history")
    history = public_context(messages, min(inputs, key=message_key))
    return history, {**context_trace(history), "history_conversations": len(allowed), "history_fetched": len(messages)}


def fetch_complete_customer_history(client, remote):
    """Return every public message available before a silence decision."""
    contact_id = ((remote.get("meta") or {}).get("sender") or {}).get("id") or (remote.get("contact_inbox") or {}).get("contact_id")
    response = client.list_contact_conversations(int(contact_id))
    conversations = response.get("payload") if isinstance(response, dict) else response
    if not isinstance(conversations, list):
        raise IncompleteHistoryError("contact_conversation_list_unknown")
    allowed = {int(item["id"]) for item in conversations if item.get("inbox_id") == remote["inbox_id"]}
    allowed.add(remote["id"])
    collected = []
    for conversation_id in sorted(allowed):
        batch, _ = fetch_message_history(client, conversation_id)
        collected.extend({**item, "conversation_id": conversation_id} for item in batch)
    history = []
    for item in sorted(collected, key=message_key):
        direction = message_direction(item.get("message_type", item.get("direction")))
        if item.get("private") or direction not in ("incoming", "outgoing") or (item.get("content_attributes") or {}).get("deleted"):
            continue
        text = str(item.get("content") or "")
        if not text and item.get("attachments"):
            text = "[attachment: " + ", ".join(str(a.get("file_type") or "file") for a in item["attachments"]) + "]"
        history.append({
            "id": item["id"],
            "conversation_id": item.get("conversation_id"),
            "direction": direction,
            "content": text,
            "created_at": message_timestamp(item.get("created_at")),
        })
    return history, {**context_trace(history), "history_conversations": len(allowed), "history_fetched": len(collected)}
