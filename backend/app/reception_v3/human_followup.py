"""Advisor follow-up permission without autonomous answering."""
from datetime import timedelta
from zoneinfo import ZoneInfo
from sqlalchemy import select
from app.models import MessageEvent, HandoffTask, OutboundMessage, utcnow
from app.runtime_settings import blocking_labels, dt
from app.reception_v3 import service
from app.reception_v3.followup_window import deadline, within_window
from app.conversation_labels import queue_labels
from app.reception_rollout import reception_inbox_allowed


def enabled(row):
    return bool(row and row.controls.get('human_followup', {}).get('enabled'))


def waiting(row):
    return enabled(row) and row.controls['human_followup'].get('phase') == 'waiting'


def scope_enabled(db, conversation):
    from app.models import ChatwootConnection, Tenant
    from app.reception_v3.live import settings, global_message_sending_enabled, reception_conversation_allowed
    from app.runtime_settings import reply_policy
    connection = db.scalar(select(ChatwootConnection).where(ChatwootConnection.tenant_id == conversation.tenant_id))
    policy, _ = reply_policy(db, conversation.inbox_binding_id)
    return bool(policy['enabled'] and settings.outbound_enabled and global_message_sending_enabled(db)
        and connection and connection.account_id == settings.live_reply_account_id
        and reception_inbox_allowed(db, conversation.inbox.chatwoot_inbox_id)
        and conversation.inbox.ai_enabled and db.get(Tenant, conversation.tenant_id).ai_enabled
        and reception_conversation_allowed(db, conversation.chatwoot_conversation_id))


def automatic_enabled(db):
    from app.reception_config import get_reception_configuration
    config = get_reception_configuration(db)['silence']
    return config['enabled'] and config['live_enabled'] and config['advisor_reply_followup_enabled']


def automatic_return(db, conversation, row, message):
    """Only a new, public human reply may grant future follow-up permission."""
    from app.models import User
    from app.operations import audit
    if not automatic_enabled(db) or row.controls.get('auto_followup_paused'):
        return
    age = dt(utcnow()) - dt(message.created_at)
    if (message.private or message.direction != 'outgoing' or message.status == 'failed'
            or message.attribution not in ('human', 'inferred_human')
            or not timedelta(seconds=-30) <= age < timedelta(minutes=5)):
        return
    if not scope_enabled(db, conversation):
        return
    tasks = list(db.scalars(select(HandoffTask).where(HandoffTask.conversation_state_id == conversation.id,
        HandoffTask.status.in_(['pending', 'claimed']))))
    if len(tasks) > 1 or any(t.assignment_target_user_id is not None for t in tasks):
        return
    task = tasks[0] if tasks else None
    user = db.scalar(select(User).where(User.chatwoot_agent_id == message.sender_id)) if message.sender_id else None
    try:
        arm(db, conversation, row, task, user.id if user else None)
    except ValueError as exc:
        audit(db, None, 'handoff.auto_followup_skipped', 'conversation', conversation.chatwoot_conversation_id,
              {'message_id': message.chatwoot_message_id, 'reason': str(exc)})
        return
    row.controls = {**row.controls, 'human_followup': {**row.controls['human_followup'], 'mode': 'automatic'}}
    audit(db, None, 'handoff.auto_followup_armed', 'conversation', conversation.chatwoot_conversation_id,
          {'message_id': message.chatwoot_message_id, 'next_check_at': service.state(row)['next_check_at']})


def sync_history(db, row):
    from app.reception_v3.customer_images import attachment_metadata
    seen = {str(m.get('chatwoot_message_id')) for m in row.messages if m.get('chatwoot_message_id')}
    seen.update(m['id'][3:] for m in row.messages if str(m.get('id', '')).startswith('cw-'))
    messages = list(row.messages)
    history = db.scalars(select(MessageEvent).where(MessageEvent.conversation_state_id == row.conversation_state_id,
        MessageEvent.private.is_(False), MessageEvent.direction.in_(['incoming', 'outgoing']))
        .order_by(MessageEvent.chatwoot_message_id)).all()
    for m in history:
        if str(m.chatwoot_message_id) in seen or m.status == 'failed':
            continue
        messages.append({'id':f'cw-{m.chatwoot_message_id}', 'chatwoot_message_id':m.chatwoot_message_id,
            'direction':m.direction, 'content':m.content or ('[已交付附件]' if m.attachments else ''),
            'content_type':m.content_type, 'created_at':m.created_at,
            'status':'submitted' if m.direction == 'outgoing' else 'received',
            'attribution':m.attribution, 'attachments':attachment_metadata(m.attachments)})
    row.messages = sorted(messages, key=lambda m:dt(m.get('created_at') or utcnow()))
    return history


