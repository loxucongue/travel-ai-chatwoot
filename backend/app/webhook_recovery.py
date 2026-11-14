"""Temporary, bounded API intake during a verified Chatwoot webhook outage.

Never changes message timestamps. Uses the normal mirror, ownership and reply
policy, and records observed event IDs so late webhooks cannot replay them.
"""
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import select, func
from app.models import ChatwootConnection, WebhookEvent, MessageEvent, OutboundMessage, utcnow
from app.automation_models import AutomationRun
from app.db import SessionLocal
from app.chatwoot_service import client_for, payload_dict
from app.history_sync import fetch_message_history, message_direction, message_timestamp, unpack_messages
from app.conversation_mirror import upsert_mirrors
from app.reception_v3 import live
from app.runtime_settings import blocking_labels

MARKER = Path('data/webhook-recovery.enabled')
STATE = Path('data/webhook-recovery-state.json')
CUTOFF = datetime(2026, 11, 14, 5, 41, tzinfo=timezone.utc).timestamp()


def public(messages):
    return sorted([m for m in messages if not m.get('private') and
        message_direction(m.get('message_type')) in ('incoming', 'outgoing')], key=lambda m: int(m['id']))


def candidate(remote, messages, known_outgoing, blocked, now):
    history = public(messages)
    if remote.get('status') not in ('open', 'pending') or remote.get('can_reply') is False:
        return 'closed_or_unreplyable'
    if set(remote.get('labels') or []).intersection(blocked):
        return 'blocked_label'
    if not history:
        return 'no_public_message'
    last = history[-1]
    if message_direction(last.get('message_type')) != 'incoming':
        return 'already_answered'
    if any(message_direction(m.get('message_type')) == 'outgoing' and m['id'] not in known_outgoing for m in history):
        return 'human_or_unknown_sender'
    at = datetime.fromisoformat(message_timestamp(last.get('created_at')).replace('Z', '+00:00')).timestamp()
    if at < CUTOFF or not -30 <= now - at < 23 * 3600:
        return 'outside_recovery_window'
    return 'candidate'


def before_send(db, client, conversation, row):
    """Outage guard: labels alone miss an advisor reply whose webhook is lost."""
    if not MARKER.exists():
        return True
    try:
        messages, _ = unpack_messages(client.get_messages(conversation.chatwoot_conversation_id))
        history = public(messages)
        if not history:
            return False
        known = set(db.scalars(select(OutboundMessage.chatwoot_message_id).where(
            OutboundMessage.conversation_state_id == conversation.id)))
        # Unknown outgoing messages include advisors and unconfirmed sends.
        if any(message_direction(m.get('message_type')) == 'outgoing' and m['id'] not in known for m in history):
            live.cancel(db, conversation.id, 'recovery_advisor_or_unknown_reply')
            db.commit()
            return False
        seen = {str(m.get('chatwoot_message_id') or str(m.get('id', '')).removeprefix('cw-')) for m in row.messages}
        if any(message_direction(m.get('message_type')) == 'incoming' and str(m['id']) not in seen for m in history):
            return False
        return True
    except Exception:
        # A failed safety read must never authorize a send.
        return False


def snapshot_event(connection, remote, message=None):
    payload = {**remote, 'account': {'id': connection.account_id}, 'event': 'conversation_updated'}
    if message is not None:
        payload = {**message, 'message_type': message_direction(message.get('message_type')),
            'created_at': message_timestamp(message.get('created_at')), 'conversation': remote,
            'account': {'id': connection.account_id}, 'event': 'message_created'}
    return SimpleNamespace(connection_id=connection.id, payload=payload)


