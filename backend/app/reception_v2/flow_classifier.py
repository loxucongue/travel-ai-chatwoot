"""Cheap preloading hints; semantic turn selection belongs to the V2 agent.

These hints must never restrict reactive skill access or lock a customer into
the previous sales stage. The actual reception_flow is returned by the agent.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from app.reception_v2.journey_state_machine import flow_for_stage
from app.reception_v2.skill_registry import SkillRegistry
from app.route_packages import ROUTES


FLOW_ROUTE_SELECTION = "route_selection"
FLOW_ROUTE_DETAIL = "route_detail"
FLOW_CONCERN_RESOLUTION = "concern_resolution"
FLOW_LEAD_HANDOFF = "lead_handoff"
FLOW_SILENCE_FOLLOWUP = "silence_followup"


@dataclass(frozen=True)
class FlowSelection:
    name: str
    reason: str
    allowed_skills: tuple[str, ...]


_CONTACT_TERMS = ("真人", "顧問", "顾问", "聯繫我", "联系我", "加我", "微信", "LINE", "email", "電話", "电话")
_CONCERN_TERMS = ("擔心", "担心", "會不會", "会不会", "高反", "氧氣", "氧气", "累不累", "適合", "适合", "包含", "不含")
_COMPARE_TERMS = ("比較", "比较", "差別", "差别", "哪個好", "哪个好", "怎麼選", "怎么选", "9日和11日", "9天和11天")
_DETAIL_TERMS = ("價格", "价格", "多少", "費用", "费用", "出發", "出发", "日期", "住宿", "飯店", "酒店", "行程", "包含", "不含", "氧氣", "氧气", "供氧")


def infer_route_variant(text: str) -> str:
    value = str(text or "").casefold()
    # Dates are not route durations; both durations mean comparison/ambiguity.
    if re.search(r"\d\s*月\s*\d{1,2}\s*日", value):
        return ""
    registry = SkillRegistry()
    matches = [route_id for route_id in ROUTES
               if (skill := registry.route_metadata(route_id)) and any(
                   token.casefold() in value for token in skill.route_aliases)]
    return matches[0] if len(matches) == 1 and not any(token in value for token in _COMPARE_TERMS) else ''


def select_flow(context: dict) -> FlowSelection:
    module = str(context.get("module") or "reply")
    text = str(context.get("customer_text") or "").strip().casefold()
    journey = context.get("journey") or {}
    route = str(context.get("route_variant") or journey.get("route_variant") or "")
    hinted_route = infer_route_variant(text) if not route else ""
    lead = context.get("lead_capture") or {}
    stage = str(journey.get("stage") or "route_selection")
    memory = context.get("journey_memory") or {}
    unresolved = memory.get("unresolved_topics") or journey.get("unresolved_topics") or []

    if module in {"silence_touch", "wakeup"}:
        return FlowSelection(FLOW_SILENCE_FOLLOWUP, "scheduled_silence_event", ("silence-followup",))
    if module == "lead_capture" or lead.get("status") == "asked" or stage in {"contact_requested", "handoff"}:
        return FlowSelection(FLOW_LEAD_HANDOFF, "durable_lead_state", ("lead-handoff",))
    if any(term.casefold() in text for term in _CONTACT_TERMS):
        return FlowSelection(FLOW_LEAD_HANDOFF, "customer_requests_human_or_channel", ("lead-handoff",))
    if not route and hinted_route:
        return FlowSelection(FLOW_ROUTE_DETAIL, "explicit_route_in_customer_message", (SkillRegistry().route_skill(hinted_route),))
    if not route and any(term.casefold() in text for term in _COMPARE_TERMS):
        return FlowSelection(FLOW_ROUTE_SELECTION, "route_comparison", ("route-selection",))
    explicit_detail = any(term.casefold() in text for term in _DETAIL_TERMS)
    if route and ((stage == "objection_handling" and not explicit_detail)
                  or (unresolved and len(text) <= 24 and not explicit_detail)):
        return FlowSelection(FLOW_CONCERN_RESOLUTION, "journey_objection_or_unresolved_topic", ("concern-resolution",))
    if route and any(term.casefold() in text for term in _CONCERN_TERMS):
        return FlowSelection(FLOW_CONCERN_RESOLUTION, "customer_concern_fallback", ("concern-resolution",))
    if route:
        return FlowSelection("route_detail" if stage in {"route_selection", "needs_discovery"} else flow_for_stage(stage), "durable_journey_stage", ("route-skill",))
    return FlowSelection(flow_for_stage(stage), "durable_journey_stage", ("route-selection",))
