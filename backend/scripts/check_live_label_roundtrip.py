"""Verify Chatwoot label -> public relay -> SQLite with the sender locked out."""
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select, func
from app.api import client_for, string_payload
from app.config import settings
from app.conversation_policy import has_ai_label
from app.db import SessionLocal
from app.live_reply import mirror_event
from app.live_reply_worker import acquire_lock
from app.models import ChatwootConnection, ConversationState, OutboundMessage, WebhookEvent
from app.relay import poll_relay_once


def await_state(remote_id, present):
    started = time.monotonic()
    while time.monotonic() - started < 35:
        poll_relay_once()
        with SessionLocal() as db:
            events = db.scalars(select(WebhookEvent).where(WebhookEvent.status == "pending").order_by(WebhookEvent.id)).all()
            for event in events:
                mirror_event(db, event)
            state = db.scalar(select(ConversationState).where(ConversationState.chatwoot_conversation_id == remote_id))
            if state.ai_label_present == present and (state.ai_mode == "enabled") == present:
                return round(time.monotonic() - started, 2)
        time.sleep(0.5)
    raise RuntimeError("label_webhook_roundtrip_timeout")


def run():
    lock = acquire_lock()
    remote_id = 26
    with SessionLocal() as db:
        connection = db.scalar(select(ChatwootConnection).where(ChatwootConnection.account_id == settings.live_reply_account_id))
        before = db.scalar(select(func.count()).select_from(OutboundMessage))
    client = client_for(connection)
    enabled_time = disabled_time = None
    try:
        base = string_payload(client.get_conversation_labels(remote_id))
        if has_ai_label(base):
            raise RuntimeError("test_conversation_must_start_disabled")
        client.set_conversation_labels(remote_id, [*base, "ai"])
        enabled_time = await_state(remote_id, True)
    finally:
        # Preserve unrelated labels and leave the test account OFF even on failure.
        current = string_payload(client.get_conversation_labels(remote_id))
        client.set_conversation_labels(remote_id, [label for label in current if not has_ai_label([label])])
        disabled_time = await_state(remote_id, False)
        client.close()
        lock.close()
    with SessionLocal() as db:
        after = db.scalar(select(func.count()).select_from(OutboundMessage))
    assert before == after
    print(json.dumps({"enabled_seconds": enabled_time, "disabled_seconds": disabled_time,
                      "real_customer_message_sends": 0, "outbound_before": before, "outbound_after": after,
                      "final_ai_enabled": False}))


if __name__ == "__main__":
    run()
