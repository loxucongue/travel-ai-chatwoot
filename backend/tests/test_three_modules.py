from datetime import timedelta
from sqlalchemy import select

from app.models import User, OutboundMessage, SopDefinition, utcnow
from app.automation_models import AutomationSession, AutomationRun, SopVersion, RehearsalJob
from app.automation_service import (dt, iso, add_customer_message, queue_passive, process_automation_run,
    gate, reserve_touch, sop_snapshot, enroll_rehearsal, advance_sops, create_cycle, queue_wakeup)
from app.decision_service import generate_decision
from app.deepseek_evaluation import EvaluationDecision, request_payload


def make_session(db, **kwargs):
    row=AutomationSession(owner_id=db.scalar(select(User.id)),virtual_now="2026-08-26T02:00:00+00:00",controls={"can_reply":True,"channel":"facebook","ai_enabled":True},**kwargs)
    db.add(row)
    db.flush()
    return row


def test_memory_is_model_driven_and_no_label_leakage():
    captured = {}

    def model(payload):
        captured.update(payload)
        return EvaluationDecision("no_action", "unclassified", "other"), [], "hash"

    decision, _, _, trace = generate_decision({
        "customer_text": "原本同行兩位夫妻突然無法陪同前往，還在找旅伴",
        "context_messages": [],
        "memory": {},
    }, model_call=model)
    assert captured["memory"] == {}
    assert decision.slots == {}
    assert trace["model_memory_checks"] == {}
    payload=request_payload({"customer_text":"桃花9日","context_messages":[],"expected_branch":"NEVER_LEAK","reference_answer":"FUTURE_ANSWER"})
    assert "NEVER_LEAK" not in str(payload) and "FUTURE_ANSWER" not in str(payload)
    attachment_payload = request_payload({
        "customer_text": "route question",
        "context_messages": [],
        "current_attachments": [{"file_type": "image", "extension": "jpg"}],
    })
    assert "current_attachments" in attachment_payload["messages"][-1]["content"]


def test_decision_service_never_persists_contact_data_as_generic_memory():
    def model(_payload):
        return EvaluationDecision(
            "handoff", "peach_9d", "contact",
            route_variant="peach_9d_2027",
            lead_action="captured",
            contact_values={"wechat": "wx_test_2027"},
            slots={"contact_wechat": "wx_test_2027", "party_size": "2"},
            slot_evidence={"contact_wechat": "wx_test_2027", "party_size": "2"},
        ), [], "hash"

    decision, _, _, trace = generate_decision({
        "customer_text": "2 people, WeChat wx_test_2027",
        "context_messages": [],
        "memory": {},
    }, model_call=model)
    assert decision.contact_values == {"wechat": "wx_test_2027"}
    assert decision.slots == {"party_size": "2"}
    assert "contact_wechat" in trace["rejected_slots"]


def test_model_clears_cancelled_party_semantics_before_persistence():
    def model(_payload):
        return EvaluationDecision(
            "reply", "unclassified", "other", reply="了解",
            slots={},
            slot_evidence={},
        ), [], "hash"

    decision, _, _, _ = generate_decision({
        "customer_text": "原本同行兩位夫妻突然無法陪同前往，召兵買馬中",
        "context_messages": [],
        "memory": {},
    }, model_call=model)
    assert "party_size" not in decision.slots


def test_historical_out_of_window_month_is_answered_without_handoff():
    def model(_payload):
        return EvaluationDecision(
            "reply", "peach_11d", "itinerary",
            reply="10月22日不在目前頁面參考區間內；住宿可先依頁面資料說明。",
            route_variant="peach_11d_2027", content_group_key="hotel_reference",
            evidence_refs=["route.shared.hotel_reference"],
        ), [], "hash"

    decision, _, _, _ = generate_decision({
        "customer_text": "住宿也包含嗎",
        "context_messages": [{"direction": "incoming", "content": "大約是10月22號"}],
        "route_variant": "peach_11d_2027",
        "memory": {},
    }, model_call=model)
    assert decision.action == "reply"
    assert decision.handoff_reason is None


