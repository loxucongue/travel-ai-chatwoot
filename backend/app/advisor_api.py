from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func
from sqlalchemy.orm import Session
from app.db import get_db
from app.models import AdvisorAssignment, ChatwootAgent, ConversationState, InboxBinding, User, utcnow
from app.ops_api import manager, manager_csrf
from app.operations import save_setting, audit
from app.advisor_assignment import AssignmentConfig, configuration, EVENTS
from app.route_packages import ROUTES

router = APIRouter(prefix='/v1/advisor-assignment')


@router.get('')
def get_config(user: User = Depends(manager), db: Session = Depends(get_db)):
    config = configuration(db)
    agents = db.scalars(select(ChatwootAgent).order_by(ChatwootAgent.name)).all()
    return {'config':config.model_dump(), 'events':EVENTS,
            'agents':[{'id':a.chatwoot_agent_id,'name':a.name,'availability':a.availability_status,
                       'inbox_ids':a.inbox_ids} for a in agents],
            'inboxes':[{'id':i.chatwoot_inbox_id,'name':i.name} for i in db.scalars(select(InboxBinding))],
            'routes':[{'id':k,'name':v['name']} for k,v in ROUTES.items()]}


@router.put('')
def put_config(payload: AssignmentConfig, user: User = Depends(manager_csrf), db: Session = Depends(get_db)):
    known = set(db.scalars(select(ChatwootAgent.chatwoot_agent_id)))
    selected = {a.agent_id for a in payload.advisors}
    targets = {a for r in payload.rules for a in r.agent_ids}
    if payload.fallback_agent_id:
        targets.add(payload.fallback_agent_id)
    if not selected <= known or not targets <= selected:
        raise HTTPException(422, detail='请先在顾问列表启用规则中使用的顾问')
    inboxes = set(db.scalars(select(InboxBinding.chatwoot_inbox_id)))
    if any(not set(r.routes) <= set(ROUTES) or not set(r.inbox_ids) <= inboxes for r in payload.rules):
        raise HTTPException(422, detail='线路或收件箱无效')
    save_setting(db, 'advisor_assignment', payload.model_dump())
    audit(db, user, 'assignment.config_updated', 'settings', 'advisor_assignment',
          {'enabled':payload.enabled,'rules':len(payload.rules)})
    db.commit()
    return {'config':payload.model_dump()}


@router.get('/records')
def records(status: str = '', page: int = Query(1,ge=1), user: User = Depends(manager), db: Session = Depends(get_db)):
    query = select(AdvisorAssignment)
    if status:
        query = query.where(AdvisorAssignment.status == status)
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    agents = {a.chatwoot_agent_id:a.name for a in db.scalars(select(ChatwootAgent))}
    items = []
    for r in db.scalars(query.order_by(AdvisorAssignment.id.desc()).offset((page-1)*30).limit(30)):
        c = db.get(ConversationState,r.conversation_state_id)
        items.append({'id':r.id,'conversation_id':c.chatwoot_conversation_id,
            'customer': c.contact.name if c.contact else '', 'event':EVENTS.get(r.event_type,r.event_type),
            'rule':r.rule_name,'agent':agents.get(r.agent_id,str(r.agent_id or '待分配')),
            'agent_id':r.agent_id,'status':r.status,'error':r.error,'created_at':r.created_at,
            'action':r.action})
    return {'items':items,'total':total,'page':page}


@router.post('/records/{record_id}/retry')
def retry(record_id: int, user: User = Depends(manager_csrf), db: Session = Depends(get_db)):
    r = db.get(AdvisorAssignment,record_id)
    if not r or r.status not in ('failed','unassigned'):
        raise HTTPException(409, detail='该记录无需重试')
    if r.agent_id is None:
        # Re-evaluate a previously empty pool explicitly, without replaying the customer event.
        from app.advisor_assignment import eligible_agents, next_agent
        c = db.get(ConversationState,r.conversation_state_id)
        cfg = configuration(db)
        rule = next((x for x in cfg.rules if x.id==r.rule_id and x.enabled),None)
        pool = eligible_agents(db,c,cfg,rule.agent_ids if rule else [])
        if not pool:
            raise HTTPException(422, detail='请先为该规则设置可接单的顾问')
        r.agent_id = c.assignee_id or next_agent(db,c,rule,pool)
    r.status,r.error,r.attempts,r.available_at = 'pending',None,0,utcnow()
    audit(db,user,'assignment.retry','assignment',r.id,{'agent_id':r.agent_id})
    db.commit()
    return {'status':r.status}
