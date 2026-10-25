"""Chatwoot transport for the same V3 session used in the playground."""
from copy import deepcopy
from datetime import timedelta
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import StaleDataError
from app.automation_models import AutomationSession
from app.chatwoot_service import client_for, payload_dict
from app.config import settings
from app.conversation_policy import compute_state, has_ai_label, observe_ai_label
from app.history_sync import message_timestamp, message_direction
from app.models import ConversationState, ChatwootConnection, MessageEvent, OutboundMessage, StoredMedia, User, Tenant, HandoffTask, utcnow
from app.operations import setting_value, ensure_handoff
from app.outbound_control import global_message_sending_enabled
from app.reception_rollout import reception_conversation_allowed, reception_inbox_allowed
from app.reception_v3 import service, release_id
from app.reception_v3.followup_window import within_window
from app.runtime_settings import dt, reply_policy, blocking_labels


def session_for(db, conversation_id):
    return db.scalar(select(AutomationSession).where(AutomationSession.environment == 'live',
                     AutomationSession.conversation_state_id == conversation_id))


def runtime_state(row, effective, reason):
    if not row:
        return effective, reason
    value = service.state(row)
    from app.reception_v3.human_followup import waiting
    if waiting(row):
        return 'HUMAN_HANDOFF', 'human:followup_waiting'
    if row.controls.get('human') or value.get('handoff') or value.get('last_reason') in ('human_reply', 'human_contact'):
        return 'HUMAN_HANDOFF', 'human:advisor_owned'
    if effective == 'AI_ACTIVE' and row.controls.get('human_followup', {}).get('enabled'):
        return 'AI_FOLLOWUP', 'advisor:followup_only'
    if effective == 'AI_ACTIVE' and row.controls.get('simulation', {}).get('status') != 'running':
        return 'AI_PAUSED_CONVERSATION', 'runtime:' + str(value.get('last_reason') or 'stopped')
    return effective, reason


def human_owned(db, conversation):
    if not conversation.contact_id:
        return False
    linked = db.scalars(select(ConversationState).where(
        ConversationState.tenant_id == conversation.tenant_id,
        ConversationState.inbox_binding_id == conversation.inbox_binding_id,
        ConversationState.contact_id == conversation.contact_id)).all()
    for item in linked:
        if db.scalar(select(HandoffTask.id).where(HandoffTask.conversation_state_id == item.id,
                                                HandoffTask.status.in_(['pending', 'claimed']))):
            return True
        row = session_for(db, item.id)
        if item.id != conversation.id and row and row.controls.get('human_followup', {}).get('enabled'):
            return True
        if runtime_state(row, '', '')[0] == 'HUMAN_HANDOFF':
            return True
    return False


