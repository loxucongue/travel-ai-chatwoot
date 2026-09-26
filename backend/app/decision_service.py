"""Customer-service decision entry point with code-owned business planning."""
from __future__ import annotations

import time
from copy import deepcopy

from app.decision_knowledge import FACTS, KNOWLEDGE_KEY, evidence_packet
from app.deepseek_evaluation import ALLOWED_MEMORY_SLOTS
from app.realtime_reply_pipeline import REALTIME_REPLY_PROMPT_VERSION, run_realtime_reply_pipeline
from app.reply_planning import SALES_HANDOFF_REASONS
from app.route_packages import ensure_route_packages_current
from app.route_packages import ROUTES, route_catalog_context
from app.route_reply import (ROUTE_SNAPSHOTS_KEY, journey_context_from_values,
                             make_route_snapshot, playbook_prompt)
from app.silence_touch_pipeline import SILENCE_TOUCH_PROMPT_VERSION, run_silence_touch_pipeline
from app.reception_v2.runtime import PROMPT_VERSION as V2_PROMPT_VERSION, run_v2_agent


VALIDATOR_VERSION = "split-reply-contract-v49"


def _current_customer_text(context: dict) -> str:
    return str(context.get("customer_text") or "")


def _explicit_stop_request(text: str) -> bool:
    normalized = "".join(text.lower().split())
    return any(term in normalized for term in (
        "\u4e0d\u8981\u518d\u8054\u7cfb",  # do not contact again
        "\u4e0d\u8981\u8054\u7cfb",      # do not contact
        "\u522b\u518d\u8054\u7cfb",      # stop contacting
        "\u4e0d\u8981\u518d\u6253\u6270",  # do not disturb again
        "\u522b\u518d\u6253\u6270",      # stop disturbing
        "不要再聯繫", "不要聯繫", "別再聯繫",
        "不要再打擾", "別再打擾",
        "不要再聯絡", "不要聯絡", "別再聯絡",
    ))


def _validated_slots(decision, customer_text: str) -> tuple[dict, dict, list[str]]:
    slots: dict = {}
    evidence: dict = {}
    rejected: list[str] = []
    for key, value in (decision.slots or {}).items():
        quote = (decision.slot_evidence or {}).get(key)
        if (
            str(key) in ALLOWED_MEMORY_SLOTS
            and isinstance(quote, str)
            and quote.strip()
            and quote in customer_text
        ):
            slots[key] = value
            evidence[key] = quote
        else:
            rejected.append(str(key))
    return slots, evidence, rejected


def _validated_route_references(decision, context: dict) -> tuple[list[str], list[str], str, list[str]]:
    route = decision.route_variant if decision.route_variant in ROUTES else ""
    group_key = str(decision.content_group_key or "")
    flags: list[str] = []
    if group_key and (not route or group_key not in ROUTES[route]["groups"]):
        group_key = ""
        flags.append("content_group_reference_rejected")

    known_facts = {
        *{item["id"] for item in FACTS},
        *{
            fact["id"]
            for route in ROUTES.values()
            for fact in route.get("knowledge_facts", [])
        },
        *{
            str(fact.get("id") or "")
            for fact in context.get("global_knowledge_facts", [])
            if isinstance(fact, dict)
            and str(fact.get("id") or "").startswith("web.")
            and str(fact.get("text") or "").strip()
        },
    }
    refs = [item for item in decision.evidence_refs if item in known_facts]
    if len(refs) != len(decision.evidence_refs):
        flags.append("unknown_evidence_reference_removed")
    if group_key:
        refs = list(dict.fromkeys([
            *refs,
            *ROUTES[route]["groups"][group_key]["evidence"],
        ]))

    available = {str(item.get("key")) for item in context.get("available_materials", [])}
    if route:
        route_assets = {
            asset
            for group in ROUTES[route].get("groups", {}).values()
            for asset in group.get("assets", [])
        }
    else:
        route_assets = set()
    covered_groups = {
        group for group in (decision.covered_content_groups or [])
        if route and group in ROUTES[route]["groups"]
    }
    if group_key:
        covered_groups.add(group_key)
    group_assets = {
        asset
        for group in covered_groups
        for asset in ROUTES[route]["groups"][group]["assets"]
    } if route and covered_groups else route_assets
    materials = [
        key for key in decision.material_keys
        if key in available and key in group_assets
    ]
    if len(materials) != len(decision.material_keys):
        flags.append("unavailable_material_reference_removed")
    return refs, materials, group_key, flags


