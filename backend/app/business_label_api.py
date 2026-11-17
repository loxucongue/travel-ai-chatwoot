"""Scoped label maintenance, corrections and explicit historical previews."""
import hashlib
import json
from collections import Counter
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy import select, update, func
from app.auth import current_user, require_csrf, require_super_admin_csrf
from app.db import get_db
from app.models import ConversationState, MessageEvent, ConversationLabelAddition, AppSetting, utcnow
from app.operations import allowed_inbox_ids, can_access_conversation, audit
from app.business_label_config import KEY, ENGAGED, LEGACY_ENGAGED, LabelConfig, configuration
from app.business_label_models import BusinessLabelJob, BusinessLabelState
from app.business_labels import state_for, enqueue, apply_result, desired_labels, state_signature

router = APIRouter(prefix='/v1/business-labels')


def conversation_for(db, user, public_id):
    c = db.scalar(select(ConversationState).where(ConversationState.chatwoot_conversation_id == public_id))
    if not c or not can_access_conversation(db, user, c): raise HTTPException(404, '会话不存在或无权访问')
    return c


def serialize(db, c):
    row = db.get(BusinessLabelState, c.id)
    config = configuration(db)
    return {'conversation_id': c.chatwoot_conversation_id, 'labels': c.labels,
            'revision': row.revision if row else 0, 'consulted': row.consulted if row else [],
            'current': [i for i in row.current if row.overrides.get(i, {}).get('mode') != 'exclude'] if row else [],
            'rejected': [i for i in row.rejected if row.overrides.get(i, {}).get('mode') != 'exclude'] if row else [],
            'evidence': row.evidence if row else [], 'overrides': row.overrides if row else {},
            'effective_labels': desired_labels(row, config) if row else [],
            'sync_status': row.sync_status if row else 'not_analyzed', 'sync_error': row.sync_error if row else None,
            'jobs': [{'id': j.id, 'status': j.status, 'mode': j.mode, 'error': j.error, 'result': j.result,
                      'updated_at': j.updated_at, 'config_version': j.config_version}
                     for j in db.scalars(select(BusinessLabelJob).where(BusinessLabelJob.conversation_state_id == c.id)
                        .order_by(BusinessLabelJob.id.desc()).limit(10))]}


@router.get('/config')
def get_config(db=Depends(get_db), user=Depends(current_user)):
    return configuration(db)


@router.put('/config')
def save_config(payload: LabelConfig, db=Depends(get_db), user=Depends(require_super_admin_csrf)):
    current = configuration(db)
    value = payload.model_dump()
    if value['version'] != current['version']: raise HTTPException(409, '配置已改变，请重新加载')
    if not {c['id'] for c in current['categories']} <= {c['id'] for c in value['categories']}:
        raise HTTPException(422, '已有分类编号不可删除或改名；请停用分类，以保留历史证据')
    value['version'] += 1
    setting = db.get(AppSetting, KEY)
    if setting:
        result = db.execute(update(AppSetting).where(AppSetting.key == KEY,
            AppSetting.value['version'].as_integer() == current['version']).values(value=value))
        if not result.rowcount: raise HTTPException(409, '配置已改变，请重新加载')
    else:
        db.add(AppSetting(key=KEY, value=value))
    for row in db.scalars(select(BusinessLabelState)):
        row.sync_status, row.due_at = 'pending', utcnow()
    # Requeue unprocessed messages with the new taxonomy, preserving old audit jobs.
    pending = list(db.scalars(select(BusinessLabelJob).where(BusinessLabelJob.status.in_(['pending','processing']))))
    db.flush()
    for job in pending:
        job.status = 'obsolete'
        existing = db.scalar(select(BusinessLabelJob.id).where(BusinessLabelJob.conversation_state_id == job.conversation_state_id,
            BusinessLabelJob.message_id == job.message_id, BusinessLabelJob.config_version == value['version'], BusinessLabelJob.mode == job.mode))
        if value['enabled'] and not existing:
            db.add(BusinessLabelJob(conversation_state_id=job.conversation_state_id, message_id=job.message_id,
                                    config_version=value['version'], mode=job.mode))
    audit(db, user, 'business_labels.config', 'setting', KEY, {'version': value['version'], 'config': value})
    db.commit()
    return value