def record_human_reply(db, conversation, message):
    """A real public advisor reply acknowledges the local handoff too."""
    from app.operations import audit
    from app.reception_v3.human_followup import advisor_replied
    row = session_for(db, conversation.id)
    if row and message.chatwoot_message_id <= row.controls.get('last_advisor_event_id', 0):
        return
    if row:
        row.controls = {**row.controls, 'last_advisor_event_id': message.chatwoot_message_id}
    if advisor_replied(db, conversation, session_for(db, conversation.id), message):
        audit(db, None, 'handoff.followup_timer_reset', 'conversation', conversation.chatwoot_conversation_id,
              {'message_id': message.chatwoot_message_id})
        return
    cancel(db, conversation.id, 'human_reply')
    row = session_for(db, conversation.id)
    if row is None:
        owner = db.scalar(select(User).order_by(User.id))
        row = AutomationSession(owner_id=owner.id, environment='live', engine_version='v3',
            engine_release_id=release_id(), conversation_state_id=conversation.id,
            inbox_binding_id=conversation.inbox_binding_id, virtual_now=utcnow(), messages=[], memory={},
            controls={'simulation': {'status': 'stopped'}, 'v3': {'last_reason': 'human_reply'}})
        db.add(row)
        from app.models import ConversationLabelSync
        previous_intent = db.get(ConversationLabelSync, conversation.id)
        if conversation.ai_mode == 'disabled' and conversation.ai_mode_source == 'platform' and not previous_intent:
            row.controls = {**row.controls, 'auto_followup_paused': True}
    row.controls = {**row.controls, 'last_advisor_event_id': message.chatwoot_message_id}
    row.controls = {**row.controls, 'human': True}
    conversation.effective_ai_state = 'HUMAN_HANDOFF'
    conversation.effective_state_reason = 'human:advisor_reply'
    from app.conversation_labels import queue_labels
    queue_labels(db, conversation, 'human')
    from app.reception_v3.human_followup import sync_history
    sync_history(db, row)
    linked_ids = [conversation.id]
    if conversation.contact_id:
        linked_ids = list(db.scalars(select(ConversationState.id).where(
            ConversationState.tenant_id == conversation.tenant_id,
            ConversationState.contact_id == conversation.contact_id,
            ConversationState.inbox_binding_id == conversation.inbox_binding_id)))
    for linked_id in linked_ids:
        if linked_id != conversation.id:
            cancel(db, linked_id, 'human_contact')
    for task in db.scalars(select(HandoffTask).where(HandoffTask.conversation_state_id.in_(linked_ids),
                                                  HandoffTask.status.in_(['pending', 'claimed']))):
        task.status = 'claimed'
        task.claimed_at = task.claimed_at or message.created_at
        task.sla_due_at = None
        task.chatwoot_assignee_id = message.sender_id or conversation.assignee_id
        user = db.scalar(select(User).where(User.chatwoot_agent_id == task.chatwoot_assignee_id)) if task.chatwoot_assignee_id else None
        task.assignee_user_id = user.id if user else None
        task.updated_at = utcnow()
        task.version += 1
    audit(db, None, 'handoff.advisor_replied', 'conversation', conversation.chatwoot_conversation_id,
          {'message_id': message.chatwoot_message_id, 'agent_id': message.sender_id})
    from app.reception_v3.human_followup import automatic_return
    automatic_return(db, conversation, row, message)


def handoff(db, row, reason, summary):
    service.control(row, 'stop')
    row.controls = {**row.controls, 'human': True, 'human_followup': {'enabled': False}}
    conversation = db.get(ConversationState, row.conversation_state_id)
    ensure_handoff(db, conversation, reason, summary)
    value = service.state(row)
    value.update(handoff=True, handoff_created=True, handoff_type=reason,
                 handoff_reason=summary, pending_event=None, next_check_at=None)
    service.save(row, value)


def handoff_from_decision(db, row):
    value = service.state(row)
    reason = value.get('handoff_type', 'other')
    if row.memory.get('contact_value'):
        reason = 'contact'
    elif row.memory.get('contact_status') == 'attachment_pending_verification':
        reason = 'contact_image'
    elif row.memory.get('contact_status') == 'self_reported_added':
        reason = 'contact_reported'
    handoff(db, row, reason, value.get('handoff_reason', ''))


def cancel(db, conversation_id, reason):
    row = session_for(db, conversation_id)
    if row:
        service.control(row, 'stop')
        value = service.state(row)
        value['last_reason'] = reason
        service.save(row, value)
        conversation = db.get(ConversationState, conversation_id)
        conversation.effective_ai_state, conversation.effective_state_reason = runtime_state(
            row, conversation.effective_ai_state, conversation.effective_state_reason)


def permitted(db, conversation, *, purpose='reply'):
    from app.conversation_labels import sync_ready
    from app.reception_v3.human_followup import enabled, waiting
    row = session_for(db, conversation.id)
    if waiting(row):
        return False
    if enabled(row) and row.controls['human_followup'].get('mode') == 'automatic':
        from app.reception_v3.human_followup import automatic_enabled
        if not automatic_enabled(db) or row.controls.get('auto_followup_paused'):
            return False
    if not sync_ready(db, conversation) or (enabled(session_for(db, conversation.id)) and purpose != 'followup'):
        return False
    connection = db.scalar(select(ChatwootConnection).where(ChatwootConnection.tenant_id == conversation.tenant_id))
    effective, _ = compute_state(db.get(Tenant, conversation.tenant_id), conversation.inbox,
        conversation.labels or [], conversation.contact.labels if conversation.contact else [],
        conversation.can_reply, conversation.ai_mode, conversation.ai_sync_status, conversation.ai_label_present)
    policy, _ = reply_policy(db, conversation.inbox_binding_id)
    return bool(policy['enabled'] and settings.outbound_enabled and global_message_sending_enabled(db)
        and connection and connection.account_id == settings.live_reply_account_id
        and reception_inbox_allowed(db, conversation.inbox.chatwoot_inbox_id)
        and reception_conversation_allowed(db, conversation.chatwoot_conversation_id)
        and effective == 'AI_ACTIVE'
        and not set((conversation.labels or []) + (conversation.contact.labels if conversation.contact else [])).intersection(blocking_labels(db))
        and not human_owned(db, conversation)
        and not db.scalar(select(HandoffTask.id).where(HandoffTask.conversation_state_id == conversation.id,
                                                      HandoffTask.status.in_(['pending','claimed']))))


