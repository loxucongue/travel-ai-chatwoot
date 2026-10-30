"""Business attribution, permissions and date boundaries for the BI dashboard."""
from datetime import datetime, timezone
from types import SimpleNamespace
import json

import pytest
from fastapi import HTTPException
from sqlalchemy import select, func
from app.models import (Contact, ConversationState, InboxBinding, User, UserInboxScope,
    MessageEvent, OutboundMessage, HandoffTask, LabelEvent, ChatwootConnection, WebhookEvent)
from app.automation_models import AutomationSession, AutomationRun
from app.bi_models import BiConversation, BiEvent
from app.bi_projection import refresh, note_birth, backfill_births
from app.bi_api import read_report, window

NOW = datetime(2026,10,30,8,tzinfo=timezone.utc)


def setup(db):
    db.add_all([InboxBinding(id=i,tenant_id=1,chatwoot_inbox_id=i*10,name=f'Channel {i}') for i in (1,2)])
    db.add_all([Contact(id=i,tenant_id=1,chatwoot_contact_id=i,name=f'Customer {i}') for i in (1,2,3)])
    db.flush()
    for cid,contact,channel in [(1,1,1),(2,1,2),(3,2,1),(4,3,2)]:
        db.add(ConversationState(id=cid,tenant_id=1,inbox_binding_id=channel,contact_id=contact,chatwoot_conversation_id=100+cid))
    db.flush()


def event(db,cid,kind,at,**evidence):
    key=f'{cid}:{kind}:{at}'
    db.add(BiEvent(key=key,conversation_state_id=cid,kind=kind,occurred_at=at,source='test',evidence=evidence))


def report(db, **kw):
    db.flush()
    return read_report(db,kw.pop('user',db.get(User,1)),now=NOW,start='2026-10-30',end='2026-10-30',**kw)


def metrics(r):
    return {x['key']:x['value'] for x in r['metrics']}


def test_utc8_window():
    lo,hi=window('2026-10-30','2026-10-30',NOW)
    assert lo.isoformat()=='2026-10-29T16:00:00+00:00'
    assert hi.isoformat()=='2026-10-30T16:00:00+00:00'


@pytest.mark.parametrize('start,end',[('bad','2026-10-30'),('2026-02-30','2026-10-30'),('2026-10-30','2026-10-29'),('2026-10-30','2026-10-31'),('2024-10-30','2026-10-30')])
def test_invalid_dates(start,end):
    with pytest.raises(HTTPException): window(start,end,NOW)


def test_period_cohort_customer_dedup_and_channel_scope(session_factory):
    with session_factory() as db:
        setup(db)
        event(db,1,'new','2026-10-28T01:00:00+00:00')
        event(db,2,'new','2026-10-30T01:00:00+00:00') # same existing customer, another channel
        event(db,1,'captured','2026-10-30T01:00:00+00:00')
        event(db,3,'new','2026-10-29T16:00:00+00:00') # exactly UTC+8 midnight
        event(db,3,'engaged','2026-10-30T01:00:00+00:00')
        event(db,4,'new','2026-10-29T15:59:59+00:00')
        assert metrics(report(db))['new']==1
        assert metrics(report(db))['captured']==1
        assert metrics(report(db,mode='cohort'))['captured']==0
        assert metrics(report(db,inbox_id=20))['new']==0
        assert metrics(report(db,mode='cohort'))['engaged']==1


def test_cohort_includes_progress_after_selected_dates(session_factory):
    with session_factory() as db:
        setup(db)
        event(db,1,'new','2026-10-28T01:00:00+00:00')
        event(db,1,'captured','2026-10-30T01:00:00+00:00')
        db.flush()
        r=read_report(db,db.get(User,1),'2026-10-28','2026-10-28',mode='cohort',now=NOW)
        assert metrics(r)['captured']==1 and r['metrics'][5]['rate']==100


def test_reads_do_not_mutate_and_followup_uses_reply_date(session_factory):
    with session_factory() as db:
        setup(db)
        event(db,1,'followup','2026-10-29T10:00:00+00:00',accepted=True,status='read',message_ids=[10],plan_id='p')
        event(db,1,'human','2026-10-29T17:00:00+00:00',status='sent')
        event(db,1,'incoming','2026-10-29T18:00:00+00:00',eligible_reply=True,meaningful=True,message_id=20)
        db.commit()
        r=report(db)
        assert metrics(r)['returned']==1 and r['followup']['rounds']==0
        assert r['_proofs'][('returned','1:contact:1')][0].evidence['shared']
        assert not db.dirty and not db.new


