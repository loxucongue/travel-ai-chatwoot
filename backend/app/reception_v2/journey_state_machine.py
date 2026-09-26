"""Single source of truth for V2 journey stages and allowed Flow selection."""
from __future__ import annotations

STAGES = {
    "route_selection", "needs_discovery", "value_building", "objection_handling",
    "contact_ready", "contact_requested", "considering", "captured", "handoff",
}

FLOW_BY_STAGE = {
    "route_selection": "route_selection",
    "needs_discovery": "route_selection",
    "value_building": "route_detail",
    "objection_handling": "concern_resolution",
    "contact_ready": "lead_handoff",
    "contact_requested": "lead_handoff",
    "captured": "lead_handoff",
    "handoff": "lead_handoff",
    "considering": "route_detail",
}

# Conversation progress is not a sales funnel: a customer may revisit selection,
# ask another product question, or request a human at any active stage.
ALLOWED_NEXT = {
    stage: ({"captured", "handoff"} if stage in {"captured", "handoff"} else set(STAGES))
    for stage in STAGES
}


def normalized_stage(value: str | None) -> str:
    value = str(value or "route_selection")
    return value if value in STAGES else "route_selection"


def allowed_next_stage(current: str | None, proposed: str | None) -> bool:
    current = normalized_stage(current)
    proposed = normalized_stage(proposed)
    return proposed in ALLOWED_NEXT[current]


def flow_for_stage(stage: str | None) -> str:
    return FLOW_BY_STAGE[normalized_stage(stage)]


def guard_decision_stage(decision, current_stage: str | None) -> str | None:
    """Keep model output inside the state graph; return a flag when corrected."""
    proposed = normalized_stage(getattr(decision, "journey_stage", None))
    current = normalized_stage(current_stage)
    events = {event.get('type') for event in getattr(decision, 'v2_events', [])}
    if (current == 'considering' and proposed == 'considering' and
            events & {'question', 'material_requested', 'route_selected', 'profile_updated'} and
            not events & {'considering', 'contact_agreed'}):
        decision.journey_stage = proposed = 'value_building'
    if current not in {'captured', 'handoff'} and getattr(decision, 'action', '') == 'reply':
        if 'route_selected' in events and proposed == 'route_selection':
            decision.journey_stage = proposed = 'value_building'
        if proposed == 'contact_requested' and getattr(decision, 'lead_action', '') != 'ask':
            decision.journey_stage = proposed = 'value_building'
    has_evidence = (
        (proposed != 'considering' or proposed == current or bool(events & {'considering', 'contact_agreed'}))
        and (proposed != 'captured' or proposed == current or
             (getattr(decision, 'lead_action', '') == 'captured' and bool(getattr(decision, 'contact_values', {}))))
        and (proposed != 'handoff' or proposed == current or getattr(decision, 'action', '') == 'handoff')
    )
    if allowed_next_stage(current, proposed) and has_evidence:
        return None
    decision.journey_stage = current
    decision.safety_flags = sorted(set([*(decision.safety_flags or []), "journey_stage_transition_rejected"]))
    return "journey_stage_transition_rejected"