def test_unsupported_product_is_answered_with_a_model_owned_boundary():
    def model(_payload):
        return EvaluationDecision(
            "reply", "unclassified", "other",
            reply="目前可完整介紹桃花9日與桃花加珠峰11日，阿里長天數行程沒有對應資料。",
            route_variant="", content_group_key="",
            evidence_refs=[],
        ), [], "hash"

    decision, _, _, _ = generate_decision({
        "customer_text": "有包含林芝桃花和阿里的長天數行程嗎",
        "context_messages": [],
        "route_variant": "peach_11d_2027",
        "memory": {},
    }, model_call=model)
    assert decision.action == "reply"
    assert decision.handoff_reason is None


def test_window_is_independent_from_can_reply(session_factory):
    with session_factory() as db:
        s=make_session(db,messages=[{"direction":"incoming","created_at":"2026-08-25T02:05:00+00:00"}])
        assert gate(s,s.virtual_now)=="automatic_window_closed"
        s.controls={**s.controls,"human":True}
        assert gate(s,s.virtual_now)=="human_or_contact_block"


def test_duplicate_coalesce_attachment_and_stale_result(session_factory,monkeypatch):
    with session_factory() as db:
        s=make_session(db)
        add_customer_message(db,s,"第一条","one")
        add_customer_message(db,s,"第二条","two")
        add_customer_message(db,s,"第二条","two")
        assert s.generation==2 and len(s.messages)==2
        s.due_at=iso(dt(utcnow())-timedelta(seconds=1))
        db.commit()
        assert queue_passive(db)
        run=db.scalar(select(AutomationRun))
        assert run.input_snapshot['customer_text']=="第一条\n第二条"
        def fake(_payload):
            s.generation+=1
            db.commit()
            return EvaluationDecision(action="reply",branch="peach_9d",intent="other",reply="draft"),[],"hash",{}
        monkeypatch.setattr("app.automation_service.generate_decision",fake)
        assert process_automation_run(db)
        assert db.get(AutomationRun,run.id).status=="discarded"
        assert db.scalar(select(OutboundMessage)) is None


def test_shared_touch_cap_and_unknown_never_retries(session_factory):
    from app.automation_models import TouchReservation
    with session_factory() as db:
        now="2026-08-26T02:00:00+00:00"
        assert reserve_touch(db,"contact:1","sop:1",now)
        assert not reserve_touch(db,"contact:1","wake:1",now)
        row=db.scalar(select(TouchReservation))
        row.status="submission_unknown"
        db.flush()
        assert not reserve_touch(db,"contact:1","sop:2","2026-08-28T02:00:00+00:00")


def test_sop_immutable_dependency_cancellation(session_factory):
    with session_factory() as db:
        s=make_session(db,messages=[{"direction":"incoming","content":"hi","created_at":"2026-08-26T01:00:00+00:00"}])
        sop=SopDefinition(tenant_id=1,name="flow",status="running",created_by=s.owner_id,nodes=[{"key":"a","content":"first","content_type":"text","delay_minutes":0},{"key":"b","content":"second","content_type":"text","basis":"previous_node","delay_minutes":5}])
        db.add(sop);db.flush()
        version=sop_snapshot(db,sop,s.owner_id)
        enrollment=enroll_rehearsal(db,s,version)
        assert enroll_rehearsal(db,s,version).id==enrollment.id
        sop.nodes=[{"key":"a","content":"CHANGED"}]
        advance_sops(db,s)
        jobs=db.scalars(select(RehearsalJob).order_by(RehearsalJob.id)).all()
        assert jobs[0].status=="simulated_delivered"
        assert s.messages[-1]['content']=="first"
        assert jobs[1].scheduled_at=="2026-08-26T02:05:00+00:00"
        add_customer_message(db,s,"replied","three")
        db.flush()
        assert jobs[1].status=="cancelled"