def test_mature_followup_rate_uses_latest_selected_touch(session_factory):
    with session_factory() as db:
        setup(db)
        event(db,1,'followup','2026-10-28T01:00:00+00:00',accepted=True,status='read')
        event(db,1,'followup','2026-10-30T01:00:00+00:00',accepted=True,status='read')
        event(db,1,'incoming','2026-10-30T02:00:00+00:00',eligible_reply=True,meaningful=True)
        event(db,3,'followup','2026-10-29T01:00:00+00:00',accepted=True,status='read')
        event(db,3,'incoming','2026-10-29T02:00:00+00:00',eligible_reply=True,meaningful=False)
        db.flush()
        r=read_report(db,db.get(User,1),'2026-10-28','2026-10-30',now=NOW)
        assert r['followup']['matured_customers']==r['followup']['matured_replied']==1
        assert r['followup']['response_rate']==100 and r['followup']['observing']==1
        assert r['followup']['meaningful_return']==1


@pytest.mark.parametrize('blocker',['failed','option','later_ai','outside_24h','later_touch'])
def test_no_false_reactivation(session_factory,blocker):
    with session_factory() as db:
        setup(db)
        event(db,1,'followup','2026-10-29T01:00:00+00:00',accepted=blocker!='failed',status='failed' if blocker=='failed' else 'read',message_ids=[1])
        at='2026-10-30T02:00:00+00:00' if blocker=='outside_24h' else '2026-10-29T03:00:00+00:00'
        event(db,1,'incoming',at,eligible_reply=blocker!='option',meaningful=True)
        if blocker=='later_ai': event(db,1,'ai','2026-10-29T02:00:00+00:00',status='sent',message_id=99)
        if blocker=='later_touch': event(db,1,'followup','2026-10-29T02:00:00+00:00',accepted=True,status='sent')
        db.flush()
        r=read_report(db,db.get(User,1),'2026-10-29','2026-10-30',now=NOW)
        assert r['followup']['matured_replied']==(1 if blocker=='later_touch' else 0)


def message(db,mid,content='',direction='incoming',**kw):
    m=MessageEvent(conversation_state_id=1,chatwoot_message_id=mid,content=content,direction=direction,created_at=kw.pop('created_at','2026-10-30T01:00:00+00:00'),**kw)
    db.add(m)
    return m


def test_projection_no_ad_engagement_and_stable_birth(session_factory):
    with session_factory() as db:
        setup(db)
        c=db.get(ConversationState,1)
        note_birth(db,c,1793232000) # 2026-10-29 00:00 UTC
        message(db,1,'西藏桃花節-不上珠峰9日')
        message(db,2,'我們四位請問價格？')
        message(db,3,'private note',private=True)
        refresh(db)
        kinds=[e.kind for e in db.scalars(select(BiEvent))]
        assert kinds.count('engaged')==1
        birth=db.get(BiEvent,'1:entry').occurred_at
        c.updated_at='2026-10-31T01:00:00+00:00'
        refresh(db)
        assert db.get(BiEvent,'1:entry').occurred_at==birth
        assert refresh(db)['changed']==0


def test_labels_contact_stages_and_removed_history(session_factory):
    with session_factory() as db:
        setup(db)
        db.add_all([LabelEvent(conversation_state_id=1,label='加私訊',action=a,created_at=f'2026-10-30T0{i}:00:00+00:00') for i,a in enumerate(['added','removed'],1)])
        db.add_all([HandoffTask(conversation_state_id=cid,reason_code=reason,created_at='2026-10-30T01:00:00+00:00') for cid,reason in [(1,'contact_reported'),(3,'contact_image'),(4,'contact')]])
        refresh(db)
        r=report(db)
        assert metrics(r)['added']==1 and metrics(r)['captured']==1
        assert len(r['_sets']['reported_added'])==len(r['_sets']['pending_contact'])==1


def test_handoff_assignment_private_note_not_answer(session_factory):
    with session_factory() as db:
        setup(db)
        db.add(HandoffTask(conversation_state_id=1,chatwoot_assignee_id=100,status='claimed',created_at='2026-10-28T01:00:00+00:00',sla_due_at='2026-10-28T02:00:00+00:00'))
        message(db,1,'note','outgoing',private=True,attribution='human',status='sent')
        refresh(db)
        r=report(db)
        assert r['handoffs']['pending']==r['handoffs']['overdue']==1
        assert r['handoffs']['assigned_unanswered']==1
        message(db,2,'answer','outgoing',attribution='human',status='sent')
        refresh(db)
        assert report(db)['handoffs']['pending']==0


