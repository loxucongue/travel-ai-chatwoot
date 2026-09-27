from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import secrets
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session

from app.automation_models import (AutomationSession, AutomationRun, ReplyPolicy, SopVersion,
    RehearsalEnrollment, RehearsalJob, WakeupPolicy, SilenceCycle, TouchReservation)
from app.models import ConversationState, SopDefinition, StoredMedia, InboxBinding, Tenant, User, utcnow
from app.route_reply import automatic_content_already_covered
from app.decision_service import generate_decision
from app.deepseek_evaluation import EvaluationCallError
from app.delivery_plan import DELIVERY_PLAN_VERSION, delivery_mode_for, ordered_delivery_parts, expand_static_delivery_nodes
from app.sop_schedule import content_items, relative_delay, schedule_at
from app.material_library import (candidate_materials, tenant_for_session, resolve_materials, material_info,
    previous_delivery, record_delivery, freeze_nodes)
from app.route_reply import (
    append_deferred_initial_follow_up,
    deferred_initial_follow_up,
    deferred_follow_up_group,
    journey_context_from_values,
    playbook_prompt,
    prepare_route_reply_values,
    update_content_progress_values,
    make_route_snapshot,
    ROUTE_SNAPSHOTS_KEY,
    route_snapshot_from_values,
    frozen_sop_nodes,
)
from app.lead_capture import bind_lead_request
from app.route_packages import DEFAULT_INITIAL_DELIVERY_INTERVAL_SECONDS, ROUTES, UNCLASSIFIED_SOP_NAME
from app.reception_config import (
    configured_silence_nodes,
    configured_silence_ttl_hours,
    effective_reception_policy,
)
from app.web_knowledge import enrich_context_with_web_knowledge

DEFAULT_REPLY = {"enabled": True, "merge_wait_seconds": 2, "merge_max_seconds": 5, "backlog_seconds": 300}
DEFAULT_WAKEUP = {"threshold_minutes": 120, "inbox_ids": [], "frequency_hours": 24, "expires_minutes": 15, "active_start": 9, "active_end": 21}
BLOCK_LABELS = {"人工接管", "客诉", "客訴", "拒绝联系", "拒絕聯繫", "黑名单", "黑名單", "AI关闭", "ai_off", "已留资", "已留資", "已成交"}
TERMINAL = {"simulated_delivered", "already_provided", "skipped", "verification_blocked", "skipped_model_failure", "cancelled", "blocked", "expired"}
JOURNEY_TERMINAL = {"completed", "stopped"}