def apply_label_change(db, conversation, previous_labels):
    """An explicit Chatwoot AI toggle may return this conversation to full reception."""
    labels = set(conversation.labels or [])
    added = labels - set(previous_labels)
    mappings = setting_value(db, 'label_mappings', {})
    handoff_labels = {'人工接管', '客诉', '客訴', *mappings.get('handoff_labels', [])}
    matched = sorted(added & handoff_labels)
    # Echoes of our human-label sync do not represent an operator pause.
    from app.models import ConversationLabelSync
    from app.conversation_labels import desired_labels
    intent = db.get(ConversationLabelSync, conversation.id)
    from app.conversation_labels import label_keys
    own_ai_sync = (intent and (intent.target == 'followup' and intent.status in ('pending','synced')
                              or intent.target == 'ai' and intent.status == 'pending')
                  and label_keys(labels) == label_keys(desired_labels(list(labels), intent.target)))
    explicit_ai = (has_ai_label(list(labels)) and not labels.intersection(blocking_labels(db))
                   and (not has_ai_label(previous_labels) or '人工接管' in previous_labels)
                   and not own_ai_sync)
    if explicit_ai:
        return return_by_ai_label(db, conversation)
    # Accepted operator changes supersede unfinished control writes. Do not
    # keep retrying an old human/follow-up intent over the operator's new labels.
    if (intent and intent.status == 'pending'
            and label_keys(labels) != label_keys(desired_labels(list(labels), intent.target))):
        intent.status, intent.error, intent.updated_at = 'cancelled', 'superseded_by_chatwoot_labels', utcnow()
    own_human_sync = intent and intent.target == 'human' and labels == set(desired_labels(list(labels), 'human'))
    if ((matched or not has_ai_label(list(labels)) or labels.intersection(blocking_labels(db)))
            and not own_human_sync):
        row = session_for(db, conversation.id)
        if row:
            row.controls = {**row.controls, 'auto_followup_paused': True}
    from app.reception_v3.human_followup import waiting
    if (own_human_sync and waiting(session_for(db, conversation.id))
            and not labels.intersection(blocking_labels(db) - {'人工接管'})
            and not (set(matched) - {'人工接管'})):
        return
    if matched:
        ensure_handoff(db, conversation, 'label', 'Chatwoot 标签触发：' + '、'.join(matched),
                       'P1' if set(matched) & {'客诉', '客訴'} else 'P2')
        cancel(db, conversation.id, 'handoff_label')
        return
    if labels.intersection(blocking_labels(db)):
        cancel(db, conversation.id, 'label_paused')
        row = session_for(db, conversation.id)
        if row:
            row.controls = {**row.controls, 'human_followup': {'enabled': False}}
        return
    if has_ai_label(list(labels)) and human_owned(db, conversation):
        # A refresh with unchanged labels is not an operator hand-back.
        return
    if not has_ai_label(list(labels)) or labels.intersection(blocking_labels(db)):
        cancel(db, conversation.id, 'label_paused')
        return
    # System label echoes may resume a normally paused session. Explicit
    # operator hand-backs, including removing 人工接管 while keeping ai, ran above.
    if has_ai_label(previous_labels) or not permitted(db, conversation, purpose='followup'):
        return
    row = session_for(db, conversation.id)
    if not row:
        # The incoming webhook can precede the operator's AI label. Start only
        # an unanswered, fresh message; an old label edit must not replay history.
        message = db.scalar(select(MessageEvent).where(
            MessageEvent.conversation_state_id == conversation.id,
            MessageEvent.private.is_(False), MessageEvent.direction.in_(['incoming', 'outgoing']))
            .order_by(MessageEvent.chatwoot_message_id.desc()).limit(1))
        armed_at = setting_value(db, 'live_reply', {}).get('armed_at')
        has_outbound = db.scalar(select(OutboundMessage.id).where(
            OutboundMessage.conversation_state_id == conversation.id))
        if (message and message.direction == 'incoming' and armed_at and not has_outbound
                and dt(message.created_at) >= dt(armed_at)
                and timedelta(seconds=-30) <= dt(utcnow()) - dt(message.created_at) < timedelta(minutes=5)):
            accept(db, conversation, message)
        return
    value = service.state(row)
    unknown = db.scalar(select(OutboundMessage.id).where(
        OutboundMessage.conversation_state_id == conversation.id,
        OutboundMessage.status.in_(['submission_unknown', 'unknown'])))
    if (unknown or value.get('opt_out') or value.get('failed_event')
            or value.get('handoff') or row.controls.get('human')):
        return
    if value.get('last_reason') not in ('label_paused', 'ai_label_removed', 'control_changed',
                                      'remote_reception_paused', 'reception_paused'):
        return
    service.control(row, 'resume')
    value['last_reason'] = 'label_restored'
    service.save(row, value)
    conversation.effective_ai_state, conversation.effective_state_reason = 'AI_ACTIVE', 'label_restored'