def test_followup_picture_caption_one_round_invitation_receipts(session_factory):
    with session_factory() as db:
        setup(db)
        parts=[{'id':str(i),'direction':'outgoing','v3_kind':'reply','proactive':True,'plan_id':'one','run_id':1,'contact_invitation':i==1} for i in (1,2)]
        db.add(AutomationSession(id=1,owner_id=1,environment='live',conversation_state_id=1,messages=parts))
        db.flush()
        db.add(AutomationRun(id=1,session_id=1,module='reply',generation=1,idempotency_key='run',status='completed',input_snapshot={'state':{'human_followup':True}}))
        for i in (1,2): db.add(OutboundMessage(id=i,conversation_state_id=1,idempotency_key=f'live-v3:1:{i}',content='hello',chatwoot_message_id=i,status='read',submitted_at='2026-10-30T01:00:00+00:00'))
        refresh(db)
        r=report(db)
        assert r['followup']['rounds']==1 and r['followup']['modes']=={'advisor':1}
        assert metrics(r)['invited']==metrics(r)['ai']==1
        db.get(OutboundMessage,1).status='failed'
        refresh(db)
        r=report(db)
        assert r['followup']['delivery']=={'partial':1} and metrics(r)['invited']==0


def test_sandbox_drafts_and_private_excluded(session_factory):
    with session_factory() as db:
        setup(db)
        db.add(AutomationSession(owner_id=1,conversation_state_id=1,environment='playground',messages=[{'id':'1','direction':'outgoing','proactive':True,'contact_invitation':True}]))
        message(db,1,'draft','outgoing',attribution='ai',status='draft')
        message(db,2,'private','outgoing',attribution='ai',status='sent',private=True)
        refresh(db)
        assert metrics(report(db))['ai']==0 and report(db)['followup']['rounds']==0


def test_deep_consultation_requires_real_distinct_incoming_references(session_factory):
    with session_factory() as db:
        setup(db)
        for mid in (1,2,3): message(db,mid,f'Question {mid}')
        message(db,4,'Internal note',private=True)
        db.add(AutomationSession(id=1,owner_id=1,conversation_state_id=1,environment='live'))
        db.flush()
        run=AutomationRun(session_id=1,module='reply',generation=1,idempotency_key='review',status='completed',decision={'lead_review':{'question_message_ids':['cw-1','cw-1','cw-4','missing']}})
        db.add(run);refresh(db)
        assert metrics(report(db))['deep']==0
        run.decision={'lead_review':{'question_message_ids':['cw-1','cw-2','cw-3']}}
        # Real runs are immutable after completion; revised evidence is normally
        # a new run, represented here by adding a new decision record.
        db.add(AutomationRun(session_id=1,module='reply',generation=2,idempotency_key='review-2',status='completed',decision=run.decision))
        refresh(db)
        assert metrics(report(db))['deep']==1


def test_unknown_birth_is_visible_but_not_guessed_from_update_time(session_factory):
    with session_factory() as db:
        setup(db)
        message(db,1,'An old outgoing','outgoing',attribution='human',status='sent')
        refresh(db)
        r=report(db)
        assert metrics(r)['new']==0 and r['meta']['unknown_birth']==3


def test_unsent_cancelled_followup_plans_are_not_counted_as_attempts(session_factory):
    with session_factory() as db:
        setup(db)
        db.add(AutomationSession(id=1,owner_id=1,conversation_state_id=1,environment='live',
            messages=[{'id':str(i),'direction':'outgoing','proactive':True,'plan_id':'p'} for i in (1,2,3)]))
        db.flush()
        for i,status in enumerate(['planned','draft','cancelled'],1):
            db.add(OutboundMessage(conversation_state_id=1,idempotency_key=f'live-v3:1:{i}',content='unsent',status=status))
        refresh(db)
        assert report(db)['followup']['rounds']==0


def test_backfill_birth_uses_conversation_not_message_timestamp(session_factory):
    with session_factory() as db:
        setup(db)
        db.add(ChatwootConnection(id=1,tenant_id=1,connection_key='test'))
        db.flush()
        for i,payload in enumerate([{'conversation':{'id':101},'created_at':1793232000},
            {'conversation':{'id':103,'created_at':1793145600},'created_at':1793232000}],1):
            db.add(WebhookEvent(connection_id=1,event='message_created',account_id=1,resource_id=str(i),idempotency_key=str(i),payload=payload))
        db.flush()
        backfill_births(db)
        assert db.get(BiConversation,1) is None
        assert db.get(BiConversation,3).source_created_at=='2026-10-28T00:00:00+00:00'


