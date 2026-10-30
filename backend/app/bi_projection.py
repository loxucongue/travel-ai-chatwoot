"""Project existing, evidence-backed business records without model/API calls."""
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
import re

from sqlalchemy import select, func, case
from app.automation_models import AutomationRun, AutomationSession
from app.bi_models import BiConversation, BiEvent, BiRefresh
from app.models import (ConversationState, ConversationLabelAddition, HandoffTask,
                        LabelEvent, MessageEvent, OutboundMessage, WebhookEvent, ChatwootConnection, utcnow)
from app.operations import setting_value

ACCEPTED = {'submitted', 'sent', 'delivered', 'read'}
STOPPED = {'failed', 'cancelled', 'draft', 'planned', 'unknown', 'submission_unknown'}
ADDED_LABELS = {'加私訊', '加私讯', '已加私訊', '已加私讯'}


def stamp(value):
    try:
        d = datetime.fromtimestamp(value, timezone.utc) if isinstance(value, (int, float)) else datetime.fromisoformat(value.replace('Z', '+00:00'))
        return d.replace(tzinfo=d.tzinfo or timezone.utc).astimezone(timezone.utc).isoformat()
    except (ValueError, TypeError, AttributeError, OverflowError):
        return None


def note_birth(db, conversation, raw):
    """Keep source creation time even if an import/label update arrives later."""
    at = stamp(raw)
    if not at:
        return
    row = db.get(BiConversation, conversation.id)
    if not row:
        row = BiConversation(conversation_state_id=conversation.id)
        db.add(row)
    if not row.source_created_at or at < row.source_created_at:
        row.source_created_at, row.signature = at, ''


def note_added_labels(db, conversation, previous, current, event):
    """Record observed label transitions even when legacy SOP side effects are off.

    Receipt time is explicit evidence of observation, never a guessed historic
    addition time. Call only for an existing conversation and accepted update.
    """
    for label in (set(current or []) - set(previous or [])) & ADDED_LABELS:
        key = f'{conversation.id}:observed-label:{event.id}:{label}'
        if not db.get(BiEvent, key):
            db.add(BiEvent(key=key, conversation_state_id=conversation.id, kind='added',
                occurred_at=stamp(event.received_at) or utcnow(), source='label_observed',
                evidence={'label':label, 'webhook_id':event.id, 'observed_at_receipt':True}))


def key_text(text):
    from app.engagement_labels import route_title_key
    return route_title_key(text)


def entry_choices(db):
    from app.route_packages import ROUTES, ensure_route_packages_current
    from app.engagement_labels import ENTRY_OPTIONS
    ensure_route_packages_current()
    # Include disabled/archived catalog routes when interpreting old clicks.
    return {key_text(s) for key,r in ROUTES.items() for s in
            [key, r['name'], r.get('selection_title',''), r.get('default_entry_message',''),
             *r.get('selection_aliases', [])]} | {key_text(s) for s in ENTRY_OPTIONS}


def option(text, choices):
    value = re.sub(r'^\d{1,2}[~～–-]\d{1,2}月[|｜]', '', (text or '').strip())
    return bool(value) and key_text(value) in choices


def acknowledgment(text):
    value = re.sub(r'[\s，。！!～~、,.?？]', '', text or '').lower()
    return not value or bool(re.fullmatch(r'(?:謝謝|谢谢|感謝|感谢|收到|好的?|嗯+|恩+|ok|了解|👍|🙏|😊|讚|赞)+', value))