def restore_blocker(db, conversation, *, ignore_current_handoff=False):
    """Return the reason an explicit resume cannot release this conversation."""
    if not ignore_current_handoff and db.scalar(select(HandoffTask.id).where(HandoffTask.conversation_state_id == conversation.id,
                                            HandoffTask.status.in_(['pending', 'claimed']))):
        return '请先完成当前人工任务后再恢复 AI'
    if db.scalar(select(OutboundMessage.id).where(OutboundMessage.conversation_state_id == conversation.id,
                                                 OutboundMessage.status.in_(['submission_unknown', 'unknown']))):
        return '存在发送结果未知的消息，请先核对渠道送达记录'
    if set(conversation.contact.labels if conversation.contact else []).intersection(blocking_labels(db)):
        return '客户仍有停止联系标签，请先核对客户意愿'
    row = session_for(db, conversation.id)
    if row and service.state(row).get('opt_out'):
        return '客户已明确拒绝联系，不能直接恢复 AI'
    if conversation.contact_id:
        for other in db.scalars(select(ConversationState).where(
                ConversationState.tenant_id == conversation.tenant_id,
                ConversationState.contact_id == conversation.contact_id,
                ConversationState.inbox_binding_id == conversation.inbox_binding_id,
                ConversationState.id != conversation.id)):
            if (runtime_state(session_for(db, other.id), '', '')[0] == 'HUMAN_HANDOFF'
                    or db.scalar(select(HandoffTask.id).where(HandoffTask.conversation_state_id == other.id,
                                                             HandoffTask.status.in_(['pending', 'claimed'])))):
                return '同一客户的其他会话仍由人工接待，请先处理对应会话'
    return None


