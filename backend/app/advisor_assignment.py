"""Event routing. Choose once in the DB transaction, synchronize separately."""
from datetime import datetime, timedelta, timezone
from typing import Literal

from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select, update, or_, and_
from sqlalchemy.dialects.sqlite import insert

from app.models import (AdvisorAssignment, AdvisorRotation, ChatwootAgent, ChatwootConnection,
                        ConversationState, HandoffTask, MessageEvent, NotificationDelivery, utcnow)
from app.operations import setting_value, create_notification

EVENTS = {
    'handoff.created': '任意转人工',
    'customer.first_message': '新客户首次进入', 'customer.message': '客户新消息',
    'customer.first_response': '客户首次回应接待', 'customer.returned': '客户沉默后回复',
    'customer.profile': '人数或行程天数已确认',
    'handoff.contact': '收到联系方式', 'handoff.contact_image': '收到联系图片待确认',
    'handoff.contact_reported': '客户自报已加好友', 'handoff.requested': '客户要求真人',
    'handoff.party_size': '人数规则转人工', 'handoff.trip_days': '行程天数转人工',
    'handoff.custom': '定制或目录外需求', 'handoff.knowledge': '问题需要人工核实',
    'handoff.system': '系统或发送失败', 'handoff.other': '其他转人工',
    'handoff.overdue': '人工接单超时',
}


class Advisor(BaseModel):
    agent_id: int = Field(gt=0)
    accepting: bool = True


class Rule(BaseModel):
    id: str = Field(min_length=1, max_length=80, pattern=r'^[a-zA-Z0-9_-]+$')
    name: str = Field(min_length=1, max_length=120)
    enabled: bool = True
    event: str
    priority: int = Field(default=0, ge=0, le=999)
    inbox_ids: list[int] = Field(default_factory=list)
    routes: list[str] = Field(default_factory=list)
    minimum_people: int | None = Field(default=None, ge=1, le=1000)
    minimum_days: int | None = Field(default=None, ge=1, le=365)
    action: Literal['assign', 'handoff', 'notify'] = 'handoff'
    strategy: Literal['fixed', 'round_robin'] = 'fixed'
    agent_ids: list[int] = Field(min_length=1, max_length=100)

    @model_validator(mode='after')
    def validate_rule(self):
        if self.event not in EVENTS:
            raise ValueError('未知事件类型')
        if len(set(self.agent_ids)) != len(self.agent_ids) or any(a <= 0 for a in self.agent_ids):
            raise ValueError('顾问列表重复或无效')
        if self.strategy == 'fixed' and len(self.agent_ids) != 1:
            raise ValueError('固定分配只能选择一位顾问')
        if self.event == 'customer.profile' and self.minimum_people is None and self.minimum_days is None:
            raise ValueError('资料事件至少设置人数或天数条件')
        return self


class AssignmentConfig(BaseModel):
    enabled: bool = False
    advisors: list[Advisor] = Field(default_factory=list)
    rules: list[Rule] = Field(default_factory=list, max_length=100)
    fallback_agent_id: int | None = Field(default=None, gt=0)

    @model_validator(mode='after')
    def distinct(self):
        if len({a.agent_id for a in self.advisors}) != len(self.advisors):
            raise ValueError('顾问重复')
        if len({r.id for r in self.rules}) != len(self.rules):
            raise ValueError('规则编号重复')
        return self


def configuration(db):
    return AssignmentConfig.model_validate(setting_value(db, 'advisor_assignment', {}))


def choose_rule(config, events, conversation, payload):
    def meets(value, minimum):
        return minimum is None or isinstance(value, (int, float)) and not isinstance(value, bool) and value >= minimum
    return next((r for r in sorted(config.rules, key=lambda r: -r.priority)
                 if r.enabled and r.event in events
                 and (not r.inbox_ids or conversation.inbox.chatwoot_inbox_id in r.inbox_ids)
                 and (not r.routes or payload.get('route_variant') in r.routes)
                 and meets(payload.get('party_size') if payload.get('party_size') is not None else payload.get('party_size_min'), r.minimum_people)
                 and meets(payload.get('trip_days'), r.minimum_days)), None)


