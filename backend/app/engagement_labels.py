"""Track customer replies without granting or removing automatic reception."""
from datetime import timedelta
import unicodedata
import re
from sqlalchemy import select
from app.models import (ConversationLabelAddition, ConversationLabelSync, ConversationState,
                        ChatwootConnection, MessageEvent, LabelEvent, utcnow)
from app.config import settings
from app.chatwoot_service import client_for
from app.runtime_settings import dt

ENGAGED_LABEL = '以開口'
RETURNED_LABEL = '洗單回'
# Existing advertisement entry option, also shown in the October 16 feedback.
ENTRY_OPTIONS = {'我人數有4位以上，想自己包團'}


def normalized(text):
    return ''.join(unicodedata.normalize('NFKC', text or '').split()).casefold()


def route_title_key(text):
    # Advertising titles move separators around (精華-8天 vs 精華8天-).
    # Only punctuation varies: the complete title must still match a catalog
    # choice. Extra question/intent words must remain meaningful engagement.
    return re.sub(r'[-–—・·()|]', '', normalized(text))


def initial_option(db, conversation, message, row):
    from app.reception_v3.skills import compile_skills
    from app.reception_v3.service import state
    text = normalized(message.content)
    if not text:
        return False
    value = state(row) if row else {}
    # A late click of the still displayed intake options is also an initial option.
    question = db.scalar(select(MessageEvent).where(
        MessageEvent.conversation_state_id == conversation.id,
        MessageEvent.direction == 'outgoing', MessageEvent.private.is_(False),
        MessageEvent.content_type == 'input_select',
        MessageEvent.chatwoot_message_id < message.chatwoot_message_id
    ).order_by(MessageEvent.chatwoot_message_id.desc()).limit(1))
    if question:
        answered = db.scalar(select(MessageEvent.id).where(
            MessageEvent.conversation_state_id == conversation.id, MessageEvent.direction == 'incoming',
            MessageEvent.private.is_(False), MessageEvent.chatwoot_message_id > question.chatwoot_message_id,
            MessageEvent.chatwoot_message_id < message.chatwoot_message_id).limit(1))
        if not answered:
            items = (question.content_attributes or {}).get('items', [])
            if text in {normalized(item.get(k)) for item in items for k in ('title', 'value')}:
                return True
    if value.get('introduction_route') or value.get('completed_introductions'):
        return False
    bundle = compile_skills(db)
    choices = list(ENTRY_OPTIONS)
    for key, route in bundle['routes'].items():
        choices.extend([key, route['name'], *route.get('aliases', [])])
    if not row or value.get('intake_status') in (None, 'sending', 'waiting'):
        intake = {**bundle.get('intake', {}), **(bundle['routes'].get((row.controls or {}).get('route_variant'), {}).get('intake') or {})} if row else bundle.get('intake', {})
        choices.extend(intake.get('options', []))
    # Ads prefix the same route title with e.g. "3~4月｜"; compare the whole
    # remaining title, never classify arbitrary mentions of a route as a click.
    route_text = re.sub(r'^\d{1,2}[~～–-]\d{1,2}月[|｜]', '', text)
    return text in {normalized(option) for option in choices} or route_title_key(route_text) in {
        route_title_key(option) for route in bundle['routes'].values()
        for option in [route['name'], *route.get('aliases', [])]}


def observe_followup_return(db, conversation, message):
    from app.reception_v3.live import permitted, session_for
    from app.reception_v3.service import followup_response
    row = session_for(db, conversation.id)
    if (message.direction != 'incoming' or message.private or not row
            or not ((message.content or '').strip() or message.attachments)
            or RETURNED_LABEL in (conversation.labels or [])
            or db.get(ConversationLabelAddition, (conversation.id, RETURNED_LABEL))
            or not permitted(db, conversation, purpose='followup')
            or not followup_response(row.messages)):
        return
    db.add(ConversationLabelAddition(conversation_state_id=conversation.id,
        label=RETURNED_LABEL, source_message_id=message.id))
    db.flush()


