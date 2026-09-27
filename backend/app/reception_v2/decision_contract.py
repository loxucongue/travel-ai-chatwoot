"""Canonical, server-derived explanation of a V2 turn decision."""
from __future__ import annotations


def build_decision_contract(context: dict, decision, *, flow: str, flow_reason: str,
                            proactive: dict | None = None) -> dict:
    memory_updates = []
    for key, value in (getattr(decision, "slots", {}) or {}).items():
        memory_updates.append({
            "field": str(key),
            "value": value,
            "evidence": (getattr(decision, "slot_evidence", {}) or {}).get(key, ""),
        })
    contract = {
        'customer_events': list(getattr(decision, 'v2_events', []) or []),
        'delivery_sections': list(getattr(decision, 'v2_delivery_sections', []) or []),
        "flow": flow,
        "flow_reason": flow_reason,
        "action": decision.action,
        "route_variant": decision.route_variant or "",
        "intent": decision.intent,
        "evidence_refs": list(decision.evidence_refs or []),
        "material_keys": list(decision.material_keys or []),
        "presentations": list(getattr(decision, "presentations", []) or []),
        "memory_updates": memory_updates,
        "journey_stage": decision.journey_stage,
        "handoff": {
            "required": decision.action == "handoff",
            "reason": decision.handoff_reason,
        },
        "silence_plan": {
            "action": decision.wakeup_action,
            "defer_minutes": decision.defer_minutes,
            "candidate_value_ids": list((proactive or {}).get("candidate_value_ids") or []),
        },
        "safety_flags": list(decision.safety_flags or []),
    }
    if decision.action == "no_action":
        contract["material_keys"] = []
    if decision.action == "handoff":
        contract["silence_plan"]["action"] = "skip"
    return contract