def eligible_agents(db, conversation, config, ids):
    accepting = {a.agent_id for a in config.advisors if a.accepting}
    agents = db.scalars(select(ChatwootAgent).where(ChatwootAgent.tenant_id == conversation.tenant_id)).all()
    accessible = {a.chatwoot_agent_id for a in agents if conversation.inbox.chatwoot_inbox_id in (a.inbox_ids or [])}
    return [a for a in ids if a in accepting and a in accessible]


def next_agent(db, conversation, rule, candidates):
    if rule.strategy != 'round_robin':
        return candidates[0]
    key = f'{conversation.tenant_id}:{rule.id}'
    db.execute(insert(AdvisorRotation).values(key=key, position=0).on_conflict_do_nothing(index_elements=['key']))
    position = db.execute(update(AdvisorRotation).where(AdvisorRotation.key == key)
                         .values(position=AdvisorRotation.position+1).returning(AdvisorRotation.position)).scalar_one()
    return candidates[(position-1) % len(candidates)]


def emit(db, conversation, events, event_key, payload=None):
    config = configuration(db)
    if not config.enabled:
        return None
    payload = payload or {}
    # The unique insert also serializes round-robin cursor changes on SQLite.
    inserted = db.execute(insert(AdvisorAssignment).values(
        conversation_state_id=conversation.id, event_key=event_key, event_type=events[0],
        payload=payload, rule_name='', action='notify', status='unmatched', attempts=0,
        available_at=utcnow(), created_at=utcnow(), updated_at=utcnow()
    ).on_conflict_do_nothing(index_elements=['conversation_state_id','event_key']).returning(AdvisorAssignment.id)).scalar()
    if inserted is None:
        return db.scalar(select(AdvisorAssignment).where(AdvisorAssignment.conversation_state_id == conversation.id,
                                                        AdvisorAssignment.event_key == event_key))
    record = db.get(AdvisorAssignment, inserted)
    rule = choose_rule(config, events, conversation, payload)
    if not rule:
        return record
    record.rule_id, record.rule_name, record.event_type, record.action = rule.id, rule.name, rule.event, rule.action
    owner = conversation.assignee_id
    # Keep the same customer's existing owner in this inbox.
    if not owner and conversation.contact_id:
        owner = db.scalar(select(ConversationState.assignee_id).where(
            ConversationState.tenant_id == conversation.tenant_id,
            ConversationState.inbox_binding_id == conversation.inbox_binding_id,
            ConversationState.contact_id == conversation.contact_id,
            ConversationState.assignee_id.is_not(None)).order_by(ConversationState.updated_at.desc()).limit(1))
    if not owner:
        # A queued assignment is already a reservation; subsequent events must
        # not consume another turn while the HTTP synchronization is pending.
        owner = db.scalar(select(AdvisorAssignment.agent_id).where(
            AdvisorAssignment.conversation_state_id == conversation.id, AdvisorAssignment.id != record.id,
            AdvisorAssignment.action != 'notify', AdvisorAssignment.agent_id.is_not(None),
            AdvisorAssignment.status.in_(['pending','processing','retry','synced'])
        ).order_by(AdvisorAssignment.id).limit(1))
    candidates = eligible_agents(db, conversation, config, rule.agent_ids)
    if rule.action == 'notify':
        owner = None  # A notification rule chooses its recipient, not a new owner.
    if owner:
        record.agent_id = owner
    elif candidates:
        record.agent_id = next_agent(db, conversation, rule, candidates)
    elif config.fallback_agent_id and eligible_agents(db, conversation, config, [config.fallback_agent_id]):
        record.agent_id = config.fallback_agent_id
    record.status = 'pending' if record.agent_id else 'unassigned'
    if rule.action == 'handoff':
        from app.operations import ensure_handoff
        from app.reception_v3.live import cancel, session_for
        reason = 'trip_days' if rule.minimum_days else 'party_size' if rule.minimum_people else rule.event
        ensure_handoff(db, conversation, reason, payload.get('summary') or rule.name, dispatch=False)
        session = session_for(db, conversation.id)
        if session:
            session.controls = {**session.controls, 'human': True}
        cancel(db, conversation.id, 'assignment_handoff')
    if not record.agent_id:
        record.error = '没有可接单且属于当前收件箱的顾问'
        create_notification(db, 'assignment.unassigned', '顾问待分配', rule.name, conversation.chatwoot_conversation_id)
    return record


