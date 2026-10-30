"""Customer-level operations reporting, with one shared scope for counts/details."""
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Literal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from app.auth import current_user
from app.bi_models import BiConversation, BiEvent, BiRefresh
from app.bi_projection import ACCEPTED, ADDED_LABELS
from app.db import get_db
from app.models import Contact, ConversationState, InboxBinding
from app.operations import allowed_inbox_ids

router = APIRouter(prefix='/v1/bi')
ZONE = ZoneInfo('Asia/Taipei')
LABELS = {'new':'新增客户', 'ai':'AI接待客户', 'engaged':'已开口', 'deep':'深入咨询／已报价',
          'invited':'已邀请私讯', 'captured':'已取得联系方式', 'added':'业务确认已加私讯', 'returned':'跟进后重新回复',
          'quoted':'已报价', 'introduced':'已介绍', 'reported_added':'客户自报已加', 'pending_contact':'联系方式待核实',
          'current_added':'当前已加私讯', 'handoff':'转人工客户', 'converted':'业务标记成交', 'followup':'跟进客户', 'meaningful_return':'跟进后继续咨询',
          'followup_lead':'跟进后留资', 'pending':'待人工接待', 'overdue':'交接超时未回复', 'failed':'发送失败客户'}
REASONS = {'contact':'取得联系方式', 'contact_reported':'客户自报已加', 'contact_image':'联系图片待核实',
           'party_size':'人数／大团', 'custom':'定制需求', 'trip_days':'天数需求', 'knowledge':'资料需核实',
           'label':'人工标签接管', 'manual':'手动接管', 'requested':'客户要求真人',
           'followup_customer_replied':'顾问跟进后客户回复', 'channel_send_failed':'渠道拒收',
           'delivery_failed':'发送失败', 'model_failed':'模型处理失败', 'reply_queue_overdue':'答复超时',
           'channel_window_closed':'跟进期限结束', 'departure_material_unverified':'团期资料待核实', 'live_price':'费用需核实'}


def manager(user=Depends(current_user)):
    if user.role not in ('super_admin', 'admin', 'supervisor'):
        raise HTTPException(403, detail={'code':'permission_denied', 'message':'仅管理人员可查看运营看板'})
    return user


def window(start, end, now):
    today = now.astimezone(ZONE).date()
    try:
        first, last = date.fromisoformat(start) if start else today, date.fromisoformat(end) if end else today
    except ValueError:
        raise HTTPException(422, detail={'message':'日期格式应为 YYYY-MM-DD'}) from None
    if first > last or (last-first).days > 365 or last > today:
        raise HTTPException(422, detail={'message':'请选择有效日期，结束不晚于今天，跨度不超过366天'})
    return (datetime.combine(first, datetime.min.time(), ZONE).astimezone(timezone.utc),
            datetime.combine(last+timedelta(days=1), datetime.min.time(), ZONE).astimezone(timezone.utc))