def observe_reply(db, conversation, message):
    observe_followup_return(db, conversation, message)
    from app.reception_v3.live import permitted, session_for
    key = (conversation.id, ENGAGED_LABEL)
    row = session_for(db, conversation.id)
    if (message.direction != 'incoming' or message.private
            or db.get(ConversationLabelAddition, key) or ENGAGED_LABEL in (conversation.labels or [])
            or row and row.controls.get('simulation', {}).get('status') != 'running'
            or not permitted(db, conversation, purpose='followup')
            or not ((message.content or '').strip() or message.attachments)):
        return
    if initial_option(db, conversation, message, row):
        return
    db.add(ConversationLabelAddition(conversation_state_id=conversation.id,
        label=ENGAGED_LABEL, source_message_id=message.id))
    db.flush()


def pending_labels(db, conversation_id):
    return list(db.scalars(select(ConversationLabelAddition.label).where(
        ConversationLabelAddition.conversation_state_id == conversation_id,
        ConversationLabelAddition.status == 'pending')))


def ownership(db, cid):
    job = db.get(ConversationLabelSync, cid, populate_existing=True)
    return (job.revision, job.status) if job else (None, None)


def remote_labels(client, public_id):
    labels = client.get_conversation_labels(public_id)
    labels = labels.get('payload') if isinstance(labels, dict) else labels
    if not isinstance(labels, list):
        raise ValueError('invalid_remote_labels')
    return labels


def process_label_addition(session_factory):
    if not settings.outbound_enabled:
        return False
    with session_factory() as db:
        job = db.scalar(select(ConversationLabelAddition).where(
            ConversationLabelAddition.status == 'pending', ConversationLabelAddition.due_at <= utcnow()
        ).order_by(ConversationLabelAddition.due_at).limit(1))
        if not job:
            return False
        cid, label = job.conversation_state_id, job.label
        owner = ownership(db, cid)
        if owner[1] == 'pending':
            job.due_at = (dt(utcnow()) + timedelta(seconds=2)).isoformat()
            db.commit()
            return True
        conversation = db.get(ConversationState, cid)
        connection = db.scalar(select(ChatwootConnection).where(ChatwootConnection.tenant_id == conversation.tenant_id))
        if not connection:
            return False
        client = client_for(connection)
        try:
            catalog = client.list_labels()
            catalog = catalog.get('payload', []) if isinstance(catalog, dict) else catalog
            if label not in {x.get('title') for x in catalog}:
                description = ('客戶在已發送的沉默跟進後首次回覆' if label == '洗單回'
                               else '客戶在 AI 接待期間主動回覆，開場預設選項除外')
                client.create_label(label, description, '#0891B2', True)
            current = remote_labels(client, conversation.chatwoot_conversation_id)
            if ownership(db, cid) != owner:
                return True
            if label not in current:
                client.set_conversation_labels(conversation.chatwoot_conversation_id, [*current, label])
            confirmed = remote_labels(client, conversation.chatwoot_conversation_id)
            if ownership(db, cid) != owner:
                # A handoff won during HTTP: reconcile that newer control intent.
                control = db.get(ConversationLabelSync, cid)
                if control:
                    control.status, control.due_at = 'pending', utcnow()
                    db.get(ConversationState, cid).ai_sync_status = 'pending'
                    db.commit()
                return True
            if label not in confirmed:
                raise ValueError('business_label_not_confirmed')
            db.refresh(conversation)
            if label not in (conversation.labels or []):
                conversation.labels = [*(conversation.labels or []), label]
                db.add(LabelEvent(conversation_state_id=cid, label=label, action='added'))
            job.status, job.error, job.updated_at = 'synced', None, utcnow()
            db.commit()
        except Exception as exc:
            db.rollback()
            job = db.get(ConversationLabelAddition, (cid, label))
            job.attempts += 1
            job.error = type(exc).__name__ + ':' + str(exc)[:120]
            job.due_at = (dt(utcnow()) + timedelta(seconds=min(300, 2 ** min(job.attempts, 8)))).isoformat()
            db.commit()
        finally:
            client.close()
    return True
