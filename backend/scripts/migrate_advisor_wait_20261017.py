"""One-time upgrade of an already scheduled advisor wait, without resetting its clock."""
from sqlalchemy import select
from app.models import ConversationState, MessageEvent, utcnow
from app.runtime_settings import dt
from app.reception_v3 import service, human_followup as hf
from app.conversation_labels import queue_labels


def migrate_pending_wait(db, row):
    if not hf.enabled(row) or row.controls['human_followup'].get('phase'):
        return None
    permission = row.controls['human_followup']
    due = service.state(row).get('next_check_at')
    latest = db.scalar(select(MessageEvent).where(MessageEvent.conversation_state_id == row.conversation_state_id,
        MessageEvent.private.is_(False), MessageEvent.direction.in_(['incoming', 'outgoing']))
        .order_by(MessageEvent.chatwoot_message_id.desc()).limit(1))
    if (not due or dt(due) <= dt(utcnow()) or not latest or latest.direction != 'outgoing'
            or latest.attribution not in ('human','inferred_human') or latest.status == 'failed'
            or latest.chatwoot_message_id != permission.get('last_human_message_id')
            or any(m.get('status') == 'draft' for m in row.messages)):
        raise ValueError('existing_followup_requires_review')
    conversation = db.get(ConversationState, row.conversation_state_id)
    before = {'session':row.id,'permission':permission,'ai_mode':conversation.ai_mode,
        'state':conversation.effective_ai_state,'next_check_at':due,'labels':conversation.labels}
    row.controls = {**row.controls,'human_followup':{**permission,'phase':'waiting','resume_at':due}}
    conversation.ai_mode = 'disabled'
    conversation.effective_ai_state,conversation.effective_state_reason = 'HUMAN_HANDOFF','human:followup_waiting'
    queue_labels(db,conversation,'human')
    return before