def test_wakeup_requires_real_customer_and_confirmed_reply(session_factory):
    with session_factory() as db:
        s=make_session(db,messages=[{"direction":"incoming","content":"hi","created_at":"2026-08-26T01:00:00+00:00"},{"direction":"outgoing","content":"draft","status":"draft","created_at":"2026-08-26T01:01:00+00:00"}])
        assert create_cycle(db,s) is None
        s.messages=[s.messages[0],{**s.messages[1],"status":"simulated_delivered"}]
        c=create_cycle(db,s)
        assert create_cycle(db,s).id==c.id
        s.virtual_now="2026-08-26T03:01:00+00:00"
        queue_wakeup(db,s,c)
        assert c.status=="queued"
        queue_wakeup(db,s,c)
        assert c.evaluation_count==1


def test_api_isolated_no_real_writes_and_csrf(authenticated,session_factory):
    client,csrf=authenticated
    assert client.post('/v1/playground/sessions',json={}).status_code==403
    response=client.post('/v1/playground/sessions',json={},headers={'X-CSRF-Token':csrf})
    assert response.status_code==201
    sid=response.json()['id']
    response=client.post(f'/v1/playground/sessions/{sid}/messages',json={'content':'你好','client_key':'a'},headers={'X-CSRF-Token':csrf})
    assert response.status_code==202
    with session_factory() as db: assert db.scalar(select(OutboundMessage)) is None


def test_scope_and_policy_conflict(authenticated,session_factory):
    client,csrf=authenticated
    h={'X-CSRF-Token':csrf}
    assert client.patch('/v1/automation/reply-policy',json={'version':1},headers=h).status_code==200
    assert client.patch('/v1/automation/reply-policy',json={'version':1},headers=h).status_code==409


def test_redaction_preserves_dates_not_contact_details():
    from app.automation_api import safe_text
    at="2026-08-26T02:00:00+00:00"
    result=safe_text({'created_at':at,'content':'mail a.person@example.com; line: test_id; 13812345678'})
    assert result['created_at']==at
    assert 'a.person@example.com' not in result['content'] and 'test_id' not in result['content'] and '13812345678' not in result['content']


def test_scheduler_replay_has_eight_assertions():
    from app.sop_replay import replay_sop_case
    _decision,calls,_digest,trace=replay_sop_case({'customer_text':'了解行程','context_messages':[]})
    assert not calls and trace['assertions_passed']==8


def test_selected_wakeup_policy_is_frozen(session_factory):
    from app.automation_models import WakeupPolicy
    with session_factory() as db:
        policy=WakeupPolicy(name='three hours',config={'threshold_minutes':180},created_by=1,status='running')
        db.add(policy);db.flush()
        s=make_session(db,messages=[{'direction':'incoming','created_at':'2026-08-26T01:00:00+00:00'},{'direction':'outgoing','created_at':'2026-08-26T01:01:00+00:00','status':'delivered'}])
        s.controls={**s.controls,'wakeup_policy_id':policy.id}
        c=create_cycle(db,s)
        assert c.due_at=='2026-08-26T04:01:00+00:00' and c.policy_id==policy.id
        policy.config={'threshold_minutes':30}
        assert create_cycle(db,s).policy_snapshot['threshold_minutes']==180


def test_supervisor_scope_and_publish_denied(authenticated,session_factory):
    from app.models import InboxBinding, UserInboxScope
    client,csrf=authenticated
    with session_factory() as db:
        db.add_all([InboxBinding(id=1,tenant_id=1,chatwoot_inbox_id=101,name='allowed'),InboxBinding(id=2,tenant_id=1,chatwoot_inbox_id=102,name='forbidden')])
        db.get(User,1).role='supervisor'
        db.add(UserInboxScope(user_id=1,inbox_binding_id=1));db.commit()
    h={'X-CSRF-Token':csrf}
    assert client.post('/v1/playground/sessions',json={'inbox_binding_id':2},headers=h).status_code==403
    assert client.post('/v1/sops',json={'name':'bad','inbox_ids':[102]},headers=h).status_code==403
    good=client.post('/v1/sops',json={'name':'good','inbox_ids':[101]},headers=h)
    assert good.status_code==200
    assert client.post(f"/v1/sops/{good.json()['id']}/publish",headers=h).status_code==403
    assert client.get('/v1/evaluation/runs').status_code==403


