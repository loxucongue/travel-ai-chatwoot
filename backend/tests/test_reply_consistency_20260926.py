import json
import pytest
from sqlalchemy import select

from app.decision_service import _explicit_stop_request, generate_decision
from app.deepseek_evaluation import EvaluationDecision, EvaluationCallError
from app.reception_v2.events import validate_events, merge_events
from app.reception_v2.runtime import _compile_delivery_contract
from app.route_packages import ROUTES
from app.reply_failures import classify_reply_failure
from app.customer_contact_policy import current_contact_refusals, contact_constraint
from app.models import ConversationState, HandoffTask, WebhookEvent, utcnow
from app.live_reply_models import LiveReplyJob
from test_live_reply import setup
import app.live_reply as live


@pytest.mark.parametrize('text', ['我不是說不要再聯絡，只是想先問價格', '他說「不要再聯絡」，我想問價格', '不要用WhatsApp聯絡我'])
def test_stop_candidate_does_not_misclassify(text):
    assert not _explicit_stop_request(text)


@pytest.mark.parametrize('channel',['WhatsApp','whatsapp','LINE'])
def test_channel_refusal_survives_validation_and_policy(channel):
    context={'customer_text':f'不要用{channel}聯絡我'}
    events=validate_events([{'type':'contact_refused','scope':channel,'quote':context['customer_text']}],context)
    slots=merge_events({},events)
    assert not slots['_v2_state'].get('proactive_opt_out')
    assert current_contact_refusals({**context,'v2_events':events})


@pytest.mark.parametrize('route',list(ROUTES))
@pytest.mark.parametrize('handoff',[False,True])
def test_pdf_and_vehicle_are_compiled_and_survive_postprocessing(route,handoff):
    spec=ROUTES[route]
    materials=[{'key':k} for g in spec['groups'].values() for k in g.get('assets',[])]
    context={'engine_version':'v2','module':'reply','route_variant':route,'available_materials':materials,
             'customer_text':'請給我高原資料和車照'}
    decision=EvaluationDecision('handoff' if handoff else 'reply',spec['branch'],'other',
        reply='我會請顧問確認。' if handoff else '資料給您',route_variant=route,
        handoff_reason='knowledge_confirmation_required' if handoff else None,
        v2_events=[{'type':'material_requested','material_kind':k} for k in ['altitude','vehicle']])
    _compile_delivery_contract(context,decision)
    expected=set(spec['groups']['vehicle_reference']['assets'])|set(spec['groups'][spec['policies']['post_capture_material_group']]['assets'])
    assert set(decision.material_keys)==expected
    result,*_=generate_decision(context,model_call=lambda _: (decision,[],'stub'))
    assert set(result.material_keys)==expected
    assert set(k for s in result.v2_delivery_sections for k in s['asset_keys'])==expected


@pytest.mark.parametrize('anchor,expected',[('2026-09-26T08:00:00+00:00','reply'),('2026-09-25T00:00:00+00:00','handoff')])
def test_appointment_uses_current_not_history(anchor,expected):
    decision=EvaluationDecision('reply','peach_9d','other',reply='好的',route_variant='peach_9d_2027',
        v2_events=[{'type':'contact_agreed','contact_at':'2026-09-26T12:00:00+00:00'}])
    _compile_delivery_contract({'trigger_customer_at':anchor,'last_customer_at':'2026-09-24T00:00:00+00:00'},decision)
    assert decision.action==expected


def test_missing_current_timestamp_does_not_fall_back_to_history():
    decision=EvaluationDecision('reply','unclassified','other',reply='好的',v2_events=[{'type':'contact_agreed','contact_at':'2026-09-26T12:00:00+00:00'}])
    with pytest.raises(ValueError,match='current_customer_time_missing'):
        _compile_delivery_contract({'trigger_customer_at':None,'last_customer_at':'2026-09-24T00:00:00+00:00'},decision)


def test_refusal_and_question_delivers_answer_and_preserves_optout(session_factory,monkeypatch):
    fake=setup(session_factory,monkeypatch)
    fake.messages[0]['content']='不要再聯絡，另外小費多少？'
    decision=EvaluationDecision('reply','unclassified','other',reply='小費每人每天30元。',safety_flags=['stop_automation'])
    monkeypatch.setattr(live,'generate_decision',lambda _: (decision,[],'stub',{}))
    live.process_job(1)
    assert fake.sent==[('text',decision.reply)]
    with session_factory() as db:
        state=db.get(ConversationState,1)
        assert state.ai_mode=='enabled'
        assert contact_constraint({'journey':{'slots':live.journey_for(db,state).slots}})[0]=='customer_opted_out'


def test_v2_failure_creates_correct_task_without_customer_receipt(session_factory,monkeypatch):
    fake=setup(session_factory,monkeypatch)
    def failed(_):
        raise EvaluationCallError('v2_reply_verification_failed',[],'test')
    monkeypatch.setattr(live,'generate_decision',failed)
    live.process_job(1)
    assert not fake.sent
    with session_factory() as db:
        assert db.scalar(select(HandoffTask)).reason_code=='ai_factual_verification_failed'


