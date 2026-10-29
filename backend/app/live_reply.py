from datetime import timedelta
from sqlalchemy import select
from app.config import settings
from app.models import ChatwootConnection, ConversationState, MessageEvent, OutboundMessage, Tenant, HandoffTask, utcnow
from app.conversation_mirror import upsert_mirrors
from app.history_sync import message_direction, message_timestamp
from app.operations import setting_value, ensure_handoff
from app.outbound_control import global_message_sending_enabled
from app.runtime_settings import dt
from app.delivery_status import apply_receipt
from app.reception_rollout import reception_inbox_allowed
from app.outgoing_identity import PENDING, defer_unlinked
from app.reception_v3.live import accept, cancel, permitted, enable_new_customer, record_human_reply, human_owned

class ReplyBlocked(Exception):
    pass


def live_policy(db):
    return setting_value(db, "live_reply", {})


def assert_worker_ready(db):
    policy = live_policy(db)
    if settings.app_profile != "live_reply" or not settings.outbound_enabled or not policy.get("armed_at"):
        raise ReplyBlocked("live_reply_not_armed")
    return policy


def assert_armed(db):
    policy = assert_worker_ready(db)
    if not global_message_sending_enabled(db):
        raise ReplyBlocked("global_message_sending_disabled")
    return policy


def fresh(at, now=None):
    try:
        return timedelta(seconds=-30) <= dt(now or utcnow()) - dt(message_timestamp(at)) < timedelta(minutes=5)
    except (ValueError, TypeError):
        return False


def mirror_event(db, event):
    payload=event.payload
    policy=live_policy(db)
    incoming=(event.event=='message_created' and message_direction(payload.get('message_type'))=='incoming'
              and not payload.get('private') and not (payload.get('content_attributes') or {}).get('external_echo'))
    current=bool(policy.get('armed_at') and event.received_at >= policy['armed_at'] and fresh(event.received_at)
                 and (not incoming or payload.get('created_at') and fresh(payload['created_at'])))
    state, message, context, _=upsert_mirrors(db,event,side_effects=False,update_controls=current)
    if (state and current and event.event in ('conversation_created', 'conversation_updated')
            and reception_inbox_allowed(db, state.inbox.chatwoot_inbox_id)
            and context.get('previous_labels') != (state.labels or [])):
        from app.reception_v3.live import apply_label_change
        apply_label_change(db, state, context.get('previous_labels', []))
    if state and message:
        out=db.scalar(select(OutboundMessage).where(OutboundMessage.conversation_state_id==state.id,
                                                   OutboundMessage.chatwoot_message_id==message.chatwoot_message_id))
        if out:
            apply_receipt(db,out,payload.get('status'),payload.get('content_attributes'))
            message.attribution = out.source_type
            if event.error_code == PENDING:
                event.error_code = None
        elif (current or event.error_code == PENDING) and event.event=='message_created' and message.direction=='outgoing' and not message.private:
            sender = payload.get('sender') or {}
            if sender.get('type') != 'agent_bot' and not (payload.get('content_attributes') or {}).get('external_echo'):
                if defer_unlinked(db, event, state, message):
                    return
                message.attribution = 'inferred_human'
                if event.error_code == PENDING:
                    event.error_code = None
                record_human_reply(db,state,message)
        if incoming and current:
            from app.engagement_labels import observe_reply
            if context.get('new_message'):
                observe_reply(db, state, message)
            from app.reception_v3.human_followup import customer_replied
            from app.reception_v3.live import session_for
            if customer_replied(db, state, session_for(db, state.id)):
                event.status,event.processed_at='live_observed',utcnow()
                db.commit()
                return
            from app.advisor_assignment import incoming as assignment_incoming
            assignment_incoming(db, state, message)
            if human_owned(db,state):
                state.effective_ai_state, state.effective_state_reason = 'HUMAN_HANDOFF', 'human:contact_owned'
                cancel(db,state.id,'human_contact')
            else:
                enable_new_customer(db,state,message)
                if context.get('new_message'):
                    observe_reply(db, state, message)
                accept(db,state,message)
        elif incoming and policy.get('armed_at') and message.created_at>=policy['armed_at'] and not fresh(message.created_at) and permitted(db,state):
            ensure_handoff(db,state,'reply_queue_overdue','客户消息等待超时，请人工接待。')
    if state and current and not permitted(db,state,purpose='followup'):
        from app.reception_v3.live import session_for
        from app.conversation_labels import sync_ready
        from app.reception_v3.human_followup import waiting
        row = session_for(db,state.id)
        from app.models import ConversationLabelSync
        sync = db.get(ConversationLabelSync, state.id)
        if (row and sync and sync.target == 'ai' and sync.status == 'pending'
                and row.controls.get('v3', {}).get('last_reason') == 'operator_ai_label_return'):
            # Preserve the pending answer while removal of AI跟进 is confirmed.
            event.status,event.processed_at='live_observed',utcnow()
            db.commit()
            return
        if row and row.controls.get('human_followup',{}).get('enabled') and (not sync_ready(db,state)
                or waiting(row) and not row.controls.get('auto_followup_paused')):
            event.status,event.processed_at='live_observed',utcnow()
            db.commit()
            return
        cancel(db,state.id,'control_changed')
    event.status,event.processed_at='live_observed',utcnow()
    db.commit()
