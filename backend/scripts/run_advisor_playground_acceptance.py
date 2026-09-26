"""Create and exercise durable AI-playground customer journeys.

The sessions remain visible in the operations UI. This script refuses to run
while global real-message sending is enabled and verifies that the delivery
outbox count is unchanged.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

from sqlalchemy import func, select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.automation_models import (
    AutomationRun,
    AutomationSession,
    RehearsalEnrollment,
    RehearsalJob,
)
from app.automation_service import (
    TERMINAL,
    advance_running_playgrounds,
    advance_sops,
    dt,
    iso,
    process_automation_run,
    queue_passive,
    set_simulation_state,
    simulation_state,
    start_open_journey,
)
from app.db import SessionLocal
from app.material_library import by_media
from app.models import InboxBinding, OutboundMessage, StoredMedia, User, utcnow
from app.outbound_control import global_message_sending_enabled
from app.reception_config import effective_reception_policy
from app.reply_generation import _character_bigrams
from app.route_packages import ROUTE_PACKAGES, ROUTES
from app.route_reply import route_snapshot_from_values


SCENARIOS = [
    {
        "key": "unselected_route",
        "title": "尚未选择线路",
        "message": "你好，我想了解西藏桃花行程。",
        "expected_route": "",
        "silence_touches": 1,
    },
    {
        "key": "route_9d",
        "title": "明确咨询9日线路",
        "message": "我想看看桃花9日不上珠峰的行程。",
        "expected_route": "peach_9d_2027",
        "silence_touches": 2,
    },
    {
        "key": "route_11d",
        "title": "明确咨询11日线路",
        "message": "我想了解桃花加珠峰11日，先发我看看。",
        "expected_route": "peach_11d_2027",
        "silence_touches": 2,
    },
    {
        "key": "hotel_images",
        "title": "询问酒店并查看图片",
        "message": "桃花加珠峰11日住什么酒店？有房间照片吗？",
        "expected_route": "peach_11d_2027",
        "silence_touches": 2,
    },
    {
        "key": "vehicle_oxygen",
        "title": "询问车辆和供氧配置",
        "message": "桃花9日用什么车？车上有供氧吗？想看看照片。",
        "expected_route": "peach_9d_2027",
        "silence_touches": 2,
    },
    {
        "key": "date_uncertain",
        "title": "日期不确定但愿意了解",
        "message": "我想看桃花9日，不过日期还不确定，先了解一下。",
        "expected_route": "peach_9d_2027",
        "silence_touches": 2,
    },
    {
        "key": "family_discussion",
        "title": "与家人讨论",
        "message": "桃花加珠峰11日我先跟家人讨论一下，你先发重点给我。",
        "expected_route": "peach_11d_2027",
        "silence_touches": 2,
    },
    {
        "key": "direct_price",
        "title": "直接询问价格",
        "message": "桃花9日现在价格多少？费用包含什么？",
        "expected_route": "peach_9d_2027",
        "silence_touches": 2,
    },
    {
        "key": "seven_people",
        "title": "7人普通客户",
        "message": "我们7个人，想了解桃花加珠峰11日。",
        "expected_route": "peach_11d_2027",
        "expected_action": "reply",
        "silence_touches": 2,
    },
    {
        "key": "eight_people_handoff",
        "title": "8人大团转人工",
        "message": "我们8个人，想了解桃花加珠峰11日和住宿。",
        "expected_route": "peach_11d_2027",
        "expected_action": "handoff",
        "expected_handoff": "large_group_custom_quote",
        "silence_touches": 0,
    },
    {
        "key": "human_request",
        "title": "主动要求真人",
        "message": "我想看桃花9日，请直接安排真人顾问跟我说。",
        "expected_route": "peach_9d_2027",
        "expected_action": "handoff",
        "expected_handoff": "explicit_human_request",
        "silence_touches": 0,
    },
    {
        "key": "contact_captured",
        "title": "留下联系方式",
        "message": "我想看桃花9日，LINE ID 是 c2g_playground_0904。",
        "expected_route": "peach_9d_2027",
        "expected_action": "handoff",
        "expected_handoff": "lead_captured",
        "silence_touches": 0,
    },
    {
        "key": "tour_doctor_service",
        "title": "询问随团医师",
        "message": "你们有随团医师吗？",
        "expected_route": "",
        "expected_fixed_answer": "tour_doctor_service",
        "silence_touches": 0,
    },
    {
        "key": "altitude_sickness_symptoms",
        "title": "询问高原反应症状",
        "message": "高原反应有哪些症状？",
        "expected_route": "",
        "expected_fixed_answer": "altitude_sickness_symptoms",
        "silence_touches": 0,
    },
    {
        "key": "altitude_sickness_response",
        "title": "询问高原反应处理",
        "message": "到了西藏高反怎么办？",
        "expected_route": "",
        "expected_fixed_answer": "altitude_sickness_response",
        "silence_touches": 0,
    },
]


SCENARIOS.extend([
    {"key": "feedback_greeting", "title": "普通开场不带官网百科", "message": "你好，我想咨询旅行行程。", "expected_route": "", "silence_touches": 0},
    {"key": "feedback_weather", "title": "季节冷暖直接回答", "message": "桃花9日那個時候會不會冷啊？", "expected_route": "peach_9d_2027", "silence_touches": 0},
    {"key": "feedback_medication", "title": "药物问题不追问人数", "message": "想看桃花9日，要先吃紅景天還是丹木斯呢？", "expected_route": "peach_9d_2027", "silence_touches": 0, "expect_no_deferred_question": True},
    {"key": "feedback_tips", "title": "小费边界", "message": "桃花9日司機和導遊小費怎麼算呢？", "expected_route": "peach_9d_2027", "silence_touches": 0},
    {"key": "feedback_namtso", "title": "未包含景点直接回答", "message": "桃花9日有去納木錯嗎？", "expected_route": "peach_9d_2027", "silence_touches": 0},
])


SILENCE_EXPECTATIONS = {
    "unselected_route": "positive_value",
    **{key: "value_or_no_action" for key in (
        "route_9d", "route_11d", "hotel_images", "vehicle_oxygen",
        "date_uncertain", "family_discussion", "direct_price", "seven_people",
    )},
}
EXPECTED_NO_ACTION_REASONS = {
    "silence_unresolved_route_no_new_value",
    "silence_contact_already_requested_no_new_value",
    "silence_no_relevant_content",
    "silence_stopped_by_terminal_stage",
}


def _silence_result(scenario: dict, runs: list, messages: list[dict]) -> dict:
    expected = scenario.get("silence_expectation") or SILENCE_EXPECTATIONS.get(scenario["key"])
    required = int(scenario.get("silence_touches") or 0)
    counts = dict.fromkeys((
        "positive_value", "expected_no_action", "verification_blocked", "invalid_output",
    ), 0)
    verdicts = []
    delivered_count = 0
    for row in runs:
        if row.module not in {"silence_touch", "wakeup"}:
            continue
        decision, trace = row.decision or {}, row.trace or {}
        output = [item for item in messages if item.get("direction") == "outgoing"
                  and item.get("run_id") == row.id]
        delivered = [item for item in output
                     if item.get("status") == "simulated_delivered"
                     and (str(item.get("content") or "").strip() or item.get("media_id"))]
        delivered_count += len(delivered)
        reason = trace.get("skip_reason") or ""
        flags = decision.get("safety_flags") or []
        if (reason in {"fact_verification_failed_no_action", "silence_content_without_facts"}
                or "silence_verification_failed_no_action" in flags
                or trace.get("fact_verification_passed") is False):
            verdict = "verification_blocked"
        elif row.status != "completed":
            verdict = "invalid_output"
        elif decision.get("action") == "no_action":
            verdict = "expected_no_action" if (
                not output and reason in EXPECTED_NO_ACTION_REASONS
            ) else "invalid_output"
        elif decision.get("action") == "reply" and delivered:
            verdict = "positive_value"
        else:
            verdict = "invalid_output"
        counts[verdict] += 1
        verdicts.append({"run_id": row.id, "verdict": verdict, "reason": reason,
                         "delivered_messages": len(delivered)})
    allowed = {
        "positive_value": {"positive_value"},
        "no_action": {"expected_no_action"},
        "value_or_no_action": {"positive_value", "expected_no_action"},
    }.get(expected, set())
    passed = (len(verdicts) >= required and all(item["verdict"] in allowed for item in verdicts))
    return {"expectation": expected, "required_touches": required,
            "observed_touches": len(verdicts), "delivered_messages": delivered_count,
            "counts": counts, "verdicts": verdicts, "passed": passed}


def _report_counts(sessions: list[dict]) -> dict:
    counts = dict.fromkeys((
        "positive_value", "expected_no_action", "verification_blocked", "invalid_output",
    ), 0)
    for session in sessions:
        for key, value in session["silence_validation"]["counts"].items():
            counts[key] += value
    return {"silence_verdict_counts": counts,
            "failed_sessions": sum(not item["passed"] for item in sessions),
            "checks_passed": sum(sum(item["checks"].values()) for item in sessions),
            "checks_failed": sum(sum(not value for value in item["checks"].values()) for item in sessions)}


def _repeats_asset_narration(value: str) -> bool:
    sentences = [
        item.strip()
        for item in re.split(r"(?<=[。！!?？])\s*", value)
        if len(re.sub(r"\s+", "", item)) >= 12
    ]
    for index, first in enumerate(sentences):
        first_bigrams = _character_bigrams(first)
        if len(first_bigrams) < 8:
            continue
        for second in sentences[index + 1:]:
            second_bigrams = _character_bigrams(second)
            if len(second_bigrams) < 8:
                continue
            overlap = len(first_bigrams & second_bigrams)
            if overlap >= 8 and overlap / min(len(first_bigrams), len(second_bigrams)) >= 0.34:
                return True
    return False


def _outbound_count(db) -> int:
    return int(db.scalar(select(func.count(OutboundMessage.id))) or 0)


def _delivered_asset_key(db, item: dict) -> str:
    if item.get("asset_key"):
        return str(item["asset_key"])
    media = db.get(StoredMedia, int(item["media_id"]))
    asset = by_media(db, media) if media else None
    return asset.asset_key if asset else f"media:{item['media_id']}"


def _delivery_order(
    messages: list[dict],
    *,
    minimum_gap_seconds: int,
    delivery_mode: str,
) -> tuple[bool, bool]:
    """Check the configured text/assets order and every adjacent delivery gap."""
    ordered = sorted(
        messages,
        key=lambda item: (
            int(item.get("timeline_sequence") or 0),
            str(item.get("created_at") or ""),
            str(item.get("id") or ""),
        ),
    )
    image_indexes = [index for index, item in enumerate(ordered) if item.get("media_id")]
    text_indexes = [
        index for index, item in enumerate(ordered)
        if str(item.get("content") or "").strip()
    ]
    if delivery_mode == "text_only":
        order_ok = bool(text_indexes) and not image_indexes
    elif delivery_mode == "assets_only":
        order_ok = bool(image_indexes) and not text_indexes
    elif delivery_mode == "text_then_assets":
        order_ok = bool(text_indexes and image_indexes) and max(text_indexes) < min(image_indexes)
    else:
        order_ok = bool(text_indexes and image_indexes) and max(image_indexes) < min(text_indexes)
    timestamps = [dt(item["created_at"]) for item in ordered]
    timing_ok = all(
        (current - previous).total_seconds() >= minimum_gap_seconds
        for previous, current in zip(timestamps, timestamps[1:])
    )
    return order_ok, timing_ok


def _initial_delivery_result(spec: dict | None, jobs: list, runs: list, messages: list[dict]) -> dict:
    """Validate receipts across split jobs against one frozen route snapshot."""
    spec = spec or {}
    nodes = [node for node in spec.get("sop", {}).get("nodes", []) if node.get("initial_delivery") is True]
    expected = list(dict.fromkeys(node["content_group_key"] for node in nodes))
    definitions = {key: dict(spec["groups"][key]) for key in expected}
    for node in nodes:
        definitions[node["content_group_key"]]["delivery_mode"] = node.get("delivery_mode", definitions[node["content_group_key"]].get("delivery_mode", "text_only"))
    initial_jobs = [job for job in jobs if job.payload.get("initial_delivery") is True]
    job_groups = {job.id: job.payload.get("content_group_key") for job in initial_jobs}
    follow_keys = []
    for job in initial_jobs:
        follow = job.payload.get("deferred_follow_up")
        if follow:
            key = job.payload["content_group_key"]
            if key not in follow_keys:
                follow_keys.append(key)
            definitions[key] = {"text": follow["question"], "assets": [], "delivery_mode": "text_only"}
    expected = [*expected, *follow_keys]
    reply_ids = {row.id for row in runs if row.module == "reply" and row.status == "completed"}
    grouped = {key: [] for key in expected}
    prior = {key: [] for key in expected}
    direct_answers = []
    direct_messages = []
    ordered = []
    def position(item):
        return (int(item.get("timeline_sequence") or 0),
                str(item.get("created_at") or ""), str(item.get("id") or ""))

    def asset_key(item):
        matches = [asset for asset, binding in spec.get("asset_bindings", {}).items()
                   if binding.get("media_id") == item.get("media_id")
                   and (not item.get("asset_key") or item["asset_key"] == asset)
                   and (not item.get("media_hash") or item["media_hash"] == binding.get("media_hash"))]
        return matches[0] if len(matches) == 1 else "unverified_media"

    fixed_messages = [item for item in messages if item.get("source") == "sop"
                      and item.get("group_id") in job_groups and item.get("direction") == "outgoing"
                      and item.get("status") == "simulated_delivered"]
    fixed_start = min((position(item) for item in fixed_messages), default=None)
    for item in messages:
        if item.get("direction") != "outgoing" or item.get("status") != "simulated_delivered":
            continue
        key = job_groups.get(item.get("group_id")) if item.get("source") == "sop" else None
        if item.get("run_id") in reply_ids and item.get("source") != "sop":
            direct_key = ((item.get("content_attributes") or {}).get("delivery_item") or {}).get("group_key")
            direct_answers.append({"message_id": item.get("id"), "content_group_key": direct_key,
                                   "media_id": item.get("media_id")})
            direct_messages.append(item)
            # A generated answer is not reviewed mainline copy. Only exact,
            # earlier receipts may discharge fixed delivery requirements.
            definition = definitions.get(direct_key)
            if definition and (fixed_start is None or position(item) < fixed_start):
                if ((item.get("media_id") and asset_key(item) in definition.get("assets", []))
                        or (not item.get("media_id") and item.get("content") == definition.get("text"))):
                    prior[direct_key].append(item)
        if key:
            ordered.append((key, item))
            grouped.setdefault(key, []).append(item)
    ordered.sort(key=lambda pair: position(pair[1]))
    actual = []
    for key, _ in ordered:
        if not actual or actual[-1] != key:
            actual.append(key)
    gap = int(spec.get("initial_delivery_interval_seconds") or 2)
    audit = []
    pending_expected = []
    preprovided = []
    for key in expected:
        definition = definitions[key]
        fixed = sorted(grouped[key], key=position)
        items = [*prior[key], *fixed]
        mode = definition.get("delivery_mode", "assets_then_text")
        texts = [str(item.get("content") or "") for item in items if str(item.get("content") or "").strip()]
        required_assets = [] if mode == "text_only" else list(definition.get("assets") or [])
        assets = []
        for item in items:
            if not item.get("media_id"):
                continue
            assets.append(asset_key(item))
        required_parts = ([] if mode == "assets_only" else ["text"])
        required_parts = ([*required_parts, *required_assets] if mode in {"text_only", "text_then_assets"}
                          else [*required_assets, *required_parts])
        remaining_parts = list(required_parts)
        for item in prior[key]:
            part = asset_key(item) if item.get("media_id") else "text"
            if part in remaining_parts:
                remaining_parts.remove(part)
        if remaining_parts:
            pending_expected.append(key)
        else:
            preprovided.append(key)
        actual_parts = [asset_key(item) if item.get("media_id") else "text" for item in fixed]
        order_ok = mode in {"text_only", "assets_only", "text_then_assets", "assets_then_text"} and actual_parts == remaining_parts
        times = [dt(item["created_at"]) for item in fixed]
        timing_ok = all((later - earlier).total_seconds() == gap for earlier, later in zip(times, times[1:]))
        audit.append({"content_group_key": key, "delivery_mode": mode, "message_count": len(items),
                      "fixed_message_count": len(fixed), "prior_receipt_ids": [item.get("id") for item in prior[key]],
                      "remaining_parts": remaining_parts, "actual_fixed_parts": actual_parts,
                      "asset_keys": assets, "required_asset_keys": required_assets,
                      "caption_ok": bool(items) and (mode == "assets_only" or bool(texts)),
                      "copy_ok": texts == ([] if mode == "assets_only" else [definition.get("text")]),
                      "assets_ok": sorted(assets) == sorted(required_assets),
                      "order_ok": order_ok, "timing_ok": timing_ok})
    visible = sorted([*fixed_messages, *direct_messages], key=position)
    visible_gaps = [{"previous_message_id": earlier.get("id"), "message_id": later.get("id"),
                     "actual_seconds": (dt(later["created_at"]) - dt(earlier["created_at"])).total_seconds(),
                     "expected_seconds": gap}
                    for earlier, later in zip(visible, visible[1:])]
    return {"groups": actual, "expected_groups": pending_expected, "required_groups": expected,
            "preprovided_groups": preprovided, "direct_answer_messages": direct_answers, "group_results": audit,
            "visible_delivery_gaps": visible_gaps,
            "checks": {
                "initial_snapshot_available": bool(spec and nodes),
                "initial_delivery_order_matches_config": bool(expected) and actual == pending_expected,
                "each_delivered_initial_group_has_caption": bool(audit) and all(row["caption_ok"] for row in audit),
                "initial_copy_matches_current_package": bool(audit) and all(row["copy_ok"] for row in audit),
                "initial_delivery_assets_match_snapshot": bool(audit) and all(row["assets_ok"] for row in audit),
                "initial_delivery_mode_is_respected": bool(audit) and all(row["order_ok"] for row in audit),
                "initial_delivery_gap_matches_route_config": bool(visible) and all(
                    item["actual_seconds"] == gap for item in visible_gaps),
                "initial_question_is_last": bool(ordered) and (not follow_keys or actual[-len(follow_keys):] == follow_keys),
                "deferred_question_is_actually_delivered": all(
                    grouped.get(key) and [item.get("content") for item in grouped[key]] == [definitions[key]["text"]]
                    for key in follow_keys
                ),
            }}


def _active_run(db, session_id: int) -> bool:
    return bool(db.scalar(select(AutomationRun.id).where(
        AutomationRun.session_id == session_id,
        AutomationRun.status.in_(["pending", "processing"]),
    )))


def _drive_once(drive_worker: bool, session_id: int) -> None:
    if not drive_worker:
        time.sleep(0.8)
        return
    with SessionLocal() as db:
        queue_passive(db, environment="playground", session_id=session_id)
        process_automation_run(db, environment="playground", session_id=session_id)
        advance_running_playgrounds(db, iso(dt(utcnow())), session_id=session_id)


def _wait_ready(session_id: int, *, drive_worker: bool, minimum_outgoing: int, timeout: int) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        _drive_once(drive_worker, session_id)
        with SessionLocal() as db:
            session = db.get(AutomationSession, session_id)
            outgoing = [m for m in (session.messages or []) if m.get("direction") == "outgoing"]
            terminal_failure = db.scalar(select(AutomationRun).where(
                AutomationRun.session_id == session_id,
                AutomationRun.status == "failed",
            ).order_by(AutomationRun.id.desc()).limit(1))
            initial_jobs = db.scalars(select(RehearsalJob).join(
                RehearsalEnrollment,
                RehearsalJob.enrollment_id == RehearsalEnrollment.id,
            ).where(
                RehearsalEnrollment.session_id == session_id,
            )).all()
            initial_jobs = [row for row in initial_jobs if row.payload.get("initial_delivery") is True]
            initial_delivery_complete = all(row.status in TERMINAL for row in initial_jobs)
            if (
                len(outgoing) >= minimum_outgoing
                and not session.due_at
                and not _active_run(db, session_id)
                and not any(m.get("status") == "draft" for m in outgoing)
                and initial_delivery_complete
            ):
                return
            if terminal_failure and not _active_run(db, session_id):
                reason = str(terminal_failure.error_code or "automation_run_failed")
                raise RuntimeError(f"playground_session_failed:{session_id}:{reason}")
    raise TimeoutError(f"playground_session_timeout:{session_id}:{minimum_outgoing}")


def _wait_silence_terminal(
    session_id: int,
    job_id: int,
    *,
    drive_worker: bool,
    timeout: int,
) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        _drive_once(drive_worker, session_id)
        with SessionLocal() as db:
            session = db.get(AutomationSession, session_id)
            job = db.get(RehearsalJob, job_id)
            if (
                job
                and job.status in {
                    "simulated_delivered", "already_provided", "skipped_model_failure",
                    "skipped", "verification_blocked", "blocked", "cancelled", "expired",
                }
                and not _active_run(db, session_id)
                and not any(
                    item.get("status") == "draft"
                    for item in (session.messages or [])
                    if item.get("direction") == "outgoing"
                )
            ):
                return
    raise TimeoutError(f"playground_silence_timeout:{session_id}:{job_id}")


def _advance_next_silence(session_id: int) -> int | None:
    with SessionLocal() as db:
        session = db.get(AutomationSession, session_id)
        candidates = db.scalars(select(RehearsalJob).join(
            RehearsalEnrollment,
            RehearsalJob.enrollment_id == RehearsalEnrollment.id,
        ).where(
            RehearsalEnrollment.session_id == session_id,
            RehearsalEnrollment.status == "active",
            RehearsalJob.status == "scheduled",
            RehearsalJob.scheduled_at.is_not(None),
        ).order_by(RehearsalJob.scheduled_at, RehearsalJob.id)).all()
        next_job = next(
            (row for row in candidates if row.payload.get("journey_trigger")),
            None,
        )
        if not next_job or not next_job.scheduled_at:
            return None
        session.virtual_now = next_job.scheduled_at
        state = simulation_state(session)
        state["last_wall_at"] = utcnow()
        set_simulation_state(session, state)
        advance_sops(db, session)
        db.commit()
        return next_job.id


def _create_session(owner_id: int, inbox_id: int, scenario: dict) -> int:
    with SessionLocal() as db:
        now = utcnow()
        session = AutomationSession(
            owner_id=owner_id,
            inbox_binding_id=inbox_id,
            mode="journey",
            virtual_now=now,
            controls={
                "can_reply": True,
                "ai_enabled": True,
                "channel": "facebook",
                "labels": [],
                "human": False,
                "permission_source": "simulated",
                "history_complete": True,
                "history_source": "isolated_session",
                "acceptance_scenario": scenario["key"],
                "acceptance_title": scenario["title"],
            },
            messages=[],
        )
        db.add(session)
        db.flush()
        start_open_journey(
            db,
            session,
            duration_minutes=525600,
            speed_multiplier=1,
            entry_message=scenario["message"],
        )
        session_id = session.id
        db.commit()
        return session_id


def _session_result(session_id: int, scenario: dict) -> dict:
    with SessionLocal() as db:
        session = db.get(AutomationSession, session_id)
        runs = db.scalars(select(AutomationRun).where(
            AutomationRun.session_id == session_id,
        ).order_by(AutomationRun.id)).all()
        messages = list(session.messages or [])
        outgoing_text = [
            str(item.get("content") or "")
            for item in messages
            if item.get("direction") == "outgoing" and item.get("content")
        ]
        media = [
            item for item in messages
            if item.get("direction") == "outgoing"
            and item.get("media_id")
            and item.get("status") == "simulated_delivered"
        ]
        delivered_assets = [
            _delivered_asset_key(db, item)
            for item in media
        ]
        reply_runs = [row for row in runs if row.module == "reply" and row.status == "completed"]
        first = reply_runs[0].decision if reply_runs else {}
        first_trace = dict(reply_runs[0].trace or {}) if reply_runs else {}
        all_assets = delivered_assets
        text_by_run = {
            int(item["run_id"]): str(item.get("content") or "")
            for item in messages
            if item.get("direction") == "outgoing"
            and item.get("run_id")
            and item.get("content")
        }
        material_runs = [
            row for row in runs
            if row.status == "completed" and (row.decision or {}).get("material_keys")
        ]
        silence_text = [
            str(item.get("content") or "")
            for item in messages
            if item.get("source") in {"sop_ai", "wakeup_ai"} and item.get("content")
        ]
        silence_groups = [
            str(group)
            for row in runs if row.module in {"silence_touch", "wakeup"}
            for group in ((row.decision or {}).get("covered_content_groups") or [])
            if group
        ]
        jobs = db.scalars(select(RehearsalJob).join(
            RehearsalEnrollment,
            RehearsalJob.enrollment_id == RehearsalEnrollment.id,
        ).where(
            RehearsalEnrollment.session_id == session_id,
        ).order_by(RehearsalJob.id)).all()
        initial_jobs = [row for row in jobs if row.payload.get("initial_delivery") is True]
        expected_route = scenario.get("expected_route", "")
        route_spec = route_snapshot_from_values(expected_route, (session.controls.get("journey") or {}).get("slots")) if expected_route else None
        expected_initial_groups = []
        if route_spec:
            expected_initial_groups = [
                str(node.get("content_group_key") or "")
                for node in route_spec["sop"]["nodes"]
                if node.get("initial_delivery") is True
            ]
        initial_groups = [str(row.payload.get("content_group_key") or "") for row in initial_jobs]
        initial_validation = None
        journey_groups = set((session.controls.get("journey") or {}).get("sent_content_groups", []))
        checks = {
            "reply_run_completed": bool(reply_runs),
            "no_failed_automation_runs": not any(row.status == "failed" for row in runs),
            "expected_route": first.get("route_variant", "") == scenario.get("expected_route", ""),
            "expected_action": first.get("action") == scenario.get("expected_action", "reply"),
            "expected_handoff": (
                not scenario.get("expected_handoff")
                or first.get("handoff_reason") == scenario["expected_handoff"]
            ),
            "no_internal_identity": not any(
                term in text for text in outgoing_text
                for term in ("我是AI", "AI機器", "大模型", "提示詞", "系統內部")
            ),
            "no_repeated_default_opening": sum(
                text.lstrip().startswith(("收到，", "了解，", "好的，"))
                for text in outgoing_text
            ) <= 1,
            "no_sales_jargon": not any(
                term in text for text in outgoing_text
                for term in ("小夥伴", "小伙伴", "匹配方案", "接續確認")
            ),
            "traditional_chinese_consistent": not any(
                re.search(r"[发这们线图间价后现还让从较见过说给对进实车团开关点华转读张当时经资问号满]", text)
                for text in outgoing_text
            ),
            "no_duplicate_assets": len(all_assets) == len(set(all_assets)),
            "outgoing_is_simulated": all(
                item.get("status") in {"simulated_delivered", "already_provided"}
                for item in messages if item.get("direction") == "outgoing"
            ),
            "every_material_run_has_caption": all(
                bool(text_by_run.get(row.id, "").strip())
                for row in material_runs
            ),
            "no_repeated_asset_narration_within_turn": all(
                not _repeats_asset_narration(text_by_run.get(row.id, ""))
                for row in material_runs
            ),
        }
        if not expected_route:
            checks["unselected_route_has_no_images"] = not media and not first.get("material_keys")
            checks["unselected_route_has_no_initial_delivery"] = not initial_jobs
            if scenario.get("expected_fixed_answer"):
                checks["expected_service_fixed_answer"] = (
                    first_trace.get("fixed_answer_scope") == "service"
                    and first_trace.get("fixed_answer_id") == scenario["expected_fixed_answer"]
                    and first_trace.get("response_source") == "service_fixed_answer"
                )
        elif scenario.get("expect_no_deferred_question"):
            checks["safety_answer_has_no_deferred_question"] = not any(
                row.payload.get("deferred_follow_up") for row in initial_jobs
            )
            checks["safety_answer_precedes_optional_mainline"] = bool(outgoing_text) and (
                "醫師" in outgoing_text[0] or "藥師" in outgoing_text[0]
            )
            checks["optional_mainline_has_no_failed_delivery"] = all(
                row.status in {"simulated_delivered", "already_provided"}
                for row in initial_jobs
            )
        elif scenario.get("expected_action", "reply") == "reply":
            initial_validation = _initial_delivery_result(route_spec, jobs, runs, messages)
            checks.update(initial_validation["checks"])
            initial_groups = initial_validation["groups"]
            checks["initial_delivery_all_completed"] = bool(initial_jobs) and all(
                row.status in {"simulated_delivered", "already_provided"}
                for row in initial_jobs
            )
            checks["initial_delivery_groups_recorded"] = set(expected_initial_groups).issubset(journey_groups)
            if scenario['key'] in {'route_9d', 'route_11d'}:
                checks['brand_copy_actually_delivered'] = any(
                    row["content_group_key"] == "brand_positioning" and row["copy_ok"]
                    for row in initial_validation["group_results"]
                )
            if scenario["key"] == "direct_price":
                checks["price_intro_ends_with_actual_question"] = any(
                    row.payload.get("deferred_follow_up") and row.status == "simulated_delivered"
                    for row in initial_jobs
                )
                questions = [
                    row.payload["deferred_follow_up"].get("question", "")
                    for row in initial_jobs
                    if row.payload.get("deferred_follow_up") and row.status == "simulated_delivered"
                ]
                checks["silence_does_not_repeat_intro_question"] = not any(
                    question and question in text
                    for question in questions for text in silence_text
                )
        silence_validation = _silence_result(scenario, runs, messages)
        if scenario.get("silence_touches"):
            checks["silence_expected_outcome"] = silence_validation["passed"]
            checks["silence_does_not_repeat_greeting"] = not any(
                text.lstrip().startswith(("您好", "哈囉", "哈啰", "嗨"))
                for text in silence_text
            )
            checks["silence_is_not_generic_chasing"] = not any(
                term in text for text in silence_text
                for term in ("看了嗎", "看了吗", "滿意嗎", "满意吗", "怎麼沒回覆", "怎么没回复")
            )
            checks["silence_does_not_reuse_initial_mainline"] = not bool(
                set(silence_groups) & set(expected_initial_groups)
            )
        if scenario["key"] == "date_uncertain":
            checks["does_not_reask_date"] = not any(
                ("幾月" in text or "什麼時候出發" in text) for text in outgoing_text
            )
            checks["contact_has_value_reason"] = any(
                any(term in text for term in ("完整行程", "隨時聯絡", "LINE", "Email"))
                for text in outgoing_text
            )
        if scenario["key"] == "family_discussion":
            checks["family_shareable_followup"] = any(
                any(term in text for term in ("家人", "轉發", "比較"))
                for text in outgoing_text[1:]
            )
        if scenario["key"] == "seven_people":
            checks["seven_not_handoff"] = not bool(session.controls.get("human"))
        if scenario.get("expected_action") == "handoff":
            checks["handoff_state_applied"] = bool(session.controls.get("human"))
            checks["handoff_stops_sop"] = not db.scalar(select(RehearsalEnrollment.id).where(
                RehearsalEnrollment.session_id == session_id,
                RehearsalEnrollment.status == "active",
            ))
        if scenario["key"] in {"eight_people_handoff", "human_request", "contact_captured"}:
            checks["handoff_copy_waiting"] = any(
                ("稍等我一下" in text or "稍等一下" in text) and "顧問" in text
                for text in outgoing_text
            )
        return {
            "scenario": scenario["key"],
            "title": scenario["title"],
            "session_id": session_id,
            "first_decision": {
                key: first.get(key)
                for key in ("action", "route_variant", "handoff_reason", "journey_stage", "lead_action")
            },
            "fixed_answer": {
                "scope": first_trace.get("fixed_answer_scope"),
                "id": first_trace.get("fixed_answer_id"),
                "source": first_trace.get("response_source"),
            },
            "outgoing_text": outgoing_text,
            "asset_keys": all_assets,
            "media_count": len(media),
            "initial_delivery_groups": initial_groups,
            "initial_delivery_validation": initial_validation,
            "silence_runs": sum(row.module == "silence_touch" for row in runs),
            "failed_runs": [
                {"run_id": row.id, "module": row.module,
                 "error": (row.trace or {}).get("failure_code") or row.error_code,
                 "calls": (row.trace or {}).get("calls", [])}
                for row in runs if row.status == "failed"
            ],
            "silence_groups": silence_groups,
            "silence_validation": silence_validation,
            "checks": checks,
            "passed": all(checks.values()),
        }


def _markdown(report: dict) -> str:
    lines = [
        "# 顾问话术与图片介绍 AI 演练验收",
        "",
        f"- 大团有效阈值：`{report['effective_large_group_threshold']}`",
        f"- 会话：`{report['passed_sessions']}/{report['total_sessions']}` 通过",
        f"- 真实出站记录：`{report['outbound_before']} -> {report['outbound_after']}`",
        "",
        "| 会话 | 场景 | 结果 | 首次动作 | 线路 | 图片 | 沉默节点 |",
        "|---:|---|---|---|---|---:|---:|",
    ]
    for item in report["sessions"]:
        decision = item["first_decision"]
        lines.append(
            f"| #{item['session_id']} | {item['title']} | {'通过' if item['passed'] else '失败'} | "
            f"{decision.get('action') or '-'} | {decision.get('route_variant') or '未选'} | "
            f"{item['media_count']} | {item['silence_runs']} |"
        )
    lines.extend(["", "## Acceptance breakdown", "",
                  f"- Counts: `{json.dumps(_report_counts(report['sessions']), sort_keys=True)}`"])
    for item in report["sessions"]:
        silence = item["silence_validation"]
        lines.append(f"- {item['scenario']}: expectation={silence['expectation']}, "
                     f"touches={silence['observed_touches']}/{silence['required_touches']}, "
                     f"delivered_messages={silence['delivered_messages']}, counts={silence['counts']}")
        lines.append("  Failed checks: " + (", ".join(
            name for name, passed in item["checks"].items() if not passed
        ) or "none"))
    provenance = report.get("recheck_provenance", report)
    if "runtime_config_fingerprint_start" in provenance:
        lines.extend(["", "## Release provenance", "",
                      f"- Source: `{provenance.get('source_fingerprint', '')}`",
                      f"- Runtime config start: `{provenance['runtime_config_fingerprint_start']}`",
                      f"- Runtime config end: `{provenance['runtime_config_fingerprint_end']}`",
                      f"- Runtime config unchanged: `{provenance['runtime_config_unchanged']}`"])
    return "\n".join(lines) + "\n"


def main() -> int:
    from app.release_provenance import source_fingerprint, assert_source_unchanged, runtime_config_fingerprint
    tested_source = source_fingerprint()
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--drive-worker", action="store_true")
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--recheck-report", type=Path, help="Recheck existing sessions without creating messages or model calls.")
    parser.add_argument(
        "--scenario",
        action="append",
        choices=[item["key"] for item in SCENARIOS],
        help="Run only the selected scenario; repeat this option to select more than one.",
    )
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    if args.recheck_report:
        with SessionLocal() as db:
            config_start = runtime_config_fingerprint(db)
        prior = json.loads(args.recheck_report.read_text(encoding="utf-8"))
        by_key = {item["key"]: item for item in SCENARIOS}
        sessions = [_session_result(item["session_id"], by_key[item["scenario"]]) for item in prior["sessions"]]
        report = {**prior, "sessions": sessions, "passed_sessions": sum(item["passed"] for item in sessions),
                  "rechecked_from": str(args.recheck_report), "new_model_calls": 0}
        report.update(_report_counts(sessions))
        with SessionLocal() as db:
            config_end = runtime_config_fingerprint(db)
        assert_source_unchanged(tested_source)
        report["recheck_provenance"] = {
            "source_fingerprint": tested_source,
            "runtime_config_fingerprint_start": config_start,
            "runtime_config_fingerprint_end": config_end,
            "runtime_config_unchanged": config_start == config_end,
        }
        (args.output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        (args.output / "report.md").write_text(_markdown(report), encoding="utf-8")
        print(json.dumps({"sessions": len(sessions), "passed": report["passed_sessions"], "new_model_calls": 0}))
        return 0 if all(item["passed"] for item in sessions) else 1
    scenarios = [
        item for item in SCENARIOS
        if not args.scenario or item["key"] in set(args.scenario)
    ]

    with SessionLocal() as db:
        config_start = runtime_config_fingerprint(db)
        if global_message_sending_enabled(db):
            raise SystemExit("global_message_sending_must_be_disabled")
        owner = db.scalar(select(User).where(
            User.active.is_(True), User.role.in_(["super_admin", "admin"]),
        ).order_by(User.id))
        inbox = db.scalar(select(InboxBinding).where(
            InboxBinding.chatwoot_inbox_id == 128859,
        ).order_by(InboxBinding.id))
        if not owner or not inbox:
            raise SystemExit("playground_owner_or_inbox_missing")
        policy = effective_reception_policy(db)
        threshold = policy["handoff"]["large_group"]["minimum_party_size"]
        if threshold != 8:
            raise SystemExit(f"effective_large_group_threshold_must_be_8:{threshold}")
        before = _outbound_count(db)
        owner_id, inbox_id = owner.id, inbox.id

    created: list[tuple[int, dict]] = []
    execution_errors: dict[int, str] = {}
    for scenario in scenarios:
        session_id = _create_session(owner_id, inbox_id, scenario)
        created.append((session_id, scenario))
        try:
            _wait_ready(session_id, drive_worker=args.drive_worker, minimum_outgoing=1, timeout=args.timeout)
            for _ in range(int(scenario.get("silence_touches") or 0)):
                job_id = _advance_next_silence(session_id)
                if not job_id:
                    break
                _wait_silence_terminal(session_id, job_id, drive_worker=args.drive_worker, timeout=args.timeout)
        except (RuntimeError, TimeoutError) as exc:
            execution_errors[session_id] = str(exc)
        print(json.dumps({"scenario": scenario["key"], "session_id": session_id,
                          "error": execution_errors.get(session_id)}, ensure_ascii=False), flush=True)

    sessions = [_session_result(session_id, scenario) for session_id, scenario in created]
    for item in sessions:
        item["execution_error"] = execution_errors.get(item["session_id"])
        item["checks"]["execution_completed"] = item["execution_error"] is None
        item["passed"] = all(item["checks"].values())
    with SessionLocal() as db:
        after = _outbound_count(db)
        config_end = runtime_config_fingerprint(db)
    assert_source_unchanged(tested_source)
    report = {
        "source_fingerprint": tested_source,
        "runtime_config_fingerprint_start": config_start,
        "runtime_config_fingerprint_end": config_end,
        "runtime_config_unchanged": config_start == config_end,
        "effective_large_group_threshold": threshold,
        "total_sessions": len(sessions),
        "passed_sessions": sum(item["passed"] for item in sessions),
        "outbound_before": before,
        "outbound_after": after,
        "outbound_unchanged": before == after,
        "sessions": sessions,
        **_report_counts(sessions),
    }
    (args.output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (args.output / "report.md").write_text(_markdown(report), encoding="utf-8")
    print(json.dumps({
        "sessions": len(sessions),
        "passed": report["passed_sessions"],
        "outbound_unchanged": report["outbound_unchanged"],
        "session_ids": [item["session_id"] for item in sessions],
        "report": str(args.output / "report.md"),
    }, ensure_ascii=False))
    return 0 if report["passed_sessions"] == len(sessions) and before == after else 1


if __name__ == "__main__":
    raise SystemExit(main())