def return_by_ai_label(db, conversation):
    """Explicit operator hand-back cancels local ownership, not customer history."""
    from app.models import ConversationLabelSync
    from app.conversation_labels import desired_labels, label_keys, queue_labels
    from app.reception_v3.human_followup import sync_history, scope_enabled
    from app.operations import audit
    job = db.get(ConversationLabelSync, conversation.id)
    if job:
        job.target, job.status, job.error, job.updated_at = 'ai', 'synced', None, utcnow()
    observe_ai_label(conversation, conversation.labels or [], 'chatwoot')
    conversation.ai_mode, conversation.ai_sync_status = 'enabled', 'synced'
    blocker = restore_blocker(db, conversation, ignore_current_handoff=True)
    if blocker or not scope_enabled(db, conversation) or not conversation.can_reply:
        conversation.effective_ai_state = 'AI_PAUSED_CONVERSATION'
        conversation.effective_state_reason = 'label_return_blocked:' + (blocker or 'reception_scope_or_channel')
        audit(db,None,'conversation.ai_label_return_blocked','conversation',conversation.chatwoot_conversation_id,
              {'reason':blocker or 'reception_scope_or_channel'})
        return
    now = utcnow()
    for task in db.scalars(select(HandoffTask).where(HandoffTask.conversation_state_id==conversation.id,
                          HandoffTask.status.in_(['pending','claimed']))):
        task.status, task.completed_at, task.sla_due_at = 'cancelled', now, None
        task.updated_at = now; task.version += 1
    from app.models import Notification, NotificationDelivery
    for delivery in db.scalars(select(NotificationDelivery).join(Notification).where(
            Notification.tenant_id==conversation.tenant_id,
            Notification.conversation_id==conversation.chatwoot_conversation_id,
            Notification.event_type.in_(['handoff.created','handoff.overdue']),
            NotificationDelivery.status.in_(['pending','retry']))):
        delivery.status, delivery.error_code = 'cancelled', 'operator_ai_label_return'
    row = session_for(db, conversation.id)
    if row is None:
        owner = db.scalar(select(User).order_by(User.id))
        row = AutomationSession(owner_id=owner.id, environment='live', mode='journey', engine_version='v3',
            engine_release_id=release_id(), conversation_state_id=conversation.id, inbox_binding_id=conversation.inbox_binding_id,
            virtual_now=now, messages=[], memory={}, controls={})
        db.add(row); db.flush()
    service.control(row, 'stop')  # Cancel in-flight generation and obsolete drafts.
    row.controls = {**row.controls, 'human':False, 'auto_followup_paused':False, 'human_followup':{'enabled':False}}
    history = sync_history(db, row)
    value = service.state(row)
    for key in ('failed_event','last_error','retry_at','attempts','v3_lease','model_not_before','merge_started_at',
                'handoff_reason','handoff_type','human_followup','departure_review_pending','route_review_pending'):
        value.pop(key, None)
    row.controls.pop('v3_lease', None)
    value.update(handoff=False,handoff_created=False,buffered_questions=[],pending_event=None,
                 last_reason='operator_ai_label_return',intake_answered=False)
    if value.get('intake_status') in ('sending','waiting'):
        value['intake_status'] = 'done'
    latest = next((m for m in reversed(history) if m.status!='failed'), None)
    # Answer the outstanding customer turn now; do not replay a reply already
    # supplied by a human, revive cancelled drafts, or send outside the window.
    if latest and latest.direction=='incoming' and within_window(latest.created_at, now):
        last_outgoing = max((m.chatwoot_message_id for m in history if m.direction=='outgoing' and m.status!='failed'),default=0)
        outstanding = [m for m in history if m.direction=='incoming' and m.chatwoot_message_id>last_outgoing][-10:]
        from app.reception_v3.customer_images import attachment_metadata
        by_id = {f'cw-{m.chatwoot_message_id}':m for m in outstanding}
        row.messages = [{**m,'attachments':attachment_metadata(by_id[m['id']].attachments)} if m.get('id') in by_id else m for m in row.messages]
        value.update(pending_event='customer_message',buffered_questions=list(by_id),model_not_before=now)
        value.setdefault('opening_started',bool(last_outgoing))
        row.due_at = now
    elif latest and latest.direction == 'outgoing' and conversation.last_customer_message_at:
        # Returning an answered conversation also resumes its silence schedule,
        # without re-sending the advisor's answer or extending the customer clock.
        from app.reception_v3.skills import compile_skills
        bundle = compile_skills(db)
        value.update(followup_last_customer_at=message_timestamp(conversation.last_customer_message_at),
                     followup_settings=bundle['silence'])
        service.schedule_followup(value, bundle, now, reset=True)
    row.virtual_now = now
    service.save(row,value);service.control(row,'resume')
    conversation.effective_ai_state,conversation.effective_state_reason='AI_ACTIVE','operator_ai_label_return'
    if label_keys(conversation.labels or []) != label_keys(desired_labels(conversation.labels or [],'ai')):
        queue_labels(db,conversation,'ai')
    audit(db,None,'conversation.ai_returned_by_label','conversation',conversation.chatwoot_conversation_id,
          {'pending_customer_message_id':latest.chatwoot_message_id if value['pending_event'] else None})