def test_budget_reserves_unknown_failures_and_refuses_excess(tmp_path,monkeypatch):
    from app.model_metering import begin_request,end_request
    path=tmp_path/'cost.json'
    monkeypatch.setenv('MODEL_TEST_COST_LEDGER',str(path))
    payload={'model':'deepseek-flash','max_tokens':1000,'messages':[{'role':'user','content':'x'*500000}]}
    for _ in range(8):
        end_request(begin_request(payload),error=TimeoutError())
    with pytest.raises(RuntimeError,match='budget_exhausted'):
        begin_request(payload)
    data=json.loads(path.read_text())
    assert data['charged_or_reserved_cny']<18
    assert all(not row['usage_known'] for row in data['calls'])


def test_budget_settles_usage_and_includes_audit_threads(tmp_path,monkeypatch):
    from app.model_metering import begin_request,end_request
    path=tmp_path/'cost.json'
    monkeypatch.setenv('MODEL_TEST_COST_LEDGER',str(path))
    handle=begin_request({'model':'deepseek-flash','max_tokens':1000})
    end_request(handle,{'usage':{'prompt_tokens':1000,'completion_tokens':100,'prompt_cache_hit_tokens':500}})
    assert json.loads(path.read_text())['charged_or_reserved_cny']==pytest.approx(.00182)


@pytest.mark.parametrize('source,labels,can_resume', [
    ('customer_stop',['ai'],True), ('customer_stop',['ai','人工接管'],False),
    ('manual',['ai'],False)])
def test_legacy_stop_migrates_only_on_new_message_without_overriding_controls(session_factory,monkeypatch,source,labels,can_resume):
    setup(session_factory,monkeypatch,labels=labels)
    with session_factory() as db:
        state=db.get(ConversationState,1)
        state.ai_mode, state.ai_mode_source, state.ai_label_present='disabled',source,True
        state.labels=labels
        event=WebhookEvent(connection_id=1,event='message_created',account_id=180474,
            resource_id='101',idempotency_key='legacy-return',received_at=utcnow(),payload={
                'event':'message_created','id':101,'account':{'id':180474},'inbox':{'id':128859},
                'message_type':'incoming','private':False,'content_type':'text','content':'小費多少？',
                'created_at':utcnow(),'conversation':{'id':26,'inbox_id':128859,'can_reply':True,
                    'labels':labels,'contact_inbox':{'contact_id':55},
                    'meta':{'sender':{'id':55,'name':'Test','type':'contact'}}}})
        db.add(event); db.flush()
        live.mirror_event(db,event)
        state=db.get(ConversationState,1)
        assert (state.effective_ai_state=='AI_ACTIVE') is can_resume
        slots=live.journey_for(db,state).slots or {}
        if source=='customer_stop':
            assert slots['_v2_state']['proactive_opt_out']
            merged=merge_events(slots,[{'type':'question','quote':'小費多少？','source_message_id':101}])
            assert merged['_v2_state']['proactive_opt_out']
        else:
            assert state.ai_mode=='disabled'


def test_materials_and_future_contact_remain_composable():
    route='peach_9d_2027'
    spec=ROUTES[route]
    decision=EvaluationDecision('reply',spec['branch'],'other',reply='好的',route_variant=route,
        v2_events=[{'type':'material_requested','material_kind':'vehicle'},
                   {'type':'contact_agreed','contact_at':'2026-09-29T12:00:00+08:00'}])
    context={'trigger_customer_at':'2026-09-26T12:00:00+08:00',
             'available_materials':[{'key':k} for k in spec['groups']['vehicle_reference']['assets']]}
    _compile_delivery_contract(context,decision)
    assert decision.action=='handoff'
    assert set(decision.material_keys)==set(spec['groups']['vehicle_reference']['assets'])


@pytest.mark.parametrize('code,category', [
    ('v2_reply_verification_failed','ai_factual_verification_failed'),
    ('model_timeout','ai_service_unavailable'),('v2_event_evidence_missing','ai_decision_invalid'),
    ('v2_requested_material_unavailable','knowledge_confirmation_required'),
    ('channel_send_failed','ai_delivery_failed')])
def test_failure_categories_keep_business_meaning(code,category):
    assert classify_reply_failure(code)[0]==category


def test_merged_current_times_are_compared_as_instants():
    assert live.current_turn_time([{'created_at':'2026-09-26T08:00:00+08:00'},
                                   {'created_at':'2026-09-26T01:00:00+00:00'}])=='2026-09-26T01:00:00+00:00'
    with pytest.raises(ValueError,match='current_customer_time_missing'):
        live.current_turn_time([{'created_at':None},{'created_at':'2026-09-26T01:00:00+00:00'}])


def test_failed_old_turn_does_not_create_task_after_customer_interrupts(session_factory,monkeypatch):
    from app.models import MessageEvent
    fake=setup(session_factory,monkeypatch)
    def interrupted(_):
        with session_factory() as db:
            db.add(MessageEvent(conversation_state_id=1,chatwoot_message_id=101,direction='incoming',
                                private=False,content='換個問題',created_at=utcnow()))
            db.commit()
        raise EvaluationCallError('v2_reply_verification_failed',[],'test')
    monkeypatch.setattr(live,'generate_decision',interrupted)
    live.process_job(1)
    assert not fake.sent
    with session_factory() as db:
        assert db.scalar(select(HandoffTask)) is None
        assert db.get(LiveReplyJob,1).status=='cancelled'
