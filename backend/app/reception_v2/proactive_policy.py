"""Server-owned eligibility rules for V2 proactive follow-up."""
from __future__ import annotations

from dataclasses import dataclass
from copy import deepcopy
from datetime import datetime, timezone
import math


def silence_schedule_templates(nodes: list[dict]) -> list[dict]:
    """V2 schedules decisions, not V1's preselected image deliveries."""
    result = []
    for source in nodes:
        if not source.get("journey_trigger"):
            continue
        node = deepcopy(source)
        node["messages"] = []
        node.pop("content_group_candidates", None)
        node.pop("content_group_key", None)
        node.pop("initial_delivery", None)
        result.append(node)
    return result


@dataclass(frozen=True)
class ProactiveEligibility:
    eligible: bool
    reason: str
    candidate_value_ids: tuple[str, ...] = ()
    defer_minutes: int = 0


def evaluate_proactive_eligibility(context: dict, memory: dict) -> ProactiveEligibility:
    """Decide whether a silence turn may reach the model.

    The model can choose wording and deferment after this gate. It cannot turn
    an ineligible conversation into a proactive outbound message.
    """
    if context.get("module") not in {"silence_touch", "wakeup"}:
        return ProactiveEligibility(True, "reactive_turn")
    journey = context.get("journey") or {}
    lead = context.get("lead_capture") or {}
    state = (journey.get('slots') or context.get('memory') or {}).get('_v2_state') or {}
    if state.get('proactive_opt_out'):
        return ProactiveEligibility(False, 'customer_opted_out')
    if journey.get("stage") in {"handoff", "captured"}:
        return ProactiveEligibility(False, "human_followup_active")
    due = state.get('contact_at') or state.get('reevaluate_at')
    if due:
        now = context.get('now') or context.get('virtual_now') or datetime.now(timezone.utc).isoformat()
        remaining = (datetime.fromisoformat(due.replace('Z', '+00:00')) - datetime.fromisoformat(now.replace('Z', '+00:00'))).total_seconds()
        if remaining > 0:
            return ProactiveEligibility(False, 'customer_requested_time', defer_minutes=min(720, max(1, math.ceil(remaining / 60))))
    if journey.get("stage") == "considering" and not due:
        return ProactiveEligibility(False, "customer_requested_time")
    if lead.get("status") == "captured":
        return ProactiveEligibility(False, "lead_already_captured")
    if journey.get("automatic_delivery_paused"):
        return ProactiveEligibility(False, "automatic_delivery_paused")
    if not memory.get("route_variant"):
        return ProactiveEligibility(False, "route_not_bound")
    candidates = tuple(memory.get("followup_candidates") or ())
    if not candidates:
        return ProactiveEligibility(False, "no_new_value")
    return ProactiveEligibility(True, "new_value_available", candidates)