def incoming(db, conversation, message):
    if not configuration(db).enabled:
        return None
    prior = db.scalars(select(MessageEvent).join(ConversationState).where(
        ConversationState.tenant_id == conversation.tenant_id,
        ConversationState.inbox_binding_id == conversation.inbox_binding_id,
        ConversationState.contact_id == conversation.contact_id,
        MessageEvent.direction == 'incoming', MessageEvent.private.is_(False),
        or_(MessageEvent.created_at < message.created_at,
            and_(MessageEvent.created_at == message.created_at,
                 MessageEvent.chatwoot_message_id < message.chatwoot_message_id))
        ).order_by(MessageEvent.created_at.desc(), MessageEvent.chatwoot_message_id.desc()).limit(1)).first()
    events = ['customer.message']
    if not prior:
        events.insert(0, 'customer.first_message')
    outgoing = db.scalar(select(MessageEvent).where(MessageEvent.conversation_state_id == conversation.id,
        MessageEvent.direction == 'outgoing', MessageEvent.private.is_(False),
        or_(MessageEvent.created_at < message.created_at,
            and_(MessageEvent.created_at == message.created_at,
                 MessageEvent.chatwoot_message_id < message.chatwoot_message_id)))
        .order_by(MessageEvent.created_at,MessageEvent.chatwoot_message_id).limit(1))
    previous_response = db.scalar(select(AdvisorAssignment.id).where(AdvisorAssignment.conversation_state_id == conversation.id,
        AdvisorAssignment.payload['first_response'].as_boolean() == True))
    if outgoing and not previous_response:
        previous_response = db.scalar(select(MessageEvent.id).where(
            MessageEvent.conversation_state_id == conversation.id, MessageEvent.direction == 'incoming',
            MessageEvent.private.is_(False),
            or_(MessageEvent.created_at > outgoing.created_at,
                and_(MessageEvent.created_at == outgoing.created_at,
                     MessageEvent.chatwoot_message_id > outgoing.chatwoot_message_id)),
            or_(MessageEvent.created_at < message.created_at,
                and_(MessageEvent.created_at == message.created_at,
                     MessageEvent.chatwoot_message_id < message.chatwoot_message_id))).limit(1))
    if outgoing and not previous_response:
        events.insert(0, 'customer.first_response')
    if prior:
        from app.runtime_settings import dt
        if dt(message.created_at) - dt(prior.created_at) >= timedelta(minutes=30):
            events.insert(0, 'customer.returned')
    from app.reception_v3.live import session_for
    row = session_for(db, conversation.id)
    route = (row.controls if row else {}).get('route_variant','')
    if not route:
        from app.route_packages import ROUTES
        route = next((key for key, value in ROUTES.items() if (message.content or '').strip() in
                      [*value.get('selection_aliases',[]),value.get('default_entry_message'),value.get('selection_title')]), '')
    return emit(db, conversation, events, f'message:{message.chatwoot_message_id}', {
        'message_id':message.chatwoot_message_id, 'first_response':'customer.first_response' in events,
        'route_variant':route,
        'summary': message.content[:300] if message.content else '客户发来附件'})


def handoff_event(reason):
    if reason in EVENTS and reason.startswith('handoff.'):
        return reason
    if reason in ('contact','contact_image','contact_reported','requested','party_size','trip_days','custom','knowledge'):
        return 'handoff.'+reason
    if reason in ('model_failed','channel_window_closed','channel_send_failed','reply_queue_overdue','delivery_failed','submission_unknown','new_customer_activation_failed') or reason.startswith(('delivery_', 'outbound_')):
        return 'handoff.system'
    return 'handoff.other'


