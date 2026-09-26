"""Read-only DeepSeek verification for the unified route journey policy.

The source intentionally stores Chinese scenarios as unicode escapes so Windows
terminal encodings cannot corrupt the test inputs.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from sqlalchemy import func, select

from app.db import SessionLocal
from app.decision_service import VALIDATOR_VERSION, generate_decision
from app.models import HandoffTask, OutboundMessage
from app.realtime_reply_pipeline import REALTIME_REPLY_PROMPT_VERSION
from app.reception_config import effective_reception_policy
from app.route_packages import JOURNEY_POLICY, ROUTES
from app.route_reply import (
    journey_context_from_values,
    playbook_prompt,
)


SCENARIOS = [
    {
        "key": "new_9d_price_hotel_2p",
        "customer_text": (
            "\u6211\u60f3\u4e86\u89e3\u6843\u82b19\u65e5\u884c\u7a0b\uff0c"
            "\u6211\u4eec2\u4f4d\uff0c\u9884\u8ba1\u660e\u5e743\u6708\u5e95"
            "\u51fa\u53d1\uff0c\u4f4f\u5bbf\u548c\u4ef7\u683c\u600e\u6837\uff1f"
        ),
        "context_messages": [],
        "route_variant": "",
        "memory": {},
        "expect": {
            "action": "reply",
            "route_variant": "peach_9d_2027",
            "lead_action": "ask",
            "slots": {"party_size": 2, "departure_window": "\u660e\u5e743\u6708\u5e95"},
            "covered_any": ["price_reference", "hotel_reference", "contact_request"],
        },
    },
    {
        "key": "large_group_handoff",
        "customer_text": (
            "\u6211\u4eec\u516c\u53f8\u9884\u8ba112\u4eba\uff0c"
            "\u60f3\u53c2\u52a0\u6843\u82b19\u65e5\u884c\u7a0b\u3002"
        ),
        "context_messages": [],
        "route_variant": "",
        "memory": {},
        "expect": {
            "action": "handoff",
            "route_variant": "peach_9d_2027",
            "handoff_reason": "large_group_custom_quote",
        },
    },
    {
        "key": "seven_people_stays_with_ai",
        "customer_text": "\u6211\u4eec7\u4f4d\uff0c\u60f3\u4e86\u89e3\u6843\u82b19\u65e5\u884c\u7a0b\u3002",
        "context_messages": [],
        "route_variant": "",
        "memory": {},
        "expect": {"action": "reply", "route_variant": "peach_9d_2027"},
    },
    {
        "key": "eight_people_uses_configured_handoff_threshold",
        "customer_text": "\u6211\u4eec8\u4f4d\uff0c\u60f3\u4e86\u89e3\u6843\u82b19\u65e5\u884c\u7a0b\u3002",
        "context_messages": [],
        "route_variant": "",
        "memory": {},
        "expect": {
            "action": "handoff",
            "route_variant": "peach_9d_2027",
            "handoff_reason": "large_group_custom_quote",
        },
    },
    {
        "key": "party_range_reaching_threshold_handoffs",
        "customer_text": "\u6211\u4eec\u5927\u69826\u523010\u4f4d\uff0c\u60f3\u4e86\u89e3\u6843\u82b19\u65e5\u3002",
        "context_messages": [],
        "route_variant": "",
        "memory": {},
        "expect": {
            "action": "handoff",
            "route_variant": "peach_9d_2027",
            "handoff_reason": "large_group_custom_quote",
            "safety_contains": "party_size_range_reaches_handoff_threshold",
        },
    },
    {
        "key": "direct_identity_question_is_answered_truthfully",
        "customer_text": "\u4f60\u662f\u673a\u5668\u4eba\u5417\uff1f",
        "context_messages": [],
        "route_variant": "",
        "memory": {},
        "expect": {
            "action": "reply",
            "route_variant": "",
            "reply_contains": "\u81ea\u52d5\u63a5\u5f85",
            "safety_contains": "identity_disclosure_required",
        },
    },
    {
        "key": "large_group_11d_price_hotel_handoff",
        "customer_text": (
            "\u6211\u60f3\u4e86\u89e3\u6843\u82b1\u52a0\u73e0\u5cf011\u65e5\uff0c"
            "\u6211\u4eec12\u4f4d\uff0c\u9884\u8ba1\u660e\u5e743\u6708\u5e95"
            "\u51fa\u53d1\uff0c\u4f4f\u5bbf\u548c\u4ef7\u683c\u600e\u6837\uff1f"
        ),
        "context_messages": [
            {"direction": "incoming", "content": "\u4e4b\u524d\u770b\u8fc7\u6843\u82b19\u65e5"},
            {
                "direction": "outgoing",
                "content": (
                    "9\u65e5\u7ebf\u8def\u4e0d\u542b\u73e0\u5cf0\uff0c"
                    "\u5982\u679c\u8981\u73e0\u5cf0\u53ef\u4ee5\u770b11\u65e5\u3002"
                ),
            },
        ],
        "route_variant": "peach_9d_2027",
        "memory": {},
        "expect": {
            "action": "handoff",
            "route_variant": "peach_11d_2027",
            "handoff_reason": "large_group_custom_quote",
        },
    },
    {
        "key": "explicit_switch_to_11d",
        "customer_text": (
            "\u90a3\u6211\u786e\u8ba4\u6539\u770b"
            "\u6709\u73e0\u5cf0\u768411\u65e5\u7ebf\u8def\u3002"
        ),
        "context_messages": [
            {"direction": "incoming", "content": "\u5148\u770b\u770b\u6843\u82b19\u65e5\u3002"},
            {"direction": "outgoing", "content": "9\u65e5\u7ebf\u8def\u4e0d\u542b\u73e0\u5cf0\u3002"},
        ],
        "route_variant": "peach_9d_2027",
        "memory": {"party_size": {"value": "2\u4f4d", "evidence": "2\u4f4d"}},
        "expect": {"action": "reply", "route_variant": "peach_11d_2027"},
    },
    {
        "key": "ambiguous_route_requires_confirmation",
        "customer_text": (
            "9\u65e5\u548c11\u65e5\u6211\u90fd\u60f3\u4e86\u89e3\uff0c"
            "\u5148\u6bd4\u8f83\u4e00\u4e0b\u3002"
        ),
        "context_messages": [],
        "route_variant": "",
        "memory": {},
        "expect": {"action": "reply", "route_variant": "", "reply_options_count": 2},
    },
    {
        "key": "outside_catalog_stays_unbound",
        "customer_text": "\u6211\u73b0\u5728\u5176\u5b9e\u60f3\u770b\u4e91\u535712\u65e5\u7ebf\u8def\u3002",
        "context_messages": [
            {"direction": "incoming", "content": "\u4e4b\u524d\u770b\u8fc7\u6843\u82b19\u65e5\u3002"},
        ],
        "route_variant": "peach_9d_2027",
        "memory": {},
        "expect": {"action": "reply", "route_variant": "", "lead_action": "none"},
    },
    {
        "key": "contact_channel_without_id_requests_actual_id",
        "customer_text": (
            "2\u6708\u5e953\u6708\u521d\uff0c\u8bf7\u95ee\u6700\u8fd1\u897f\u85cf"
            "\u7684\u60c5\u51b5\u4f1a\u5f71\u54cd\u666f\u70b9\u5417\uff1f"
            "\u53ef\u4ee5\u7528\u5fae\u4fe1\u8054\u7cfb\u5417\uff1f"
        ),
        "context_messages": [],
        "route_variant": "peach_11d_2027",
        "memory": {},
        "expect": {"action": "reply", "route_variant": "peach_11d_2027", "lead_action": "ask"},
    },
]


def _counts() -> dict:
    with SessionLocal() as db:
        return {
            "outbound": db.scalar(select(func.count()).select_from(OutboundMessage)),
            "handoff": db.scalar(select(func.count()).select_from(HandoffTask)),
        }


def _next_sop_preview(route_variant: str, sent_groups: list[str]) -> dict:
    if route_variant not in ROUTES:
        return {"enabled": False, "reason": "route_unbound"}
    route = ROUTES[route_variant]
    sent = set(sent_groups or [])
    for group_key in route["sequence"]:
        if group_key in sent:
            continue
        group = route["groups"][group_key]
        return {
            "enabled": True,
            "after_minutes": int(JOURNEY_POLICY["silence_journey"]["mainline_after_minutes"]),
            "next_content_group": group_key,
            "purpose": group.get("purpose"),
            "preview_text": group.get("text"),
            "asset_keys": group.get("assets", []),
        }
    return {"enabled": False, "reason": "route_sequence_completed"}


def _matches(value: dict, expected: dict) -> list[str]:
    failures = []
    for key, target in expected.items():
        if key == "reply_options_count":
            actual = len(value.get("reply_options") or [])
        elif key == "reply_contains":
            if target not in str(value.get("reply") or ""):
                failures.append(f"reply missing: {target!r}")
            continue
        elif key == "safety_contains":
            if target not in set(value.get("safety_flags") or []):
                failures.append(f"safety_flags missing: {target!r}")
            continue
        elif key == "covered_any":
            covered = set(value.get("covered_content_groups") or [])
            missing = [item for item in target if item not in covered]
            if missing:
                failures.append(f"covered_any missing: {missing!r}")
            continue
        elif key == "slots":
            slots = value.get("slots") or {}
            for slot_key, slot_value in target.items():
                if slots.get(slot_key) != slot_value:
                    failures.append(f"slot {slot_key}: expected {slot_value!r}, got {slots.get(slot_key)!r}")
            continue
        else:
            actual = value.get(key)
        if actual != target:
            failures.append(f"{key}: expected {target!r}, got {actual!r}")
    return failures


def _question_count(text: str) -> int:
    return sum(text.count(mark) for mark in ("?", "\uff1f"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="../output/unified-journey-verification.json")
    args = parser.parse_args()

    before = _counts()
    with SessionLocal() as db:
        active_policy = effective_reception_policy(db)
    results = []
    for scenario in SCENARIOS:
        context = {
            "module": "reply",
            "customer_text": scenario["customer_text"],
            "context_messages": scenario["context_messages"],
            "context_complete": True,
            "route_variant": scenario["route_variant"],
            "memory": scenario["memory"],
            "journey": journey_context_from_values(
                scenario["route_variant"],
                slots=scenario["memory"],
                sent_groups=[],
            ),
            "route_playbook": playbook_prompt(),
            "reception_policy": active_policy,
            "available_materials": [],
            "lead_capture": {"status": "not_started"},
        }
        decision, logs, _digest, trace = generate_decision(context)
        value = asdict(decision)
        failures = _matches(value, scenario["expect"])
        reply = value.get("reply") or ""
        failures += ["reply exceeds 200 characters"] if len(reply) > 200 else []
        failures += ["more than one question"] if _question_count(reply) > 1 else []
        sop_preview = (
            None if value.get("action") == "handoff"
            else _next_sop_preview(value.get("route_variant") or "", value.get("covered_content_groups") or [])
        )
        results.append({
            "key": scenario["key"],
            "customer_text": scenario["customer_text"],
            "passed": not failures,
            "failures": failures,
            "decision": value,
            "sop_preview": sop_preview,
            "trace": trace,
            "model_calls": len(logs),
        })
        print(f"{scenario['key']}: {'passed' if not failures else 'failed'}")

    after = _counts()
    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "passed": all(item["passed"] for item in results) and before == after,
        "prompt_version": REALTIME_REPLY_PROMPT_VERSION,
        "validator_version": VALIDATOR_VERSION,
        "policy_version": JOURNEY_POLICY["policy_version"],
        "results": results,
        "database_counts_before": before,
        "database_counts_after": after,
        "chatwoot_writes": 0,
    }
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "passed": report["passed"],
        "before": before,
        "after": after,
        "output": str(output),
    }, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