def project(db, conversation, session, messages, labels, additions, tasks, outbounds, runs, choices, mappings):
    cid = conversation.id
    row = db.get(BiConversation, cid)
    if not row:
        row = BiConversation(conversation_state_id=cid)
        db.add(row)
    controls = session.controls if session else {}
    state = controls.get('v3', {})
    snapshot = {'route': state.get('route_variant') or controls.get('route_variant', ''), 'next_followup_at': stamp(state.get('next_check_at')),
                'followup_stop_reason': state.get('followup_skip_reason') or state.get('last_reason', ''),
                'human_followup': bool(controls.get('human_followup', {}).get('enabled')),
                'contact_status': (session.memory or {}).get('contact_status') if session else None}
    # The scheduler increments revision while advancing its clock, even with no
    # business changes. Hash only fields the projection actually consumes.
    part_keys = ('id','direction','private','contact_invitation','v3_kind','proactive','plan_id',
                 'run_id','human_followup','chatwoot_message_id','client_key')
    parts = [[p.get(k) for k in part_keys] for p in (session.messages or [])] if session else []
    signature = hashlib.sha256(json.dumps([
        4, conversation.updated_at, conversation.version, row.source_created_at,
        snapshot, parts,
        [(m.id, m.status, m.attribution) for m in messages], [(l.id, l.action) for l in labels],
        [(a.label, a.source_message_id, a.status) for a in additions],
        [(t.id, t.version, t.status) for t in tasks], [(o.id, o.status) for o in outbounds],
        [(r.id, r.status) for r in runs], mappings,
    ], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    if row.signature == signature:
        return False
    existing = {e.key: e for e in db.scalars(select(BiEvent).where(BiEvent.conversation_state_id == cid))}

    def emit(key, kind, at, source, **evidence):
        at = stamp(at)
        if not at:
            return
        key = f'{cid}:{key}'
        e = existing.get(key)
        if not e:
            e = BiEvent(key=key, conversation_state_id=cid)
            db.add(e)
            existing[key] = e
        e.kind, e.occurred_at, e.source, e.evidence = kind, at, source, evidence

    messages = sorted(messages, key=lambda m:(m.created_at,m.id))
    incoming = [m for m in messages if m.direction == 'incoming' and not m.private]
    by_remote = {str(m.chatwoot_message_id): m for m in messages}
    by_id = {m.id: m for m in messages}
    first = min((stamp(m.created_at) for m in incoming if stamp(m.created_at)), default=None)
    born = min(filter(None, [first, row.source_created_at]), default=None)
    if born:
        emit('entry', 'new', born, 'chatwoot_created' if row.source_created_at else 'first_observed_message', historical_incomplete=not bool(row.source_created_at))
    option_values = set(choices)
    for m in messages:
        if m.private:
            continue
        mid = m.chatwoot_message_id
        if m.direction == 'incoming':
            emit(f'm:{mid}', 'incoming', m.created_at, 'message', message_id=mid,
                 eligible_reply=bool(m.content.strip() or m.attachments) and not option(m.content, option_values),
                 meaningful=not option(m.content, option_values) and not acknowledgment(m.content))
            if (m.content.strip() or m.attachments) and not option(m.content, option_values):
                emit(f'engaged:{mid}', 'engaged', m.created_at, 'message_rule', message_id=mid)
        elif m.direction == 'outgoing':
            kind = 'ai' if m.attribution == 'ai' else 'human' if m.attribution in ('human', 'inferred_human') else 'unattributed'
            emit(f'm:{mid}', kind, m.created_at, 'message', message_id=mid, status=m.status or 'unknown', inferred=m.attribution == 'inferred_human')
        if m.content_type == 'input_select':
            option_values.update(key_text(x.get(k, '')) for x in (m.content_attributes or {}).get('items', []) for k in ('title', 'value'))
    # Engagement is source-message evidence, not the current presence of a tag.
    for a in additions:
        m = by_id.get(a.source_message_id)
        if a.label == '以開口' and m and not m.private and m.direction == 'incoming' and not option(m.content, option_values):
            emit(f'engaged:{m.chatwoot_message_id}', 'engaged', m.created_at, 'business_reply', message_id=m.chatwoot_message_id)
    for l in labels:
        if l.action != 'added':
            continue
        kind = ('captured' if l.label in mappings['lead_labels'] else
                'added' if l.label in ('加私訊', '加私讯', '已加私訊', '已加私讯') else
                'converted' if l.label in mappings['conversion_labels'] else None)
        if kind:
            emit(f'label:{l.id}', kind, l.created_at, 'business_label', label=l.label)
    human_times = [stamp(m.created_at) for m in messages if not m.private and m.direction == 'outgoing'
                   and m.attribution in ('human', 'inferred_human') and m.status not in STOPPED]
    for t in tasks:
        next_task = min((stamp(x.created_at) for x in tasks if x.created_at > t.created_at), default=None)
        until = min(filter(None,[next_task, stamp(t.completed_at)]),default=None)
        answered = min((at for at in human_times if at and at >= stamp(t.created_at) and (not until or at <= until)), default=None)
        emit(f'handoff:{t.id}', 'handoff', t.created_at, 'handoff_task', reason=t.reason_code,
             task_id=t.id, status=t.status, assigned=bool(t.chatwoot_assignee_id or t.assignee_user_id or t.assignment_target_agent_id),
             answered_at=answered, sla_due_at=stamp(t.sla_due_at))
        if t.reason_code in ('contact', 'contact_reported', 'contact_image'):
            emit(f'contact:{t.id}', {'contact': 'captured', 'contact_reported': 'reported_added', 'contact_image': 'pending_contact'}[t.reason_code],
                 t.created_at, 'handoff_evidence', task_id=t.id)
    if session:
        lookup = {o.idempotency_key: o for o in outbounds}
        plans = defaultdict(list)
        for part in session.messages or []:
            if part.get('direction') != 'outgoing' or part.get('private'):
                continue
            outbound = lookup.get(f'live-v3:{session.id}:{part.get("id")}')
            if not outbound:
                continue  # Drafts, sandbox, imported team messages have no live outbox evidence.
            status = outbound.status
            at = outbound.submitted_at or outbound.created_at
            evidence = {'message_id': outbound.chatwoot_message_id, 'status': status, 'outbound_id': outbound.id}
            # Outbox attribution is authoritative even if a receipt mirror has
            # not arrived yet. Reuse the mirror key to avoid double counting.
            emit(f'm:{outbound.chatwoot_message_id}' if outbound.chatwoot_message_id else f'out:{outbound.id}',
                 'ai', at, 'live_outbox', **evidence)
            if part.get('contact_invitation'):
                emit(f'invite:{outbound.id}', 'invited', at, 'live_outbox', **evidence)
            if part.get('v3_kind') == 'introduction':
                emit(f'intro:{outbound.id}', 'introduced', at, 'live_outbox', **evidence)
            from app.reception_v3.seasonal_quotes import contains_trip_quote
            if contains_trip_quote(outbound.content):
                emit(f'quote:{outbound.id}', 'quoted', at, 'live_outbox', **evidence)
            if part.get('proactive') and status not in ('planned', 'draft', 'cancelled'):
                plans[part.get('plan_id') or part['id']].append((part, outbound))
        for plan, parts in plans.items():
            ats = [o.submitted_at or o.created_at for _, o in parts]
            statuses = [o.status for _, o in parts]
            status = ('failed' if all(s == 'failed' for s in statuses) else 'partial' if 'failed' in statuses else
                      'read' if all(s == 'read' for s in statuses) else 'delivered' if all(s in ('read', 'delivered') for s in statuses) else
                      'submitted' if any(s in ACCEPTED for s in statuses) else 'unknown')
            mode = 'advisor' if any(p.get('human_followup') for p, _ in parts) else 'ordinary'
            # Historical advisor markers are kept on the originating run.
            run_ids = {p.get('run_id') for p, _ in parts}
            if any(run.id in run_ids and getattr(run, '_bi_advisor', False) for run in runs):
                mode = 'advisor'
            emit(f'followup:{plan}', 'followup', min(ats), 'live_plan', plan_id=plan, status=status,
                 mode=mode, message_ids=[o.chatwoot_message_id for _, o in parts if o.chatwoot_message_id],
                 accepted=any(s in ACCEPTED for s in statuses))
        for run in runs:
            if run.status != 'completed':
                continue
            reviewed = (run.decision or {}).get('lead_review') or {}
            questions = reviewed.get('question_message_ids') or []
            proof = []
            for mid in set(map(str, questions)):
                match = by_remote.get(mid.removeprefix('cw-'))
                if not match:
                    p = next((m for m in (session.messages or []) if str(m.get('id')) == mid), {})
                    match = by_remote.get(str(p.get('chatwoot_message_id') or p.get('client_key', '').removeprefix('cw-')))
                if match and match.direction == 'incoming' and not match.private:
                    proof.append(match)
            if len(proof) >= 3:
                emit(f'deep:{run.id}', 'deep', max(m.created_at for m in proof), 'consultation_evidence', message_ids=[m.chatwoot_message_id for m in proof])
    row.snapshot = snapshot
    row.signature, row.refreshed_at = signature, utcnow()
    return True


def backfill_births(db):
    """Recover creation timestamps from retained webhooks, without loading payloads."""
    connections = {c.id:c.tenant_id for c in db.scalars(select(ChatwootConnection))}
    conversations = {(c.tenant_id,c.chatwoot_conversation_id):c for c in db.scalars(select(ConversationState))}
    j = lambda path: func.json_extract(WebhookEvent.payload,path)
    is_conversation = WebhookEvent.event.in_(['conversation_created','conversation_updated','conversation_status_changed'])
    records = db.execute(select(WebhookEvent.connection_id,
        func.coalesce(j('$.conversation.id'),j('$.payload.conversation.id'),
                      case((is_conversation,func.coalesce(j('$.payload.id'),j('$.id'))))).label('cid'),
        func.coalesce(j('$.conversation.created_at'),j('$.payload.conversation.created_at'),
                      case((is_conversation,func.coalesce(j('$.payload.created_at'),j('$.created_at'))))).label('created'))
        .where(WebhookEvent.event.in_(['conversation_created','conversation_updated','conversation_status_changed'])
               | j('$.conversation.id').is_not(None) | j('$.payload.conversation.id').is_not(None)))
    earliest = {}
    for connection, cid, created in records:
        key = (connections.get(connection),cid)
        at = stamp(created)
        if key in conversations and at:
            earliest[key] = min(at,earliest.get(key,at))
    for key,at in earliest.items():
        note_birth(db,conversations[key],at)
    db.flush()
    return len(earliest)


def refresh(db, limit=50):
    """Bounded write batches; no customer messages, tags, controls or API calls."""
    groups = {}
    # Select only needed run columns: input snapshots can be hundreds of KB each.
    from sqlalchemy.orm import load_only
    models = (MessageEvent, LabelEvent, ConversationLabelAddition, HandoffTask, OutboundMessage)
    for model in models:
        grouped = defaultdict(list)
        for item in db.scalars(select(model)):
            grouped[item.conversation_state_id].append(item)
        groups[model] = grouped
    sessions = {s.conversation_state_id: s for s in db.scalars(select(AutomationSession).where(AutomationSession.environment == 'live'))}
    run_groups = defaultdict(list)
    for run, advisor in db.execute(select(AutomationRun, func.json_extract(AutomationRun.input_snapshot, '$.state.human_followup'))
            .where(AutomationRun.session_id.in_([s.id for s in sessions.values()]))
            .options(load_only(AutomationRun.id, AutomationRun.session_id, AutomationRun.status, AutomationRun.decision, AutomationRun.created_at))):
        run._bi_advisor = bool(advisor)
        run_groups[run.session_id].append(run)
    choices = entry_choices(db)
    mappings = setting_value(db, 'label_mappings', {'lead_labels':['已留资'], 'conversion_labels':['已成交']})
    changed = 0
    remaining = 0
    # Older projections go first so busy early conversations cannot starve
    # later ones when a batch reaches its write limit.
    for c in db.scalars(select(ConversationState).outerjoin(BiConversation, BiConversation.conversation_state_id == ConversationState.id)
                       .order_by(BiConversation.refreshed_at.asc().nulls_first(), ConversationState.id)):
        if changed >= limit:
            remaining += 1
            continue
        s = sessions.get(c.id)
        changed += project(db, c, s, *(groups[m][c.id] for m in models), run_groups[s.id] if s else [], choices, mappings)
    status = db.get(BiRefresh, 1) or BiRefresh(id=1)
    status.refreshed_at, status.remaining = utcnow(), remaining
    db.add(status)
    db.commit()
    return {'changed':changed, 'remaining':remaining}


if __name__ == '__main__':
    from app.db import SessionLocal
    with SessionLocal() as db:
        backfill_births(db)
        print(json.dumps(refresh(db, limit=100000)))