def test_repeated_label_events_invalidate_generation(session_factory):
    from app.models import ChatwootConnection,InboxBinding,ConversationState
    from app.webhook_ingest import ingest_payload
    with session_factory() as db:
        connection=ChatwootConnection(tenant_id=1,account_id=180474,base_url='https://example.test',connection_key='test')
        db.add(connection)
        db.add(InboxBinding(id=1,tenant_id=1,chatwoot_inbox_id=128859,name='FB'))
        db.add(ConversationState(id=1,tenant_id=1,inbox_binding_id=1,chatwoot_conversation_id=26));db.flush()
        s=make_session(db,environment='shadow',conversation_state_id=1,inbox_binding_id=1)
        db.commit()
        p={'event':'conversation_updated','account':{'id':180474},'id':26,'labels':['ai']}
        assert not ingest_payload(db,connection,p).duplicate
        first=s.generation
        assert ingest_payload(db,connection,p).duplicate
        assert s.generation==first
        p['labels']=['人工接管']
        assert not ingest_payload(db,connection,p).duplicate
        assert s.generation>first and s.controls['labels']==['人工接管']


def test_expired_lease_recovers_without_outbound(session_factory,monkeypatch):
    with session_factory() as db:
        s=make_session(db,messages=[{'direction':'incoming','content':'hello','created_at':'2026-08-26T01:00:00+00:00'}])
        run=AutomationRun(session_id=s.id,module='reply',generation=0,idempotency_key='restart',status='processing',lease_until='2000-01-01T00:00:00+00:00',input_snapshot={})
        db.add(run);db.commit()
        monkeypatch.setattr('app.automation_service.generate_decision',lambda _: (EvaluationDecision(action='no_action',branch='unclassified',intent='other'),[],'hash',{}))
        assert process_automation_run(db)
        assert run.status=='completed' and db.scalar(select(OutboundMessage)) is None


def test_live_boundary_blocked_before_database_or_network(monkeypatch):
    import pytest
    from app.controlled_delivery import submit_once
    from app.chatwoot import ChatwootError
    from app.config import settings
    monkeypatch.setattr(settings,'outbound_mode','disabled')
    with pytest.raises(ChatwootError):submit_once(None,None,1,'blocked','text','sop',1)


def test_deepseek_retry_and_format_repair_budget(monkeypatch):
    import httpx,json
    from app import deepseek_evaluation as adapter
    monkeypatch.setattr(adapter.settings,'deepseek_api_key','test-not-real')
    monkeypatch.setattr(adapter.time,'sleep',lambda _:None)
    calls=[]
    def post(payload,timeout):
        calls.append(timeout)
        request=httpx.Request('POST','https://example.test')
        if len(calls)==1:return httpx.Response(429,request=request)
        content='not json' if len(calls)==2 else json.dumps({'action':'no_action','branch':None,'intent':'other'})
        return httpx.Response(200,request=request,json={'choices':[{'message':{'content':content}}]})
    monkeypatch.setattr(adapter,'_post_with_deadline',post)
    result,logs,_=adapter.call_deepseek({'customer_text':'hi','context_messages':[]})
    assert result.branch=='unclassified' and len(calls)==3 and all(t<=20 for t in calls)
    assert [x['status'] for x in logs]==['retry','invalid_json','completed']


def test_deepseek_allows_two_model_owned_contract_repairs(monkeypatch):
    import httpx,json
    from app import deepseek_evaluation as adapter
    monkeypatch.setattr(adapter.settings,'deepseek_api_key','test-not-real')
    calls=[]
    def post(payload,timeout):
        calls.append(payload)
        reply = '第一個問題？第二個問題？' if len(calls) < 3 else '只保留一個問題？'
        content=json.dumps({
            'action':'reply','branch':'unclassified','intent':'other','reply':reply,
        },ensure_ascii=False)
        return httpx.Response(200,request=httpx.Request('POST','https://example.test'),
                              json={'choices':[{'message':{'content':content}}]})
    monkeypatch.setattr(adapter,'_post_with_deadline',post)
    result,logs,_=adapter.call_deepseek({'customer_text':'hi','context_messages':[]})
    assert result.reply=='只保留一個問題？'
    assert len(calls)==3
    assert [x['status'] for x in logs]==['invalid_json','invalid_json','completed']


