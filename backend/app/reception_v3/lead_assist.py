"""Notify advisors about a captured lead without taking reply permission away."""
import hashlib
import json


def contact_key(profile):
    channel = str(profile.get('contact_channel') or '').strip().casefold()
    value = str(profile.get('contact_value') or '').strip()
    if value:
        identity = [channel, value.casefold() if channel == 'email' else value]
    elif profile.get('contact_status') in ('attachment_pending_verification', 'self_reported_added'):
        evidence = profile.get('contact_message_id') or profile.get('contact_evidence')
        if not evidence:
            return None
        identity = [channel, profile['contact_status'], str(evidence)]
    else:
        return None
    return hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()


def remember_contact(state, profile):
    key = contact_key(profile)
    if key:
        state['notified_contact_keys'] = list(dict.fromkeys([*state.get('notified_contact_keys', []), key]))


def notify(db, row, state, decision, bundle):
    if bundle['lead_capture'].get('after_contact') != 'assist_until_advisor':
        raise ValueError('lead_assist_not_enabled')
    key = contact_key(row.memory)
    if not key:
        raise ValueError('contact_notice_requires_contact_evidence')
    if key in state.get('notified_contact_keys', []):
        return
    reason = ('contact' if row.memory.get('contact_value') else 'contact_image'
              if row.memory.get('contact_status') == 'attachment_pending_verification' else 'contact_reported')
    if row.environment == 'live':
        from app.models import ConversationState
        from app.operations import ensure_handoff
        conversation = db.get(ConversationState, row.conversation_state_id)
        task = ensure_handoff(db, conversation, reason, decision.get('handoff_reason', ''), allow_ai_assist=True)
        state['lead_notice_task_id'] = task.id
    remember_contact(state, row.memory)
    state.update(lead_assist=True, next_check_at=None, next_check_minutes=None)
