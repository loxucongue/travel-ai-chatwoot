"""Explicit opt-in reset. GET by default; --apply removes only existing ai labels."""
import argparse
import json
from pathlib import Path
import sqlite3
import sys
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select, func
from app.api import client_for, payload_dict, string_payload
from app.config import settings
from app.conversation_policy import has_ai_label
from app.db import SessionLocal
from app.material_library import catalog_assets
from app.models import (AppSetting, ChatwootConnection, ConversationState, InboxBinding,
                        OutboundMessage, SopDefinition, Tenant, utcnow)
from app.relay import RelayClient
from app.worker_main import conversation_page


def inspect():
    with SessionLocal() as db:
        conn = db.scalar(select(ChatwootConnection).where(ChatwootConnection.account_id == settings.live_reply_account_id))
        if not conn:
            raise RuntimeError("connection_missing")
        client = client_for(conn)
        relay = RelayClient()
        try:
            h = client.list_webhooks()
            hooks = h.get("payload", {}).get("webhooks", [])
            expected = settings.relay_base_url.rstrip("/") + "/v1/webhooks/chatwoot/" + conn.connection_key
            matched = [h for h in hooks if h.get("url") == expected]
            if len(matched) != 1 or not {"message_created", "message_updated", "conversation_updated"} <= set(matched[0].get("subscriptions") or []):
                raise RuntimeError("relay_webhook_configuration_mismatch")
            response = relay.client.get("/v1/relay/stats")
            response.raise_for_status()
            found = {}
            # Two complete passes avoid losing a conversation shifted by new activity.
            for _ in range(2):
                for page in range(1, 501):
                    items, _ = conversation_page(client.list_conversations(page))
                    if not items:
                        break
                    found.update({int(item["id"]): item for item in items})
                else:
                    raise RuntimeError("conversation_pagination_incomplete")
            print(json.dumps({"remote_conversations": len(found), "existing_ai_conversation_ids": [i for i, r in found.items() if has_ai_label(r.get("labels") or [])],
                "relay_host": urlsplit(settings.relay_base_url).netloc, "relay_status": response.json(),
                "webhook_id": matched[0]["id"], "webhook_events": matched[0].get("subscriptions"),
                "outbound_count": db.scalar(select(func.count()).select_from(OutboundMessage))}, ensure_ascii=True))
            return conn.id, found
        finally:
            client.close()
            relay.close()


def apply(conn_id, rows):
    if settings.outbound_enabled:
        raise RuntimeError("stop_senders_and_disable_outbound_before_reset")
    database = Path(settings.database_url.removeprefix("sqlite:///"))
    backup = database.parent / "backups" / ("before-live-reply-" + utcnow().replace(":", "").replace("+", "_") + ".db")
    backup.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database) as source, sqlite3.connect(backup) as target:
        source.backup(target)
    with SessionLocal() as db:
        policy = db.get(AppSetting, "live_reply") or AppSetting(key="live_reply", value={})
        policy.value = {"armed_at": None}
        db.add(policy)
        for state in db.scalars(select(ConversationState)).all():
            state.ai_mode, state.ai_mode_source, state.ai_mode_updated_at = "disabled", "default_human_reset", utcnow()
            state.effective_ai_state, state.effective_state_reason = "AI_PAUSED_CONVERSATION", "ai_opt_in_required"
            state.version += 1
        # Explicitly keep every proactive strategy non-live.
        for sop in db.scalars(select(SopDefinition)).all():
            sop.live_enabled, sop.dry_run = False, True
        db.commit()
        conn = db.get(ChatwootConnection, conn_id)
        client = client_for(conn)
        removed = []
        old = settings.app_profile, settings.outbound_mode, settings.chatwoot_write_enabled
        try:
            # Maintenance is narrowly restricted by ChatwootClient to message/control endpoints.
            # This script calls only labels POST and never constructs a message.
            settings.app_profile, settings.outbound_mode, settings.chatwoot_write_enabled = "live_reply", "live", True
            for remote_id, item in rows.items():
                if has_ai_label(item.get("labels") or []):
                    current = string_payload(client.get_conversation_labels(remote_id))
                    keep = [x for x in current if not has_ai_label([x])]
                    if keep != current:
                        client.set_conversation_labels(remote_id, keep)
                    verified = string_payload(client.get_conversation_labels(remote_id))
                    if has_ai_label(verified) or not set(keep) <= set(verified):
                        raise RuntimeError("label_reset_conflict")
                    item["labels"] = verified
                    removed.append(remote_id)
        finally:
            settings.app_profile, settings.outbound_mode, settings.chatwoot_write_enabled = old
            client.close()
        for remote_id, item in rows.items():
            state = db.scalar(select(ConversationState).where(ConversationState.tenant_id == conn.tenant_id,
                                                             ConversationState.chatwoot_conversation_id == remote_id))
            if state:
                state.labels, state.ai_label_present, state.ai_sync_status = item.get("labels") or [], False, "synced"
        for inbox in db.scalars(select(InboxBinding)).all():
            inbox.ai_enabled = inbox.tenant_id == conn.tenant_id and inbox.chatwoot_inbox_id == settings.live_reply_inbox_id
        db.get(Tenant, conn.tenant_id).ai_enabled = True
        enabled_materials = []
        for asset in catalog_assets(db, conn.tenant_id):
            enabled = asset.available and asset.metadata_json.get("review_state") == "evaluation_ready"
            asset.metadata_json = {**asset.metadata_json, "live_approved": enabled,
                                   "live_scope": "user_requested_opt_in_facebook_pilot" if enabled else None}
            if enabled:
                enabled_materials.append(asset.asset_key)
        policy.value = {"armed_at": utcnow(), "default": "human", "trigger_label": "ai", "proactive_enabled": False,
                        "reset_remote_ids": removed, "allowed_material_count": len(enabled_materials)}
        db.commit()
        print(json.dumps({"reset_completed": True, "ai_labels_removed_from": removed,
                          "live_materials": len(enabled_materials), "armed_at": policy.value["armed_at"]}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    connection, rows = inspect()
    if args.apply:
        apply(connection, rows)
