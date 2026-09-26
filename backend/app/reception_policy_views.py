"""Read-only policy views for the split real-time reply pipeline."""
from __future__ import annotations

from copy import deepcopy
from app.advisor_voice import tone_preset_guidance


def reception_policy_views(effective_policy: dict) -> dict:
    """Separate model preferences, business decisions and runtime guards.

    The persisted operator configuration stays backward-compatible. New LLM
    nodes only receive the smallest view they need.
    """
    policy = effective_policy or {}
    operator = policy.get("operator_configuration") or {}
    reply_style = policy.get("reply_style") or {}
    return {
        "prompt_policy": {
            "business_goal": operator.get("business_goal", ""),
            "tone": reply_style.get("operator_tone", "friendly_professional"),
            "tone_description": tone_preset_guidance(reply_style.get("operator_tone", "friendly_professional")),
            "tone_guidance": operator.get("tone_guidance", ""),
            "custom_guidance": operator.get("custom_guidance", ""),
            "answer_before_advancing": reply_style.get("answer_before_advancing", True),
            "language": reply_style.get("language", "follow_customer"),
            "default_language": reply_style.get("default_language", "traditional_chinese"),
        },
        "decision_policy": {
            "opening_message": operator.get("opening_message", ""),
            "opening_messages": deepcopy(operator.get("opening_messages")),
            "opening_items": deepcopy(operator.get("opening_items")),
            "opening_interval_seconds": operator.get("opening_interval_seconds", 2),
            "route_switch": deepcopy(policy.get("route_switch") or {}),
            "handoff": deepcopy(policy.get("handoff") or {}),
            "lead_capture": deepcopy(operator.get("lead_capture") or {}),
            "business_rules": deepcopy(operator.get("business_rules") or []),
        },
        "runtime_policy": {
            "reply_limits": {
                "max_characters": int(reply_style.get("max_characters", 200)),
                "max_messages_per_turn": int(reply_style.get("max_messages_per_turn", 3)),
                "max_images_per_turn": int(reply_style.get("max_images_per_turn", 2)),
            },
            "silence": deepcopy(policy.get("silence_journey") or {}),
            "safety": deepcopy(policy.get("safety") or {}),
        },
    }


def views_for_context(context: dict) -> dict:
    return context.get("reception_policy_views") or reception_policy_views(
        context.get("reception_policy") or {}
    )
