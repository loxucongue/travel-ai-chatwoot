"""Deterministic business planning for real-time customer replies."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from app.advisor_voice import (
    contact_follow_up_question,
    requested_contact_question,
    route_choice_question,
    slot_follow_up_question,
)
from app.decision_knowledge import FACTS
from app.lead_capture import valid_contact_value
from app.reception_policy_views import views_for_context
from app.reply_understanding import CustomerUnderstanding
from app.route_packages import ROUTES
from app.service_knowledge import SERVICE_FIXED_ANSWERS


PLANNER_VERSION = "deterministic-reply-planner-v35"
CONTACT_CHANNEL_KEYS = {
    "LINE": "line",
    "微信": "wechat",
    "电话": "phone",
    "Email": "email",
    "WhatsApp": "whatsapp",
}


@dataclass(frozen=True)
class FollowUp:
    type: str
    field: str
    question: str


@dataclass(frozen=True)
class ReplyPlan:
    action: str
    intent: str
    route_variant: str
    branch: str
    next_stage: str
    reply_goal: str
    follow_up: FollowUp | None
    allowed_fact_ids: list[str]
    allowed_content_group_keys: list[str]
    allowed_asset_ids: list[str]
    reply_options: list[str]
    slots: dict
    slot_evidence: dict
    missing_slots: list[str]
    handoff_reason: str | None
    lead_action: str
    contact_values: dict
    route_evidence: str
    confidence: float
    safety_flags: list[str]
    stop_automation: bool = False
    fixed_answer_id: str = ""
    fixed_answer_text: str = ""
    fixed_answer_source_ref: str = ""
    fixed_answer_scope: str = ""
    opening_messages: list[str] = field(default_factory=list)
    opening_items: list[dict] = field(default_factory=list)
    opening_interval_seconds: int = 2

    def to_dict(self) -> dict:
        return asdict(self)


QUESTION_GROUP_NAMES = {
    "itinerary": "itinerary_overview",
    "highlights": "peach_highlights",
    "weather": "spring_weather",
    "hotel": "hotel_reference",
    "rongbuk": "rongbuk_reference",
    "vehicle": "vehicle_reference",
    "tips": "tips_reference",
    "destination_check": "route_scope",
    "altitude_health": "altitude_health",
}

SALES_HANDOFF_REASONS = {
    "large_group_custom_quote",
    "explicit_human_request",
    "lead_captured",
}
HANDOFF_VALUE_GROUPS = (
    "hotel_reference",
    "peach_highlights",
    "rongbuk_reference",
    "itinerary_overview",
)


def _visual_group_count(route: str, group_keys: set[str]) -> int:
    if not route:
        return 0
    groups = ROUTES[route]["groups"]
    return sum(1 for key in group_keys if groups.get(key, {}).get("assets"))


def _with_early_visual_group(route: str, selected: list[str], context: dict) -> list[str]:
    """Front-load one fresh visual topic on each of the first three route turns."""
    if not route:
        return selected
    spec = ROUTES[route]
    sent = set((context.get("journey") or {}).get("sent_content_groups") or [])
    if _visual_group_count(route, sent) >= 3:
        return selected
    if any(spec["groups"].get(key, {}).get("assets") and key not in sent for key in selected):
        return selected
    available = {
        str(item.get("key") or "")
        for item in context.get("available_materials") or []
        if route in (item.get("routes") or [])
    }
    candidate = next((
        key
        for key in spec.get("sequence", [])
        if key not in sent
        and key not in selected
        and set(spec["groups"].get(key, {}).get("assets") or []) & available
    ), None)
    return [*selected, candidate] if candidate else selected


def _contact_follow_up(
    lead_config: dict,
    *,
    departure_undecided: bool = False,
    customer_questions: set[str] | None = None,
    itinerary_delivered: bool = False,
) -> FollowUp:
    channels = [str(item) for item in (lead_config.get("channels") or ["LINE"])]
    channel = channels[0]
    names = " 或".join(channels[:2])
    field = CONTACT_CHANNEL_KEYS.get(channel, channel.lower())
    question = contact_follow_up_question(
        names,
        departure_undecided=departure_undecided,
        commercial=bool({"price", "availability", "booking"} & (customer_questions or set())),
        itinerary_delivered=itinerary_delivered,
    )
    return FollowUp("contact", field, question)


def _profile(context: dict, understanding: CustomerUnderstanding) -> dict:
    result: dict = {}
    journey = context.get("journey") or {}
    for source in (journey.get("customer_profile") or {}, context.get("memory") or {}):
        for key, raw in source.items():
            if key == "_profile_meta":
                continue
            result[key] = raw.get("value") if isinstance(raw, dict) and "value" in raw else raw
    result.update({key: update.value for key, update in understanding.slot_updates.items()})
    return result


def _party_size_bounds(value: object) -> tuple[int, int] | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        size = int(value)
        return (size, size) if size > 0 else None
    numbers = [int(item) for item in re.findall(r"\d+", str(value or ""))]
    if not numbers:
        return None
    return min(numbers), max(numbers)


def _party_size(value: object) -> int | None:
    bounds = _party_size_bounds(value)
    return bounds[1] if bounds else None


def _validated_contacts(understanding: CustomerUnderstanding) -> dict[str, str]:
    contacts: dict[str, str] = {}
    for candidate in understanding.contact_candidates:
        if valid_contact_value(candidate.channel, candidate.value):
            contacts.setdefault(candidate.channel, candidate.value)
    return contacts


def _enabled_routes(policy: dict) -> list[str]:
    configured = policy.get("route_switch", {}).get("allowed_routes") or list(ROUTES)
    return [route for route in configured if route in ROUTES]


def _looks_like_pasted_itinerary(value: object) -> bool:
    text = str(value or "")
    day_markers = re.findall(r"(?i)(?:\bday\s*\d{1,2}\b|第\s*\d{1,2}\s*天)", text)
    return len(day_markers) >= 3


def _resolve_route(context: dict, understanding: CustomerUnderstanding, policy: dict) -> tuple[str, list[str]]:
    enabled = _enabled_routes(policy)
    current = str(context.get("route_variant") or "")
    current = current if current in enabled else ""
    candidate = understanding.route_candidate if understanding.route_candidate in enabled else ""
    flags: list[str] = []
    if understanding.route_resolution in {"comparison", "outside_catalog"}:
        return "", flags
    if (
        not candidate
        and "itinerary" in understanding.customer_questions
        and _looks_like_pasted_itinerary(context.get("customer_text"))
    ):
        flags.append("unverified_pasted_itinerary_clears_current_route")
        return "", flags
    if not candidate:
        historical = understanding.historical_route_choice.get("route_variant")
        if not current and historical in enabled:
            return historical, ["route_restored_from_customer_history"]
        return current, flags
    if not current or candidate == current:
        return candidate, flags
    if not policy.get("route_switch", {}).get("enabled", True):
        flags.append("route_switch_disabled")
        return current, flags
    if understanding.route_resolution != "confirmed":
        flags.append("route_switch_requires_confirmation")
        return current, flags
    flags.append("route_switched")
    return candidate, flags


def _selected_groups(route: str, understanding: CustomerUnderstanding, context: dict) -> list[str]:
    if not route:
        return []
    spec = ROUTES[route]
    groups = spec["groups"]
    policies = spec["policies"]
    sent = set((context.get("journey") or {}).get("sent_content_groups") or [])
    if ("details_provided" in understanding.semantic_signals and understanding.slot_updates
            and set(understanding.customer_questions).issubset({"other", "party_size"})
            and _visual_group_count(route, sent) >= 3):
        return []
    selected: list[str] = []
    for question in understanding.customer_questions:
        key = QUESTION_GROUP_NAMES.get(question)
        if question == "party_size":
            size = _party_size(_profile(context, understanding).get("party_size"))
            band = "solo" if size == 1 else "small" if size and size <= 3 else "group"
            key = policies.get("party_intro_groups", {}).get(band)
        elif question == "price":
            key = policies.get("price_group")
        elif question == "documents":
            key = policies.get("price_group")
        elif question in {"departure", "availability"}:
            key = policies.get("departure_group")
        elif question == "booking":
            key = policies.get("price_group")
        elif question == "route_comparison":
            key = "itinerary_overview"
        if key in groups and key not in selected:
            selected.append(key)

    if understanding.intent == "contact" and set(understanding.customer_questions) <= {"contact", "other"}:
        return []
    if not selected and set(understanding.customer_questions) - {"other", "party_size"}:
        return []

    intent_key = policies.get("intent_groups", {}).get(understanding.intent)
    if not selected and intent_key in groups:
        selected.append(intent_key)
    if not selected and "party_size" in understanding.slot_updates:
        size = _party_size(understanding.slot_updates["party_size"].value)
        band = "solo" if size == 1 else "small" if size and size <= 3 else "group"
        key = policies.get("party_intro_groups", {}).get(band)
        if key in groups:
            selected.append(key)
    if not selected:
        sent = set((context.get("journey") or {}).get("sent_content_groups") or [])
        key = next(
            (item for item in spec.get("sequence", []) if item in groups and item not in sent),
            None,
        )
        if key in groups:
            selected.append(key)
    return selected


def _handoff_value_groups(
    route: str,
    selected: list[str],
    context: dict,
    handoff_reason: str | None,
) -> list[str]:
    """Choose one useful, unsent sales topic while a specialist takes over."""
    if not route or handoff_reason not in SALES_HANDOFF_REASONS:
        return []
    groups = ROUTES[route]["groups"]
    sent = set((context.get("journey") or {}).get("sent_content_groups") or [])
    candidates = [
        *[key for key in selected if key in HANDOFF_VALUE_GROUPS],
        *HANDOFF_VALUE_GROUPS,
    ]
    return next(
        ([key] for key in dict.fromkeys(candidates) if key in groups and key not in sent),
        [],
    )


def _allowed_facts(route: str, group_keys: list[str], *, all_routes: list[str]) -> list[str]:
    refs: list[str] = []
    if route:
        if not group_keys:
            return []
        for key in group_keys:
            refs.extend(ROUTES[route]["groups"].get(key, {}).get("evidence", []))
        if not refs:
            overview = next(
                (fact["id"] for fact in ROUTES[route]["knowledge_facts"] if fact["id"].endswith(".overview")),
                "",
            )
            if overview:
                refs.append(overview)
    else:
        for route_id in all_routes:
            overview = next(
                (fact["id"] for fact in ROUTES[route_id]["knowledge_facts"] if fact["id"].endswith(".overview")),
                "",
            )
            if overview:
                refs.append(overview)
    known = {fact["id"] for fact in FACTS}
    return list(dict.fromkeys(ref for ref in refs if ref in known))


def _allowed_assets(context: dict, route: str, group_keys: list[str]) -> list[str]:
    if not route:
        return []
    sent = set((context.get("journey") or {}).get("sent_content_groups") or [])
    sent_assets = set((context.get("journey") or {}).get("sent_asset_keys") or [])
    allowed_by_group = {
        asset
        for group_key in group_keys
        if group_key not in sent
        for asset in ROUTES[route]["groups"].get(group_key, {}).get("assets", [])
    }
    result = []
    for material in context.get("available_materials") or []:
        key = str(material.get("key") or "")
        routes = material.get("routes") or []
        if key in allowed_by_group and key not in sent_assets and route in routes and key not in result:
            result.append(key)
    return result


_MATCHING_VARIANT_TRANSLATION = str.maketrans({
    "個": "个", "參": "参", "與": "与", "還": "还", "發": "发", "圖": "图",
    "價": "价", "團": "团", "費": "费", "飯": "饭", "間": "间", "會": "会",
    "條": "条", "線": "线", "覽": "览", "麼": "么", "說": "说", "聯": "联",
    "絡": "络", "裡": "里", "這": "这", "張": "张", "車": "车", "開": "开",
    "點": "点", "後": "后", "實": "实", "單": "单", "時": "时", "問": "问",
    "衛": "卫", "獨": "独", "備": "备", "氣": "气", "體": "体", "醫": "医",
    "戶": "户", "長": "长", "歲": "岁", "證": "证", "號": "号", "華": "华",
    "隨": "随", "師": "师", "們": "们", "嗎": "吗", "應": "应", "處": "处",
    "療": "疗", "覺": "觉", "頭": "头", "噁": "恶", "暈": "晕", "唇": "唇",
    "導": "导", "遊": "游", "員": "员", "診": "诊", "藥": "药", "現": "现",
    "狀": "状", "辦": "办",
})


def _normalized_example(value: object) -> str:
    normalized = str(value or "").translate(_MATCHING_VARIANT_TRANSLATION)
    return re.sub(r"[^\u3400-\u9fffA-Za-z0-9]", "", normalized).lower()


def _fixed_answer(
    route: str,
    understanding: CustomerUnderstanding,
    context: dict,
    *,
    blocked: bool,
) -> dict | None:
    """Resolve a reviewed route answer without giving the model wording control."""
    if not route or blocked:
        return None
    request_topics = {
        str(item) for item in understanding.customer_questions
        if str(item) not in {"other", "contact"}
    }
    if not request_topics and understanding.intent in {"route_intro", "itinerary"}:
        request_topics.add(str(understanding.intent))
    customer_text = _normalized_example(context.get("customer_text"))
    if not customer_text:
        return None
    sent = set((context.get("journey") or {}).get("sent_content_groups") or [])
    available = {
        str(item.get("key") or "")
        for item in context.get("available_materials") or []
        if route in (item.get("routes") or [])
    }
    party_bounds = _party_size_bounds(_profile(context, understanding).get("party_size"))
    candidates: list[tuple[int, dict]] = []
    for answer in ROUTES[route].get("fixed_answers", []):
        if answer.get("status") != "active":
            continue
        answer_topics = {str(item) for item in answer.get("topics") or []}
        party_min = answer.get("party_size_min")
        party_max = answer.get("party_size_max")
        if party_min is not None or party_max is not None:
            if not party_bounds:
                continue
            if party_min is not None and party_bounds[0] < int(party_min):
                continue
            if party_max is not None and party_bounds[1] > int(party_max):
                continue
        if any(
            example and example in customer_text
            for example in map(_normalized_example, answer.get("negative_examples") or [])
        ):
            continue
        configured_assets = [str(item) for item in answer.get("asset_ids") or []]
        if configured_assets and (
            answer.get("content_group_key") in sent
            or any(asset not in available for asset in configured_assets)
        ):
            continue
        positive_match = any(
            example and (example in customer_text or customer_text in example)
            for example in map(_normalized_example, answer.get("positive_examples") or [])
        )
        if request_topics and not request_topics.issubset(answer_topics):
            continue
        # Fixed answers are exact-output assets, so topic confidence alone is not
        # enough to choose between multiple route answers with the same topic.
        if not positive_match:
            continue
        score = 10000 + int(answer.get("priority") or 0)
        candidates.append((score, answer))
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


def _service_fixed_answer(
    understanding: CustomerUnderstanding,
    context: dict,
    *,
    blocked: bool,
) -> dict | None:
    """Resolve reviewed cross-route service copy from exact customer examples."""
    if blocked:
        return None
    request_topics = {
        str(item) for item in understanding.customer_questions
        if str(item) not in {"other", "contact"}
    }
    customer_text = _normalized_example(context.get("customer_text"))
    if not customer_text:
        return None
    signals = set(understanding.semantic_signals)
    candidates: list[tuple[int, dict]] = []
    for answer in SERVICE_FIXED_ANSWERS:
        if answer.get("status") != "active":
            continue
        if signals.intersection(str(item) for item in answer.get("block_signals") or []):
            continue
        if any(
            example and example in customer_text
            for example in map(_normalized_example, answer.get("negative_examples") or [])
        ):
            continue
        answer_topics = {str(item) for item in answer.get("topics") or []}
        if request_topics and not request_topics.issubset(answer_topics):
            continue
        if not any(
            example and (example in customer_text or customer_text in example)
            for example in map(_normalized_example, answer.get("positive_examples") or [])
        ):
            continue
        candidates.append((10000 + int(answer.get("priority") or 0), answer))
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


def _rule_action(understanding: CustomerUnderstanding, policy: dict) -> tuple[str | None, str | None]:
    matched = {
        rule_id
        for rule_id in understanding.matched_rule_ids
        if understanding.matched_rule_evidence.get(rule_id)
    }
    for rule in policy.get("business_rules", []):
        if not rule.get("enabled") or rule.get("system_key"):
            continue
        trigger = rule.get("trigger") or "semantic"
        if trigger == "outside_catalog":
            if understanding.route_resolution != "outside_catalog":
                continue
        elif trigger != "semantic" or rule.get("id") not in matched:
            continue
        action = str(rule.get("action") or "")
        if action in {"handoff", "stop_ai", "request_contact", "recommend_routes"}:
            return action, str(rule.get("id") or "")
    return None, None


def build_reply_plan(context: dict, understanding: CustomerUnderstanding) -> ReplyPlan:
    """Turn validated semantics into the only executable business decision."""
    policy = views_for_context(context)["decision_policy"]
    enabled_routes = _enabled_routes(policy)
    route, route_flags = _resolve_route(context, understanding, policy)
    flags = [*understanding.validation_flags, *route_flags]
    if understanding.route_resolution == "outside_catalog":
        flags.extend(["outside_catalog_request", "skip_silence_enrollment"])
    profile = _profile(context, understanding)
    slots = {key: update.value for key, update in understanding.slot_updates.items()}
    slot_evidence = {key: update.evidence_quote for key, update in understanding.slot_updates.items()}
    contacts = _validated_contacts(understanding)
    signals = set(understanding.semantic_signals)
    topics = set(understanding.customer_questions)
    if topics - {"other", "itinerary", "route_comparison"}:
        flags.append("direct_customer_question")
    has_advisor_history = any(
        item.get("direction") == "outgoing" and not item.get("private")
        for item in context.get("context_messages") or []
    )
    unresolved_direct_question = "unresolved_direct_question" in signals
    custom_action, custom_rule_id = _rule_action(understanding, policy)

    action = "reply"
    handoff_reason: str | None = None
    lead_action = "none"
    stop_automation = False
    if "explicit_stop" in signals or "pause_proactive" in signals or custom_action == "stop_ai":
        action = "no_action"
        stop_automation = True
        flags.append("customer_requested_stop")
        if "pause_proactive" in signals and "explicit_stop" not in signals and custom_action != "stop_ai":
            flags.append("customer_paused_proactive")
    elif contacts:
        action = "handoff"
        handoff_reason = "lead_captured"
        lead_action = "captured"
    elif "explicit_human_request" in signals:
        action, handoff_reason = "handoff", "explicit_human_request"
    elif "refund" in signals:
        action, handoff_reason = "handoff", "refund"
    elif "contract_dispute" in signals:
        action, handoff_reason = "handoff", "contract_dispute"
    elif "complaint" in signals:
        action, handoff_reason = "handoff", "complaint"
    elif "attachment_requires_vision" in signals:
        action, handoff_reason = "handoff", "attachment_requires_vision"
    elif custom_action == "handoff":
        action, handoff_reason = "handoff", f"business_rule:{custom_rule_id}"
    else:
        handoff = policy.get("handoff", {}).get("large_group", {})
        bounds = _party_size_bounds(profile.get("party_size"))
        threshold = int(handoff.get("minimum_party_size", 8))
        if handoff.get("enabled") and bounds is not None and bounds[1] >= threshold:
            action, handoff_reason = "handoff", str(handoff.get("reason") or "large_group_custom_quote")
            if bounds[0] != bounds[1]:
                flags.append("party_size_range_reaches_handoff_threshold")

    if custom_action == "recommend_routes" and action == "reply":
        route = ""
    initial_unselected_entry = bool(
        action == "reply" and not route and not has_advisor_history
        and not (context.get("journey") or {}).get("sent_content_groups")
        and understanding.route_resolution not in {"comparison", "outside_catalog"}
        and topics.issubset({"other", "itinerary"})
        and signals.issubset({"general_inquiry"})
        and (understanding.intent in {"route_intro", "itinerary"} or "general_inquiry" in signals)
    )
    group_keys = _selected_groups(route, understanding, context)
    initial_route_entry = bool(
        action == "reply"
        and route
        and not has_advisor_history
        and not (context.get("journey") or {}).get("sent_content_groups")
        and "advisor_greeting" in ROUTES[route]["groups"]
        and set(understanding.customer_questions).issubset({"other", "itinerary"})
        and ("general_inquiry" in signals or understanding.intent == "route_intro"
             or understanding.route_resolution == "confirmed")
    )
    route_selected_after_opening = bool(
        action == "reply"
        and route
        and has_advisor_history
        and not (context.get("journey") or {}).get("sent_content_groups")
        and "brand_positioning" in ROUTES[route]["groups"]
        and understanding.route_resolution == "confirmed"
        and set(understanding.customer_questions).issubset({"other", "itinerary"})
    )
    if initial_route_entry:
        group_keys = ["advisor_greeting"]
    elif route_selected_after_opening:
        group_keys = ["brand_positioning"]
    if unresolved_direct_question and action == "reply":
        flags.extend(["unresolved_question_requires_clarification", "skip_silence_enrollment"])
        group_keys = []
    if action == "handoff":
        group_keys = _handoff_value_groups(route, group_keys, context, handoff_reason)
    availability_question = "availability" in understanding.customer_questions
    health_suitability = "personal_health_suitability" in signals
    current_conditions = "current_conditions" in signals
    customization_request = "customization_request" in signals
    medical_guarantee = "medical_guarantee_request" in signals
    identity_disclosure = "identity_disclosure_question" in signals
    if availability_question:
        flags.append("availability_confirmation_required")
    if health_suitability:
        flags.extend(["health_confirmation_required", "skip_silence_enrollment"])
    if current_conditions:
        flags.append("current_conditions_confirmation_required")
        if topics.issubset({"other"}):
            group_keys = []
    if customization_request:
        flags.append("customization_planning_required")
        if topics.issubset({"other"}):
            group_keys = []
    if medical_guarantee:
        flags.append("medical_guarantee_prohibited")
    if identity_disclosure and action == "reply":
        flags.extend(["identity_disclosure_required", "skip_silence_enrollment"])
        group_keys = []
    if health_suitability:
        requested_health_groups = {
            QUESTION_GROUP_NAMES[question]
            for question in understanding.customer_questions
            if question in QUESTION_GROUP_NAMES
        }
        group_keys = [key for key in group_keys if key in requested_health_groups]
    if (
        action == "reply"
        and route
        and not contacts
        and not {
            "current_conditions",
            "customization_request",
            "medical_guarantee_request",
            "explicit_stop",
            "unresolved_direct_question",
        } & signals
        and not availability_question
        and not health_suitability
        and not initial_route_entry
        and not route_selected_after_opening
        and understanding.intent not in {"price", "booking", "availability", "medical_service", "altitude_health"}
        and (
            topics.issubset({"other", "party_size"})
            or "general_inquiry" in signals
        )
    ):
        group_keys = _with_early_visual_group(route, group_keys, context)
    fixed_answer = _service_fixed_answer(
        understanding,
        context,
        blocked=bool(
            action != "reply"
            or identity_disclosure
            or availability_question
            or current_conditions
            or customization_request
            or medical_guarantee
        ),
    )
    fixed_answer_scope = "service" if fixed_answer else ""
    if not fixed_answer:
        fixed_answer = _fixed_answer(
            route,
            understanding,
            context,
            blocked=bool(
                action != "reply"
                or identity_disclosure
                or availability_question
                or health_suitability
                or current_conditions
                or customization_request
                or medical_guarantee
            or understanding.route_resolution in {"comparison", "outside_catalog"}
            or initial_route_entry
            ),
        )
        fixed_answer_scope = "route" if fixed_answer else ""
    if fixed_answer:
        fixed_group = str(fixed_answer.get("content_group_key") or "")
        group_keys = [fixed_group] if fixed_group else []
        flags = [item for item in flags if item != "unresolved_question_requires_clarification"]
        unresolved_direct_question = False
    required_slots = ROUTES.get(route, {}).get("required_slots", [])
    missing_slots = [slot for slot in required_slots if profile.get(slot) in (None, "", [], {})]
    sent_groups = set((context.get("journey") or {}).get("sent_content_groups") or [])
    answered_groups = sent_groups | set(group_keys)
    visual_rounds = _visual_group_count(route, answered_groups)

    lead_config = policy.get("lead_capture", {})
    capture_status = (context.get("lead_capture") or {}).get("status", "not_started")
    preferred_profile_ready = bool(
        (not lead_config.get("require_party_size", False) or profile.get("party_size"))
        and (not lead_config.get("require_departure_window", False) or profile.get("departure_window"))
    )
    flexible_contact_reason = "departure_undecided" in signals or visual_rounds >= 3
    mature_for_contact = bool(
        action == "reply"
        and lead_config.get("enabled", True)
        and capture_status == "not_started"
        and route
        and (not lead_config.get("require_supported_route", False) or route)
        and len(answered_groups) >= int(lead_config.get("ask_after_answered_topics", 2))
        and (preferred_profile_ready or flexible_contact_reason)
        and (
            bool({"price", "availability", "booking", "departure"} & set(understanding.customer_questions))
            or flexible_contact_reason
        )
    )
    if custom_action == "request_contact" and action == "reply":
        mature_for_contact = True

    reply_options: list[str] = []
    follow_up: FollowUp | None = None
    outside_action = policy.get("route_switch", {}).get("outside_catalog_action", "recommend_supported_routes")
    if fixed_answer:
        follow_up = None
    elif action == "reply" and unresolved_direct_question:
        follow_up = FollowUp("clarification", "topic", "您主要想問的是哪一段安排呢？")
    elif action == "reply" and identity_disclosure:
        follow_up = None
    elif action == "reply" and (availability_question or health_suitability or medical_guarantee
                               or topics & {"altitude_health", "medical_service", "medication", "tips", "destination_check"}):
        # Answer the current high-risk question directly. Do not turn an
        # availability check into an unrelated slot or lead-capture question.
        follow_up = None
    elif action == "reply" and "asks_contact_channel" in signals and not contacts and lead_config.get("enabled", True):
        channels = lead_config.get("channels") or ["LINE"]
        requested = understanding.requested_contact_channel
        channel = next(
            (str(item) for item in channels if CONTACT_CHANNEL_KEYS.get(str(item)) == requested),
            str(channels[0]),
        )
        follow_up = FollowUp(
            "contact",
            CONTACT_CHANNEL_KEYS.get(channel, channel.lower()),
            requested_contact_question(channel),
        )
        lead_action = "ask"
    elif action == "reply" and not route:
        outside_exclusive = "outside_catalog_exclusive" in signals
        if understanding.route_resolution != "outside_catalog" or (
            str(outside_action).startswith("recommend") and not outside_exclusive
        ):
            reply_options = [ROUTES[item]["selection_title"] for item in enabled_routes]
        if reply_options:
            follow_up = FollowUp("route_choice", "route_variant", route_choice_question())
    elif action == "reply" and ({"considering", "low_intent"} & signals):
        # A customer who is comparing with family or deliberately slowing the
        # conversation has already told us what the next step is. Give them
        # useful, shareable content without forcing another requirement field.
        follow_up = None
    elif action == "reply" and mature_for_contact:
        follow_up = _contact_follow_up(
            lead_config,
            departure_undecided="departure_undecided" in signals,
            customer_questions=set(understanding.customer_questions),
            itinerary_delivered="itinerary_overview" in ((context.get("journey") or {}).get("completed_content_groups") or []),
        )
        lead_action = "ask"
    elif action == "reply" and "departure_undecided" in signals:
        # "Not decided yet" is a valid answer. Continue the route mainline
        # instead of replacing it with another requirement question.
        follow_up = None
    elif action == "reply" and missing_slots:
        slot = missing_slots[0]
        question = slot_follow_up_question(slot)
        question_group = ROUTES.get(route, {}).get("policies", {}).get(
            "party_question_group" if slot == "party_size" else "departure_question_group"
        )
        if question_group not in sent_groups:
            follow_up = FollowUp("slot", slot, question)

    # A missing profile field is not permission for a later executor to invent
    # a question. These turns intentionally end without a follow-up because the
    # customer has asked a sensitive/direct question or has already slowed the
    # conversation down.
    if action == "reply" and follow_up is None and (
        fixed_answer
        or identity_disclosure
        or availability_question
        or health_suitability
        or medical_guarantee
        or "departure_undecided" in signals
        or bool({"considering", "low_intent"} & signals)
        or bool(topics & {"altitude_health", "medical_service", "medication", "tips", "destination_check"})
    ):
        flags.append("deferred_follow_up_prohibited")

    if action == "handoff":
        next_stage = "captured" if lead_action == "captured" else "handoff"
    elif identity_disclosure:
        next_stage = str((context.get("journey") or {}).get("stage") or ("value_building" if route else "route_selection"))
    elif lead_action == "ask":
        next_stage = "contact_requested"
    elif not route:
        next_stage = "route_selection"
    elif unresolved_direct_question:
        next_stage = str((context.get("journey") or {}).get("stage") or "value_building")
    elif "considering" in signals or "low_intent" in signals:
        next_stage = "considering"
    elif "has_objection" in signals:
        next_stage = "objection_handling"
    elif capture_status == "asked":
        next_stage = "contact_requested"
    elif missing_slots:
        next_stage = "needs_discovery"
    elif mature_for_contact:
        next_stage = "contact_ready"
    else:
        next_stage = "value_building"

    if fixed_answer:
        reply_goal = "逐字傳送已審核固定回答，不增加、刪減或改寫任何客戶可見文字"
    elif unresolved_direct_question:
        reply_goal = "只說明需要先確認客戶問題的具體範圍，不介紹行程主線、住宿、車輛、價格或其他無關內容"
    elif action == "handoff":
        reply_goal = {
            "lead_captured": "確認已收到聯絡方式並正在安排旅遊顧問；再提供 allowed_facts 中一項尚未介紹的住宿或行程亮點，不追加問題",
            "large_group_custom_quote": "先說明正在安排專人旅遊顧問繼續規劃團體行程與費用；再提供 allowed_facts 中一項尚未介紹的住宿或行程亮點，讓客戶等待時有內容可看；不得承諾價格、餘位或處理時間，不追加問題",
            "explicit_human_request": "確認正在安排真人顧問繼續接待；再提供 allowed_facts 中一項尚未介紹的住宿或行程亮點，不追加問題",
            "refund": "說明取消或退款需要由專人依訂單與條款處理",
            "contract_dispute": "說明合約問題需要由專人核對處理",
            "complaint": "確認收到客戶反映的情況，並轉交專人處理",
            "attachment_requires_vision": "說明需要由專人查看附件後再繼續回覆",
        }.get(handoff_reason or "", "說明將由真人顧問繼續處理目前需求")
    elif action == "no_action":
        reply_goal = "不傳送訊息，並停止後續自動接待"
    elif identity_disclosure:
        reply_goal = "如實說明這是 China2Go 的自動接待助手而不是真人顧問；說明可以協助了解行程，也可以依客戶意願安排真人顧問；不追加銷售問題"
    elif health_suitability:
        reply_goal = (
            "先準確回答客戶本輪詢問的住宿、車輛或行程安排，再說明個人是否適合高原旅行屬於健康判斷，"
            "不能依據年齡、單次高山反應經驗或行程設施下結論；建議客戶先請醫師依個人健康狀況評估，不追加業務問題"
        )
    elif customization_request:
        reply_goal = (
            "承接客戶明確表達的自由行、客製或住宿預算偏好；說明特殊走法、住宿調整與報價需要另外規劃，"
            "不得聲稱現有行程不能調整，也不得替客戶決定安排"
        )
    elif medical_guarantee:
        reply_goal = (
            "先準確回答客戶詢問的住宿或車輛供氧設施，再明確說明設施不能保證不會出現高山反應，也不能保證個人安全；"
            "不提供用藥、飲水或醫療處置建議"
        )
    elif current_conditions:
        reply_goal = (
            "直接說明近期天氣、道路或景點開放等即時狀況，需要依客戶出發日期核對；"
            "如果 planned_follow_up 是聯絡方式，只由該欄位承擔唯一追問"
        )
    elif availability_question:
        reply_goal = (
            "直接回答客戶詢問的即時名額、目前報名人數或是否成團。"
            "可以說明 allowed_facts 中已公布的出發安排，但必須明確說明即時團況需要由顧問依具體日期核對；"
            "不要承諾一定成團，不要重複介紹行程總覽，也不要追加其他問題"
        )
    elif lead_action == "ask" and "asks_contact_channel" in signals and not group_keys:
        reply_goal = "只回答客戶可以使用所詢問的聯絡方式；不要在正文要求客戶提供任何資訊"
    elif not route and topics & {"medical_service", "medication", "altitude_health"}:
        reply_goal = "直接回答目前的服務或高原準備問題，說明已審核的實際協助與界線；不要求先選行程，不作個人醫療判斷，也不追加銷售追問"
    elif not route:
        if understanding.route_resolution == "outside_catalog" and not reply_options:
            reply_goal = "說明目前沒有該行程的已公布資料，不推測，也不強行推薦其他行程"
        else:
            reply_goal = "用自然的旅遊顧問口吻，簡短說明可選行程的核心差別並協助客戶選擇；不得使用已上線、目前可接待等後台用語"
    else:
        purposes = [ROUTES[route]["groups"][key].get("purpose", key) for key in group_keys]
        if purposes:
            reply_goal = "只回答客戶本輪問題；下一步由 planned_follow_up 單獨承擔。重點：" + "；".join(purposes)
        else:
            reply_goal = "直接回答客戶目前的問題，不重複介紹無關行程內容"

    facts = _allowed_facts(route, group_keys, all_routes=enabled_routes)
    if route and "transport" in topics:
        # Transfers belong to the published itinerary, not general website
        # transport advice. Supplying evidence does not schedule its images.
        facts = list(dict.fromkeys([*facts, *_allowed_facts(
            route, ["itinerary_overview"], all_routes=enabled_routes,
        )]))
    if not route and "direct_customer_question" in flags:
        requested_groups = {QUESTION_GROUP_NAMES[topic] for topic in topics if topic in QUESTION_GROUP_NAMES}
        facts = list(dict.fromkeys(
            fact_id
            for route_id in enabled_routes
            for fact_id in _allowed_facts(
                route_id,
                [key for key in requested_groups if key in ROUTES[route_id]["groups"]],
                all_routes=enabled_routes,
            )
        ))
    if topics & {"altitude_health", "medical_service"}:
        facts = [*facts, "service.safety"]
        if "medical_service" in topics:
            facts.append("service.medical_support")
            facts.extend(["service.medical_preparedness", "service.medical_logistics"])
    if "medication" in topics:
        facts.append("service.medication")
        facts.append("service.website_medication_precautions")
    if fixed_answer:
        known_facts = {fact["id"] for fact in FACTS}
        facts = [
            str(item) for item in fixed_answer.get("fact_ids") or []
            if str(item) in known_facts
        ]
    if (
        action == "reply"
        and not route
        and reply_options
        and {"price", "route_comparison"} & set(understanding.customer_questions)
    ):
        price_refs = {
            fact["id"]
            for route_id in enabled_routes
            for fact in ROUTES[route_id]["knowledge_facts"]
            if str(fact["id"]).endswith(".price")
        }
        facts = list(dict.fromkeys([*facts, *sorted(price_refs)]))
    if action == "reply" and availability_question:
        facts = list(dict.fromkeys([*facts, "service.availability"]))
    if action == "reply" and health_suitability:
        facts = list(dict.fromkeys([*facts, "service.safety"]))
    if action == "reply" and current_conditions:
        facts = list(dict.fromkeys([*facts, "service.current_conditions"]))
    if action == "reply" and customization_request:
        facts = list(dict.fromkeys([*facts, "service.customization"]))
    if action == "reply" and medical_guarantee:
        facts = list(dict.fromkeys([*facts, "service.safety"]))
    if action == "reply" and identity_disclosure:
        facts = []
    web_fact_ids = [
        str(item.get("id") or "")
        for item in context.get("global_knowledge_facts") or []
        if isinstance(item, dict)
        and str(item.get("id") or "").startswith("web.")
        and str(item.get("text") or "").strip()
    ]
    if (action == "reply" and web_fact_ids and not fixed_answer and not identity_disclosure
            and not initial_unselected_entry and not initial_route_entry
            and not health_suitability and not medical_guarantee
            and not topics & {"medication", "altitude_health", "tips", "destination_check"}
            and "details_provided" not in signals):
        facts = list(dict.fromkeys([*facts, *web_fact_ids]))
        # Retrieval supplies evidence; it cannot overrule the planned business action.
        if context.get("question_details") and unresolved_direct_question:
            flags = [item for item in flags if item != "unresolved_question_requires_clarification"]
            unresolved_direct_question = False
            if follow_up and follow_up.type == "clarification":
                follow_up = None
            reply_goal = "逐項回答 question_details；只說有依據的內容，缺少哪項安排就明確說需核對哪項，不改談其他主題。"
        if context.get("question_details") and topics <= {"company", "other", "contact", "transport", "payment", "documents", "eligibility"}:
            reply_options = []
            if follow_up and follow_up.type == "route_choice":
                follow_up = None
    if not route and not reply_options and not fixed_answer:
        facts = [item for item in facts if item.startswith(("web.", "service."))]
    if (action == "reply" and not fixed_answer and len(understanding.question_details) > 1
            and follow_up and follow_up.type in {"slot", "contact"}):
        follow_up = None
    if (action == "reply" and not route and not fixed_answer
            and follow_up and follow_up.type == "route_choice"
            and "direct_customer_question" in flags):
        reply_goal = "先回答客戶本輪具體問題；未確認行程時不可把其中一條的安排當成已選安排。必要時說清不同適用條件，再承接唯一追問，不以行程選擇介紹代替答案。"
    if action == "handoff" and handoff_reason not in SALES_HANDOFF_REASONS:
        facts = []
    assets = _allowed_assets(context, route, group_keys) if (
        action == "reply" or handoff_reason in SALES_HANDOFF_REASONS
    ) else []
    if fixed_answer:
        fixed_assets = set(str(item) for item in fixed_answer.get("asset_ids") or [])
        assets = [item for item in assets if item in fixed_assets]
    if not route:
        # Route-specific visuals are unavailable until the customer selects a route.
        assets = []
    opening_messages = policy.get("opening_messages") or ([policy["opening_message"]] if policy.get("opening_message") else [])
    from app.opening_messages import delivery_items
    opening_items = delivery_items(policy.get("opening_items"), opening_messages)
    if policy.get("opening_items"):
        opening_messages = [item["content"] for item in opening_items if item["content"]]
    if initial_unselected_entry and opening_messages:
        fixed_answer = {"id": "unselected_opening", "answer_text": "\n\n".join(opening_messages)}
        fixed_answer_scope = "reception"
        facts, group_keys, assets = [], [], []
        follow_up = None
        reply_options = [ROUTES[item]["selection_title"] for item in enabled_routes]
        reply_goal = "逐字傳送已發布開場白，僅附行程選項，不增加介紹"
    # Profile-only turns must not invoke old sales scripts promising a replay.
    # Mixed questions and newly selected routes keep their ordinary handling.
    if (action == "reply" and route and slots and "details_provided" in signals
            and topics.issubset({"other", "party_size"})
            and not understanding.question_details
            and not initial_route_entry and not route_selected_after_opening
            and understanding.route_resolution not in {"confirmed", "comparison", "outside_catalog"}):
        fixed_answer = None
        fixed_answer_scope = ""
        facts, group_keys, assets, reply_options = [], [], [], []
        follow_up, lead_action = None, "none"
        reply_goal = "只簡短確認本輪已記錄或更新的人數／日期；不重述品牌、報價、住宿、用車，不承諾再次發送資料，不追加問題。"
        flags.append("profile_update_only")
        next_stage = str((context.get("journey") or {}).get("stage") or "needs_discovery")
    return ReplyPlan(
        action=action,
        intent=understanding.intent,
        route_variant=route,
        branch=ROUTES[route]["branch"] if route else "unclassified",
        next_stage=next_stage,
        reply_goal=reply_goal,
        follow_up=follow_up,
        allowed_fact_ids=facts,
        allowed_content_group_keys=group_keys,
        allowed_asset_ids=assets,
        reply_options=reply_options,
        slots=slots,
        slot_evidence=slot_evidence,
        missing_slots=missing_slots,
        handoff_reason=handoff_reason,
        lead_action=lead_action,
        contact_values=contacts if lead_action == "captured" else {},
        route_evidence=understanding.route_evidence,
        confidence=understanding.confidence,
        safety_flags=sorted(set(flags)),
        stop_automation=stop_automation,
        fixed_answer_id=str(fixed_answer.get("id") or "") if fixed_answer else "",
        fixed_answer_text=str(fixed_answer.get("answer_text") or "") if fixed_answer else "",
        fixed_answer_source_ref=str(fixed_answer.get("source_ref") or "") if fixed_answer else "",
        fixed_answer_scope=fixed_answer_scope,
        opening_messages=list(opening_messages) if fixed_answer_scope == "reception" else [],
        opening_items=opening_items if fixed_answer_scope == "reception" and policy.get("opening_items") else [],
        opening_interval_seconds=int(policy.get("opening_interval_seconds", 2)),
    )