def recover_one(client, cid, apply=False):
    remote = payload_dict(client.get_conversation(cid))
    messages, _ = fetch_message_history(client, cid, max_pages=12)
    with SessionLocal() as db:
        connection = db.scalar(select(ChatwootConnection).where(ChatwootConnection.account_id == 180474))
        known = set(db.scalars(select(OutboundMessage.chatwoot_message_id)))
        verdict = candidate(remote, messages, known, blocking_labels(db), time.time())
        history = public(messages)
        last = history[-1] if history else None
        if verdict != 'candidate':
            return {'conversation': cid, 'result': verdict}
        key = f'180474:message_created:{last["id"]}'
        if db.scalar(select(WebhookEvent.id).where(WebhookEvent.idempotency_key == key)):
            return {'conversation': cid, 'result': 'already_ingested'}
        from app.models import ConversationState, Contact
        existing = db.scalar(select(ConversationState).where(ConversationState.chatwoot_conversation_id == cid,
                                                            ConversationState.tenant_id == connection.tenant_id))
        if existing and (live.human_owned(db, existing) or existing.ai_mode == 'off'):
            return {'conversation': cid, 'result': 'local_human_or_paused'}
        contact_id = (remote.get('meta') or {}).get('sender', {}).get('id')
        if not contact_id:
            return {'conversation': cid, 'result': 'missing_contact'}
        labels_result = client.get_contact_labels(contact_id)
        labels = labels_result.get('payload', []) if isinstance(labels_result, dict) else labels_result
        if not isinstance(labels, list) or set(labels).intersection(blocking_labels(db)):
            return {'conversation': cid, 'result': 'contact_blocked'}
        if not apply:
            return {'conversation': cid, 'result': 'eligible', 'message': last['id']}
        # Fetch again immediately before scheduling. Never replay a stale snapshot.
        remote2 = payload_dict(client.get_conversation(cid))
        recent, _ = unpack_messages(client.get_messages(cid))
        if not public(recent) or public(recent)[-1]['id'] != last['id'] or candidate(remote2, recent, known, blocking_labels(db), time.time()) != 'candidate':
            return {'conversation': cid, 'result': 'changed_during_check'}
        state, _, _, _ = upsert_mirrors(db, snapshot_event(connection, remote2), side_effects=False)
        if not state:
            return {'conversation': cid, 'result': 'unbound_inbox'}
        if state.contact:
            state.contact.labels = labels
        # Activate only genuinely new customers, using the existing policy.
        first = history[0]
        _, first_message, _, _ = upsert_mirrors(db, snapshot_event(connection, remote2, first), side_effects=False)
        live.enable_new_customer(db, state, first_message)
        if not live.permitted(db, state):
            db.rollback()
            return {'conversation': cid, 'result': 'policy_blocked'}
        for m in messages:
            event = snapshot_event(connection, remote2, m)
            _, stored, _, _ = upsert_mirrors(db, event, side_effects=False, update_controls=False)
            event_key = f'180474:message_created:{m["id"]}'
            if not db.scalar(select(WebhookEvent.id).where(WebhookEvent.idempotency_key == event_key)):
                db.add(WebhookEvent(connection_id=connection.id, event='message_created', account_id=180474,
                    inbox_id=remote2['inbox_id'], resource_id=str(m['id']), idempotency_key=event_key,
                    payload=event.payload, status='live_observed', processed_at=utcnow()))
            if m['id'] == last['id']:
                target = stored
        db.flush()
        if not live.permitted(db, state):
            db.rollback()
            return {'conversation': cid, 'result': 'policy_changed'}
        from app.engagement_labels import observe_reply
        observe_reply(db, state, target)
        row = live.session_for(db, state.id)
        if row:
            from app.reception_v3.human_followup import sync_history
            sync_history(db, row)
            row.messages = [m for m in row.messages if str(m.get('chatwoot_message_id') or str(m.get('id', '')).removeprefix('cw-')) != str(target.chatwoot_message_id)]
        live.accept(db, state, target)
        db.commit()
        return {'conversation': cid, 'result': 'scheduled', 'message': last['id']}


def cycle(apply=False):
    if apply and not MARKER.exists():
        return []
    # Leave headroom on the 2-vCPU host. Do not pile up model work.
    if os.getloadavg()[0] > 4:
        return [{'result': 'resource_backoff'}]
    with SessionLocal() as db:
        if db.scalar(select(func.count()).select_from(AutomationRun).where(AutomationRun.status == 'processing')) >= 2:
            return [{'result': 'model_backoff'}]
        cl = client_for(db.scalar(select(ChatwootConnection).where(ChatwootConnection.account_id == 180474)))
    cache = json.loads(STATE.read_text()) if STATE.exists() and apply else {}
    results, scheduled, examined = [], 0, 0
    try:
        for page in range(1, 9):
            value = cl.list_conversations(page=page)
            items = value.get('data', value).get('payload', [])
            if not items:
                break
            for item in items:
                if float(item.get('last_activity_at') or 0) < CUTOFF:
                    continue
                cid = item['id']; revision = str(item.get('last_activity_at'))
                if cache.get(str(cid)) == revision:
                    continue
                try:
                    result = recover_one(cl, cid, apply)
                except Exception as exc:
                    result = {'conversation': cid, 'result': 'read_or_recovery_error', 'error': type(exc).__name__}
                results.append(result); examined += 1
                if result['result'] not in ('changed_during_check', 'read_or_recovery_error'):
                    cache[str(cid)] = revision
                if result['result'] == 'scheduled':
                    scheduled += 1
                time.sleep(1)
                if scheduled >= 2 or examined >= 8:
                    break
            if scheduled >= 2 or examined >= 8 or all(float(i.get('last_activity_at') or 0) < CUTOFF for i in items):
                break
        if apply:
            temp = STATE.with_suffix('.tmp');temp.write_text(json.dumps(cache));temp.replace(STATE)
    finally:
        cl.close()
    return results


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser();parser.add_argument('--apply', action='store_true');args = parser.parse_args()
    import fcntl
    with open('data/webhook-recovery.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        print(json.dumps({'at': utcnow(), 'results': cycle(args.apply)}, ensure_ascii=False), flush=True)
