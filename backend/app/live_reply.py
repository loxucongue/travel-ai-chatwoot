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
from app.reception_v3.live import accept, cancel, permitted

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
    state, message, _, _=upsert_mirrors(db,event,side_effects=False,update_controls=current)
    if state and message:
        out=db.scalar(select(OutboundMessage).where(OutboundMessage.conversation_state_id==state.id,
                                                   OutboundMessage.chatwoot_message_id==message.chatwoot_message_id))
        if out:
            apply_receipt(db,out,payload.get('status'))
        elif current and message.direction=='outgoing' and not message.private:
            cancel(db,state.id,'human_reply')
        if incoming and current:
            accept(db,state,message)
        elif incoming and policy.get('armed_at') and message.created_at>=policy['armed_at'] and not fresh(message.created_at) and permitted(db,state):
            ensure_handoff(db,state,'reply_queue_overdue','客户消息等待超时，请人工接待。')
    if state and current and not permitted(db,state):
        cancel(db,state.id,'control_changed')
    event.status,event.processed_at='live_observed',utcnow()
    db.commit()