def _validated_reply_options(decision) -> tuple[list[str], list[str]]:
    allowed = {spec["selection_title"] for spec in ROUTES.values()}
    options = list(dict.fromkeys(
        item for item in decision.reply_options if item in allowed
    ))
    flags = [] if len(options) == len(decision.reply_options) else ["unknown_reply_option_removed"]
    return options, flags


def generate_decision(context: dict, model_call=None):
    """Pin all planning, generation, verification and validation to one catalog."""
    ensure_route_packages_current()
    journey = context.get("journey") or {}
    slots = journey.get("slots") if "slots" in journey else context.get("slots")
    # A route-only input is a selection hint, not proof of a durable journey.
    route = str(journey.get("route_variant") or (
        context.get("route_variant") if slots is not None or journey else ""
    ) or "")
    with route_catalog_context(route, slots, available_materials=context.get("available_materials")):
        if route and journey_context_from_values(
            route, slots=slots, sent_groups=journey.get("sent_content_groups"),
        )["automatic_delivery_paused"]:
            raise ValueError("route_history_unverifiable")
        snapshots = {key: make_route_snapshot(key, ROUTES[key]) for key in ROUTES}
        if route:
            snapshots[route] = deepcopy(slots[ROUTE_SNAPSHOTS_KEY][route])
        result = _generate_decision({**context, "route_playbook": playbook_prompt()}, model_call)
        decision = result[0]
        decision.bound_route_snapshot = deepcopy(snapshots.get(decision.route_variant))
        return result