def test_deepseek_auth_error_never_retries(monkeypatch):
    import httpx,pytest
    from app import deepseek_evaluation as adapter
    monkeypatch.setattr(adapter.settings,'deepseek_api_key','test-not-real')
    calls=[]
    def post(payload,timeout):
        calls.append(timeout)
        return httpx.Response(401,request=httpx.Request('POST','https://example.test'))
    monkeypatch.setattr(adapter,'_post_with_deadline',post)
    with pytest.raises(adapter.EvaluationCallError):adapter.call_deepseek({'customer_text':'hi','context_messages':[]})
    assert len(calls)==1


def test_contact_reservation_concurrent_claim(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from app.automation_models import TouchReservation
    engine=create_engine('sqlite:///'+str(tmp_path/'concurrent.db'),connect_args={'timeout':5})
    TouchReservation.__table__.create(engine)
    barrier=Barrier(2)
    def claim(owner):
        with Session(engine) as db:
            barrier.wait()
            result=reserve_touch(db,'same-contact',owner,'2026-08-26T02:00:00+00:00')
            db.commit()
            return result
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(claim,['sop','wake']))==[False,True]
    engine.dispose()


def test_label_rehearsal_enrollment_does_not_touch_real_jobs(authenticated,session_factory):
    from app.models import SopJob, ChatwootLabel
    from app.automation_models import RehearsalEnrollment
    client,csrf=authenticated
    with session_factory() as db:
        db.add(ChatwootLabel(tenant_id=1,chatwoot_label_id=1,title="qa-followup"));db.commit()
    h={'X-CSRF-Token':csrf}
    sop=client.post('/v1/sops',json={'name':'label rehearsal','trigger_type':'label','trigger_labels':['qa-followup'],'nodes':[{'key':'n1','content':'fixed','delay_minutes':5}]},headers=h).json()
    assert client.post(f"/v1/sops/{sop['id']}/publish",headers=h).status_code==200
    session=client.post('/v1/playground/sessions',json={},headers=h).json()
    response=client.post(f"/v1/playground/sessions/{session['id']}/advance",json={'generation':0,'labels':['qa-followup']},headers=h)
    assert response.status_code==200
    with session_factory() as db:
        assert db.scalar(select(RehearsalEnrollment)) is not None
        assert db.scalar(select(SopJob)) is None


def test_upload_bind_preview_missing_file(authenticated,session_factory,tmp_path,monkeypatch):
    import base64
    from app.config import settings
    from app.business_knowledge import seed_business_knowledge
    from app.models import MaterialAsset
    monkeypatch.setattr(settings,'upload_dir',str(tmp_path))
    client,csrf=authenticated
    with session_factory() as db:
        v=seed_business_knowledge(db)
        a=MaterialAsset(knowledge_version_id=v.id,asset_key='qa-only',source_path=str(tmp_path/'missing.png'),display_name='QA structure',available=False)
        db.add(a);db.commit();asset_id=a.id
    h={'X-CSRF-Token':csrf}
    png=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aGxkAAAAASUVORK5CYII=')
    response=client.post('/v1/media',files={'file':('qa.png',png,'image/png')},headers=h)
    assert response.status_code==200
    mid=response.json()['id']
    assert client.get(f'/v1/media/{mid}/preview').status_code==200
    assert client.post(f'/v1/knowledge/assets/{asset_id}/bind',json={'media_id':mid},headers=h).status_code==200
    with session_factory() as db:
        a=db.get(MaterialAsset,asset_id)
        assert a.available and a.file_hash
        from pathlib import Path
        Path(a.source_path).unlink()
    assert client.get(f'/v1/media/{mid}/preview').status_code==404