def enable_new_customer(db, conversation, message):
    """Opt new arrivals into the configured inbox, never re-enable old customers."""
    cutoff = setting_value(db, 'live_reply', {}).get('auto_new_customers_since')
    if (not cutoff or not settings.outbound_enabled or not global_message_sending_enabled(db)
            or not reception_inbox_allowed(db, conversation.inbox.chatwoot_inbox_id)
            or not reception_conversation_allowed(db, conversation.chatwoot_conversation_id)
            or not conversation.inbox.ai_enabled or not db.get(Tenant, conversation.tenant_id).ai_enabled
            or human_owned(db, conversation)
            or has_ai_label(conversation.labels or []) or session_for(db, conversation.id)
            or conversation.ai_mode_source not in ('system', 'chatwoot')
            or db.scalar(select(MessageEvent.id).where(MessageEvent.conversation_state_id == conversation.id,
                MessageEvent.id != message.id, MessageEvent.private.is_(False)))
            or db.scalar(select(HandoffTask.id).where(HandoffTask.conversation_state_id == conversation.id))):
        return
    connection = db.scalar(select(ChatwootConnection).where(ChatwootConnection.tenant_id == conversation.tenant_id))
    if not connection or connection.account_id != settings.live_reply_account_id:
        return
    client = client_for(connection)
    try:
        remote = payload_dict(client.get_conversation(conversation.chatwoot_conversation_id))
        created = remote.get('created_at')
        if (not created or dt(message_timestamp(created)) < dt(cutoff)
                or remote.get('inbox_id') != conversation.inbox.chatwoot_inbox_id or remote.get('can_reply') is False):
            return
        labels = client.get_conversation_labels(conversation.chatwoot_conversation_id)
        labels = labels.get('payload') if isinstance(labels, dict) else labels
        if (not isinstance(labels, list) or set(labels).intersection(blocking_labels(db))
                or set(conversation.contact.labels if conversation.contact else []).intersection(blocking_labels(db))):
            return
        if not has_ai_label(labels):
            client.set_conversation_labels(conversation.chatwoot_conversation_id, [*labels, 'ai'])
            labels = client.get_conversation_labels(conversation.chatwoot_conversation_id)
            labels = labels.get('payload') if isinstance(labels, dict) else labels
        if not isinstance(labels, list) or not has_ai_label(labels):
            raise ValueError('new_customer_ai_label_not_confirmed')
        conversation.labels = labels
        observe_ai_label(conversation, labels, 'chatwoot')
        conversation.effective_ai_state, conversation.effective_state_reason = compute_state(
            db.get(Tenant, conversation.tenant_id), conversation.inbox, labels,
            conversation.contact.labels if conversation.contact else [], conversation.can_reply,
            conversation.ai_mode, conversation.ai_sync_status, conversation.ai_label_present)
    except Exception:
        ensure_handoff(db, conversation, 'new_customer_activation_failed', '新客户自动接待未启用，请顾问接手。')
    finally:
        client.close()


def accept(db, conversation, message):
    if not permitted(db, conversation):
        return
    row = session_for(db, conversation.id)
    if row is None:
        owner = db.scalar(select(User).order_by(User.id))
        row = AutomationSession(owner_id=owner.id, environment='live', mode='journey', engine_version='v3',
            engine_release_id=release_id(), conversation_state_id=conversation.id,
            inbox_binding_id=conversation.inbox_binding_id, virtual_now=utcnow(), controls={}, messages=[], memory={})
        db.add(row)
        db.flush()
        history = db.scalars(select(MessageEvent).where(MessageEvent.conversation_state_id == conversation.id,
            MessageEvent.private.is_(False), MessageEvent.chatwoot_message_id < message.chatwoot_message_id)
            .order_by(MessageEvent.chatwoot_message_id)).all()
        from app.reception_v3.customer_images import attachment_metadata
        row.messages = [{'id': f'cw-{m.chatwoot_message_id}', 'direction': m.direction, 'content': m.content,
                         'status': 'submitted' if m.direction == 'outgoing' else m.status, 'created_at': m.created_at,
                         'content_type': m.content_type, 'attachments': attachment_metadata(m.attachments)}
                        for m in history if m.direction in ('incoming','outgoing')]
        now=utcnow()
        row.controls = {'simulation': {'status':'running', 'last_wall_at':now, 'start_virtual_at':now},
                        'v3': {'buffered_questions':[], 'opening_started':bool(history)}}
    if row.controls.get('simulation', {}).get('status') != 'running':
        if service.state(row).get('last_reason') in ('global_message_sending_disabled', 'ai_reception_allowlist_changed'):
            service.control(row, 'resume')
        else:
            return
    row.virtual_now = utcnow()
    from app.engagement_labels import observe_followup_return
    observe_followup_return(db, conversation, message)
    service.add_message(db, row, message.content or '[客户发送附件]', f'cw-{message.chatwoot_message_id}',
                        message.content_type, attachments=message.attachments)
    value = service.state(row)
    policy, _ = reply_policy(db, conversation.inbox_binding_id)
    now = utcnow()
    start = value.get('merge_started_at')
    if not start or dt(start) + timedelta(seconds=policy['merge_max_seconds']) < dt(now):
        start = now
    value['merge_started_at'] = start
    value['model_not_before'] = min(service.later(now, policy['merge_wait_seconds']),
                                    service.later(start, policy['merge_max_seconds']))
    service.save(row, value)