@router.get('/summary')
def summary(db=Depends(get_db), user=Depends(current_user)):
    query = select(ConversationState, BusinessLabelState).join(BusinessLabelState,
                BusinessLabelState.conversation_state_id == ConversationState.id)
    allowed = allowed_inbox_ids(db, user)
    if allowed is not None: query = query.where(ConversationState.inbox_binding_id.in_(allowed))
    config, counts, unique, pending = configuration(db), Counter(), 0, 0
    for c, row in db.execute(query):
        labels = desired_labels(row, config)
        counts.update(labels); unique += bool(labels); pending += row.sync_status in ('pending', 'syncing')
    return {'unique_conversations': unique, 'counts': dict(counts), 'pending': pending,
            'note': '咨询历史支持多标签；分类合计可能大于去重会话数。'}


@router.get('/conversations/{public_id}')
def get_conversation(public_id: int, db=Depends(get_db), user=Depends(current_user)):
    return serialize(db, conversation_for(db, user, public_id))


class Correction(BaseModel):
    model_config = ConfigDict(extra='forbid')
    category_id: str
    mode: Literal['include', 'exclude', 'auto']
    reason: str = Field(min_length=1, max_length=500)
    revision: int


@router.patch('/conversations/{public_id}')
def correct(public_id: int, payload: Correction, db=Depends(get_db), user=Depends(require_csrf)):
    c = conversation_for(db, user, public_id)
    existing = db.get(BusinessLabelState, c.id)
    if (existing.revision if existing else 0) != payload.revision: raise HTTPException(409, '结果已改变，请刷新后重试')
    if payload.category_id not in {x['id'] for x in configuration(db)['categories']}: raise HTTPException(422, '分类无效')
    row = state_for(db, c.id)
    overrides = dict(row.overrides)
    if payload.mode == 'auto': overrides.pop(payload.category_id, None)
    else: overrides[payload.category_id] = {'mode': payload.mode, 'reason': payload.reason, 'user_id': user.id, 'at': utcnow()}
    row.overrides, row.sync_status, row.due_at = overrides, 'pending', utcnow()
    audit(db, user, 'business_labels.correct', 'conversation', public_id, payload.model_dump())
    db.commit()
    return serialize(db, c)


class Batch(BaseModel):
    conversation_ids: list[int] = Field(min_length=1, max_length=50)


@router.post('/history/preview')
def preview(payload: Batch, db=Depends(get_db), user=Depends(require_csrf)):
    if not configuration(db)['enabled']: raise HTTPException(409, '请先开启业务标签')
    conversations = [conversation_for(db, user, i) for i in sorted(set(payload.conversation_ids))]
    items = []
    for c in conversations:
        message = db.scalar(select(MessageEvent).where(MessageEvent.conversation_state_id == c.id,
            MessageEvent.direction == 'incoming', MessageEvent.private.is_(False)).order_by(MessageEvent.chatwoot_message_id.desc()).limit(1))
        if message:
            job = enqueue(db, c, message, 'preview')
            items.append({'conversation_id': c.chatwoot_conversation_id, 'job_id': job.id, 'status': job.status})
        else: items.append({'conversation_id': c.chatwoot_conversation_id, 'status': 'no_customer_messages'})
    audit(db, user, 'business_labels.preview', 'conversations', details={'items': items})
    db.commit()
    return {'items': items, 'note': '每个会话分析最近80条公开消息；仅预览，不写标签。'}