def test_api_permissions_scope_drilldown_and_pagination(authenticated,session_factory):
    client,_=authenticated
    today=datetime.now(timezone.utc).isoformat()
    with session_factory() as db:
        setup(db)
        for cid in (1,3,4): event(db,cid,'captured',today)
        db.commit()
    r=client.get('/v1/bi/dashboard').json()
    details=client.get('/v1/bi/customers?metric=captured&page_size=1').json()
    assert metrics(r)['captured']==details['total']==3 and len(details['items'])==1
    assert client.get('/v1/bi/customers?metric=bad').status_code==422
    with session_factory() as db:
        u=db.get(User,1); u.role='supervisor'
        db.add(UserInboxScope(user_id=1,inbox_binding_id=1)); db.commit()
    assert metrics(client.get('/v1/bi/dashboard').json())['captured']==2
    assert client.get('/v1/bi/customers?metric=captured&inbox_id=20').json()['total']==0
    assert client.get('/v1/bi/dashboard').json()['inboxes']==[{'id':10,'name':'Channel 1'}]
    with session_factory() as db:
        db.get(User,1).role='agent'; db.commit()
    assert client.get('/v1/bi/dashboard').status_code==403


def test_empty_authorized_channels_remain_selectable(session_factory):
    with session_factory() as db:
        setup(db)
        db.add(InboxBinding(id=3,tenant_id=1,chatwoot_inbox_id=30,name='Empty channel'))
        r=report(db,inbox_id=30)
        assert {'id':30,'name':'Empty channel'} in r['inboxes']
        assert all(m['value']==0 for m in r['metrics'])


def test_existing_added_labels_are_snapshot_not_fabricated_period_events(session_factory):
    with session_factory() as db:
        setup(db)
        db.get(ConversationState,1).labels=['加私訊']
        db.get(ConversationState,2).labels=['加私訊'] # same customer
        db.get(ConversationState,4).labels=['已加私讯']
        r=report(db)
        assert r['meta']['current_added']==r['meta']['added_time_unknown']==2
        assert metrics(r)['added']==0
        assert report(db,mode='cohort')['meta']['current_added']==2
        assert report(db,inbox_id=10)['meta']['current_added']==1
        event(db,1,'added','2026-10-30T01:00:00+00:00',label='加私訊')
        r=report(db)
        assert metrics(r)['added']==1 and r['meta']['added_time_unknown']==1
        assert r['_proofs'][('current_added','1:contact:1')][0].source=='current_label'


def test_added_label_observation_with_legacy_side_effects_disabled(session_factory,monkeypatch):
    from live_fixture import setup as live_setup
    from app.conversation_mirror import upsert_mirrors
    live_setup(session_factory,monkeypatch)
    with session_factory() as db:
        for i,labels,revision in [(1,['ai','加私訊'],30),(2,['ai','加私訊'],31),(3,['ai'],29)]:
            payload={'event':'conversation_updated','id':26,'inbox_id':128859,
                'labels':labels,'updated_at':revision}
            w=WebhookEvent(connection_id=1,event='conversation_updated',account_id=180474,
                resource_id='26',idempotency_key=f'bi-observe-{i}',payload=payload,
                received_at=f'2026-10-30T01:0{i}:00+00:00')
            db.add(w);db.flush()
            upsert_mirrors(db,w,side_effects=False)
        db.flush()
        observations=list(db.scalars(select(BiEvent).where(BiEvent.source=='label_observed')))
        assert len(observations)==1 and observations[0].occurred_at=='2026-10-30T01:01:00+00:00'
        assert db.get(ConversationState,1).labels==['ai','加私訊']
        assert not db.scalar(select(LabelEvent.id))


def test_scheduler_clock_alone_does_not_refresh_projection(session_factory):
    with session_factory() as db:
        setup(db)
        s=AutomationSession(id=1,owner_id=1,conversation_state_id=1,environment='live',revision=1,
            controls={'simulation':{'last_wall_at':'2026-10-30T01:00:00+00:00'},'v3':{'next_check_at':'2026-10-31T01:00:00+00:00'}})
        db.add(s);refresh(db,100)
        s.revision+=1
        s.controls={**s.controls,'simulation':{'last_wall_at':'2026-10-30T01:01:00+00:00'}}
        db.commit()
        assert refresh(db)['changed']==0
        s.controls={**s.controls,'v3':{'next_check_at':'2026-11-01T01:00:00+00:00'}}
        db.commit()
        assert refresh(db)['changed']==1


def test_busy_early_conversation_cannot_starve_later_projection(session_factory):
    with session_factory() as db:
        setup(db)
        refresh(db,100)
        for i in range(1,5):
            db.get(BiConversation,i).refreshed_at=f'2026-10-30T00:00:0{i}+00:00'
            db.get(ConversationState,i).version+=1
        db.commit()
        assert refresh(db,limit=1)['changed']==1
        db.get(ConversationState,1).version+=1
        db.commit()
        assert refresh(db,limit=1)['changed']==1
        assert db.get(BiConversation,2).refreshed_at>'2026-10-30T00:00:02+00:00'
