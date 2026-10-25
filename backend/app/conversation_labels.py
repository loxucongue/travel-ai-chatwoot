"""Retry control labels without sending messages or erasing unrelated labels."""
from datetime import timedelta
from sqlalchemy import select
from app.models import ConversationLabelSync, ConversationState, ChatwootConnection, utcnow
from app.config import settings
from app.chatwoot_service import client_for
from app.conversation_policy import observe_ai_label, compute_state
from app.models import Tenant
from app.runtime_settings import dt

FOLLOWUP_LABEL = 'AI跟进'
CONTROL_LABELS = {'ai', '人工接管', FOLLOWUP_LABEL}


def label_keys(labels):
    controls = {x.casefold() for x in CONTROL_LABELS}
    return {x.casefold() if x.casefold() in controls else x for x in labels}


def desired_labels(labels, target):
    keep = [x for x in labels if str(x).casefold() not in {k.casefold() for k in CONTROL_LABELS}]
    return list(dict.fromkeys(keep + (['人工接管'] if target == 'human' else
                                      ['ai', FOLLOWUP_LABEL] if target == 'followup' else ['ai'])))


def queue_labels(db, conversation, target):
    assert target in ('human', 'followup', 'ai')
    job = db.get(ConversationLabelSync, conversation.id)
    if job and job.target == target and job.status == 'pending':
        return job
    if job and job.target == target and job.status == 'synced' and label_keys(conversation.labels or []) == label_keys(desired_labels(conversation.labels or [], target)):
        return job
    if not job:
        job = ConversationLabelSync(conversation_state_id=conversation.id, target=target)
        db.add(job)
    job.target, job.status, job.attempts = target, 'pending', 0
    job.due_at = job.updated_at = utcnow()
    job.error = None
    conversation.ai_sync_status = 'pending'
    return job


def sync_ready(db, conversation):
    job = db.get(ConversationLabelSync, conversation.id)
    return not job or job.status in ('synced', 'cancelled')


