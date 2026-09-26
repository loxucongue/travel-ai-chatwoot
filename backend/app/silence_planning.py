"""Deterministic planning for customer-silence journey touches."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass

from app.advisor_voice import contact_follow_up_question, slot_follow_up_question
from app.decision_knowledge import FACTS
from app.reception_policy_views import views_for_context
from app.reply_planning import FollowUp, ReplyPlan
from app.route_packages import ROUTES
from app.route_reply import automatic_content_already_covered


SILENCE_PLANNER_VERSION = "deterministic-silence-planner-v13"


@dataclass(frozen=True)
class SilencePlan:
    reply_plan: ReplyPlan
    touch_goal: str
    touch_reason: str
    skip_reason: str = ""

    def to_dict(self) -> dict:
        return {
            "reply_plan": self.reply_plan.to_dict(),
            "touch_goal": self.touch_goal,
            "touch_reason": self.touch_reason,
            "skip_reason": self.skip_reason,
        }


def _profile(context: dict) -> dict:
    profile: dict = {}
    journey = context.get("journey") or {}
    for source in (journey.get("customer_profile") or {}, context.get("memory") or {}):
        for key, raw in source.items():
            if key == "_profile_meta":
                continue
            profile[key] = raw.get("value") if isinstance(raw, dict) and "value" in raw else raw
    return profile


def _enabled_routes(context: dict) -> list[str]:
    policy = views_for_context(context)["decision_policy"]
    configured = policy.get("route_switch", {}).get("allowed_routes") or list(ROUTES)
    return [route for route in configured if route in ROUTES]


def _fact_ids(route: str, groups: list[str], *, include_safety: bool = False) -> list[str]:
    refs: list[str] = []
    if route:
        for group in groups:
            refs.extend(ROUTES[route]["groups"].get(group, {}).get("evidence", []))
    if include_safety:
        refs.append("service.safety")
    known = {fact["id"] for fact in FACTS}
    return list(dict.fromkeys(ref for ref in refs if ref in known))


def _overview_facts(routes: list[str]) -> list[str]:
    refs: list[str] = []
    for route in routes:
        ref = next(
            (fact["id"] for fact in ROUTES[route]["knowledge_facts"] if fact["id"].endswith(".overview")),
            "",
        )
        if ref:
            refs.append(ref)
    return refs


def _assets(context: dict, route: str, groups: list[str]) -> list[str]:
    if not route:
        return []
    material_by_key = {
        str(material.get("key") or ""): material
        for material in context.get("available_materials") or []
    }
    result: list[str] = []
    sent_assets = set((context.get("journey") or {}).get("sent_asset_keys") or [])
    for group in groups:
        for key in ROUTES[route]["groups"].get(group, {}).get("assets", []):
            material = material_by_key.get(key) or {}
            routes = material.get("routes") or material.get("route_variants") or []
            route_id = str(material.get("route_variant") or "")
            if (route in routes or route_id == route) and key not in sent_assets and key not in result:
                result.append(key)
    return result


def _missing_slots(route: str, profile: dict) -> list[str]:
    return [
        slot for slot in ROUTES.get(route, {}).get("required_slots", [])
        if profile.get(slot) in (None, "", [], {})
    ]


def _has_unanswered_slot_question(route: str, sent: set[str], profile: dict) -> bool:
    policies = ROUTES.get(route, {}).get("policies", {})
    question_groups = {
        "party_size": policies.get("party_question_group"),
        "departure_window": policies.get("departure_question_group"),
    }
    return any(
        group in sent and profile.get(slot) in (None, "", [], {})
        for slot, group in question_groups.items()
        if group
    )


def _party_size(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    numbers = [int(item) for item in re.findall(r"\d+", str(value or ""))]
    return max(numbers) if numbers else None


def _follow_up_for_slot(slot: str) -> FollowUp:
    return FollowUp("slot", slot, slot_follow_up_question(slot))


def _contact_follow_up(context: dict) -> FollowUp:
    lead = views_for_context(context)["decision_policy"].get("lead_capture", {})
    channels = [str(item) for item in (lead.get("channels") or ["LINE"])]
    channel = channels[0]
    names = " 或".join(channels[:2])
    field = {"LINE": "line", "微信": "wechat", "电话": "phone", "Email": "email"}.get(
        channel, channel.lower()
    )
    profile = (context.get("journey") or {}).get("customer_profile") or {}
    departure = profile.get("departure_window")
    if isinstance(departure, dict):
        departure = departure.get("value")
    if departure is None:
        departure = (context.get("memory") or {}).get("departure_window")
        if isinstance(departure, dict):
            departure = departure.get("value")
    departure_undecided = str(departure or "").strip() in {
        "未确定", "未確定", "不确定", "不確定", "还没定", "還沒定",
    }
    reminder = str((context.get("lead_capture") or {}).get("status") or "") == "asked"
    return FollowUp(
        "contact",
        field,
        contact_follow_up_question(
            names,
            departure_undecided=departure_undecided,
            reminder=reminder,
            itinerary_delivered="itinerary_overview" in ((context.get("journey") or {}).get("completed_content_groups") or []),
        ),
    )


def _empty_plan(
    *,
    route: str,
    stage: str,
    action: str = "no_action",
    handoff_reason: str | None = None,
    stop_automation: bool = False,
    flags: list[str] | None = None,
) -> ReplyPlan:
    return ReplyPlan(
        action=action,
        intent="other",
        route_variant=route,
        branch=ROUTES[route]["branch"] if route else "unclassified",
        next_stage=stage,
        reply_goal="不傳送訊息" if action == "no_action" else "說明將由真人顧問繼續處理",
        follow_up=None,
        allowed_fact_ids=[],
        allowed_content_group_keys=[],
        allowed_asset_ids=[],
        reply_options=[],
        slots={},
        slot_evidence={},
        missing_slots=[],
        handoff_reason=handoff_reason,
        lead_action="none",
        contact_values={},
        route_evidence="",
        confidence=1.0,
        safety_flags=flags or [],
        stop_automation=stop_automation,
    )


def build_silence_plan(context: dict) -> SilencePlan:
    """Choose whether and what to send without asking a model for system state."""
    views = views_for_context(context)
    decision_policy = views["decision_policy"]
    runtime_policy = views["runtime_policy"]
    enabled_routes = _enabled_routes(context)
    journey = context.get("journey") or {}
    route = str(context.get("route_variant") or journey.get("route_variant") or "")
    route = route if route in enabled_routes else ""
    stage = str(journey.get("stage") or "route_selection")
    profile = _profile(context)
    completed = set(journey.get("completed_content_groups") or []) if "completed_content_groups" in journey else set(
        journey.get("sent_content_groups") or []
    )
    touch_index = max(1, int(context.get("touch_index") or 1))
    silence = runtime_policy.get("silence") or {}
    total_touches = max(1, 1 + len(silence.get("wakeup_after_minutes") or []))
    capture_status = str((context.get("lead_capture") or {}).get("status") or "not_started")

    if stage in {"captured", "handoff"} or capture_status == "captured":
        reason = "silence_stopped_by_terminal_stage"
        return SilencePlan(
            _empty_plan(route=route, stage=stage, stop_automation=True, flags=[reason]),
            "",
            "客户旅程已经进入人工或留资完成状态",
            reason,
        )

    handoff = decision_policy.get("handoff", {}).get("large_group", {})
    size = _party_size(profile.get("party_size"))
    if handoff.get("enabled") and size is not None and size >= int(handoff.get("minimum_party_size", 8)):
        reason = str(handoff.get("reason") or "large_group_custom_quote")
        return SilencePlan(
            _empty_plan(route=route, stage="handoff", action="handoff", handoff_reason=reason),
            "",
            "已验证同行人数达到大团转人工阈值",
        )

    if not route:
        if touch_index > 1:
            reason = "silence_unresolved_route_no_new_value"
            return SilencePlan(
                _empty_plan(route="", stage="route_selection", flags=[reason]),
                "",
                "线路仍未确认，且已完成一次有效比较，不再重复催问",
                reason,
            )
        plan = ReplyPlan(
            action="reply",
            intent="route_intro",
            route_variant="",
            branch="unclassified",
            next_stage="contact_requested",
            reply_goal="不重複上一則自我介紹或按鈕文字；只補充兩條行程最重要的差異，並以低壓力方式提供完整資料的傳送方式",
            follow_up=_contact_follow_up(context),
            allowed_fact_ids=_overview_facts(enabled_routes),
            allowed_content_group_keys=[],
            allowed_asset_ids=[],
            reply_options=[],
            slots={},
            slot_evidence={},
            missing_slots=[],
            handoff_reason=None,
            lead_action="ask",
            contact_values={},
            route_evidence="",
            confidence=1.0,
            safety_flags=[],
        )
        return SilencePlan(plan, "request_contact", "行程尚未確認，補充一次有效差異並提供完整資料的傳送方式")

    spec = ROUTES[route]
    progress = journey.get("content_progress") or {}
    # Topic coverage is a receipt-derived view, never a model's cited groups.
    topics = {
        group for group in journey.get("topic_covered_groups") or []
        if group in spec["groups"]
        and (progress.get(group) or {}).get("schema_version") == 2
        and (progress.get(group) or {}).get("topic_covered") is True
        and not (progress.get(group) or {}).get("history_unknown", True)
    }
    observed = completed | topics
    # Optional dynamic touches need not reproduce approved copy verbatim.
    # Mandatory initial groups still require their full text/asset completion.
    covered = completed | {group for group in topics if not spec["groups"][group].get("initial_delivery")}
    # Once delivered, offering the same itinerary again is not new value.
    if "itinerary_overview" in completed:
        covered.update({"read_check", "contact_transition", "contact_request"})
    missing = _missing_slots(route, profile)
    pending_slot_question = _has_unanswered_slot_question(route, observed, profile)
    remaining = [
        group for group in spec.get("sequence", [])
        if not (group in completed if spec["groups"].get(group, {}).get("initial_delivery")
                else automatic_content_already_covered(group, covered))
    ]
    departure_undecided = str(profile.get("departure_window") or "").strip() in {
        "未确定", "未確定", "不确定", "不確定", "不知道", "还没定", "還沒定",
    }
    groups: list[str] = []
    follow_up: FollowUp | None = None
    lead_action = "none"
    include_safety = False
    safety_flags: list[str] = []

    if stage == "objection_handling":
        groups = []
        touch_goal = "handle_objection"
        touch_reason = "針對客戶已記錄的顧慮，提供可驗證的處理邊界"
        include_safety = True
        if profile.get("permit_awareness") not in (None, "", [], {}):
            safety_flags.append("requirements_confirmation_required")
    elif stage == "considering":
        groups = remaining[:1]
        touch_goal = "soft_nurture"
        touch_reason = "客戶正在比較或與家人討論，提供一項方便轉傳的新資訊"
    elif stage == "contact_requested":
        groups = remaining[:1]
        if touch_index >= total_touches:
            if not groups:
                groups = [
                    group for group in ("contact_transition", "read_check")
                    if group in spec["groups"] and not automatic_content_already_covered(group, covered)
                ][:1]
            touch_goal = "contact_reminder"
            touch_reason = "已詢問聯絡方式，在最後一個節點低壓力保留聯絡入口"
            follow_up = _contact_follow_up(context)
        elif groups:
            touch_goal = "build_value"
            touch_reason = "不重複索取聯絡方式，先補充一項尚未涵蓋的行程價值"
        else:
            reason = "silence_contact_already_requested_no_new_value"
            return SilencePlan(
                _empty_plan(route=route, stage=stage, flags=[reason]),
                "",
                "联系方式已经询问且当前没有新的相关内容",
                reason,
            )
    else:
        groups = remaining[:1]
        if departure_undecided:
            touch_goal = "build_value"
            touch_reason = "客戶已說明日期未定，本輪繼續提供主線價值，不重複追問需求"
        elif missing and not pending_slot_question:
            touch_goal = "collect_need"
            touch_reason = "重要需求仍缺少，先提供一項新價值，再只詢問一個欄位"
            follow_up = _follow_up_for_slot(missing[0])
        elif missing:
            touch_goal = "build_value"
            touch_reason = "重要需求已詢問但客戶尚未回答，本輪只補充新價值，不重複或疊加問題"
        else:
            touch_goal = "build_value"
            purpose = spec["groups"].get(groups[0], {}).get("purpose", "行程價值") if groups else ""
            touch_reason = f"推進尚未涵蓋的內容：{purpose}" if purpose else "推進尚未涵蓋的行程價值"

    if not groups and follow_up is None and not include_safety and touch_index >= total_touches:
        groups = [
            group for group in ("contact_transition", "read_check")
            if group in spec["groups"] and not automatic_content_already_covered(group, covered)
        ][:1]
        if groups:
            touch_goal = "soft_nurture"
            touch_reason = "主線素材已介紹完成，最後用一則純文字低壓力收尾"

    lead = decision_policy.get("lead_capture", {})
    profile_ready = bool(
        (not lead.get("require_party_size", False) or profile.get("party_size"))
        and (not lead.get("require_departure_window", False) or profile.get("departure_window"))
    )
    enough_value = len(observed | set(groups)) >= int(lead.get("ask_after_answered_topics", 2))
    if (
        (stage == "contact_ready" or departure_undecided)
        and capture_status == "not_started"
        and lead.get("enabled", True)
        and profile_ready
        and enough_value
    ):
        touch_goal = "request_contact"
        touch_reason = "行程與重要需求已明確，完成本輪價值後詢問一種聯絡方式"
        follow_up = _contact_follow_up(context)
        lead_action = "ask"

    if not groups and not include_safety:
        reason = "silence_no_relevant_content"
        return SilencePlan(
            _empty_plan(route=route, stage=stage, flags=[reason]),
            "",
            "当前没有尚未覆盖且与客户阶段相关的内容",
            reason,
        )

    facts = _fact_ids(route, groups, include_safety=include_safety)
    if groups and not facts:
        reason = "silence_content_without_facts"
        return SilencePlan(
            _empty_plan(route=route, stage=stage, flags=[reason]),
            "",
            "候选内容没有可验证事实，已安全跳过",
            reason,
        )
    if lead_action == "ask":
        next_stage = "contact_requested"
    elif stage in {"objection_handling", "considering", "contact_requested"}:
        next_stage = stage
    elif missing:
        next_stage = "needs_discovery"
    else:
        next_stage = "value_building"

    purpose = "；".join(spec["groups"][group].get("purpose", group) for group in groups)
    content_progress = (context.get("journey") or {}).get("content_progress") or {}
    partial_text_groups = [
        group for group in groups
        if (content_progress.get(group) or {}).get("text_delivered")
    ]
    reply_goal = {
        "route_choice": "補充行程差異並協助客戶選擇",
        "collect_need": f"先補充一項新價值，再完成唯一需求追問。重點：{purpose}",
        "build_value": f"自然承接前後文，只補充一項尚未涵蓋的新價值。重點：{purpose}",
        "handle_objection": "只針對客戶已表達的顧慮，說明需要由顧問確認的邊界；不說很簡單、不用擔心，不要求客戶重複提供已知資訊，也不提 AI 或系統內部規則",
        "request_contact": f"先補充本輪新價值，再以低壓力方式詢問一種聯絡方式。重點：{purpose}",
        "contact_reminder": f"低壓力保留顧問聯絡入口，不製造緊迫感。可補充：{purpose}" if purpose else "低壓力保留顧問聯絡入口，不製造緊迫感",
        "soft_nurture": f"寫成方便客戶比較或轉傳給家人的短訊息，不催促決定。重點：{purpose}",
    }[touch_goal]
    if partial_text_groups:
        reply_goal = (
            "這是同一內容組的未送達素材續接。只簡短說明本輪新傳的圖片是什麼、"
            "畫面可看什麼；不要重述先前已送出的文字介紹，也不要重新總結整個內容組。"
        )
        safety_flags.append("resume_partial_content_group")
    plan = ReplyPlan(
        action="reply",
        intent="other",
        route_variant=route,
        branch=spec["branch"],
        next_stage=next_stage,
        reply_goal=reply_goal,
        follow_up=follow_up,
        allowed_fact_ids=facts,
        allowed_content_group_keys=groups,
        allowed_asset_ids=_assets(context, route, groups),
        reply_options=[],
        slots={},
        slot_evidence={},
        missing_slots=missing,
        handoff_reason=None,
        lead_action=lead_action,
        contact_values={},
        route_evidence="",
        confidence=1.0,
        safety_flags=safety_flags,
    )
    return SilencePlan(plan, touch_goal, touch_reason)
