"""Actual ingress paths must identify the current turn, not a previous answer."""
from sqlalchemy import select
from app.automation_models import AutomationRun, AutomationSession
from app.automation_service import add_customer_message, queue_passive
from app.deepseek_evaluation import EvaluationDecision
from app.models import ConversationState, InboxBinding, utcnow
from app.live_reply_models import LiveReplyJob
from app.reception_v2 import ENGINE_RELEASE_ID
from app.reception_v2.events import validate_events, merge_events, rebuild_answers


def test_live_worker_passes_current_message_identity(session_factory,monkeypatch):
    import app.live_reply as live
    from test_live_reply import setup
    setup(session_factory,monkeypatch)
    with session_factory() as db:
        state,job=db.get(ConversationState,1),db.get(LiveReplyJob,1)
        state.ai_engine_version=job.engine_version='v2'
        state.ai_engine_release_id=job.engine_release_id=ENGINE_RELEASE_ID
        expected=job.trigger_message_id
        db.commit()
    seen=[]
    def model(context):
        seen.append(context)
        return EvaluationDecision('no_action','unclassified','other'),[],'fixture',{}
    monkeypatch.setattr(live,'generate_decision',model)
    live.process_job(1)
    assert seen and seen[0].get('source_message_id')==expected
    assert seen[0].get('source_message_ids')==[100]


def test_rehearsal_batch_keeps_current_trigger_and_all_source_ids(session_factory):
    with session_factory() as db:
        db.add(InboxBinding(id=1,tenant_id=1,chatwoot_inbox_id=1,name='isolated'))
        session=AutomationSession(owner_id=1,inbox_binding_id=1,mode='journey',environment='playground',
            engine_version='v2',engine_release_id=ENGINE_RELEASE_ID,virtual_now=utcnow(),
            controls={'can_reply':True,'ai_enabled':True,'history_complete':True,'human':False,'labels':[]})
        db.add(session);db.commit()
        add_customer_message(db,session,'我們6位','first')
        add_customer_message(db,session,'3月28日出發','second')
        session.due_at=utcnow();db.commit()
        assert queue_passive(db,environment='playground',session_id=session.id)
        run=db.scalar(select(AutomationRun))
        assert len(run.input_snapshot['source_message_ids'])==2
        assert run.input_snapshot.get('source_message_id')==run.input_snapshot['source_message_ids'][-1]


def test_queued_batch_identity_does_not_reuse_previous_journey_trigger():
    context={'customer_text':'在哪集合？','source_message_ids':['current'],
             'journey':{'last_trigger_message_id':'previous'}}
    events=validate_events([{'type':'question','quote':'在哪集合？'}],context)
    assert events[0]['source_message_id']=='current'


def test_repeated_question_is_pending_until_its_own_answer_receipt():
    old={'type':'question','quote':'在哪集合？','source_message_id':1}
    new={**old,'source_message_id':2}
    slots=merge_events({},[old])
    slots=rebuild_answers(slots,[{'route':'route','fact_ids':['arrival'],'questions':[old]}])
    slots=merge_events(slots,[new])
    assert [q['status'] for q in slots['_v2_state']['questions']]==['answer_provided','pending']