def process_assignment(session_factory=None):
    from app.config import settings
    from app.db import SessionLocal
    from app.chatwoot_service import client_for, payload_dict
    from app.chatwoot import normalize_collection
    if settings.app_profile == 'evaluation' or not settings.outbound_enabled:
        return False
    factory = session_factory or SessionLocal
    with factory() as db:
        if not configuration(db).enabled:
            return False
        now = utcnow()
        record = db.scalar(select(AdvisorAssignment).where(
            AdvisorAssignment.status.in_(['pending','retry','processing']), AdvisorAssignment.available_at <= now
        ).order_by(AdvisorAssignment.id).limit(1))
        if not record:
            return False
        claimed = db.execute(update(AdvisorAssignment).where(AdvisorAssignment.id == record.id,
            AdvisorAssignment.status == record.status, AdvisorAssignment.available_at == record.available_at)
            .values(status='processing', attempts=AdvisorAssignment.attempts+1,
                    available_at=(datetime.now(timezone.utc)+timedelta(seconds=90)).isoformat()))
        if not claimed.rowcount:
            db.rollback()
            return False
        conversation = db.get(ConversationState, record.conversation_state_id)
        if record.event_type == 'handoff.overdue' and not db.scalar(select(HandoffTask.id).where(
                HandoffTask.conversation_state_id == conversation.id, HandoffTask.status == 'pending',
                HandoffTask.claimed_at.is_(None), HandoffTask.sla_due_at < now)):
            record.status, record.error = 'cancelled', '顾问已接单或任务已结束'
            db.commit()
            return True
        connection = db.scalar(select(ChatwootConnection).where(ChatwootConnection.tenant_id == conversation.tenant_id))
        record_id, remote_id, inbox_id = record.id, conversation.chatwoot_conversation_id, conversation.inbox.chatwoot_inbox_id
        target, action = record.agent_id, record.action
        db.commit()
    client = None
    try:
        if connection is None:
            raise ValueError('Chatwoot连接未配置')
        if settings.app_profile == 'live_reply' and (connection.account_id != settings.live_reply_account_id or inbox_id != settings.live_reply_inbox_id):
            raise ValueError('分配不在当前正式接待范围')
        client = client_for(connection)
        remote = payload_dict(client.get_conversation(remote_id))
        if remote.get('inbox_id') != inbox_id:
            raise ValueError('会话收件箱发生变化')
        meta = remote.get('meta') or {}
        owner = (meta.get('assignee') or {}).get('id') if meta.get('assignee_type') != 'AgentBot' else None
        if owner and action != 'notify':
            target = int(owner)
        agents = normalize_collection(client.list_inbox_agents(inbox_id))
        if target not in [int(a['id']) for a in agents]:
            raise ValueError('目标顾问不属于当前收件箱')
        if action != 'notify' and owner != target:
            client.assign_conversation(remote_id, target)
            confirmation = payload_dict(client.get_conversation(remote_id))
            confirmed = ((confirmation.get('meta') or {}).get('assignee') or {}).get('id')
            if confirmed != target:
                raise RuntimeError('分配尚未确认')
        with factory() as db:
            record = db.get(AdvisorAssignment, record_id)
            conversation = db.get(ConversationState, record.conversation_state_id)
            record.agent_id, record.status, record.error, record.updated_at = target, 'synced', None, utcnow()
            if action != 'notify':
                conversation.assignee_id = target
                agent = next(a for a in agents if int(a['id']) == target)
                conversation.assignee_name = agent.get('name') or agent.get('available_name') or str(target)
                for task in db.scalars(select(HandoffTask).where(HandoffTask.conversation_state_id == conversation.id,
                                                                HandoffTask.status == 'pending')):
                    task.chatwoot_assignee_id = target
            if not record.notification_id:
                note = create_notification(db, 'handoff.overdue' if record.event_type == 'handoff.overdue' else 'assignment.routed', EVENTS[record.event_type],
                    record.payload.get('summary') or record.rule_name, remote_id)
                note.target_agent_id = target
                record.notification_id = note.id
                config = setting_value(db, 'notification_settings', {})
                if config.get('enabled') and not db.scalar(select(NotificationDelivery.id).where(NotificationDelivery.notification_id == note.id)):
                    db.add(NotificationDelivery(notification_id=note.id))
            db.commit()
    except Exception as exc:
        with factory() as db:
            record = db.get(AdvisorAssignment, record_id)
            record.status = 'failed' if record.attempts >= 5 or isinstance(exc, ValueError) else 'retry'
            record.error, record.updated_at = str(exc)[:200], utcnow()
            record.available_at = (datetime.now(timezone.utc)+timedelta(seconds=min(300,2**record.attempts))).isoformat()
            db.commit()
    finally:
        if client:
            client.close()
    return True
