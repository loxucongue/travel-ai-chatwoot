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
from app.reception_rollout import reception_conversation_allowed
from app.reception_v3 import service, release_id
from app.runtime_settings import dt, reply_policy, blocking_labels


def session_for(db, conversation_id):
    return db.scalar(select(AutomationSession).where(AutomationSession.environment == 'live',
                     AutomationSession.conversation_state_id == conversation_id))


def handoff(db, row, reason, summary):
    conversation = db.get(ConversationState, row.conversation_state_id)
    ensure_handoff(db, conversation, reason, summary)
    value = service.state(row)
    value.update(handoff=True, handoff_reason=reason, pending_event=None, next_check_at=None)
    service.save(row, value)


def cancel(db, conversation_id, reason):
    row = session_for(db, conversation_id)
    if row:
        service.control(row, 'stop')
        value = service.state(row)
        value['last_reason'] = reason
        service.save(row, value)


def permitted(db, conversation):
    connection = db.scalar(select(ChatwootConnection).where(ChatwootConnection.tenant_id == conversation.tenant_id))
    effective, _ = compute_state(db.get(Tenant, conversation.tenant_id), conversation.inbox,
        conversation.labels or [], conversation.contact.labels if conversation.contact else [],
        conversation.can_reply, conversation.ai_mode, conversation.ai_sync_status, conversation.ai_label_present)
    policy, _ = reply_policy(db, conversation.inbox_binding_id)
    return bool(policy['enabled'] and settings.outbound_enabled and global_message_sending_enabled(db)
        and connection and connection.account_id == settings.live_reply_account_id
        and conversation.inbox.chatwoot_inbox_id == settings.live_reply_inbox_id
        and reception_conversation_allowed(db, conversation.chatwoot_conversation_id)
        and effective == 'AI_ACTIVE'
        and not db.scalar(select(HandoffTask.id).where(HandoffTask.conversation_state_id == conversation.id,
                                                      HandoffTask.status.in_(['pending','claimed']))))


def enable_new_customer(db, conversation, message):
    """Opt new arrivals into the configured inbox, never re-enable old customers."""
    cutoff = setting_value(db, 'live_reply', {}).get('auto_new_customers_since')
    if (not cutoff or not settings.outbound_enabled or not global_message_sending_enabled(db)
            or conversation.inbox.chatwoot_inbox_id != settings.live_reply_inbox_id
            or not reception_conversation_allowed(db, conversation.chatwoot_conversation_id)
            or not conversation.inbox.ai_enabled or not db.get(Tenant, conversation.tenant_id).ai_enabled
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
                or remote.get('inbox_id') != settings.live_reply_inbox_id or remote.get('can_reply') is False):
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
        row.messages = [{'id': f'cw-{m.chatwoot_message_id}', 'direction': m.direction, 'content': m.content,
                         'status': 'submitted' if m.direction == 'outgoing' else m.status, 'created_at': m.created_at}
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
    service.add_message(db, row, message.content or '[客户发送附件]', f'cw-{message.chatwoot_message_id}', message.content_type)
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
    if row.controls.get('simulation', {}).get('status') != 'running':
        return
    drafts = [m for m in row.messages if m.get('status') == 'draft']
    if not drafts:
        if value.get('handoff') and not value.get('handoff_created'):
            handoff(db, row, value.get('handoff_reason') or 'customer_request',
                    str(row.memory) + '\n' + value.get('handoff_reason', ''))
            value = service.state(row)
            value['handoff_created'] = True
            service.save(row, value)
            db.commit()
        return
    if not value.get('delivery_due_at') or dt(value['delivery_due_at']) > dt(row.virtual_now):
        return
    conversation = db.get(ConversationState, row.conversation_state_id)
    if not permitted(db, conversation):
        cancel(db, conversation.id, 'reception_paused')
        db.commit()
        return
    last = conversation.last_customer_message_at
    if not last or dt(utcnow()) - dt(message_timestamp(last)) >= timedelta(hours=23, minutes=55):
        handoff(db, row, 'channel_window_closed', '渠道回复窗口已结束，请人工跟进。')
        service.control(row, 'stop')
        db.commit()
        return
    part = drafts[0]
    key = f'live-v3:{row.id}:{part["id"]}'
    existing = db.scalar(select(OutboundMessage).where(OutboundMessage.idempotency_key == key))
    if existing:
        handoff(db, row, 'delivery_unknown', '请核对上一条消息实际是否送达。')
        service.control(row, 'stop')
        db.commit()
        return
    connection = db.scalar(select(ChatwootConnection).where(ChatwootConnection.tenant_id == conversation.tenant_id))
    client = client_for(connection)
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
        media = db.get(StoredMedia, part.get('media_id')) if part.get('media_id') else None
        out = OutboundMessage(conversation_state_id=conversation.id, idempotency_key=key,
            source_type='ai', source_id=row.id, content=part.get('content',''), content_type=part.get('content_type','text'),
            status='submission_unknown', media_id=media.id if media else None,
            content_attributes=part.get('content_attributes',{}))
        db.add(out)
        row.messages = [{**m, 'status':'submitting'} if m.get('id') == part['id'] else m for m in row.messages]
        db.commit()
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
    except Exception:
        db.rollback()
        db.refresh(row)
        handoff(db, row, 'delivery_failed', '消息提交未完成，请核对渠道记录；系统不会自动重发。')
        service.control(row, 'stop')
        db.commit()
        raise
    finally:
        client.close()