def read_report(db, user, start=None, end=None, inbox_id=None, mode='period', now=None):
    now = now or datetime.now(timezone.utc)
    begin, finish = window(start, end, now)
    low, high, asof = begin.isoformat(), finish.isoformat(), now.isoformat()
    allowed = allowed_inbox_ids(db, user)
    q = select(ConversationState)
    if allowed is not None:
        q = q.where(ConversationState.inbox_binding_id.in_(allowed))
    # First entry is computed across authorized channels BEFORE channel filtering.
    conversations = {c.id:c for c in db.scalars(q)}
    inbox_query = select(InboxBinding).order_by(InboxBinding.id)
    if allowed is not None:
        inbox_query = inbox_query.where(InboxBinding.id.in_(allowed))
    inboxes = {i.id:i for i in db.scalars(inbox_query)}
    contacts = {c.id:c for c in db.scalars(select(Contact).where(Contact.id.in_({c.contact_id for c in conversations.values() if c.contact_id})))}
    subject = {cid:f'{c.tenant_id}:contact:{c.contact_id}' if c.contact_id else f'{c.tenant_id}:conversation:{cid}' for cid,c in conversations.items()}
    events = list(db.scalars(select(BiEvent).where(BiEvent.conversation_state_id.in_(conversations), BiEvent.occurred_at <= asof).order_by(BiEvent.occurred_at, BiEvent.key)))
    projections = {x.conversation_state_id:x for x in db.scalars(select(BiConversation).where(BiConversation.conversation_state_id.in_(conversations)))}
    births = {}
    for e in events:
        if e.kind == 'new':
            births.setdefault(subject[e.conversation_state_id], e)
    visible = {cid for cid,c in conversations.items() if inbox_id is None or inboxes[c.inbox_binding_id].chatwoot_inbox_id == inbox_id}
    cohort = {s for s,e in births.items() if low <= e.occurred_at < high and e.conversation_state_id in visible}
    selected = [e for e in events if e.conversation_state_id in visible and
                (subject[e.conversation_state_id] in cohort if mode == 'cohort' else low <= e.occurred_at < high)]
    # Records remain durable, but a later negative receipt removes success credit.
    def accepted(e):
        return e.evidence.get('status') in ACCEPTED if e.kind in ('ai','human','invited','introduced','quoted') else True
    sets = defaultdict(set)
    proofs = defaultdict(list)
    def add(kind, e):
        s = subject[e.conversation_state_id]
        sets[kind].add(s)
        proofs[(kind,s)].append(e)
    for e in selected:
        if e.kind == 'new':
            if births.get(subject[e.conversation_state_id]) is e:
                add('new', e)
        elif accepted(e):
            add(e.kind, e)
        if e.kind == 'quoted' and accepted(e):
            add('deep', e)
        if e.kind in ('ai','invited','introduced','quoted') and e.evidence.get('status') == 'failed':
            add('failed', e)
    # A customer may skip stages. Each stage is an independent unique set.
    by_cid = defaultdict(list)
    for e in events:
        by_cid[e.conversation_state_id].append(e)
    touches = [e for e in selected if e.kind == 'followup']
    delivery = Counter(e.evidence.get('status', 'unknown') for e in touches)
    followup_modes = Counter(e.evidence.get('mode', 'unknown') for e in touches)
    outcomes = {}
    # Match on the entire visible history, then filter by reply date. A reply
    # today to yesterday's follow-up belongs to today's activity report.
    for touch in (e for e in events if e.kind == 'followup' and e.conversation_state_id in visible):
        if not touch.evidence.get('accepted'):
            continue
        t = datetime.fromisoformat(touch.occurred_at)
        cutoff = min(t+timedelta(hours=24), now)
        later = [e for e in by_cid[touch.conversation_state_id] if touch.occurred_at < e.occurred_at <= cutoff.isoformat()]
        incoming = next((e for e in later if e.kind == 'incoming' and e.evidence.get('eligible_reply', True)), None)
        next_touch = next((e for e in later if e.kind == 'followup' and e.evidence.get('accepted')), None)
        if next_touch and (not incoming or next_touch.occurred_at <= incoming.occurred_at):
            incoming = None
        # A subsequent direct AI answer starts another conversation turn.
        if incoming and any(e.kind == 'ai' and e.evidence.get('status') in ACCEPTED
                            and e.evidence.get('message_id') not in touch.evidence.get('message_ids', [])
                            and e.occurred_at < incoming.occurred_at for e in later):
            incoming = None
        shared = bool(incoming and any(e.kind == 'human' and e.occurred_at < incoming.occurred_at for e in later))
        outcomes[touch.key] = {'reply':incoming, 'shared':shared}
        included = bool(incoming and (subject[touch.conversation_state_id] in cohort if mode == 'cohort'
                                     else low <= incoming.occurred_at < high))
        if included:
            reply = SimpleNamespace(conversation_state_id=touch.conversation_state_id, kind='returned',
                occurred_at=incoming.occurred_at, source='followup_reply', evidence={**incoming.evidence,
                'followup_at':touch.occurred_at,'plan_id':touch.evidence.get('plan_id'),'shared':shared})
            add('returned', reply)
            if incoming.evidence.get('meaningful'):
                add('meaningful_return', reply)
            for lead in later:
                if lead.kind == 'captured' and lead.occurred_at >= incoming.occurred_at and (mode == 'cohort' or low <= lead.occurred_at < high):
                    add('followup_lead', lead)
    # Use each customer's LAST eligible touch in the chosen sending period;
    # do not mix a fresh reply with an unrelated mature touch's denominator.
    last_touches = {subject[e.conversation_state_id]:e for e in touches if e.evidence.get('accepted')}
    mature = [e for e in last_touches.values() if datetime.fromisoformat(e.occurred_at)+timedelta(hours=24) <= now]
    mature_replied = sum(bool(outcomes.get(e.key, {}).get('reply')) for e in mature)
    # Pending queue is a current snapshot, independent of the selected dates.
    current_handoffs = [e for e in events if e.conversation_state_id in visible and e.kind == 'handoff'
                        and e.evidence.get('status') in ('pending','claimed') and not e.evidence.get('answered_at')]
    for e in current_handoffs:
        add('pending', e)
        if e.evidence.get('sla_due_at') and e.evidence['sla_due_at'] < asof:
            add('overdue', e)
    # Current labels survive missing historic label events. Show this snapshot
    # separately; do not invent a date or inject it into period/cohort counts.
    timed_added = {subject[e.conversation_state_id] for e in events if e.kind == 'added' and e.conversation_state_id in visible}
    for cid in visible:
        c = conversations[cid]
        contact = contacts.get(c.contact_id)
        labels = (set(c.labels or []) | set(contact.labels or [] if contact else [])) & ADDED_LABELS
        if labels:
            add('current_added', SimpleNamespace(conversation_state_id=cid, kind='current_added',
                occurred_at=asof, source='current_label', evidence={'label':'、'.join(sorted(labels)),
                    'snapshot_only':True, 'addition_time_unknown':subject[cid] not in timed_added}))
    queue = Counter()
    for cid in visible:
        c, p = conversations[cid], projections.get(cid)
        s = p.snapshot if p else {}
        if s.get('next_followup_at') and c.effective_ai_state in ('AI_ACTIVE','AI_FOLLOWUP'):
            queue['scheduled'] += 1
        elif s.get('next_followup_at') and s.get('human_followup'):
            queue['advisor_waiting'] += 1
        elif s.get('followup_stop_reason'):
            queue['stopped_or_waiting'] += 1
    handoffs = [e for e in selected if e.kind == 'handoff']
    reasons = defaultdict(set)
    for e in handoffs:
        reasons[REASONS.get(e.evidence.get('reason'), '其他／历史原因')].add(subject[e.conversation_state_id])
    response_times = [(datetime.fromisoformat(e.evidence['answered_at'])-datetime.fromisoformat(e.occurred_at)).total_seconds()/60
                      for e in handoffs if e.evidence.get('answered_at')]
    trends = {}
    day = begin.astimezone(ZONE).date()
    while day < finish.astimezone(ZONE).date():
        trends[day.isoformat()] = {k:set() for k in ('new','engaged','captured','returned')}
        day += timedelta(days=1)
    for k in ('new','engaged','captured','returned'):
        for s in sets[k]:
            for e in proofs[(k,s)]:
                day = datetime.fromisoformat(e.occurred_at).astimezone(ZONE).date().isoformat()
                if day in trends:
                    trends[day][k].add(s)
    total = len({subject[cid] for cid in visible})
    baseline = len(cohort) if mode == 'cohort' else len(sets['incoming'])
    metrics = [{'key':k, 'label':LABELS[k], 'value':len(sets[k]),
                'rate':round(len(sets[k])/baseline*100,1) if mode == 'cohort' and baseline else None}
               for k in ('new','ai','engaged','deep','invited','captured','added','returned')]
    refresh = db.get(BiRefresh,1)
    covered = sum(cid in projections and bool(projections[cid].signature) for cid in visible)
    return {'meta':{'start':begin.astimezone(ZONE).date().isoformat(), 'end':(finish.astimezone(ZONE)-timedelta(days=1)).date().isoformat(),
                    'mode':mode, 'timezone':'Asia/Taipei', 'as_of':asof, 'refreshed_at':refresh.refreshed_at if refresh else None,
                    'remaining':refresh.remaining if refresh else len(visible), 'covered':covered,'conversations':len(visible),
                    'incomplete_history':sum(e.evidence.get('historical_incomplete',False) for s,e in births.items() if e.conversation_state_id in visible),
                    'unknown_birth':len({subject[cid] for cid in visible}-set(births)),
                    'current_added':len(sets['current_added']),
                    'added_time_unknown':len(sets['current_added']-timed_added),
                    'unlinked_customers':sum(conversations[cid].contact_id is None for cid in visible),
                    'population':baseline, 'all_customers':total},
            'inboxes':[{'id':i.chatwoot_inbox_id,'name':i.name} for i in inboxes.values()], 'metrics':metrics,
            'progress':[{'key':k,'label':LABELS[k],'value':len(sets[k])} for k in ('new','engaged','deep','introduced','quoted','invited','captured','reported_added','pending_contact','added','converted')],
            'handoffs':{'customers':len(sets['handoff']), 'tasks':len(handoffs), 'pending':len(sets['pending']),
                        'overdue':len(sets['overdue']), 'answered':sum(bool(e.evidence.get('answered_at')) for e in handoffs),
                        'mean_reply_minutes':round(sum(response_times)/len(response_times),1) if response_times else None,
                        'unassigned':sum(not e.evidence.get('assigned') for e in current_handoffs),
                        'assigned_unanswered':sum(bool(e.evidence.get('assigned')) for e in current_handoffs),
                        'reasons':[{'name':k,'value':len(v)} for k,v in sorted(reasons.items(),key=lambda x:-len(x[1]))]},
            'followup':{'customers':len({subject[e.conversation_state_id] for e in touches}), 'rounds':len(touches),
                        'delivery':dict(delivery), 'modes':dict(followup_modes), 'returned':len(sets['returned']),
                        'meaningful_return':len(sets['meaningful_return']), 'leads':len(sets['followup_lead']),
                        'matured_customers':len(mature), 'matured_replied':mature_replied,
                        'response_rate':round(mature_replied/len(mature)*100,1) if mature else None,
                        'observing':len(last_touches)-len(mature),
                        'shared_rounds':sum(bool(outcomes.get(e.key,{}).get('shared')) for e in touches), 'queue':dict(queue)},
            'ai_value':{'ai_only':len(sets['ai']-sets['human']), 'mixed':len(sets['ai'] & sets['human']),
                        'captured_with_ai':len(sets['captured'] & sets['ai']), 'failed_customers':len(sets['failed'])},
            'trends':[{'day':d,**{k:len(v) for k,v in counts.items()}} for d,counts in trends.items()],
            '_sets':sets,'_proofs':proofs,'_conversations':conversations,'_contacts':contacts,'_subject':subject,'_projections':projections}