def schedule(row, conversation, now, last_reply_at):
    value = service.state(row)
    current = dt(now)
    target = dt(last_reply_at) + timedelta(minutes=30)
    if target <= current:
        target = current + timedelta(minutes=30)
    local = target.astimezone(ZoneInfo('Asia/Taipei'))
    if 1 <= local.hour < 7:
        target = local.replace(hour=7, minute=0, second=0, microsecond=0)
    customer = conversation.last_customer_message_at
    window = deadline(customer) if customer else current
    touches = [dt(m['created_at']).astimezone(ZoneInfo('Asia/Taipei')).date() for m in row.messages
        if m.get('proactive') and m.get('status') in ('submitted','sent','delivered','read','simulated_delivered')]
    if target.astimezone(ZoneInfo('Asia/Taipei')).date() in touches:
        target = (target.astimezone(ZoneInfo('Asia/Taipei')) + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
    value.update(human_followup=True, pending_event=None, next_check_at=target.isoformat() if target < window else None,
        followup_until=window.isoformat(), followup_anchor=now, followup_last_customer_at=customer,
        followup_customer_replied=True, appointment_pending=False, last_reason='advisor_return_followup')
    service.save(row,value)
    if enabled(row):
        row.controls = {**row.controls, 'human_followup': {**row.controls['human_followup'],
            'resume_at': value['next_check_at']}}


def arm(db, conversation, row, task, actor_id):
    if not row:
        raise ValueError('没有可恢复的接待记录')
    if enabled(row):
        raise ValueError('此会话已交回AI跟进，请勿重复交回')
    if not conversation.can_reply:
        raise ValueError('渠道已关闭回复窗口')
    labels = set(conversation.labels or []) | set(conversation.contact.labels if conversation.contact else [])
    if labels.intersection(blocking_labels(db) - {'人工接管'}):
        raise ValueError('仍有客诉、停止联系或已留资等保护标签，请先处理')
    if service.state(row).get('opt_out'):
        raise ValueError('客户已明确拒绝联系')
    if db.scalar(select(OutboundMessage.id).where(OutboundMessage.conversation_state_id == conversation.id,
            OutboundMessage.status.in_(['unknown','submission_unknown']))):
        raise ValueError('请先核对发送结果未知的消息')
    remaining_tasks = select(HandoffTask.id).where(HandoffTask.conversation_state_id == conversation.id,
            HandoffTask.status.in_(['pending','claimed']))
    if task:
        remaining_tasks = remaining_tasks.where(HandoffTask.id != task.id)
    if db.scalar(remaining_tasks):
        raise ValueError('仍有其他未完成的人工任务')
    from app.reception_v3.live import session_for, runtime_state
    # Check other conversations without releasing their protection.
    from app.models import ConversationState
    if conversation.contact_id:
        others = db.scalars(select(ConversationState).where(ConversationState.contact_id == conversation.contact_id,
            ConversationState.tenant_id == conversation.tenant_id, ConversationState.inbox_binding_id == conversation.inbox_binding_id,
            ConversationState.id != conversation.id)).all()
        if any(runtime_state(session_for(db,other.id),'','')[0] == 'HUMAN_HANDOFF' or enabled(session_for(db,other.id))
               or db.scalar(select(HandoffTask.id).where(HandoffTask.conversation_state_id == other.id,
                    HandoffTask.status.in_(['pending','claimed']))) for other in others):
            raise ValueError('同一客户其他会话仍在人工处理')
    history = sync_history(db,row)
    latest = history[-1] if history else None
    if (not latest or latest.direction != 'outgoing' or latest.attribution not in ('human','inferred_human')
            or latest.status == 'failed'):
        raise ValueError('请先回复客户最新问题，再交回AI跟进')
    now = utcnow()
    if not within_window(conversation.last_customer_message_at, now):
        raise ValueError('客户已超过七天未回复，无法安排自动跟进')
    service.control(row,'stop')
    row.virtual_now=now
    row.controls={**row.controls,'human':False,'auto_followup_paused':False,'human_followup':{'enabled':True,'phase':'waiting','task_id':task.id if task else None,
        'actor_id':actor_id,'armed_at':now,'last_human_message_id':latest.chatwoot_message_id}}
    value=service.state(row)
    for key in ('attempts','retry_at','failed_event','last_error','model_not_before','v3_lease'):
        value.pop(key,None)
    value.update(handoff=False,handoff_created=False,buffered_questions=[],intake_answered=False,
        opening_started=True,opening_media_pending=False,intake_status='done',intake_deadline=None)
    from app.reception_v3.skills import compile_skills
    value['followup_settings']=compile_skills(db)['silence']
    service.save(row,value)
    schedule(row,conversation,now,latest.created_at)
    service.control(row,'resume')
    if task:
        task.status,task.completed_at,task.completed_by='completed',now,actor_id
        task.sla_due_at=None;task.updated_at=now;task.version+=1
    conversation.ai_mode='disabled'
    conversation.effective_ai_state,conversation.effective_state_reason='HUMAN_HANDOFF','human:followup_waiting'
    queue_labels(db,conversation,'human')


def activate_due(db, conversation, row):
    """Switch labels only when the wait ends; confirmed labels still gate sending."""
    if not waiting(row):
        return
    permission = row.controls['human_followup']
    due = permission.get('resume_at')
    if not due or dt(row.virtual_now) < dt(due):
        return
    from app.models import ConversationState
    from app.reception_v3.live import session_for, runtime_state
    labels = set(conversation.labels or [])
    contact_labels = set(conversation.contact.labels if conversation.contact else [])
    blocked = (row.controls.get('auto_followup_paused') or not scope_enabled(db, conversation)
        or permission.get('mode') == 'automatic' and not automatic_enabled(db)
        or not conversation.can_reply or service.state(row).get('opt_out')
        or labels.intersection(blocking_labels(db) - {'人工接管'})
        or contact_labels.intersection(blocking_labels(db))
        or not within_window(conversation.last_customer_message_at, row.virtual_now)
        or db.scalar(select(OutboundMessage.id).where(OutboundMessage.conversation_state_id == conversation.id,
            OutboundMessage.status.in_(['unknown','submission_unknown'])))
        or db.scalar(select(HandoffTask.id).where(HandoffTask.conversation_state_id == conversation.id,
            HandoffTask.status.in_(['pending','claimed']))))
    if conversation.contact_id:
        others = db.scalars(select(ConversationState).where(ConversationState.tenant_id == conversation.tenant_id,
            ConversationState.contact_id == conversation.contact_id, ConversationState.inbox_binding_id == conversation.inbox_binding_id,
            ConversationState.id != conversation.id)).all()
        blocked = blocked or any(runtime_state(session_for(db,c.id),'','')[0] == 'HUMAN_HANDOFF'
            or enabled(session_for(db,c.id)) or db.scalar(select(HandoffTask.id).where(
                HandoffTask.conversation_state_id == c.id, HandoffTask.status.in_(['pending','claimed']))) for c in others)
    if blocked:
        service.control(row,'stop')
        row.controls = {**row.controls, 'human': True, 'human_followup': {'enabled':False}}
        conversation.effective_ai_state,conversation.effective_state_reason='HUMAN_HANDOFF','human:followup_blocked'
        queue_labels(db,conversation,'human')
        return
    history = sync_history(db,row)
    latest = history[-1] if history else None
    if latest and latest.direction == 'incoming':
        customer_replied(db,conversation,row)
        return
    if latest and latest.chatwoot_message_id > permission['last_human_message_id']:
        if latest.attribution in ('human','inferred_human') and latest.status != 'failed':
            advisor_replied(db,conversation,row,latest)
        else:
            service.control(row,'stop')
            row.controls = {**row.controls,'human':True,'human_followup':{'enabled':False}}
            conversation.ai_mode='disabled'
            conversation.effective_ai_state,conversation.effective_state_reason='HUMAN_HANDOFF','human:followup_interrupted'
            queue_labels(db,conversation,'human')
        return
    row.controls = {**row.controls,'human_followup':{**permission,'phase':'active'}}
    conversation.ai_mode='enabled'
    conversation.effective_ai_state,conversation.effective_state_reason='AI_PAUSED_SYNC','advisor_return_labels_pending'
    queue_labels(db,conversation,'followup')
    from app.operations import audit
    audit(db,None,'handoff.followup_activated','conversation',conversation.chatwoot_conversation_id,
        {'last_human_message_id':permission['last_human_message_id']})


def advisor_replied(db, conversation, row, message):
    if not enabled(row):
        return False
    permission=row.controls['human_followup']
    if (message.chatwoot_message_id <= permission.get('last_human_message_id',0)
            or dt(message.created_at) < dt(permission['armed_at'])):
        return True
    sync_history(db,row)
    service.control(row,'stop')
    row.virtual_now=utcnow()
    row.controls={**row.controls,'human_followup':{**permission,'phase':'waiting','last_human_message_id':message.chatwoot_message_id}}
    schedule(row,conversation,row.virtual_now,message.created_at)
    service.control(row,'resume')
    conversation.ai_mode='disabled'
    conversation.effective_ai_state,conversation.effective_state_reason='HUMAN_HANDOFF','human:followup_waiting'
    queue_labels(db,conversation,'human')
    return True


def customer_replied(db, conversation, row):
    if not enabled(row):
        return False
    sync_history(db,row)
    from app.reception_v3.live import handoff
    handoff(db,row,'followup_customer_replied','客户已回复AI跟进，请继续接待；AI已停止答复和跟进。')
    return True