def process_label_sync(session_factory):
    if not settings.outbound_enabled:
        return False
    with session_factory() as db:
        job = db.scalar(select(ConversationLabelSync).where(ConversationLabelSync.status == 'pending',
            ConversationLabelSync.due_at <= utcnow()).order_by(ConversationLabelSync.due_at).limit(1))
        if not job:
            return False
        cid, revision, target = job.conversation_state_id, job.revision, job.target
        conversation = db.get(ConversationState, cid)
        connection = db.scalar(select(ChatwootConnection).where(ChatwootConnection.tenant_id == conversation.tenant_id))
        if not connection:
            return False
        client = client_for(connection)
        try:
            catalog = client.list_labels()
            catalog = catalog.get('payload', []) if isinstance(catalog, dict) else catalog
            titles = {str(x.get('title')).casefold(): str(x.get('title')) for x in catalog}
            required = ['人工接管'] if target == 'human' else ['ai', FOLLOWUP_LABEL] if target == 'followup' else ['ai']
            for label in required:
                if label.casefold() not in titles:
                    created = client.create_label(label, '接待状态自动同步', '#DC2626' if target == 'human' else '#16A34A', True)
                    # Chatwoot normalizes Latin letters in label titles to lowercase.
                    titles[label.casefold()] = (created or {}).get('title', label)
            current = client.get_conversation_labels(conversation.chatwoot_conversation_id)
            current = current.get('payload') if isinstance(current, dict) else current
            if not isinstance(current, list):
                raise ValueError('invalid_remote_labels')
            from app.chatwoot_service import payload_dict
            from app.conversation_mirror import control_revision
            remote = payload_dict(client.get_conversation(conversation.chatwoot_conversation_id))
            db.refresh(job)
            db.refresh(conversation)
            if job.revision != revision or job.status != 'pending':
                return True
            # An operator may add a stop label after the timer fires, before
            # its webhook arrives. Recheck remote labels before restoring AI.
            from app.runtime_settings import blocking_labels
            from app.reception_rollout import reception_inbox_allowed
            stop_labels = (set(current) | set(conversation.contact.labels if conversation.contact else [])) & (blocking_labels(db) - {'人工接管'})
            if target != 'human' and (stop_labels or not conversation.inbox.ai_enabled
                                      or (settings.app_profile == 'live_reply' and
                                          not reception_inbox_allowed(db, conversation.inbox.chatwoot_inbox_id))
                                      or not db.get(Tenant, conversation.tenant_id).ai_enabled):
                job.status, job.error, job.updated_at = 'cancelled', 'reception_blocked', utcnow()
                conversation.labels = current
                observe_ai_label(conversation, current, 'chatwoot')
                from app.reception_v3.live import cancel, session_for
                cancel(db, cid, 'label_paused' if stop_labels else 'reception_paused')
                row = session_for(db, cid)
                if row:
                    row.controls = {**row.controls, 'auto_followup_paused': True,
                                    'human_followup': {'enabled': False}}
                db.commit()
                return True
            controls = {x.casefold() for x in CONTROL_LABELS}
            current_controls = label_keys([x for x in current if x.casefold() in controls])
            local_controls = label_keys([x for x in conversation.labels or [] if x.casefold() in controls])
            desired_controls = label_keys([x for x in desired_labels(current,target) if x.casefold() in controls])
            if (current_controls != local_controls and current_controls != desired_controls
                    and control_revision(remote) >= (conversation.chatwoot_control_updated_at or 0)):
                previous = list(conversation.labels or [])
                conversation.labels = current
                conversation.chatwoot_control_updated_at = control_revision(remote)
                observe_ai_label(conversation,current,'chatwoot')
                from app.reception_v3.live import apply_label_change
                apply_label_change(db,conversation,previous)
                db.commit()
                return True
            from app.engagement_labels import pending_labels
            final = desired_labels(list(dict.fromkeys(current + pending_labels(db, cid))), target)
            final = [titles.get(x.casefold(), x) if x.casefold() in {k.casefold() for k in CONTROL_LABELS} else x for x in final]
            db.refresh(job)
            if job.revision != revision:
                return True
            if label_keys(current) != label_keys(final):
                client.set_conversation_labels(conversation.chatwoot_conversation_id, final)
            confirmed = client.get_conversation_labels(conversation.chatwoot_conversation_id)
            confirmed = confirmed.get('payload') if isinstance(confirmed, dict) else confirmed
            if not isinstance(confirmed, list) or label_keys(confirmed) != label_keys(final):
                raise ValueError('control_labels_not_confirmed')
            remote = payload_dict(client.get_conversation(conversation.chatwoot_conversation_id))
            db.refresh(job)
            if job.revision != revision:
                # A new owner won while HTTP was in flight. Reconcile its intent.
                job.status, job.due_at = 'pending', utcnow()
                db.commit()
                return True
            db.refresh(conversation)
            conversation.chatwoot_control_updated_at = max(conversation.chatwoot_control_updated_at or 0, control_revision(remote))
            conversation.labels = confirmed
            conversation.ai_mode = 'disabled' if target == 'human' else 'enabled'
            observe_ai_label(conversation, confirmed, 'platform')
            from app.reception_v3.live import runtime_state, session_for
            state, reason = compute_state(db.get(Tenant, conversation.tenant_id), conversation.inbox,
                confirmed, conversation.contact.labels if conversation.contact else [], conversation.can_reply,
                conversation.ai_mode, conversation.ai_sync_status, conversation.ai_label_present)
            conversation.effective_ai_state, conversation.effective_state_reason = runtime_state(session_for(db, cid), state, reason)
            conversation.version += 1
            job.status, job.error, job.updated_at = 'synced', None, utcnow()
            db.commit()
        except Exception as exc:
            db.rollback()
            job = db.get(ConversationLabelSync, cid)
            if job and job.revision == revision:
                job.attempts += 1
                job.error = type(exc).__name__ + ':' + str(exc)[:120]
                job.due_at = (dt(utcnow()) + timedelta(seconds=min(300, 2 ** min(job.attempts, 8)))).isoformat()
                db.get(ConversationState, cid).ai_sync_status = 'pending'
                db.commit()
        finally:
            client.close()
    return True
