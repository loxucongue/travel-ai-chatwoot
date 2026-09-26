"""Deterministic scheduler verification against frozen cases; no external calls."""
from copy import deepcopy
from datetime import timedelta
import hashlib
import time
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from app.db import Base
from app.models import Tenant, User, SopDefinition
from app.automation_models import AutomationSession, RehearsalJob
from app.automation_service import iso, dt, sop_snapshot, enroll_rehearsal, advance_sops, add_customer_message, gate
from app.deepseek_evaluation import EvaluationDecision


def replay_sop_case(context):
    start=time.monotonic()
    engine=create_engine("sqlite://")
    Base.metadata.create_all(engine)
    outcomes={}
    try:
        with Session(engine,expire_on_commit=False) as db:
            db.add(Tenant(id=1,name="isolated-sop-replay"))
            db.add(User(id=1,email="replay@invalid.test",display_name="Rehearsal",password_hash="unusable",role="admin"))
            db.flush()
            baseline="2026-08-26T02:00:00+00:00"
            for scenario in ("on_time","customer_reply","human","expired","window","missing_media","paused","dependency_frequency"):
                s=AutomationSession(owner_id=1,mode="sop",virtual_now=baseline,controls={"can_reply":True,"channel":"facebook","ai_enabled":True},messages=[*deepcopy(context.get("context_messages",[])),{"direction":"incoming","created_at":baseline,"content":context["customer_text"]}])
                db.add(s)
                nodes=[{"key":"first","content":"您還有其他行程問題嗎？","content_type":"text","basis":"enrollment","delay_minutes":1}]
                if scenario=="missing_media":nodes[0].update({"content_type":"image","media_id":None})
                if scenario=="dependency_frequency":nodes.append({"key":"second","content":"第二節點","content_type":"text","basis":"previous_node","delay_minutes":0})
                sop=SopDefinition(tenant_id=1,name=scenario,status="paused" if scenario=="paused" else "running",created_by=1,nodes=nodes)
                db.add(sop);db.flush()
                try:
                    v=sop_snapshot(db,sop,1)
                except ValueError as exc:
                    if scenario != "missing_media" or str(exc) != "material_unavailable":
                        raise
                    outcomes[scenario]=[{"status":"blocked_at_publication","reason":str(exc)}]
                    continue
                enrollment=enroll_rehearsal(db,s,v)
                s.virtual_now=iso(dt(baseline)+timedelta(minutes=1))
                if scenario=="customer_reply":add_customer_message(db,s,"[new inbound]","new")
                if scenario=="human":s.controls={**s.controls,"human":True}
                if scenario=="expired":s.virtual_now=iso(dt(baseline)+timedelta(minutes=17))
                if scenario=="window":
                    s.messages=[{"direction":"incoming","content":context["customer_text"],"created_at":iso(dt(baseline)-timedelta(hours=24))}]
                advance_sops(db,s)
                db.flush()
                jobs=db.scalars(select(RehearsalJob).where(RehearsalJob.enrollment_id==enrollment.id).order_by(RehearsalJob.id)).all()
                outcomes[scenario]=[{"status":j.status,"reason":j.reason} for j in jobs]
            assert outcomes["on_time"][0]["status"]=="simulated_delivered"
            assert outcomes["customer_reply"][0]["status"]=="cancelled"
            assert outcomes["human"][0]["reason"]=="human_or_contact_block"
            assert outcomes["expired"][0]["reason"]=="expired"
            assert outcomes["window"][0]["reason"]=="automatic_window_closed"
            assert outcomes["missing_media"][0]["reason"]=="material_unavailable"
            assert outcomes["paused"][0]["status"]=="scheduled"
            assert outcomes["dependency_frequency"][1]["status"]=="simulated_delivered"
    finally:
        engine.dispose()
    return EvaluationDecision(action="no_action",branch="unclassified",intent="other"),[],hashlib.sha256(context["customer_text"].encode()).hexdigest(),{"scenarios":outcomes,"assertions_passed":8,"total_ms":int((time.monotonic()-start)*1000),"model_ms":0,"request_count":0,"outbound":False,"permission_source":"simulated_historical","clock":"normalized_10am_shanghai"}