def dt(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        result = result.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    return result.astimezone(timezone.utc)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def next_timeline_sequence(session: AutomationSession) -> int:
    controls = dict(session.controls or {})
    existing = [
        int(item.get("timeline_sequence") or 0)
        for item in [
            *(session.messages or []),
            *(controls.get("timeline_events") or []),
        ]
    ]
    sequence = max([int(controls.get("timeline_sequence") or 0), *existing]) + 1
    controls["timeline_sequence"] = sequence
    session.controls = controls
    return sequence


def append_timeline_event(session: AutomationSession, event_type: str, content: str, *, at: str | None = None) -> None:
    sequence = next_timeline_sequence(session)
    controls = dict(session.controls or {})
    events = list(controls.get("timeline_events", []))
    events.append({
        "id": f"event:{session.id or 'new'}:{len(events) + 1}",
        "event_type": event_type,
        "content": content,
        "created_at": at or session.virtual_now,
        "timeline_sequence": sequence,
    })
    controls["timeline_events"] = events[-200:]
    session.controls = controls


def simulation_state(session: AutomationSession) -> dict:
    return dict((session.controls or {}).get("simulation") or {})


def set_simulation_state(session: AutomationSession, state: dict) -> None:
    session.controls = {**(session.controls or {}), "simulation": state}


def mark_session_content_group(session: AutomationSession, group_key: str | None) -> None:
    """Keep SOP and AI on one shared route-progress ledger in rehearsals."""
    if not group_key:
        return
    journey = dict((session.controls or {}).get("journey") or {})
    journey["slots"], journey["sent_content_groups"], _ = update_content_progress_values(
        str(journey.get("route_variant") or session.controls.get("route_variant") or ""),
        journey.get("slots"), journey.get("sent_content_groups"), group_key, complete=True,
    )
    session.controls = {**session.controls, "journey": journey}


def record_session_content_delivery(
    session: AutomationSession,
    group_key: str | None,
    *,
    text_delivered: bool = False,
    asset_keys: list[str] | None = None,
    delivered_text: str | None = None,
) -> bool:
    if not group_key:
        return False
    journey = dict((session.controls or {}).get("journey") or {})
    journey["slots"], journey["sent_content_groups"], completed = update_content_progress_values(
        str(journey.get("route_variant") or session.controls.get("route_variant") or ""),
        journey.get("slots"), journey.get("sent_content_groups"), group_key,
        text_delivered=text_delivered, asset_keys=asset_keys, delivered_text=delivered_text,
    )
    session.controls = {**session.controls, "journey": journey}
    return completed


def reply_policy(db: Session, inbox_binding_id: int | None = None) -> tuple[dict, int]:
    global_row = db.scalar(select(ReplyPolicy).where(ReplyPolicy.scope_key == "global"))
    row = db.scalar(select(ReplyPolicy).where(ReplyPolicy.scope_key == f"inbox:{inbox_binding_id}")) if inbox_binding_id else None
    config = {**DEFAULT_REPLY, **(global_row.config if global_row else {}), **(row.config if row else {})}
    if global_row and not global_row.config.get("enabled", True):
        config["enabled"] = False
    return config, (row or global_row).version if (row or global_row) else 1


def confirmed(message: dict) -> bool:
    return message.get("direction") == "outgoing" and not message.get("private") and message.get("status") in ("sent", "delivered", "read", "simulated_delivered")


def blocking_labels(db: Session | None = None) -> set[str]:
    labels = set(BLOCK_LABELS)
    if db is not None:
        from app.operations import setting_value
        mappings = setting_value(db, "label_mappings", {})
        for key in ("handoff_labels", "lead_labels", "conversion_labels", "contact_block_labels"):
            labels.update(mappings.get(key, []))
    return labels


def control_block(session: AutomationSession, db: Session | None = None) -> str | None:
    c = session.controls or {}
    if c.get("contact_state_unknown"):
        return "contact_state_unknown"
    if c.get("human") or set(c.get("labels", []) + c.get("contact_labels", [])) & blocking_labels(db):
        return "human_or_contact_block"
    if c.get("ai_enabled") is False or c.get("account_enabled") is False or c.get("inbox_enabled") is False:
        return "ai_disabled"
    if not c.get("can_reply", False):
        return "channel_cannot_reply"
    return None


def gate(session: AutomationSession, at: str, proactive: bool = False, db: Session | None = None) -> str | None:
    blocked = control_block(session, db)
    if blocked:
        return blocked
    c = session.controls or {}
    incoming = [m for m in session.messages if m.get("direction") == "incoming" and not m.get("private") and dt(m["created_at"]) <= dt(at)]
    if not incoming:
        return "trusted_customer_message_missing"
    if c.get("channel", "facebook") not in ("facebook", "instagram", "Channel::FacebookPage", "Channel::Instagram"):
        return "channel_not_verified"
    latest = max(dt(m["created_at"]) for m in incoming)
    if dt(at) >= latest + timedelta(hours=24, minutes=-5):
        return "automatic_window_closed"
    if proactive:
        from app.customer_contact_policy import contact_constraint
        reason, _ = contact_constraint({'journey': session.controls.get('journey'), 'memory': session.memory, 'now': at})
        if reason:
            return reason
        if c.get("proactive_pause"):
            return "customer_paused_proactive"
        if session.due_at:
            return "passive_reply_pending"
        # A playground journey is an accelerated, isolated product preview. Its
        # virtual clock can cross midnight within seconds, so applying the live
        # contact window here can permanently kill the remaining rehearsal SOP.
        # Live and shadow sessions still retain the real 09:00-21:00 guard.
        if session.environment != "playground":
            local_hour = dt(at).astimezone(ZoneInfo("Asia/Shanghai")).hour
            if not 9 <= local_hour < 21:
                return "outside_contact_hours"
    return None


def reserve_touch(db: Session, contact_key: str, owner_key: str, at: str, frequency_hours: int = 24, *, continuation: bool = False) -> bool:
    expiry = iso(dt(at) + timedelta(hours=max(24, frequency_hours)))
    stmt = insert(TouchReservation).values(contact_key=contact_key, owner_key=owner_key, status="reserved", reserved_at=at, expires_at=expiry)
    eligible = TouchReservation.expires_at <= at
    if continuation:
        eligible = eligible | ((TouchReservation.owner_key == owner_key) & TouchReservation.status.in_([
            "reserved", "simulated_confirmed", "submitted", "confirmed"
        ]))
    stmt = stmt.on_conflict_do_update(index_elements=[TouchReservation.contact_key], set_={"owner_key": owner_key, "status": "reserved", "reserved_at": at, "expires_at": expiry, "confirmed_at": None}, where=eligible & (TouchReservation.status != "submission_unknown"))
    return bool(db.execute(stmt).rowcount)


def touch_key(session: AutomationSession) -> str:
    if session.environment == "shadow" and session.controls.get("contact_key"):
        return f"shadow:contact:{session.controls['contact_key']}"
    return f"{session.environment}:session:{session.id}"


def subject_key(db: Session, session: AutomationSession) -> str:
    conversation = db.get(ConversationState, session.conversation_state_id) if session.conversation_state_id else None
    if conversation:
        identity = f"contact:{conversation.tenant_id}:{conversation.contact_id}" if conversation.contact_id else f"conversation:{conversation.tenant_id}:{conversation.id}"
        return f"{session.environment}:{identity}"
    return f"{session.environment}:session:{session.id}"


def stop_enrollment(db: Session, enrollment: RehearsalEnrollment, reason: str) -> None:
    enrollment.status = "cancelled"
    db.execute(update(RehearsalJob).where(RehearsalJob.enrollment_id == enrollment.id, RehearsalJob.status.in_(["scheduled", "waiting_dependency", "model_pending"])).values(status="cancelled", reason=reason))


def cancel_generation(db: Session, session: AutomationSession, reason: str, *, cancel_sops: bool = True) -> None:
    session.messages = [
        {**message, "status": "cancelled", "reason": reason}
        if message.get("status") == "draft" else message
        for message in session.messages
    ]
    db.execute(update(AutomationRun).where(AutomationRun.session_id == session.id, AutomationRun.status == "pending").values(status="discarded", error_code=reason, completed_at=utcnow()))
    enrollments = db.scalars(select(RehearsalEnrollment).where(RehearsalEnrollment.session_id == session.id, RehearsalEnrollment.status == "active")).all()
    if cancel_sops:
        for enrollment in enrollments:
            stop_enrollment(db, enrollment, reason)
    db.execute(update(SilenceCycle).where(SilenceCycle.session_id == session.id, SilenceCycle.status.in_(["candidate", "queued", "deferred"])).values(status="cancelled", reason=reason))


def apply_controls(db: Session, session: AutomationSession, changes: dict) -> set[str]:
    changes = {k: sorted(set(v)) if k in ("labels", "contact_labels") else v for k, v in changes.items()}
    old = session.controls or {}
    changes = {k: v for k, v in changes.items() if (sorted(set(old.get(k, []))) if k in ("labels", "contact_labels") else old.get(k)) != v}
    if not changes:
        return set()
    added = set(changes.get("labels", [])) - set(old.get("labels", []))
    session.controls = {**old, **changes}
    cancel_generation(db, session, "controls_changed", cancel_sops=False)
    session.generation += 1
    blocked = control_block(session, db)
    if blocked:
        session.due_at = None
    for enrollment in db.scalars(select(RehearsalEnrollment).where(RehearsalEnrollment.session_id == session.id, RehearsalEnrollment.status == "active")).all():
        config = db.get(SopVersion, enrollment.sop_version_id).config
        reason = "exit_label" if set(session.controls.get("labels", [])) & set(config.get("exit_labels", [])) else blocked
        if reason:
            stop_enrollment(db, enrollment, reason)
    return added


def add_customer_message(db: Session, session: AutomationSession, content: str, client_key: str,
                         content_type: str = "text", *, queue_reply: bool = True,
                         trigger_entry_sops: bool = True) -> None:
    if any(x.get("client_key") == client_key for x in session.messages):
        return
    had_active_sop = bool(db.scalar(select(RehearsalEnrollment.id).where(
        RehearsalEnrollment.session_id == session.id,
        RehearsalEnrollment.status == "active",
    )))
    version = session.generation
    if not db.execute(update(AutomationSession).where(AutomationSession.id == session.id, AutomationSession.generation == version).values(generation=version + 1)).rowcount:
        raise ValueError("version_conflict")
    cancel_generation(db, session, "customer_new_message")
    policy, _ = reply_policy(db, session.inbox_binding_id)
    wall_now = dt(utcnow())
    first = dt(session.batch_started_at) if session.batch_started_at else wall_now
    due = min(first + timedelta(seconds=policy["merge_max_seconds"]), wall_now + timedelta(seconds=policy["merge_wait_seconds"]))
    session.messages = [*session.messages, {"id": client_key, "client_key": client_key, "direction": "incoming", "content": content, "content_type": content_type, "created_at": session.virtual_now,
                                            "timeline_sequence": next_timeline_sequence(session)}]
    first_message = not session.controls.get("customer_added_at")
    if first_message:
        session.controls = {**session.controls, "customer_added_at": session.virtual_now, "customer_added_source": "first_customer_message"}
    session.batch_started_at, session.due_at = (iso(first), iso(due)) if queue_reply else (None, None)
    session.generation = version + 1
    if had_active_sop:
        append_timeline_event(session, "sop_exited", "客户发来新消息，当前 SOP 已结束，AI 被动回复接管。")
    if first_message and trigger_entry_sops:
        trigger_sops(db, session, first_message=True)


def queue_passive(db: Session, environment: str | None = None, *, session_id: int | None = None) -> bool:
    query = select(AutomationSession).where(AutomationSession.due_at <= utcnow())
    if environment:
        query = query.where(AutomationSession.environment == environment)
    if session_id is not None:
        query = query.where(AutomationSession.id == session_id)
    session = db.scalar(query.order_by(AutomationSession.due_at).limit(1))
    if not session:
        return False
    # Claim this exact batch before creating its run; another worker or a new
    # customer generation must not enqueue or clear the same pending batch.
    claimed = db.execute(update(AutomationSession).where(
        AutomationSession.id == session.id,
        AutomationSession.generation == session.generation,
        AutomationSession.due_at == session.due_at,
    ).values(due_at=None, batch_started_at=None).execution_options(synchronize_session=False))
    if not claimed.rowcount:
        db.commit()
        return False
    policy, version = reply_policy(db, session.inbox_binding_id)
    messages = list(session.messages)
    split = len(messages)
    handled = str(session.controls.get("last_handled_customer_message_id") or "")
    while (split > 0 and messages[split-1].get("direction") == "incoming"
           and (not handled or str(messages[split-1].get("id") or "") != handled)):
        split -= 1
    target = messages[split:]
    reason = gate(session, session.virtual_now)
    if not policy["enabled"]: reason = "reply_policy_disabled"
    if target and session.environment == "shadow" and dt(utcnow()) - dt(target[-1]["created_at"]) > timedelta(seconds=policy["backlog_seconds"]):
        reason = "backlog_sync_only"
    attachment = any(x.get("content_type", "text") != "text" for x in target)
    key = f"reply:{session.id}:{session.generation}"
    if not db.scalar(select(AutomationRun.id).where(AutomationRun.idempotency_key == key)):
        row = AutomationRun(session_id=session.id, generation=session.generation, module="reply", idempotency_key=key, policy_version=version,
            input_snapshot={"customer_text": "\n".join(x.get("content", "") for x in target), "context_messages": messages[:split], "memory": session.memory, "module": "reply",
                            "source_message_ids": [x.get("id") for x in target],
                            "source_message_id": target[-1].get("id") if target else None,
                            "context_complete": session.controls.get("history_complete", False)})
        if reason:
            row.status, row.error_code, row.completed_at = "blocked", reason, utcnow()
        elif attachment:
            row.status, row.completed_at = "completed", utcnow()
            row.decision = {"action": "handoff", "handoff_reason": "attachment_requires_review", "reply": "附件需要由顧問協助查看。", "material_keys": []}
        db.add(row)
    session.due_at, session.batch_started_at = None, None
    db.commit()
    return True


def process_automation_run(
    db: Session,
    environment: str | None = None,
    session_id: int | None = None,
) -> bool:
    stale = update(AutomationRun).where(AutomationRun.status == "processing", AutomationRun.lease_until < utcnow())
    if environment:
        stale = stale.where(AutomationRun.session_id.in_(select(AutomationSession.id).where(
            AutomationSession.environment == environment
        )))
    if session_id is not None:
        stale = stale.where(AutomationRun.session_id == session_id)
    db.execute(stale.values(status="pending", lease_token=None))
    db.commit()
    from sqlalchemy import case
    query = select(AutomationRun).where(AutomationRun.status == "pending")
    if environment:
        query = query.join(AutomationSession).where(AutomationSession.environment == environment)
    if session_id is not None:
        query = query.where(AutomationRun.session_id == session_id)
    run = db.scalar(query.order_by(case((AutomationRun.module=="reply",0),else_=1),AutomationRun.id).limit(1))
    if not run:
        return False
    token = secrets.token_hex(16)
    if not db.execute(update(AutomationRun).where(AutomationRun.id == run.id, AutomationRun.status == "pending").values(status="processing", lease_token=token, lease_until=iso(dt(utcnow())+timedelta(seconds=60)))).rowcount:
        db.rollback()
        return True
    payload, run_id = dict(run.input_snapshot), run.id
    if not payload.get('source_message_id') and payload.get('source_message_ids'):
        payload['source_message_id']=payload['source_message_ids'][-1]
    session = db.get(AutomationSession, run.session_id)
    session_tenant_id = tenant_for_session(db, session) or db.scalar(select(Tenant.id).order_by(Tenant.id))
    payload["available_materials"] = candidate_materials(db, session_tenant_id)
    payload["engine_version"] = session.engine_version
    payload["route_variant"] = session.controls.get("route_variant", "")
    journey = dict(session.controls.get("journey") or {})
    payload["journey"] = journey_context_from_values(
        session.controls.get("route_variant", ""),
        journey.get("stage", "route_selection"),
        journey.get("slots", {}),
        journey.get("sent_content_groups", []),
    )
    payload["route_playbook"] = (
        playbook_prompt(payload["route_variant"], journey.get("slots", {}))
        if payload["route_variant"] else playbook_prompt()
    )
    payload["reception_policy"] = effective_reception_policy(db)
    payload['now'] = session.virtual_now
    payload["lead_capture"] = dict(session.controls.get("lead_capture") or {"status": "not_started"})
    if run.module == "reply":
        payload = enrich_context_with_web_knowledge(db, session_tenant_id, payload, environment="playground")
    db.commit()
    # The network call runs after commit: SQLite writers are never held for inference.
    try:
        decision, calls, digest, trace = generate_decision(payload)
        route_progress = None
        if run.module in {"reply", "silence_touch", "wakeup"}:
            current_journey = payload.get("journey") or {}
            decision, route_progress = prepare_route_reply_values(
                decision,
                current_route=session.controls.get("route_variant", ""),
                preserve_current_route=run.module in {"silence_touch", "wakeup"},
                stage=current_journey.get("stage", "route_selection"),
                slots=current_journey.get("slots", {}),
                sent_groups=current_journey.get("sent_content_groups", []),
                source_message_id=payload.get("source_message_id"),
            )
            lead_state = session.controls.get("lead_capture") or {"status": "not_started"}
            decision, _ = bind_lead_request(
                decision,
                route_variant=route_progress.get("route_variant", ""),
                journey_stage=route_progress.get("stage", "route_selection"),
                capture_status=lead_state.get("status", "not_started"),
            )
        outcome, error = asdict(decision), None
        trace = {**trace, "request_hash": digest, "calls": calls}
    except Exception as exc:
        route_progress = None
        outcome = {"action": "no_action", "reply": None, "handoff_reason": "model_unavailable"}
        calls = exc.logs if isinstance(exc, EvaluationCallError) else []
        trace, error = {"calls": calls, "outbound": False}, type(exc).__name__
        if isinstance(exc, EvaluationCallError):
            trace["failure_code"] = exc.code
            trace["request_hash"] = exc.digest
    db.expire_all()
    run = db.get(AutomationRun, run_id)
    session = db.get(AutomationSession, run.session_id)
    if run.lease_token != token or run.status != "processing":
        return True
    blocked = gate(session, session.virtual_now, run.module in {"wakeup", "silence_touch"}, db)
    policy, current_version = reply_policy(db, session.inbox_binding_id)
    if run.module == "reply" and (not policy["enabled"] or current_version != run.policy_version): blocked = "reply_policy_changed"
    if run.module == "wakeup":
        cycle = db.scalar(select(SilenceCycle).where(SilenceCycle.run_id==run.id))
        if not cycle or cycle.status != "queued": blocked = "cycle_cancelled"
        elif cycle.policy_id:
            current_policy=db.get(WakeupPolicy,cycle.policy_id)
            if not current_policy or current_policy.status!="running" or current_policy.version!=run.policy_version:blocked="wakeup_policy_changed"
    if session.generation != run.generation or blocked:
        run.status, run.error_code = "discarded", blocked or "generation_changed"
        run.decision = {}
    else:
        run.status, run.error_code, run.decision = "failed" if error else "completed", error, outcome
        if not error and run.module == "reply":
            handled_ids = run.input_snapshot.get("source_message_ids") or []
            if handled_ids:
                session.controls = {**session.controls, "last_handled_customer_message_id": str(handled_ids[-1])}
            flags = set(outcome.get("safety_flags") or [])
            if "stop_automation" in flags:
                session.controls = {**session.controls, "proactive_pause": {
                    "reason": "customer_paused_proactive" if "customer_paused_proactive" in flags else "customer_requested_stop",
                    "run_id": run.id, "customer_text": run.input_snapshot.get("customer_text", ""),
                }}
                for enrollment in db.scalars(select(RehearsalEnrollment).where(
                    RehearsalEnrollment.session_id == session.id,
                    RehearsalEnrollment.status == "active",
                )).all():
                    stop_enrollment(db, enrollment, "customer_requested_stop")
                for cycle in db.scalars(select(SilenceCycle).where(
                    SilenceCycle.session_id == session.id,
                    SilenceCycle.status.in_(["scheduled", "queued", "deferred"]),
                )).all():
                    cycle.status, cycle.reason = "excluded", "customer_requested_stop"
                append_timeline_event(session, "customer_paused_proactive", "客户已暂缓咨询，停止主动跟进，等待客户重新咨询。")
            elif (outcome.get("action") == "reply" and session.controls.get("proactive_pause")
                  and (set(trace.get("customer_questions") or []) - {"other"}
                       or "general_inquiry" in (trace.get("semantic_signals") or []))):
                session.controls = {key: value for key, value in session.controls.items() if key != "proactive_pause"}
                append_timeline_event(session, "customer_resumed_inquiry", "客户重新咨询，恢复接待未发送内容。")
        if run.module in {"reply", "silence_touch", "wakeup"}:
            profile_memory = {
                key: {
                    "value": update.get("value"),
                    "quote": update.get("evidence_quote") or "",
                    "confidence": update.get("confidence"),
                    "reason": update.get("reason") or "",
                    "kind": "customer_fact" if update.get("evidence_quote") else "model_inference",
                    "generation": run.generation,
                }
                for key, update in (outcome.get("profile_updates") or {}).items()
            }
            session.memory = {
                **session.memory,
                **{k: {"value": v, "quote": outcome.get("slot_evidence", {}).get(k), "kind": "customer_fact", "generation": run.generation} for k, v in outcome.get("slots", {}).items()},
                **profile_memory,
            }
            journey = dict(session.controls.get("journey") or {})
            if route_progress:
                journey = route_progress
            else:
                journey["slots"] = {**journey.get("slots", {}), **outcome.get("slots", {})}
            session.controls = {**session.controls, "journey": journey}
            route = (
                route_progress.get("route_variant", "")
                if route_progress is not None
                else session.controls.get("route_variant", "")
            )
            if route_progress is not None:
                session.controls = {**session.controls, "route_variant": route}
            materials = []
            if outcome.get("action") in {"reply", "handoff"} and outcome.get("material_keys"):
                try:
                    material_batches = ([section['asset_keys'] for section in outcome['v2_delivery_sections']]
                                        if session.engine_version == 'v2' and outcome.get('v2_delivery_sections')
                                        else [outcome['material_keys']])
                    materials = [material for keys in material_batches
                        for material in resolve_materials(db, keys, route, tenant_for_session(db, session),
                            snapshot=route_snapshot_from_values(route, journey.get('slots')))]
                except ValueError as exc:
                    if outcome.get('v2_delivery_sections'):
                        raise
                    outcome = {
                        **outcome,
                        "material_keys": [],
                        "safety_flags": sorted(set([
                            *(outcome.get("safety_flags") or []),
                            str(exc),
                        ])),
                    }
            outcome["materials"] = materials
            run.decision = outcome
            direct_handoff_delivery = (
                outcome.get("action") == "handoff"
                and session.mode == "journey"
                and session.environment == "playground"
            )
            if direct_handoff_delivery:
                labels = list(dict.fromkeys([*(session.controls.get("labels") or []), "人工接管"]))
                task = {"id": f"playground:{session.id}:handoff:{run.id}", "status": "pending",
                        "simulated": True, "run_id": run.id, "created_at": session.virtual_now,
                        "reason": outcome.get("handoff_reason") or "ai_handoff",
                        "customer_text": run.input_snapshot.get("customer_text", ""),
                        "questions": trace.get("confirmation_questions") or trace.get("unanswered_questions") or [],
                        "unsupported_claims": trace.get("unsupported_claims") or [],
                        "pending_materials": [flag.removeprefix('pending_material:') for flag in outcome.get('safety_flags', []) if flag.startswith('pending_material:')],
                        "contact_at": ((journey.get('slots') or {}).get('_v2_state') or {}).get('contact_at')}
                session.controls = {**session.controls, "human": True, "labels": labels,
                                    "handoff_tasks": [*session.controls.get("handoff_tasks", []), task]}
                trace["handoff_task"] = task
                db.flush()
                for enrollment in db.scalars(select(RehearsalEnrollment).where(
                    RehearsalEnrollment.session_id == session.id,
                    RehearsalEnrollment.status == "active",
                )).all():
                    stop_enrollment(db, enrollment, "ai_handoff")
                append_timeline_event(session, "ai_handoff", "已切换为人工接管，剩余 SOP 已停止。")
            if outcome.get("reply"):
                deferred = deferred_initial_follow_up(
                    outcome,
                    (session.controls.get("journey") or {}).get("sent_content_groups", []),
                    slots=journey.get("slots", {}),
                ) if run.module == "reply" and session.engine_version == "v1" else None
                visible_reply = (
                    str(outcome.get("reply_body") or "").strip()
                    if deferred else str(outcome["reply"]).strip()
                )
                follow_up_question = str(outcome.get("follow_up_question") or "").strip() if not deferred else ""
                if follow_up_question:
                    visible_reply = str(outcome.get("reply_body") or visible_reply).strip()
                    if visible_reply.endswith(follow_up_question):
                        visible_reply = visible_reply[:-len(follow_up_question)].rstrip()
                options = outcome.get("reply_options") or []
                text_message = {"id": f"draft:{run.id}", "direction": "outgoing", "content": visible_reply,
                          "content_type": "input_select" if options else "text",
                          "content_attributes": {"items": [{"title": item, "value": item} for item in options]} if options else {},
                          "status": "simulated_delivered" if direct_handoff_delivery else "draft", "created_at": session.virtual_now,
                          "source": "sop_ai" if run.module == "silence_touch" else "wakeup_ai" if run.module == "wakeup" else "ai", "run_id": run.id}
                media_messages = [{**info, "id": f"draft:{run.id}:media:{index}", "direction": "outgoing", "content": "",
                           "status": "simulated_delivered" if direct_handoff_delivery else "draft", "created_at": session.virtual_now,
                           "source": "sop_ai" if run.module == "silence_touch" else "wakeup_ai" if run.module == "wakeup" else "ai", "run_id": run.id}
                          for index, info in enumerate(materials)]
                route = route_snapshot_from_values(str(outcome.get("route_variant") or ""), journey.get("slots"))
                covered = set(outcome.get("covered_content_groups") or [])
                asset_keys = {str(item.get("asset_key") or "") for item in materials}
                delivery_mode = delivery_mode_for(
                    str(outcome.get("route_variant") or ""), list(covered), asset_keys, route_spec=route
                )
                interval_seconds = int(route.get("initial_delivery_interval_seconds", DEFAULT_INITIAL_DELIVERY_INTERVAL_SECONDS)) if route else DEFAULT_INITIAL_DELIVERY_INTERVAL_SECONDS
                ordered = ordered_delivery_parts(
                    visible_reply, media_messages, delivery_mode,
                    text_segments=outcome.get("reply_segments"),
                    plan_id=f"rehearsal:{run.id}",
                    content_group_key=str(outcome.get("content_group_key") or ""),
                    interval_seconds=interval_seconds,
                    follow_up_question=follow_up_question,
                    follow_up_type=str(outcome.get("follow_up_type") or ""),
                    follow_up_field=str(outcome.get("follow_up_field") or ""),
                )
                if session.engine_version == 'v2' and outcome.get('v2_delivery_sections'):
                    from app.reception_v2.material_delivery import introduction_parts
                    ordered = introduction_parts(outcome['v2_delivery_sections'], media_messages,
                        plan_id=f'rehearsal:{run.id}', interval_seconds=interval_seconds)
                timed_delivery = len(ordered) > 1
                first_text_index = next((i for i, part in enumerate(ordered) if part.kind == "text"), -1)
                group = [
                    {
                        **(text_message if part.kind == "text" else (part.material or {})),
                        **({"content": part.content, "id": f"draft:{run.id}:follow-up" if part.is_follow_up else text_message["id"] if index == first_text_index else f"draft:{run.id}:text:{index}"} if part.kind == "text" else {}),
                        "content_attributes": {
                            **(text_message["content_attributes"] if part.kind == "text" else {}),
                            "delivery_item": {
                                "item_id": part.part_id, "plan_version": part.plan_version,
                                "group_key": part.content_group_key,
                                "is_follow_up": part.is_follow_up,
                            },
                        },
                    }
                    for index, part in enumerate(ordered)
                ]
                opening = outcome.get("opening_messages") or []
                if opening:
                    from app.opening_messages import delivery_items, opening_media_info
                    interval_seconds = int(outcome.get("opening_interval_seconds", 2))
                    timed_delivery = True
                    items = delivery_items(outcome.get("opening_items"), opening)
                    group = []
                    for index, item in enumerate(items):
                        info = opening_media_info(db, item, tenant_for_session(db, session)) if item["content_type"] != "text" else {}
                        group.append({
                            **text_message, **info, "id": f"draft:{run.id}:opening:{index}",
                            "content": item["content"],
                            "content_type": text_message["content_type"] if index == len(items) - 1 else item["content_type"],
                            "content_attributes": text_message["content_attributes"] if index == len(items) - 1 else {},
                        })
                for index, message in enumerate(group):
                    if index and timed_delivery:
                        message["created_at"] = iso(dt(session.virtual_now) + timedelta(seconds=index * interval_seconds))
                    if timed_delivery:
                        message["preserve_delivery_timing"] = True
                    message["timeline_sequence"] = next_timeline_sequence(session)
                if group and (session.mode != "journey" or direct_handoff_delivery):
                    session.virtual_now = group[-1]["created_at"]
                session.messages = [*session.messages, *group]
        if run.module == "wakeup":
            cycle = db.scalar(select(SilenceCycle).where(SilenceCycle.run_id == run.id))
            if cycle:
                action = outcome.get("wakeup_action") or "handoff"
                cycle.status = {"generate": "draft", "skip": "excluded", "defer": "deferred", "handoff": "handoff_suggested"}[action]
                cycle.reason = outcome.get("handoff_reason") or action
                if action == "defer":
                    minimum = 1 if session.engine_version == 'v2' else 15
                    cycle.due_at = iso(dt(session.virtual_now) + timedelta(minutes=max(minimum, outcome.get("defer_minutes") or 60)))
                    cycle.expires_at = iso(min(dt(cycle.due_at)+timedelta(minutes=15),dt(cycle.customer_at)+timedelta(hours=24,minutes=-5)))
    if run.module == "silence_touch":
        job = db.get(RehearsalJob, int(run.input_snapshot.get("sop_job_id") or 0))
        if job and run.status == "failed":
            job.status, job.reason, job.confirmed_at = (
                "skipped_model_failure", "model_generation_or_verification_failed", session.virtual_now
            )
            for successor in db.scalars(select(RehearsalJob).where(
                RehearsalJob.predecessor_id == job.id,
                RehearsalJob.status == "waiting_dependency",
            )).all():
                successor.status = "scheduled"
                successor.scheduled_at = iso(
                    dt(session.virtual_now)
                    + relative_delay(successor.payload)
                )
            append_timeline_event(
                session,
                "silence_model_warning",
                "本次沉默触达生成或事实核验未通过，已记录预警并跳过；后续节点会继续。",
            )
        elif job and run.status == "discarded":
            job.status, job.reason, job.confirmed_at = "blocked", run.error_code, session.virtual_now
        elif job and outcome.get("action") == "handoff":
            job.status, job.reason, job.confirmed_at = (
                "blocked", outcome.get("handoff_reason") or "ai_handoff", session.virtual_now
            )
        elif job and outcome.get("wakeup_action") == "defer":
            from app.reception_v2.scheduling import defer_job
            if not defer_job(job, session.virtual_now, int(outcome.get('defer_minutes') or 60)):
                enrollment = db.get(RehearsalEnrollment, job.enrollment_id)
                if enrollment:
                    stop_enrollment(db, enrollment, 'v2_defer_limit')
                job.status, job.reason = 'skipped', 'v2_defer_limit'
            append_timeline_event(
                session, "silence_touch_deferred",
                outcome.get("touch_reason") or "当前不适合打扰客户，已延后评估。",
            )
        elif job and outcome.get("action") == "no_action":
            reason = next(
                (
                    flag for flag in (outcome.get("safety_flags") or [])
                    if str(flag).startswith("silence_")
                ),
                "silence_no_relevant_content",
            )
            job.status, job.reason, job.confirmed_at = (
                "verification_blocked" if "silence_verification_failed_no_action" in (outcome.get("safety_flags") or []) else "skipped",
                reason, session.virtual_now
            )
            job.payload = {
                **(job.payload or {}),
                "model_decision": {
                    "touch_goal": outcome.get("touch_goal"),
                    "touch_reason": outcome.get("touch_reason"),
                    "journey_stage": outcome.get("journey_stage"),
                    "covered_content_groups": [],
                    "material_keys": [],
                },
            }
            if "stop_automation" in (outcome.get("safety_flags") or []):
                for enrollment in db.scalars(select(RehearsalEnrollment).where(
                    RehearsalEnrollment.session_id == session.id,
                    RehearsalEnrollment.status == "active",
                )).all():
                    stop_enrollment(db, enrollment, reason)
                job.status = "blocked"
            else:
                for successor in db.scalars(select(RehearsalJob).where(
                    RehearsalJob.predecessor_id == job.id,
                    RehearsalJob.status == "waiting_dependency",
                )).all():
                    successor.status = "scheduled"
                    successor.scheduled_at = iso(
                        dt(session.virtual_now)
                        + relative_delay(successor.payload)
                    )
            append_timeline_event(
                session,
                "silence_touch_skipped",
                outcome.get("touch_reason") or "当前没有新的相关内容，本次触达已跳过。",
            )
        elif job:
            job.reason = "model_output_pending_delivery"
    run.trace, run.completed_at = trace, utcnow()
    db.commit()
    return True


def sop_snapshot(db: Session, sop: SopDefinition, user_id: int) -> SopVersion:
    config = {key: getattr(sop, key) for key in ("name", "nodes", "trigger_type", "trigger_labels", "inbox_ids", "exit_labels", "stop_on_incoming", "frequency_hours", "dry_run", "live_enabled", "route_variant", "test_conversation_ids")}
    config["nodes"] = freeze_nodes(db, sop.nodes, sop.route_variant or "", sop.tenant_id)
    horizon = max([24, *[(n.get("day_number", 1) + 1) * 24 for n in sop.nodes if n.get("schedule_type") == "calendar_day"],
                   sum((n.get("delay_minutes") or 0) for n in sop.nodes) / 60 + 24])
    config = {**config, "ttl_hours": horizon, "grace_minutes": 15}
    digest = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
    existing = db.scalar(select(SopVersion).where(SopVersion.sop_id == sop.id, SopVersion.version == sop.version))
    if existing:
        if existing.content_hash != digest: raise ValueError("published_version_immutable")
        return existing
    row = SopVersion(sop_id=sop.id, version=sop.version, config=config, content_hash=digest, created_by=user_id)
    db.add(row)
    db.flush()
    return row


def media_error(db: Session, node: dict) -> str | None:
    if node.get("messages") is not None:
        if not node["messages"]: return "messages_missing"
        for item in node["messages"]:
            problem = media_error(db, item)
            if problem: return problem
        return None
    if node.get("content_type", "text") == "text":
        return None if (node.get("content") or "").strip() else "text_missing"
    media = db.get(StoredMedia, node.get("media_id")) if node.get("media_id") else None
    if not media or not Path(media.storage_path).is_file():
        return "material_unavailable"
    if node.get("content_type") != media.media_type:
        return "media_type_mismatch"
    return None


def enrollment_allowed(db: Session, session: AutomationSession, config: dict) -> bool:
    inbox = db.get(InboxBinding, session.inbox_binding_id) if session.inbox_binding_id else None
    if config.get("test_conversation_ids") and session.conversation_state_id:
        conversation = db.get(ConversationState, session.conversation_state_id)
        if not conversation or conversation.chatwoot_conversation_id not in config["test_conversation_ids"]:
            return False
    if config.get("test_conversation_ids") and session.environment == "shadow" and not session.conversation_state_id:
        return False
    return not config.get("inbox_ids") or bool(inbox and inbox.chatwoot_inbox_id in config["inbox_ids"])


def trigger_sops(db: Session, session: AutomationSession, *, added_labels: set[str] | None = None, first_message: bool = False) -> None:
    from app.operations import allowed_inbox_ids
    user = db.get(User, session.owner_id)
    allowed = allowed_inbox_ids(db, user)
    remote = set(db.scalars(select(InboxBinding.chatwoot_inbox_id).where(InboxBinding.id.in_(allowed))).all()) if allowed is not None else None
    for sop in db.scalars(select(SopDefinition).where(SopDefinition.status == "running")).all():
        version = db.scalar(select(SopVersion).where(SopVersion.sop_id == sop.id).order_by(SopVersion.version.desc()))
        if not version:
            continue
        config = version.config
        if remote is not None and (not config.get("inbox_ids") or not set(config["inbox_ids"]).issubset(remote)):
            continue
        if not enrollment_allowed(db, session, config):
            continue
        match = first_message if config.get("trigger_type") == "first_message" else config.get("trigger_type") in ("label", "stage") and bool(set(config.get("trigger_labels", [])) & (added_labels or set()))
        if match:
            enroll_rehearsal(db, session, version, source="first_message" if first_message else "label")


def enroll_rehearsal(db: Session, session: AutomationSession, version: SopVersion, *, source: str = "manual", reenroll: bool = False, request_key: str | None = None,
                     schedule_intervals: list[int] | None = None,
                     silence_enabled: bool = True,
                     deferred_follow_up: dict | None = None) -> RehearsalEnrollment:
    if not enrollment_allowed(db, session, version.config):
        raise ValueError("sop_inbox_mismatch")
    route = version.config.get("route_variant", "")
    if route and session.controls.get("route_variant") not in (None, "", route):
        raise ValueError("material_route_mismatch")
    key = subject_key(db, session)
    query = select(RehearsalEnrollment).where(RehearsalEnrollment.sop_id == version.sop_id, RehearsalEnrollment.subject_key == key)
    if request_key:
        repeated = db.scalar(query.where(RehearsalEnrollment.request_key == request_key))
        if repeated:
            return repeated
    existing = db.scalar(query.order_by(RehearsalEnrollment.round_number.desc()))
    if existing:
        if source in ("label", "first_message") or (existing.status == "active" and not reenroll):
            return existing
        if existing.status == "active":
            raise ValueError("sop_round_active")
        if not reenroll:
            raise ValueError("sop_reenrollment_required")
    if reenroll and not request_key:
        raise ValueError("reenroll_request_key_required")
    if reenroll and not existing:
        raise ValueError("sop_round_missing")
    incoming = [x for x in session.messages if x.get("direction") == "incoming" and not x.get("private")]
    added_at = session.controls.get("customer_added_at") or (min(x["created_at"] for x in incoming) if incoming else None)
    number = existing.round_number + 1 if existing else 1
    ttl_hours = configured_silence_ttl_hours(version.config.get("ttl_hours", 24), schedule_intervals)
    result = db.execute(insert(RehearsalEnrollment).values(session_id=session.id, sop_id=version.sop_id, subject_key=key, round_number=number,
        request_key=request_key, trigger_source=source, sop_version_id=version.id, generation=session.generation, status="active",
        enrolled_at=session.virtual_now, expires_at=iso(dt(session.virtual_now)+timedelta(hours=ttl_hours))).on_conflict_do_nothing())
    if not result.rowcount:
        repeated = db.scalar(query.where(RehearsalEnrollment.request_key == request_key)) if request_key else None
        if repeated:
            return repeated
        if source in ("label", "first_message"):
            return db.scalar(query.order_by(RehearsalEnrollment.round_number.desc()))
        raise ValueError("sop_round_conflict")
    enrollment = db.scalar(query.where(RehearsalEnrollment.round_number == number))
    if route:
        session.controls = {**session.controls, "route_variant": route}
    db.flush()
    previous = None
    frozen_slots = (session.controls.get("journey") or {}).get("slots")
    bound_spec = route_snapshot_from_values(route, frozen_slots) if route and source == "model_route" else None
    if route and source == "model_route" and bound_spec is None:
        raise ValueError("route_snapshot_unverifiable")
    if bound_spec is not None and session.engine_version == "v2" and source == "model_route":
        from app.reception_v2.proactive_policy import silence_schedule_templates
        schedule_nodes = silence_schedule_templates(bound_spec["sop"]["nodes"])
    else:
        schedule_nodes = frozen_sop_nodes(bound_spec) if bound_spec is not None else version.config["nodes"]
    source_nodes = configured_silence_nodes(
        schedule_nodes,
        schedule_intervals,
        silence_enabled=silence_enabled,
    )
    if session.engine_version == "v2" and source == "model_route":
        source_nodes = [node for node in source_nodes if node.get("journey_trigger")]
    else:
        source_nodes = append_deferred_initial_follow_up(
            source_nodes, route, deferred_follow_up,
            slots=frozen_slots if bound_spec is not None else None,
        )
    if source == "model_route" and session.engine_version == "v1":
        source_nodes = expand_static_delivery_nodes(source_nodes)
    for source_node in source_nodes:
        node = dict(source_node)
        reason = None
        try:
            due = schedule_at(node, customer_added_at=added_at, enrolled_at=session.virtual_now,
                              last_customer_at=incoming[-1]["created_at"] if incoming else None)
        except ValueError as exc:
            due, reason = None, str(exc)
        if (due and previous is None and source == "model_route"
                and node.get("initial_delivery") and not node.get("journey_trigger")):
            times = [item["created_at"] for item in session.messages
                     if item.get("direction") == "outgoing" and not item.get("private")
                     and item.get("status") == "simulated_delivered" and item.get("created_at")]
            if times:
                earliest = dt(max(times, key=dt)) + timedelta(seconds=float((bound_spec or {}).get("initial_delivery_interval_seconds", 2)))
                due = iso(max(dt(due), earliest))
        dependency = previous.id if previous and node.get("basis") == "previous_node" else None
        if node.get("basis") == "previous_node" and node.get("schedule_type") == "relative" and not previous:
            reason = "previous_message_required"
        job = RehearsalJob(enrollment_id=enrollment.id, node_key=node["key"], predecessor_id=dependency, scheduled_at=due,
                          status="blocked" if reason else "waiting_dependency" if due is None else "scheduled", reason=reason, payload=node)
        db.add(job)
        db.flush()
        previous = job
    return enrollment


def _initial_rehearsal_skip_anchor(session, enrollment, job, previous):
    if (enrollment.trigger_source != "model_route" or not job.payload.get("initial_delivery")
            or job.payload.get("journey_trigger")):
        return
    messages = [item for item in session.messages if item.get("direction") == "outgoing"
                and item.get("status") == "simulated_delivered" and item.get("created_at")]
    anchor = max((item["created_at"] for item in messages), key=dt, default=None)
    anchor = anchor or (previous.confirmed_at if previous else enrollment.enrolled_at)
    job.payload = {**job.payload, "initial_delivery_anchor_at": anchor}


def _deliver_static_rehearsal_job(
    db: Session,
    session: AutomationSession,
    enrollment: RehearsalEnrollment,
    version: SopVersion,
    job: RehearsalJob,
    previous: RehearsalJob | None,
) -> None:
    """Preserve manually authored static SOPs outside the AI journey product."""
    group_key = job.payload.get("content_group_key")
    sent_groups = set((session.controls.get("journey") or {}).get("sent_content_groups", []))
    if group_key and automatic_content_already_covered(group_key, sent_groups):
        job.status, job.reason, job.confirmed_at = "already_provided", "content_group_already_provided", session.virtual_now
        mark_session_content_group(session, group_key)
        _initial_rehearsal_skip_anchor(session, enrollment, job, previous)
        job.payload = {**job.payload, "reused_message_ids": [
            item["id"] for item in session.messages if item.get("direction") == "outgoing"
            and item.get("status") == "simulated_delivered" and item.get("id")
            and (item.get("content_group_key") == group_key or group_key in (
                (item.get("content_attributes") or {}).get("delivery_item", {}).get("group_keys") or []))
        ]}
        return
    reason = gate(session, session.virtual_now, True, db)
    if dt(session.virtual_now) > dt(enrollment.expires_at) or dt(session.virtual_now) > dt(job.scheduled_at) + timedelta(minutes=15):
        reason = "expired"
    if set(session.controls.get("labels", [])) & set(version.config.get("exit_labels", [])):
        reason = "exit_label"
    reason = reason or media_error(db, job.payload)
    items = content_items(job.payload)
    route = version.config.get("route_variant", "")
    if route and session.controls.get("route_variant") not in (None, "", route):
        reason = reason or "material_route_mismatch"
    infos = []
    try:
        infos = [
            material_info(db, item, route, tenant_for_session(db, session))
            for item in items if item.get("content_type", "text") != "text"
        ]
    except ValueError as exc:
        reason = reason or str(exc)
    existing_infos = [info for info in infos if previous_delivery(
        db, subject_key(db, session), info, include_group=False,
    )]
    if not reason and existing_infos and not job.payload.get("skip_if_materials_provided", True):
        job.status, job.reason = "blocked", "material_already_provided"
        return
    prior_confirmed_job = db.scalar(select(RehearsalJob.id).where(
        RehearsalJob.enrollment_id == enrollment.id,
        RehearsalJob.id < job.id,
        RehearsalJob.status.in_(["simulated_delivered", "already_provided", "skipped_model_failure"]),
    ))
    if not reason and not reserve_touch(
        db, subject_key(db, session), f"sop-round:{enrollment.id}", session.virtual_now,
        version.config.get("frequency_hours", 24), continuation=bool(previous or prior_confirmed_job),
    ):
        reason = "contact_frequency_limit"
    if reason:
        job.status, job.reason = "skipped", reason
        return
    for info in existing_infos:
        record_session_content_delivery(session, group_key, asset_keys=[str(info.get("asset_key") or "")])
    existing_media_ids = {info["media_id"] for info in existing_infos}
    items = [item for item in items if not item.get("media_id") or item["media_id"] not in existing_media_ids]
    if not items and existing_infos:
        job.status, job.reason, job.confirmed_at = "already_provided", "material_already_provided", session.virtual_now
        job.payload = {**job.payload, "delivery_items": [], "reused_delivery_ids": [
            previous_delivery(db, subject_key(db, session), info, include_group=False).id for info in existing_infos
        ]}
        _initial_rehearsal_skip_anchor(session, enrollment, job, previous)
        return
    for info in infos:
        if info in existing_infos:
            continue
        if not record_delivery(db, session, subject_key(db, session), info, f"sop:{job.id}", "sop", route):
            raise ValueError("material_claim_conflict")
    interval_seconds = int(job.payload.get("delivery_interval_seconds") or 0)
    started_at = dt(session.virtual_now)
    slots = (session.controls.get("journey") or {}).get("slots", {})
    spec = route_snapshot_from_values(route, slots)
    snapshot = (slots.get(ROUTE_SNAPSHOTS_KEY) or {}).get(route, {}) if spec else {}
    delivered = [{
        "id": f"sop:{job.id}:{item['key']}", "direction": "outgoing",
        "content": item.get("content", ""), "content_type": item.get("content_type"),
        "media_id": item.get("media_id"),
        "asset_key": item.get("asset_key"), "media_hash": item.get("media_hash"),
        "content_group_key": group_key,
        "content_attributes": {
            **(item.get("content_attributes") or {}),
            "delivery_item": {
                "item_id": f"sop:{job.id}:{item['key']}",
                "plan_version": job.payload.get("plan_version") or DELIVERY_PLAN_VERSION,
                "group_key": group_key or "", "group_keys": [group_key] if group_key else [],
                "route": route, "asset_key": item.get("asset_key") or "",
                "sop_version_id": version.id, "sop_content_hash": version.content_hash,
                "package_version": (spec or {}).get("package_version"),
                "snapshot_digest": snapshot.get("digest"),
                "is_follow_up": bool(job.payload.get("deferred_follow_up")),
                "status": "simulated_delivered",
                "confirmed_at": iso(started_at + timedelta(seconds=index * interval_seconds)),
            },
        },
        "created_at": iso(started_at + timedelta(seconds=index * interval_seconds)),
        "status": "simulated_delivered", "source": "sop", "group_id": job.id,
        "item_key": item["key"], "timeline_sequence": next_timeline_sequence(session),
    } for index, item in enumerate(items)]
    confirmed_at = delivered[-1]["created_at"] if delivered else session.virtual_now
    job.status, job.confirmed_at, job.reason = "simulated_delivered", confirmed_at, None
    session.virtual_now = confirmed_at
    session.messages = [*session.messages, *delivered]
    job.payload = {**job.payload, "delivery_items": [
        {**item["content_attributes"]["delivery_item"], "key": item["item_key"]}
        for item in delivered
    ]}
    for item in items:
        if item.get("media_id"):
            info = next((value for value in infos if value.get("media_id") == item["media_id"]), {})
            record_session_content_delivery(session, group_key, asset_keys=[str(info.get("asset_key") or "")])
        else:
            record_session_content_delivery(session, group_key, delivered_text=str(item.get("content") or ""))
    deferred = (job.payload or {}).get("deferred_follow_up") or {}
    if deferred.get("type") == "contact":
        capture = dict(session.controls.get("lead_capture") or {})
        if capture.get("status", "not_started") == "not_started":
            capture.update({
                "status": "asked",
                "request_count": int(capture.get("request_count", 0)) + 1,
            })
            session.controls = {**session.controls, "lead_capture": capture}
    reservation = db.scalar(select(TouchReservation).where(
        TouchReservation.contact_key == subject_key(db, session)
    ))
    if reservation:
        reservation.status, reservation.confirmed_at = "simulated_confirmed", session.virtual_now


def advance_sops(db: Session, session: AutomationSession) -> None:
    enrollments = db.scalars(select(RehearsalEnrollment).where(RehearsalEnrollment.session_id == session.id, RehearsalEnrollment.status == "active")).all()
    for enrollment in enrollments:
        version = db.get(SopVersion, enrollment.sop_version_id)
        sop = db.get(SopDefinition, version.sop_id)
        if sop.status != "running": continue
        jobs = db.scalars(select(RehearsalJob).where(RehearsalJob.enrollment_id == enrollment.id).order_by(RehearsalJob.id)).all()
        for job in jobs:
            if job.status in TERMINAL: continue
            previous = db.get(RehearsalJob, job.predecessor_id) if job.predecessor_id else None
            if previous and (previous.status not in ("simulated_delivered", "already_provided", "skipped_model_failure", "verification_blocked", "skipped") or not previous.confirmed_at):
                if previous.status in TERMINAL: job.status, job.reason = "cancelled", "predecessor_not_confirmed"
                continue
            if job.scheduled_at is None:
                anchor = previous.confirmed_at
                if (enrollment.trigger_source == "model_route" and job.payload.get("initial_delivery")
                        and not job.payload.get("journey_trigger")):
                    anchor = previous.payload.get("initial_delivery_anchor_at") or anchor
                job.scheduled_at = iso(dt(anchor) + relative_delay(job.payload))
                job.status = "scheduled"
            if dt(job.scheduled_at) > dt(session.virtual_now): continue
            active_reply = db.scalar(select(AutomationRun.id).where(AutomationRun.session_id == session.id,
                AutomationRun.module == "reply", AutomationRun.status.in_(["pending", "processing"])))
            # A due SOP node must wait for the passive reply pipeline instead of
            # becoming terminal. The reply may have been queued at the same
            # virtual timestamp and model latency is deliberately excluded from
            # the rehearsal clock.
            if active_reply or session.due_at:
                job.reason = "passive_reply_pending"
                continue
            from app.customer_contact_policy import contact_constraint
            constraint, delay = contact_constraint({'journey': session.controls.get('journey'), 'memory': session.memory, 'now': session.virtual_now})
            if constraint:
                if delay:
                    from app.reception_v2.scheduling import defer_job
                    if not defer_job(job, session.virtual_now, delay):
                        stop_enrollment(db, enrollment, 'v2_defer_limit')
                else:
                    stop_enrollment(db, enrollment, constraint)
                continue
            if not job.payload.get("journey_trigger"):
                _deliver_static_rehearsal_job(db, session, enrollment, version, job, previous)
                continue
            reason = gate(session, session.virtual_now, True, db)
            if job.reason == "passive_reply_pending":
                job.reason = None
            if dt(session.virtual_now) > dt(enrollment.expires_at) or dt(session.virtual_now) > dt(job.scheduled_at)+timedelta(minutes=15): reason = "expired"
            if set(session.controls.get("labels", [])) & set(version.config.get("exit_labels", [])): reason = "exit_label"
            if reason:
                job.status, job.reason = "skipped", reason
                job.confirmed_at = session.virtual_now
                continue
            retry = int((job.payload or {}).get('v2_defer_count', 0))
            key = f"silence-touch:{job.id}:{session.generation}" + (f":defer:{retry}" if retry else "")
            existing_run = db.scalar(select(AutomationRun).where(AutomationRun.idempotency_key == key))
            if not existing_run:
                incoming = [item for item in session.messages if item.get("direction") == "incoming" and not item.get("private")]
                source_message = incoming[-1] if incoming else {}
                db.add(AutomationRun(
                    session_id=session.id,
                    generation=session.generation,
                    module="silence_touch",
                    policy_version=1,
                    idempotency_key=key,
                    input_snapshot={
                        "module": "silence_touch",
                        "customer_text": source_message.get("content", ""),
                        "source_message_id": source_message.get("id"),
                        "context_messages": session.messages,
                        "context_complete": session.controls.get("history_complete", False),
                        "memory": session.memory,
                        "sop_job_id": job.id,
                        "touch_index": sum(
                            1 for item in jobs[:jobs.index(job) + 1]
                            if (item.payload or {}).get("journey_trigger")
                        ),
                        "scheduled_at": job.scheduled_at,
                    },
                ))
                append_timeline_event(session, "silence_touch_due", "客户持续沉默，系统正在筛选本轮仍有价值的跟进内容。")
            job.status, job.reason = "model_pending", "stage_decision_pending"
        if jobs and all(x.status in TERMINAL for x in jobs): enrollment.status = "completed"


def create_cycle(db: Session, session: AutomationSession, policy: WakeupPolicy | None = None) -> SilenceCycle | None:
    if policy is None and session.controls.get("wakeup_policy_id"):
        policy=db.get(WakeupPolicy,session.controls["wakeup_policy_id"])
        if not policy:return None
    existing = db.scalar(select(SilenceCycle).where(SilenceCycle.session_id == session.id, SilenceCycle.generation == session.generation))
    if existing:
        if policy and existing.policy_id != policy.id: raise ValueError("cycle_policy_already_frozen")
        return existing
    incoming = [x for x in session.messages if x.get("direction") == "incoming" and not x.get("private")]
    if not incoming: return None
    customer = incoming[-1]
    replies = [x for x in session.messages if confirmed(x) and dt(x["created_at"]) >= dt(customer["created_at"])]
    if not replies: return None
    config = {**DEFAULT_WAKEUP, **(policy.config if policy else {})}
    due = dt(replies[-1]["created_at"]) + timedelta(minutes=config["threshold_minutes"])
    cycle = SilenceCycle(session_id=session.id, generation=session.generation, policy_id=policy.id if policy else None, policy_snapshot={**config, "version": policy.version if policy else 1}, customer_at=customer["created_at"], reply_at=replies[-1]["created_at"], due_at=iso(due), expires_at=iso(min(due+timedelta(minutes=15),dt(customer["created_at"])+timedelta(hours=24,minutes=-5))))
    db.add(cycle)
    db.flush()
    return cycle


def queue_wakeup(db: Session, session: AutomationSession, cycle: SilenceCycle) -> None:
    if cycle.status not in ("candidate", "deferred") or dt(session.virtual_now) < dt(cycle.due_at): return
    if cycle.policy_id:
        policy = db.get(WakeupPolicy, cycle.policy_id)
        if not policy or policy.status != "running": return
    reason = gate(session, session.virtual_now, True)
    if reason == 'customer_requested_time':
        from app.customer_contact_policy import contact_constraint
        _, delay = contact_constraint({'journey': session.controls.get('journey'), 'memory': session.memory, 'now': session.virtual_now})
        cycle.status, cycle.reason = 'deferred', reason
        cycle.due_at = iso(dt(session.virtual_now) + timedelta(minutes=delay))
        cycle.expires_at = iso(min(dt(cycle.due_at) + timedelta(minutes=15), dt(cycle.customer_at) + timedelta(hours=23, minutes=55)))
        return
    if db.scalar(select(AutomationRun.id).where(AutomationRun.session_id==session.id,AutomationRun.module=="reply",AutomationRun.status.in_(["pending","processing"]))):reason="passive_reply_pending"
    if dt(session.virtual_now) > dt(cycle.expires_at): reason = "expired"
    if session.generation != cycle.generation: reason = "customer_new_message"
    if cycle.evaluation_count >= (6 if session.engine_version == 'v2' else 2): reason = "evaluation_limit"
    near = db.scalar(select(RehearsalJob).join(RehearsalEnrollment).join(SopVersion,RehearsalEnrollment.sop_version_id==SopVersion.id).join(SopDefinition,SopVersion.sop_id==SopDefinition.id).where(SopDefinition.status=="running",RehearsalEnrollment.session_id==session.id, RehearsalEnrollment.status=="active", RehearsalJob.status=="scheduled", RehearsalJob.scheduled_at <= iso(dt(session.virtual_now)+timedelta(minutes=10)), RehearsalJob.scheduled_at >= session.virtual_now))
    if near: reason = "sop_priority_reservation"
    reservation = db.scalar(select(TouchReservation).where(TouchReservation.contact_key==subject_key(db, session), (TouchReservation.expires_at>session.virtual_now)|(TouchReservation.status=="submission_unknown")))
    if reservation: reason = "contact_frequency_limit"
    if reason:
        cycle.status, cycle.reason = "blocked", reason
        return
    cycle.evaluation_count += 1
    incoming = [x for x in session.messages if x.get("direction") == "incoming"]
    run = AutomationRun(session_id=session.id, generation=session.generation, module="wakeup", policy_version=cycle.policy_snapshot.get("version",1), idempotency_key=f"wakeup:{cycle.id}:{cycle.evaluation_count}", input_snapshot={"module":"wakeup", "customer_text": incoming[-1].get("content", ""), "context_messages": session.messages, "memory":session.memory, "evaluation_at":session.virtual_now,
        "touch_index": cycle.evaluation_count, "context_complete": session.controls.get("history_complete", False)})
    db.add(run)
    db.flush()
    cycle.run_id, cycle.status = run.id, "queued"


def confirm_draft(db: Session, session: AutomationSession, message_id: str, *, due_only: bool = False) -> None:
    messages = [dict(x) for x in session.messages]
    target = next((x for x in messages if str(x.get("id")) == message_id and x.get("status") == "draft"), None)
    if not target:
        if any(str(x.get("id")) == message_id and x.get("status") == "simulated_delivered" for x in messages):
            return
        raise ValueError("draft_not_found")
    run = db.get(AutomationRun, target.get("run_id"))
    reason = gate(session, session.virtual_now, run.module in {"silence_touch", "wakeup"}, db=db)
    policy,version=reply_policy(db,session.inbox_binding_id)
    if not policy["enabled"] or (run and run.policy_version!=version):reason="reply_policy_changed"
    if reason or not run or run.generation != session.generation or run.decision.get("action") != "reply":
        raise ValueError(reason or "draft_not_sendable")
    route = run.decision.get("route_variant", "")
    if route and session.controls.get("route_variant") != route:
        raise ValueError("material_route_mismatch")
    journey = session.controls.get("journey") or {}
    frozen_slots = journey.get("slots", {})
    route_spec = route_snapshot_from_values(route, frozen_slots) if route else None
    if route and route_spec is None:
        raise ValueError("route_snapshot_unverifiable")
    if route and journey_context_from_values(
        route, slots=frozen_slots, sent_groups=journey.get("sent_content_groups", []),
    )["automatic_delivery_paused"]:
        raise ValueError("route_history_unverifiable")
    group = [x for x in messages if x.get("run_id") == run.id and x.get("status") == "draft"
             and (not due_only or dt(x["created_at"]) <= dt(session.virtual_now))]
    if not group:
        return
    target = next((x for x in group if not x.get("media_id")), target)
    from app.opening_messages import opening_media_info
    infos = [(item, opening_media_info(db, item, tenant_for_session(db, session))
              if item.get("opening_media") else material_info(db, item, route, tenant_for_session(db, session)))
             for item in group if item.get("media_id")]
    resend = bool(run.decision.get("allow_material_resend"))
    seen = [previous_delivery(db, subject_key(db, session), info, include_group=False) for _, info in infos]
    for (item, info), existing in zip(infos, seen):
        if (existing and not resend) or not record_delivery(db, session, subject_key(db, session), info, f"ai:{run.id}", "ai", route, resend=bool(existing and resend)):
            item["status"] = "already_provided"
    for item in group:
        if item["status"] == "draft":
            item["status"] = "simulated_delivered"
            if not item.get("preserve_delivery_timing"):
                item["created_at"] = session.virtual_now
        attributes = item.get("content_attributes") or {}
        if isinstance(attributes.get("delivery_item"), dict):
            item["content_attributes"] = {**attributes, "delivery_item": {
                **attributes["delivery_item"], "status": item["status"],
                "confirmed_at": session.virtual_now,
            }}
    session.messages = messages
    if session.engine_version == 'v2':
        from app.reception_v2.events import answer_receipt, rebuild_answers
        for item in group:
            if item.get('status') == 'simulated_delivered':
                item['v2_answer'] = answer_receipt(run.decision, str(item.get('content') or ''))
        session.messages = list(messages)
        current_journey = dict(session.controls.get('journey') or {})
        current_journey['slots'] = rebuild_answers(current_journey.get('slots') or {}, [
            item['v2_answer'] for item in session.messages
            if item.get('status') == 'simulated_delivered' and isinstance(item.get('v2_answer'), dict)
        ])
        session.controls = {**session.controls, 'journey': current_journey}
    covered_groups = list(dict.fromkeys([
        run.decision.get("content_group_key"),
        *(run.decision.get("covered_content_groups") or []),
    ]))
    deferred = deferred_initial_follow_up(
        run.decision, journey.get("sent_content_groups", []), slots=frozen_slots,
    ) if run.module != "silence_touch" and session.engine_version == "v1" else None
    deferred_group = deferred_follow_up_group(route, deferred, slots=frozen_slots)
    delivered_asset_keys = [
        str(item.get("asset_key") or "")
        for item in group
        if item.get("status") in {"simulated_delivered", "already_provided"}
        and item.get("asset_key")
    ]
    for group_key in covered_groups:
        if deferred_group and group_key == deferred_group:
            continue
        group_assets = set((route_spec or {}).get("groups", {}).get(group_key, {}).get("assets") or [])
        record_session_content_delivery(
            session, group_key,
            asset_keys=[key for key in delivered_asset_keys if key in group_assets],
        )
        for item in group:
            if item.get("status") == "simulated_delivered" and not item.get("media_id"):
                if run.decision.get('v2_delivery_sections') and (
                    (item.get('content_attributes') or {}).get('delivery_item') or {}
                ).get('group_key') != group_key:
                    continue
                record_session_content_delivery(session, group_key, delivered_text=str(item.get("content") or ""))
    if any(item.get("run_id") == run.id and item.get("status") == "draft" for item in messages):
        db.commit()
        return
    if (
        run.decision.get("lead_action") == "ask"
        and (session.engine_version == "v2" or not deferred_initial_follow_up(
            run.decision,
            (session.controls.get("journey") or {}).get("sent_content_groups", []),
            slots=(session.controls.get("journey") or {}).get("slots", {}),
        ))
    ):
        capture = dict(session.controls.get("lead_capture") or {})
        if capture.get("status", "not_started") == "not_started":
            capture.update({"status": "asked", "request_count": int(capture.get("request_count", 0)) + 1})
            session.controls = {**session.controls, "lead_capture": capture}
    if run.module == "silence_touch":
        job = db.get(RehearsalJob, int(run.input_snapshot.get("sop_job_id") or 0))
        if not job:
            raise ValueError("silence_touch_job_missing")
        job.status, job.reason, job.confirmed_at = "simulated_delivered", None, session.virtual_now
        job.payload = {
            **(job.payload or {}),
            "model_decision": {
                "touch_goal": run.decision.get("touch_goal"),
                "touch_reason": run.decision.get("touch_reason"),
                "journey_stage": run.decision.get("journey_stage"),
                "covered_content_groups": run.decision.get("covered_content_groups") or [],
                "material_keys": run.decision.get("material_keys") or [],
            },
        }
        append_timeline_event(
            session,
            "silence_touch_sent",
            f"沉默触达已发送：{run.decision.get('touch_reason') or run.decision.get('touch_goal')}",
        )
    elif run.module == "wakeup":
        cycle = db.scalar(select(SilenceCycle).where(SilenceCycle.run_id == run.id))
        if not cycle:
            raise ValueError("wakeup_cycle_missing")
        cycle.status = "simulated_delivered"
        cycle.reason = None
        append_timeline_event(session, "silence_touch_sent", "沉默唤醒草稿已确认。")
    else:
        enrollment = enroll_detected_journey_route(db, session, run)
        if enrollment:
            advance_sops(db, session)
    # Full-journey rehearsals use the same reviewed 1/3/5/10/30/60 SOP as
    # production.  The legacy single 120-minute wake-up cycle is kept only for
    # the standalone reply playground and must not create a second timer here.
    if session.mode != "journey" and run.module != "wakeup":
        create_cycle(db, session)


def enroll_detected_journey_route(
    db: Session,
    session: AutomationSession,
    run: AutomationRun,
) -> RehearsalEnrollment | None:
    """Attach the reviewed SOP after the model has selected a route.

    The playground starts as an unclassified new customer. Route selection is
    therefore owned by the same model decision used in production, not by an
    operator-only setup form. This function only binds the selected route ID to
    its latest published SOP.
    """
    if session.environment != "playground" or session.mode != "journey":
        return None
    if run.decision.get("action") != "reply":
        return None
    if "skip_silence_enrollment" in set(run.decision.get("safety_flags") or []):
        append_timeline_event(session, "sop_exited", "当前需求不进入自动沉默跟进。")
        return None
    route = run.decision.get("route_variant", "")
    frozen_slots = (session.controls.get("journey") or {}).get("slots")
    route_spec = route_snapshot_from_values(route, frozen_slots) if route else None
    if route and route_spec is None:
        append_timeline_event(session, "sop_exited", "接待版本无法确认，已暂停自动推进。")
        return None
    canonical_name = route_spec["sop"]["name"] if route_spec else UNCLASSIFIED_SOP_NAME
    active = db.scalar(select(RehearsalEnrollment).where(
        RehearsalEnrollment.session_id == session.id,
        RehearsalEnrollment.status == "active",
    ))
    if active:
        return active
    candidate_query = (
        select(SopVersion, SopDefinition)
        .join(SopDefinition, SopVersion.sop_id == SopDefinition.id)
        .where(
            SopDefinition.status == "running",
            SopDefinition.route_variant == route,
            SopDefinition.name == canonical_name,
        )
    )
    candidates = db.execute(
        candidate_query.order_by(SopVersion.version.desc(), SopVersion.id.desc())
    ).all()
    selected = next(
        ((version, sop) for version, sop in candidates if enrollment_allowed(db, session, version.config)),
        None,
    )
    if not selected:
        append_timeline_event(session, "sop_exited", "AI 已识别线路，但当前没有可执行的已发布 SOP。")
        return None
    version, sop = selected
    subject = subject_key(db, session)
    previous = db.scalar(select(RehearsalEnrollment).where(
        RehearsalEnrollment.sop_id == version.sop_id,
        RehearsalEnrollment.subject_key == subject,
    ).order_by(RehearsalEnrollment.round_number.desc()))
    request_key = f"model-route:{session.id}:{session.generation}:{route or 'unclassified'}"
    from app.reception_config import get_reception_configuration, silence_intervals, v2_silence_intervals

    reception = get_reception_configuration(db)
    deferred = (
        deferred_initial_follow_up(
            run.decision,
            (session.controls.get("journey") or {}).get("sent_content_groups", []),
            slots=frozen_slots,
        )
        if session.engine_version == "v1" else None
    )

    enrollment = enroll_rehearsal(
        db,
        session,
        version,
        source="model_route",
        reenroll=bool(previous),
        request_key=request_key,
        schedule_intervals=(v2_silence_intervals(db) if session.engine_version == "v2" else silence_intervals(db)) if route else None,
        silence_enabled=bool(reception["silence"]["enabled"]),
        deferred_follow_up=deferred,
    )
    state = simulation_state(session)
    state.update({
        "sop_version_id": version.id,
        "sop_id": sop.id,
        "sop_name": canonical_name,
    })
    set_simulation_state(session, state)
    append_timeline_event(
        session,
        "sop_enrolled",
        f"AI 识别线路后已自动进入 {state['sop_name']}；客户回复会立即结束当前沉默轮次。",
    )
    return enrollment


def start_open_journey(
    db: Session,
    session: AutomationSession,
    *,
    duration_minutes: int,
    speed_multiplier: int,
    entry_message: str,
) -> None:
    """Start a realistic journey without asking the operator to pick a route."""
    if session.environment != "playground" or session.mode != "journey":
        raise ValueError("journey_requires_playground")
    if not entry_message.strip():
        raise ValueError("entry_message_required")
    started_wall = utcnow()
    state = {
        "status": "running",
        "duration_minutes": duration_minutes,
        "speed_multiplier": speed_multiplier,
        "start_virtual_at": session.virtual_now,
        "end_virtual_at": iso(dt(session.virtual_now) + timedelta(minutes=duration_minutes)),
        "last_wall_at": started_wall,
        "started_wall_at": started_wall,
        "completed_wall_at": None,
        "sop_version_id": None,
        "sop_id": None,
        "sop_name": "等待 AI 识别线路",
        "entry_message": entry_message.strip(),
    }
    set_simulation_state(session, state)
    append_timeline_event(session, "journey_started", "一位新客户已进入，系统将按真实 AI 接待流程处理。")
    add_customer_message(
        db,
        session,
        entry_message.strip(),
        f"journey-entry:{session.id}",
        trigger_entry_sops=False,
    )


def start_journey(db: Session, session: AutomationSession, version: SopVersion, *,
                  duration_minutes: int, speed_multiplier: int, entry_message: str) -> RehearsalEnrollment:
    if session.environment != "playground" or session.mode != "journey":
        raise ValueError("journey_requires_playground")
    route = version.config.get("route_variant", "")
    if not route or route != session.controls.get("route_variant"):
        raise ValueError("journey_route_mismatch")
    if not entry_message.strip():
        raise ValueError("entry_message_required")
    if not any(item.get("direction") == "outgoing" for item in session.messages or []):
        journey = dict(session.controls.get("journey") or {})
        slots = dict(journey.get("slots") or {})
        snapshots = dict(slots.get(ROUTE_SNAPSHOTS_KEY) or {})
        if route not in snapshots:
            spec = dict(ROUTES[route])
            candidates = candidate_materials(db, tenant_for_session(db, session))
            spec["asset_hashes"] = {item["key"]: item["media_hash"] for item in candidates if route in item.get("routes", [])}
            spec["asset_bindings"] = {item["key"]: {"asset_key": item["key"], "media_id": item["media_id"],
                "media_hash": item["media_hash"], "content_type": item["content_type"]} for item in candidates if route in item.get("routes", [])}
            snapshots[route] = make_route_snapshot(route, spec)
            slots[ROUTE_SNAPSHOTS_KEY] = snapshots
            journey.update(route_variant=route, slots=slots)
            session.controls = {**session.controls, "journey": journey}
    started_wall = utcnow()
    end_virtual = iso(dt(session.virtual_now) + timedelta(minutes=duration_minutes))
    state = {
        "status": "running",
        "duration_minutes": duration_minutes,
        "speed_multiplier": speed_multiplier,
        "start_virtual_at": session.virtual_now,
        "end_virtual_at": end_virtual,
        "last_wall_at": started_wall,
        "started_wall_at": started_wall,
        "completed_wall_at": None,
        "sop_version_id": version.id,
        "sop_id": version.sop_id,
        "sop_name": version.config.get("name", f"SOP #{version.sop_id}"),
        "entry_message": entry_message.strip(),
    }
    set_simulation_state(session, state)
    append_timeline_event(session, "journey_started", "全链路演练已开始，真实 Chatwoot 发送保持关闭。")
    add_customer_message(
        db,
        session,
        entry_message.strip(),
        f"journey-entry:{session.id}",
        trigger_entry_sops=False,
    )
    enrollment = enroll_rehearsal(db, session, version, source="journey")
    append_timeline_event(
        session,
        "sop_enrolled",
        f"已自动入组 {state['sop_name']}；客户回复时本轮 SOP 将结束并由 AI 接管。",
    )
    advance_sops(db, session)
    return enrollment


def change_journey_status(db: Session, session: AutomationSession, action: str) -> None:
    state = simulation_state(session)
    status = state.get("status")
    if action == "pause":
        if status != "running":
            raise ValueError("journey_not_running")
        state["status"] = "paused"
        append_timeline_event(session, "journey_paused", "演练已暂停，虚拟时间停止推进。")
    elif action == "resume":
        if status != "paused":
            raise ValueError("journey_not_paused")
        state["status"] = "running"
        state["last_wall_at"] = utcnow()
        append_timeline_event(session, "journey_resumed", "演练已恢复。")
    elif action == "stop":
        if status in JOURNEY_TERMINAL:
            return
        cancel_generation(db, session, "journey_stopped")
        session.generation += 1
        session.due_at = None
        session.batch_started_at = None
        state["status"] = "stopped"
        state["completed_wall_at"] = utcnow()
        append_timeline_event(session, "journey_stopped", "演练已手动结束。")
    else:
        raise ValueError("journey_action_invalid")
    set_simulation_state(session, state)


def _auto_confirm_journey_drafts(db: Session, session: AutomationSession) -> bool:
    messages = list(session.messages or [])
    run_ids = []
    for item in messages:
        if item.get("status") == "draft" and dt(item["created_at"]) <= dt(session.virtual_now) and item.get("source") in {"ai", "sop_ai", "wakeup_ai"} and item.get("run_id") not in run_ids:
            run_ids.append(item["run_id"])
    changed = False
    for run_id in run_ids:
        run = db.get(AutomationRun, run_id)
        if not run or run.generation != session.generation or run.status != "completed":
            continue
        first = next((x for x in session.messages if x.get("run_id") == run_id and x.get("status") == "draft"), None)
        if not first:
            continue
        try:
            confirm_draft(db, session, str(first["id"]), due_only=True)
            if not any(item.get("run_id") == run_id and item.get("status") == "draft" for item in session.messages):
                append_timeline_event(session, "ai_replied", "本轮消息已逐条通过发送前校验，并在沙盒中模拟送达。")
        except ValueError as exc:
            updated = [dict(x) for x in session.messages]
            for item in updated:
                if item.get("run_id") == run_id and item.get("status") == "draft":
                    item["status"] = "blocked"
            session.messages = updated
            append_timeline_event(session, "ai_blocked", f"AI 草稿未通过发送前校验：{exc}")
        changed = True
    return changed


def _active_run(db: Session, session: AutomationSession) -> bool:
    return bool(db.scalar(select(AutomationRun.id).where(
        AutomationRun.session_id == session.id,
        AutomationRun.status.in_(["pending", "processing"]),
    )))


def _next_sop_due(db: Session, session: AutomationSession) -> str | None:
    return db.scalar(select(RehearsalJob.scheduled_at).join(
        RehearsalEnrollment, RehearsalJob.enrollment_id == RehearsalEnrollment.id
    ).where(
        RehearsalEnrollment.session_id == session.id,
        RehearsalEnrollment.status == "active",
        RehearsalJob.status == "scheduled",
        RehearsalJob.scheduled_at.is_not(None),
        RehearsalJob.scheduled_at > session.virtual_now,
    ).order_by(RehearsalJob.scheduled_at).limit(1))


def advance_running_playgrounds(db: Session, wall_now: str | None = None, *, session_id: int | None = None) -> bool:
    now = dt(wall_now or utcnow())
    query = select(AutomationSession).where(
        AutomationSession.environment == "playground",
        AutomationSession.mode == "journey",
    )
    if session_id is not None:
        query = query.where(AutomationSession.id == session_id)
    rows = db.scalars(query.order_by(AutomationSession.id.desc())).all()
    changed = False
    for session in rows:
        state = simulation_state(session)
        if state.get("status") != "running":
            continue
        if _auto_confirm_journey_drafts(db, session):
            state = simulation_state(session)
            state["last_wall_at"] = iso(now)
            set_simulation_state(session, state)
            advance_sops(db, session)
            changed = True
            continue
        if session.due_at or _active_run(db, session):
            state["last_wall_at"] = iso(now)
            set_simulation_state(session, state)
            changed = True
            continue
        before = [(x.id, x.status) for x in db.scalars(select(RehearsalJob).join(
            RehearsalEnrollment, RehearsalJob.enrollment_id == RehearsalEnrollment.id
        ).where(RehearsalEnrollment.session_id == session.id)).all()]
        advance_sops(db, session)
        after = [(x.id, x.status) for x in db.scalars(select(RehearsalJob).join(
            RehearsalEnrollment, RehearsalJob.enrollment_id == RehearsalEnrollment.id
        ).where(RehearsalEnrollment.session_id == session.id)).all()]
        if before != after:
            changed = True
        last_wall = dt(state.get("last_wall_at") or iso(now))
        wall_seconds = max(0.0, (now - last_wall).total_seconds())
        if wall_seconds < 0.2:
            continue
        current = dt(session.virtual_now)
        target = current + timedelta(seconds=wall_seconds * max(1, int(state.get("speed_multiplier") or 1)))
        end = dt(state["end_virtual_at"])
        if target > end:
            target = end
        next_due = _next_sop_due(db, session)
        draft_due = min((item["created_at"] for item in session.messages
                         if item.get("status") == "draft" and dt(item["created_at"]) > current), default=None)
        if draft_due and (not next_due or dt(draft_due) < dt(next_due)):
            next_due = draft_due
        if next_due and current < dt(next_due) < target:
            target = dt(next_due)
        session.virtual_now = iso(target)
        state["last_wall_at"] = iso(now)
        if target >= end:
            state["status"] = "completed"
            state["completed_wall_at"] = iso(now)
            append_timeline_event(session, "journey_completed", "已到达设定的演练时长，全链路演练完成。", at=session.virtual_now)
        set_simulation_state(session, state)
        advance_sops(db, session)
        changed = True
    if changed:
        db.commit()
    return changed


def rehearse_tick(db: Session, environment: str | None = None) -> bool:
    return queue_passive(db, environment) or process_automation_run(db, environment)