def _generate_decision(context: dict, model_call=None):
    """Use split production pipelines; injected legacy calls exist for contract tests only."""
    start = time.monotonic()
    durable_memory = dict(context.get("memory") or {})
    packet = {
        **context,
        "knowledge": evidence_packet(
            context.get("global_knowledge_facts"),
            str(context.get("global_knowledge_version") or ""),
        ),
        "memory": durable_memory,
    }
    packet.pop("expected_branch", None)
    packet.pop("reference_answer", None)
    evidence_ms = int((time.monotonic() - start) * 1000)

    pipeline_trace: dict = {}
    module = str(context.get("module") or "reply")
    engine_version = str(context.get("engine_version") or "v1")
    if module in {'silence_touch', 'wakeup'}:
        from app.customer_contact_policy import contact_constraint
        from app.deepseek_evaluation import EvaluationDecision
        constraint, delay = contact_constraint(context)
        if constraint:
            return (EvaluationDecision(action='no_action', branch='unclassified', intent='other',
                wakeup_action='defer' if delay else 'skip', defer_minutes=delay,
                safety_flags=[constraint], journey_stage=(context.get('journey') or {}).get('stage', 'considering')),
                [], '', {'outbound': False, 'engine_version': engine_version, 'customer_constraint': constraint})
    if engine_version not in {"v1", "v2"}:
        raise ValueError("unsupported_reception_engine")
    if engine_version == "v2" and module in {"reply", "lead_capture", "silence_touch", "wakeup"} and model_call is None:
        decision, logs, digest, pipeline_trace = run_v2_agent(packet)
        context = {**context, "global_knowledge_facts": pipeline_trace.get("selected_web_facts", context.get("global_knowledge_facts", []))}
        active_prompt_version = V2_PROMPT_VERSION
    elif module == "reply" and model_call is None:
        decision, logs, digest, pipeline_trace = run_realtime_reply_pipeline(packet)
        context = {**context, "global_knowledge_facts": pipeline_trace.get("selected_web_facts", context.get("global_knowledge_facts", []))}
        active_prompt_version = REALTIME_REPLY_PROMPT_VERSION
    elif module in {"silence_touch", "wakeup"} and model_call is None:
        decision, logs, digest, pipeline_trace = run_silence_touch_pipeline(packet)
        active_prompt_version = SILENCE_TOUCH_PROMPT_VERSION
    elif model_call is not None:
        decision, logs, digest = model_call(packet)
        active_prompt_version = "legacy-injected-model-call"
    else:
        raise ValueError(f"unsupported_decision_module:{module}")
    model_ms = int((time.monotonic() - start) * 1000) - evidence_ms

    customer_text = _current_customer_text(context)
    if _explicit_stop_request(customer_text):
        decision.safety_flags = sorted(set([*(decision.safety_flags or []), "stop_automation"]))
        decision.wakeup_action = "skip"
        if module == "reply" and decision.action == "no_action":
            decision.action = "reply"
            decision.reply = "\u597d\u7684\uff0c\u5df2\u505c\u6b62\u4e3b\u52a8\u8054\u7cfb\u3002"
            decision.reply_body = decision.reply
    decision.slots, decision.slot_evidence, rejected_slots = _validated_slots(
        decision, customer_text
    )
    refs, materials, group_key, reference_flags = _validated_route_references(
        decision, context
    )
    decision.evidence_refs = refs
    decision.material_keys = materials
    decision.content_group_key = group_key
    allowed_groups = set(ROUTES.get(decision.route_variant, {}).get("groups", {}))
    decision.covered_content_groups = list(dict.fromkeys(
        group for group in [decision.content_group_key, *(decision.covered_content_groups or [])]
        if group in allowed_groups
    ))
    decision.reply_options, option_flags = _validated_reply_options(decision)
    decision.safety_flags = sorted(set([
        *(decision.safety_flags or []),
        *reference_flags,
        *option_flags,
        *(["unsupported_slot_evidence_removed"] if rejected_slots else []),
    ]))
    decision.missing_slots = [
        item for item in decision.missing_slots if item not in decision.slots
    ]
    if decision.action == "no_action":
        decision.material_keys = []
        decision.reply_options = []
        decision.covered_content_groups = []
    elif decision.action == "handoff":
        decision.reply_options = []
        if decision.handoff_reason not in SALES_HANDOFF_REASONS:
            decision.material_keys = []
            decision.covered_content_groups = []

    memory_checks = {
        key: {
            "present": key in decision.slots,
            "matches_memory": str(decision.slots.get(key)) == str(
                item.get("value") if isinstance(item, dict) else item
            ),
        }
        for key, item in durable_memory.items()
    }
    total_ms = int((time.monotonic() - start) * 1000)
    retrieved_web_facts = [
        item for item in context.get("global_knowledge_facts", [])
        if isinstance(item, dict) and str(item.get("id") or "").startswith("web.")
    ]
    retrieved_web_fact_ids = [str(item["id"]) for item in retrieved_web_facts]
    used_web_fact_ids = [
        fact_id for fact_id in decision.evidence_refs
        if str(fact_id).startswith("web.")
    ]
    web_knowledge_active = bool(context.get("global_knowledge_version"))
    if engine_version == "v2":
        from app.reception_v2.decision_contract import build_decision_contract
        pipeline_trace["decision_contract"] = build_decision_contract(
            context, decision, flow=pipeline_trace.get("flow", ""),
            flow_reason=pipeline_trace.get("flow_reason", ""),
            proactive=pipeline_trace.get("proactive_gate", {}),
        )
    knowledge_status = (
        "used" if used_web_fact_ids
        else "retrieved_not_used" if retrieved_web_fact_ids
        else "no_match" if web_knowledge_active
        else "disabled"
    )
    trace = {
        "evidence_ms": evidence_ms,
        "model_ms": model_ms,
        "validation_ms": max(0, total_ms - evidence_ms - model_ms),
        "total_ms": total_ms,
        "engine_version": engine_version,
        "request_count": len(logs),
        "input_tokens": sum(item.get("input_tokens") or 0 for item in logs),
        "output_tokens": sum(item.get("output_tokens") or 0 for item in logs),
        "knowledge_version": packet["knowledge"]["version"],
        "prompt_version": active_prompt_version,
        "schema_valid": True,
        "outbound": False,
        "validator_version": VALIDATOR_VERSION,
        "context_complete": context.get("context_complete") is True,
        "context_messages": len(context.get("context_messages", [])),
        "context_characters": sum(
            len(str(item.get("content") or ""))
            for item in context.get("context_messages", [])
        ),
        "memory_slots": sorted(decision.slots),
        "model_memory_checks": memory_checks,
        "content_group_key": decision.content_group_key,
        "covered_content_groups": decision.covered_content_groups,
        "rejected_slots": rejected_slots,
        "knowledge_usage": {
            "status": knowledge_status,
            "active": web_knowledge_active,
            "version": str(context.get("global_knowledge_version") or ""),
            "retrieved_fact_count": len(retrieved_web_fact_ids),
            "retrieved_fact_ids": retrieved_web_fact_ids,
            "used_fact_count": len(used_web_fact_ids),
            "used_fact_ids": used_web_fact_ids,
            "source_urls": list(dict.fromkeys(
                str(item.get("source") or "")
                for item in retrieved_web_facts if item.get("source")
            )),
            "verification_passed": pipeline_trace.get("fact_verification_passed"),
            "response_source": pipeline_trace.get("response_source", "none"),
        },
        "first_token_ms": next((
            item.get("response_meta", {}).get("first_token_ms")
            for item in logs if item.get("status") == "completed"
        ), None),
        **pipeline_trace,
    }
    return decision, logs, digest, trace