@router.post('/history/{job_id}/apply')
def apply_preview(job_id: int, db=Depends(get_db), user=Depends(require_csrf)):
    job = db.get(BusinessLabelJob, job_id)
    c = db.get(ConversationState, job.conversation_state_id) if job else None
    if not c or not can_access_conversation(db, user, c): raise HTTPException(404, '预览不存在')
    if job.status == 'applied': return serialize(db, c)
    row = state_for(db, c.id)
    config = configuration(db)
    latest = db.scalar(select(MessageEvent.id).where(MessageEvent.conversation_state_id == c.id,
        MessageEvent.direction == 'incoming', MessageEvent.private.is_(False)).order_by(MessageEvent.chatwoot_message_id.desc()).limit(1))
    if (not config['enabled'] or job.status != 'preview' or job.config_version != config['version']
            or state_signature(row) != job.input_snapshot['state_signature'] or latest != job.message_id):
        raise HTTPException(409, '预览未就绪或已过期，请重新预览')
    apply_result(db, row, job, job.result)
    job.status = 'applied'
    audit(db, user, 'business_labels.apply', 'job', job.id)
    db.commit()
    return serialize(db, c)


def engagement_preview(db, c):
    from app.engagement_rules import introduction_boundary, eligible
    from app.engagement_labels import initial_option
    session, start = introduction_boundary(db, c.id)
    messages = list(db.scalars(select(MessageEvent).where(MessageEvent.conversation_state_id == c.id,
        MessageEvent.direction == 'incoming', MessageEvent.private.is_(False)).order_by(MessageEvent.id)))
    match = next((m for m in messages if eligible(m, start) and not initial_option(db, c, m, session)), None) if start else None
    additions = {label: db.get(ConversationLabelAddition, (c.id, label)) for label in [ENGAGED, LEGACY_ENGAGED]}
    add = [ENGAGED] if match and ENGAGED not in (c.labels or []) else []
    remove = []
    uncertain = []
    if not start: uncertain.append('没有可核实的介绍发送记录，历史标签保持原样')
    for label, source in additions.items():
        if label not in (c.labels or []): continue
        if not source or source.status != 'synced' or not source.source_message_id:
            uncertain.append(label+' 的系统来源未核实，保留原标签')
        elif start and (label == LEGACY_ENGAGED or not match): remove.append(label)
    result = {'conversation_id': c.chatwoot_conversation_id, 'boundary': start,
              'source_message_id': match.id if match else None, 'add': add, 'remove': remove, 'uncertain': uncertain,
              'revision': c.version, 'latest_message_id': max((m.id for m in messages), default=0)}
    result['token'] = hashlib.sha256(json.dumps([result, c.labels,
        [(k, v.status if v else None, v.source_message_id if v else None) for k,v in additions.items()]], sort_keys=True).encode()).hexdigest()
    return result


@router.post('/engagement/preview')
def preview_engagement(payload: Batch, db=Depends(get_db), user=Depends(require_csrf)):
    return {'items': [engagement_preview(db, conversation_for(db, user, i)) for i in sorted(set(payload.conversation_ids))]}


class Repair(BaseModel):
    conversation_id: int
    token: str


@router.post('/engagement/apply')
def repair_engagement(payload: Repair, db=Depends(get_db), user=Depends(require_csrf)):
    if not configuration(db)['enabled']: raise HTTPException(409, '请先开启业务标签')
    c = conversation_for(db, user, payload.conversation_id)
    preview = engagement_preview(db, c)
    if preview['token'] != payload.token: raise HTTPException(409, '预览已改变，请重新预览')
    row = state_for(db, c.id)
    row.engagement = preview
    row.remove_labels = sorted(set(row.remove_labels) | set(preview['remove']))
    for label in preview['remove']:
        job = db.get(ConversationLabelAddition, (c.id, label))
        if job: job.status = 'retired'
    for label in preview['add']:
        job = db.get(ConversationLabelAddition, (c.id, label)) or ConversationLabelAddition(conversation_state_id=c.id, label=label)
        job.source_message_id, job.status, job.due_at = preview['source_message_id'], 'pending', utcnow()
        db.add(job)
    row.sync_status, row.due_at = 'pending', utcnow()
    audit(db, user, 'business_labels.engagement_repair', 'conversation', payload.conversation_id, preview)
    db.commit()
    return preview