@router.get('/dashboard')
def dashboard(start: str | None=None, end: str | None=None, inbox_id: int | None=None,
              mode: Literal['period','cohort']='period', user=Depends(manager), db=Depends(get_db)):
    with db.no_autoflush:
        result = read_report(db,user,start,end,inbox_id,mode)
    return {k:v for k,v in result.items() if not k.startswith('_')}


@router.get('/customers')
def customers(metric: str='new', start: str | None=None, end: str | None=None, inbox_id: int | None=None,
              mode: Literal['period','cohort']='period', page: int=Query(1,ge=1), page_size: int=Query(20,ge=1,le=100),
              user=Depends(manager), db=Depends(get_db)):
    if metric not in LABELS:
        raise HTTPException(422,detail={'message':'未知指标'})
    with db.no_autoflush:
        r = read_report(db,user,start,end,inbox_id,mode)
    subjects = sorted(r['_sets'][metric], key=lambda s:max(e.occurred_at for e in r['_proofs'][(metric,s)]), reverse=True)
    items=[]
    for s in subjects[(page-1)*page_size:page*page_size]:
        evidence=sorted(r['_proofs'][(metric,s)],key=lambda e:e.occurred_at,reverse=True)
        c=r['_conversations'][evidence[0].conversation_state_id]
        contact=r['_contacts'].get(c.contact_id)
        p=r['_projections'].get(c.id)
        items.append({'customer_key':s,'name':contact.name if contact else '未关联客户', 'conversation_id':c.chatwoot_conversation_id,
                      'state':c.effective_ai_state,'advisor':c.assignee_name,'route':p.snapshot.get('route','') if p else '',
                      'at':evidence[0].occurred_at,'evidence':[{'kind':e.kind,'at':e.occurred_at,'source':e.source,
                        **{k:v for k,v in e.evidence.items() if k != 'reason'}} for e in evidence[:8]],
                      'next_followup_at':p.snapshot.get('next_followup_at') if p else None})
    return {'items':items,'total':len(subjects),'page':page,'page_size':page_size,'metric':metric,'label':LABELS[metric]}