def deliver_one(db, row):
    """Persist submission intent before HTTP. Unknown results never auto-repeat."""
    value = service.state(row)
    # Old plans may still contain a handoff acknowledgement. Never send it,
    # even when it predates this release or the session is already stopped.
    if value.get('handoff') or row.controls.get('human'):
        if value.get('handoff') and not value.get('handoff_created'):
            handoff_from_decision(db, row)
        elif (row.controls.get('simulation', {}).get('status') == 'running'
              or any(m.get('status') == 'draft' for m in row.messages)):
            service.control(row, 'stop')
        db.commit()
        return
    if row.controls.get('simulation', {}).get('status') != 'running':
        return
    if value.get('departure_review_pending') or value.get('route_review_pending'):
        return
    drafts = [m for m in row.messages if m.get('status') == 'draft']
    if not drafts:
        return
    if not value.get('delivery_due_at') or dt(value['delivery_due_at']) > dt(row.virtual_now):
        return
    conversation = db.get(ConversationState, row.conversation_state_id)
    from app.conversation_labels import sync_ready
    if not sync_ready(db, conversation):
        # A confirmed owner change is required before delivery; keep the new
        # answer queued while the label write retries instead of cancelling it.
        return
    if not permitted(db, conversation, purpose='followup' if row.controls.get('human_followup', {}).get('enabled') else 'reply'):
        cancel(db, conversation.id, 'reception_paused')
        db.commit()
        return
    last = conversation.last_customer_message_at
    if not within_window(message_timestamp(last) if last else None, utcnow()):
        handoff(db, row, 'channel_window_closed', '客户已超过七天未回复，自动跟进已停止。')
        service.control(row, 'stop')
        db.commit()
        return
    part = drafts[0]
    if part.get('material_rule_id') or part.get('proactive'):
        from app.reception_v3.skills import compile_skills
        from app.reception_v3.material_rules import eligible_rules, conversation_labels, rule_digest
        current_bundle = compile_skills(db)
        rules = eligible_rules(current_bundle, [], row.controls.get('route_variant', ''),
            conversation_labels(db, row), 'silence_due' if part.get('proactive') else 'customer_message', [])
        allowed_ids = {rule['id'] for rule in rules}
        current = next((r for r in current_bundle.get('material_rules', []) if r['id'] == part.get('material_rule_id')), None)
        revoked = bool(part.get('material_rule_id')) and (not current or current['id'] not in allowed_ids
                or rule_digest(current) != part.get('material_rule_digest'))
        unmanaged_image = (part.get('proactive') and part.get('content_type') == 'image'
            and not part.get('material_rule_id') and current_bundle['silence'].get('material_selection') == 'configured_rules')
        if revoked or unmanaged_image:
            row.messages = [{**m, 'status':'cancelled'} if m.get('plan_id') == part.get('plan_id')
                            and m.get('status') == 'draft' else m for m in row.messages]
            value.update(delivery_kind=None, delivery_due_at=None, last_reason='material_rule_changed')
            service.schedule_followup(value, current_bundle, row.virtual_now, reset=True)
            service.save(row, value)
            db.commit()
            return
    if service.skip_quiet_delivery(row, part):
        db.commit()
        return
    key = f'live-v3:{row.id}:{part["id"]}'
    existing = db.scalar(select(OutboundMessage).where(OutboundMessage.idempotency_key == key))
    if existing:
        handoff(db, row, 'delivery_unknown', '请核对上一条消息实际是否送达。')
        service.control(row, 'stop')
        db.commit()
        return
    connection = db.scalar(select(ChatwootConnection).where(ChatwootConnection.tenant_id == conversation.tenant_id))
    client = client_for(connection)
    submission_started = False
    try:
        # Refresh channel controls before the external side effect.
        remote = payload_dict(client.get_conversation(conversation.chatwoot_conversation_id))
        labels = client.get_conversation_labels(conversation.chatwoot_conversation_id)
        labels = labels.get('payload') if isinstance(labels, dict) else labels
        if (not isinstance(labels, list) or not has_ai_label(labels)
                or set(labels).intersection(blocking_labels(db)) or remote.get('can_reply') is False):
            cancel(db, conversation.id, 'remote_reception_paused')
            db.commit()
            return
        # Recheck local ownership and generation after network reads. A new
        # customer/advisor message may have cancelled this plan meanwhile.
        generation = row.generation
        db.refresh(row)
        db.refresh(conversation)
        if (row.generation != generation or row.controls.get('simulation', {}).get('status') != 'running'
                or not permitted(db,conversation,purpose='followup' if row.controls.get('human_followup',{}).get('enabled') else 'reply')):
            return
        media = db.get(StoredMedia, part.get('media_id')) if part.get('media_id') else None
        out = OutboundMessage(conversation_state_id=conversation.id, idempotency_key=key,
            source_type='ai', source_id=row.id, content=part.get('content',''), content_type=part.get('content_type','text'),
            status='submission_unknown', media_id=media.id if media else None,
            content_attributes=part.get('content_attributes',{}))
        db.add(out)
        row.messages = [{**m, 'status':'submitting'} if m.get('id') == part['id'] else m for m in row.messages]
        db.commit()
        submission_started = True
        remote_id = conversation.chatwoot_conversation_id
        if media:
            result = payload_dict(client.create_attachment_message(remote_id, out.content, media.storage_path, media.mime_type))
        elif out.content_type == 'input_select':
            result = payload_dict(client.create_input_select_message(remote_id,out.content,
                [item['title'] for item in out.content_attributes['items']]))
        else:
            result = payload_dict(client.create_text_message(remote_id,out.content))
        if not isinstance(result.get('id'), int):
            raise ValueError('submission_unknown')
        out.chatwoot_message_id, out.status, out.submitted_at = result['id'], 'submitted', utcnow()
        # Keep the channel acceptance even if subsequent local bookkeeping fails.
        db.commit()
        lookup = select(MessageEvent).where(MessageEvent.conversation_state_id == conversation.id,
                                             MessageEvent.chatwoot_message_id == result['id'])
        message = db.scalar(lookup)
        if message is None:
            try:
                with db.begin_nested():
                    message = MessageEvent(conversation_state_id=conversation.id, chatwoot_message_id=result['id'],
                        direction='outgoing', content=out.content, content_type=out.content_type,
                        status='submitted', attribution='ai', content_attributes=out.content_attributes)
                    db.add(message)
                    db.flush()
            except IntegrityError:
                # The webhook may insert the same message after our lookup.
                message = db.scalar(lookup)
                if message is None:
                    raise
        # Preserve callback receipts, attachment metadata and server timestamps.
        message.attribution = 'ai'
        from app.delivery_status import apply_receipt
        apply_receipt(db, out, message.status)
        db.commit()
        if out.status == 'failed':
            return
        # Reload after HTTP; preserve any incoming message that arrived meanwhile.
        for attempt in range(3):
            try:
                db.refresh(row)
                row.virtual_now = utcnow()
                service.complete_delivery(row, part['id'], 'submitted')
                row.messages = [{**m, 'status':'submitted', 'chatwoot_message_id':result['id']} if m.get('id') == part['id'] else m for m in row.messages]
                db.commit()
                break
            except StaleDataError:
                db.rollback()
                if attempt == 2:
                    raise
    except Exception as exc:
        db.rollback()
        if isinstance(exc, StaleDataError) and not submission_started:
            # A model/webhook updated the session while the channel GET was in
            # flight. The intent transaction rolled back before any POST. Let
            # delivery_turn reload current controls and retry safely; this is
            # not an uncertain customer send and must not create a handoff.
            raise
        db.refresh(row)
        handoff(db, row, 'delivery_failed', '消息提交未完成，请核对渠道记录；系统不会自动重发。')
        service.control(row, 'stop')
        db.commit()
        raise
    finally:
        client.close()
