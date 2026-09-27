"""Config-driven reply orchestration for the two reviewed China2Go routes."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json

from sqlalchemy import select
from sqlalchemy.orm import Session
from app.advisor_voice import slot_follow_up_question

from app.models import ConversationJourney, ConversationState, utcnow
from app.route_packages import (
    JOURNEY_POLICY,
    KNOWLEDGE_VERSION,
    ROUTES,
)


STAGE_ALIASES = {
    "discovering_needs": "needs_discovery",
    "introducing": "value_building",
    "answering": "value_building",
    "completed": "captured",
}

CONTENT_PROGRESS_KEY = "_content_progress"
ROUTE_SNAPSHOTS_KEY = "_route_snapshots"


def _snapshot_digest(spec: dict) -> str:
    return hashlib.sha256(json.dumps(spec, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def make_route_snapshot(route_variant: str, spec: dict) -> dict:
    """Capture a code-owned catalog view before invoking a model."""
    spec = deepcopy(spec)
    return {"schema_version": 1, "route_variant": route_variant,
            "knowledge_version": KNOWLEDGE_VERSION, "digest": _snapshot_digest(spec), "spec": spec}


def bind_new_route_snapshot(route_variant: str, slots: dict | None, *, available_materials=None) -> dict:
    """Creation-only binding before any sends; never repair unknown history.

    The caller must establish that this is a brand-new session. Empty slots alone
    cannot prove that historical messages do not exist. Supply material metadata
    at creation to pin media identities before the first decision or delivery.
    """
    from app.route_packages import route_catalog_context

    merged = deepcopy(slots or {})
    snapshots = merged.get(ROUTE_SNAPSHOTS_KEY, {})
    progress = merged.get(CONTENT_PROGRESS_KEY, {})
    if not isinstance(snapshots, dict) or not isinstance(progress, dict):
        raise ValueError("route_snapshot_unverifiable")
    if route_variant in snapshots or route_variant in progress:
        raise ValueError("route_snapshot_already_initialized")
    with route_catalog_context(available_materials=available_materials):
        if route_variant not in ROUTES:
            raise ValueError("route_snapshot_route_unknown")
        snapshots[route_variant] = make_route_snapshot(route_variant, ROUTES[route_variant])
    merged[ROUTE_SNAPSHOTS_KEY] = snapshots
    return merged


def route_snapshot_from_values(route_variant: str, slots: dict | None) -> dict | None:
    """Return an isolated, verified route spec; never fall back to current ROUTES."""
    snapshots = (slots or {}).get(ROUTE_SNAPSHOTS_KEY)
    entry = snapshots.get(route_variant) if isinstance(snapshots, dict) else None
    if not isinstance(entry, dict) or entry.get("schema_version") != 1:
        return None
    if entry.get("route_variant", route_variant) != route_variant:
        return None
    spec = entry.get("spec")
    if (not isinstance(spec, dict) or not isinstance(spec.get("groups"), dict)
            or entry.get("digest") != _snapshot_digest(spec)):
        return None
    return deepcopy(spec)


def group_content_from_values(
    route_variant: str, slots: dict | None, group_key: str,
) -> dict | None:
    """Resolve reviewed group content without reinterpreting a frozen SOP payload.

    Asset keys are requirements, not frozen media identities. Callers must still
    validate frozen media hashes and pause on absent or mismatched bindings.
    """
    spec = route_snapshot_from_values(route_variant, slots)
    group = spec["groups"].get(group_key) if spec is not None else None
    return group if isinstance(group, dict) else None


def frozen_sop_nodes(spec: dict | None) -> list[dict]:
    """Build enrollment content from a verified snapshot, never a latest SOP.

    Pass the result of route_snapshot_from_values. This freezes media identities,
    not file availability or approval: the sender must validate those at send
    time. Apply current global gates and scheduling policy outside this helper.
    Missing bindings block the whole enrollment, including dynamic candidates.
    """
    if not isinstance(spec, dict) or not isinstance(spec.get("sop"), dict):
        raise ValueError("route_snapshot_sop_invalid")
    nodes = spec["sop"].get("nodes")
    if not isinstance(nodes, list):
        raise ValueError("route_snapshot_sop_invalid")
    groups = spec.get("groups")
    bindings = spec.get("asset_bindings", {})
    hashes = spec.get("asset_hashes", {})
    if not all(isinstance(value, dict) for value in (groups, bindings, hashes)):
        raise ValueError("route_snapshot_sop_invalid")

    def freeze_node(node: dict) -> dict:
        if not isinstance(node, dict):
            raise ValueError("route_snapshot_sop_invalid")
        result = deepcopy(node)
        group_key = result.get("content_group_key")
        if group_key and group_key not in groups:
            raise ValueError("route_snapshot_sop_group_unknown")
        messages = result.get("messages")
        if not isinstance(messages, list):
            raise ValueError("route_snapshot_sop_invalid")
        for item in messages:
            if not isinstance(item, dict):
                raise ValueError("route_snapshot_sop_invalid")
            if item.get("content_type", "text") == "text":
                continue
            key = item.get("asset_key")
            binding = bindings.get(key)
            if (not group_key or key not in groups[group_key].get("assets", [])
                    or not isinstance(binding, dict)
                    or binding.get("asset_key") != key
                    or not binding.get("media_id") or not binding.get("media_hash")
                    or not binding.get("content_type")):
                raise ValueError("route_snapshot_asset_binding_missing")
            if hashes.get(key) != binding["media_hash"]:
                raise ValueError("route_snapshot_asset_binding_mismatch")
            for field in ("asset_key", "media_id", "media_hash", "content_type"):
                if item.get(field) is not None and item[field] != binding[field]:
                    raise ValueError("route_snapshot_asset_binding_mismatch")
                item[field] = deepcopy(binding[field])
        if "content_group_candidates" in result:
            candidates = result["content_group_candidates"]
            if not isinstance(candidates, list):
                raise ValueError("route_snapshot_sop_invalid")
            result["content_group_candidates"] = [freeze_node(candidate) for candidate in candidates]
        return result

    return [freeze_node(node) for node in nodes]


def group_requirements_from_values(
    route_variant: str, slots: dict | None, group_key: str,
) -> tuple[bool, set[str]] | None:
    """Unknown requirements require a pause, not completion or a catalog lookup."""
    group = group_content_from_values(route_variant, slots, group_key)
    if group is None:
        return None
    mode = str(group.get("delivery_mode") or "assets_then_text")
    return mode != "assets_only", set() if mode == "text_only" else set(group.get("assets") or [])


def content_progress_from_values(
    route_variant: str,
    slots: dict | None,
    sent_groups: list[str] | None,
) -> dict:
    """Keep explicit receipts, topic coverage and unverifiable history distinct."""
    stored = (slots or {}).get(CONTENT_PROGRESS_KEY)
    stored = stored if isinstance(stored, dict) else {}
    values = stored.get(route_variant)
    values = values if isinstance(values, dict) else {}
    route_progress = {
        key: {"text_delivered": bool(value.get("text_delivered")) and value.get("schema_version") == 2,
              "asset_keys": list(dict.fromkeys(value.get("asset_keys") or [])),
              "topic_covered": bool(value.get("topic_covered")),
              "history_unknown": bool(value.get("history_unknown")) or value.get("schema_version") != 2
                  or group_requirements_from_values(route_variant, slots, key) is None,
              "schema_version": 2}
        for key, value in values.items()
        if isinstance(value, dict)
    }
    for group_key in sent_groups or []:
        if group_key not in route_progress:
            route_progress[group_key] = {
                "text_delivered": False, "asset_keys": [], "topic_covered": False,
                "history_unknown": True, "schema_version": 2,
            }
    return route_progress


def update_content_progress_values(
    route_variant: str,
    slots: dict | None,
    sent_groups: list[str] | None,
    group_key: str,
    *,
    text_delivered: bool = False,
    asset_keys: list[str] | None = None,
    complete: bool = False,
    delivered_text: str | None = None,
    topic_covered: bool = False,
) -> tuple[dict, list[str], bool]:
    """Merge a confirmed delivery and derive group completion deterministically."""
    merged_slots = dict(slots or {})
    groups = list(sent_groups or [])
    if not route_variant or not group_key:
        return merged_slots, groups, False
    stored = merged_slots.get(CONTENT_PROGRESS_KEY)
    all_progress = dict(stored) if isinstance(stored, dict) else {}
    route_progress = content_progress_from_values(route_variant, merged_slots, groups)
    current = dict(route_progress.get(group_key) or {})
    delivered_assets = list(dict.fromkeys([
        *(current.get("asset_keys") or []), *(asset_keys or []),
    ]))
    requirements = group_requirements_from_values(route_variant, merged_slots, group_key)
    group = group_content_from_values(route_variant, merged_slots, group_key)
    reviewed_text = (group or {}).get("text")
    exact_text = isinstance(reviewed_text, str) and bool(reviewed_text) and delivered_text == reviewed_text
    needs_text, required_assets = requirements if requirements is not None else (True, set())
    current = {
        "text_delivered": bool(current.get("text_delivered") or exact_text),
        "asset_keys": delivered_assets,
        "topic_covered": bool(current.get("topic_covered") or topic_covered or complete or text_delivered or delivered_text),
        "history_unknown": bool(current.get("history_unknown")) or requirements is None,
        "schema_version": 2,
    }
    route_progress[group_key] = current
    all_progress[route_variant] = route_progress
    merged_slots[CONTENT_PROGRESS_KEY] = all_progress
    is_complete = (
        not current["history_unknown"]
        and (not needs_text or current["text_delivered"])
        and required_assets <= set(current["asset_keys"])
    )
    if is_complete and group_key not in groups:
        groups.append(group_key)
    return merged_slots, groups, is_complete


def automatic_content_already_covered(group: str, sent_groups) -> bool:
    sent = set(sent_groups or [])
    if group in sent:
        return True
    if group == "advisor_greeting":
        return bool(sent & {"brand_positioning", "itinerary_overview", "peach_highlights",
                           "hotel_reference", "rongbuk_reference", "vehicle_reference",
                           "landmarks", "zhaji"})
    if group == "brand_positioning":
        return bool(sent & {"itinerary_overview", "peach_highlights", "hotel_reference",
                           "rongbuk_reference", "vehicle_reference", "landmarks", "zhaji"})
    if group == "accommodation_summary":
        return "hotel_reference" in sent
    if group in {"contact_transition", "read_check"}:
        return bool(sent & {"contact_transition", "read_check"})
    return False


def normalize_journey_stage(value: str | None) -> str:
    return STAGE_ALIASES.get(str(value or "route_selection"), str(value or "route_selection"))


def merge_profile(
    slots: dict | None,
    decision,
    *,
    source_message_id: int | str | None = None,
) -> dict:
    """Persist facts and model inferences with explicit provenance metadata."""
    merged = dict(slots or {})
    metadata = dict(merged.get("_profile_meta") or {})
    for key, value in (getattr(decision, "slots", {}) or {}).items():
        if key.startswith("_"):
            continue
        merged[key] = value
        metadata[key] = {
            "kind": "customer_fact",
            "evidence_quote": (getattr(decision, "slot_evidence", {}) or {}).get(key, ""),
            "source_message_id": source_message_id,
            "confidence": 1.0,
            "status": (
                "undecided"
                if key == "departure_window" and str(value or "").strip() in {
                    "未确定", "未確定", "不确定", "不確定", "还没定", "還沒定",
                }
                else "known"
            ),
        }
    for key, update in (getattr(decision, "profile_updates", {}) or {}).items():
        if key.startswith("_"):
            continue
        merged[key] = update.get("value")
        metadata[key] = {
            "kind": "customer_fact" if update.get("evidence_quote") else "model_inference",
            "evidence_quote": update.get("evidence_quote") or "",
            "source_message_id": source_message_id if update.get("evidence_quote") else None,
            "confidence": float(update.get("confidence") or 0),
            "reason": update.get("reason") or "",
            "status": (
                "undecided"
                if key == "departure_window" and str(update.get("value") or "").strip() in {
                    "未确定", "未確定", "不确定", "不確定", "还没定", "還沒定",
                }
                else "known"
            ),
        }
    if metadata:
        merged["_profile_meta"] = metadata
    return merged


def journey_for(db: Session, state: ConversationState) -> ConversationJourney:
    journey = db.scalar(select(ConversationJourney).where(
        ConversationJourney.conversation_state_id == state.id
    ))
    if journey is None:
        journey = ConversationJourney(
            conversation_state_id=state.id,
            knowledge_version_key=KNOWLEDGE_VERSION,
        )
        db.add(journey)
        db.flush()
    return journey


def _next_group(spec: dict, sent_groups: list[str]) -> str | None:
    sent = set(sent_groups or [])
    return next((key for key in spec.get("sequence", []) if key not in sent), None)


def journey_context_from_values(
    route_variant: str = "",
    stage: str = "route_selection",
    slots: dict | None = None,
    sent_groups: list[str] | None = None,
) -> dict:
    route = route_variant or ""
    sent = list(sent_groups or [])
    snapshot = route_snapshot_from_values(route, slots)
    spec = snapshot or {}
    policies = spec.get("policies", {})
    price_gate = policies.get("price_after_group")
    content_progress = content_progress_from_values(route, slots, sent) if route else {}
    history_unknown = bool(route) and (snapshot is None or any(
        value["history_unknown"] for value in content_progress.values()
    ))
    completed = [key for key, value in content_progress.items()
                 if not value["history_unknown"]
                 and (requirements := group_requirements_from_values(route, slots, key)) is not None
                 and (not requirements[0] or value["text_delivered"])
                 and requirements[1] <= set(value["asset_keys"])]
    snapshots = (slots or {}).get(ROUTE_SNAPSHOTS_KEY)
    entry = snapshots.get(route, {}) if isinstance(snapshots, dict) else {}
    return {
        "knowledge_version": entry.get("knowledge_version", "") if snapshot else "",
        "route_variant": route,
        "stage": normalize_journey_stage(stage),
        "slots": slots or {},
        "customer_profile": {
            key: value for key, value in (slots or {}).items() if not key.startswith("_")
        },
        "profile_provenance": dict((slots or {}).get("_profile_meta") or {}),
        "profile_field_states": {
            key: str(value.get("status") or "known")
            for key, value in dict((slots or {}).get("_profile_meta") or {}).items()
            if isinstance(value, dict)
        },
        "sent_content_groups": sent,
        "content_progress": content_progress,
        "completed_content_groups": completed,
        "topic_covered_groups": [key for key, value in content_progress.items() if value["topic_covered"]],
        "history_unknown": history_unknown,
        "automatic_delivery_paused": history_unknown,
        "sent_asset_keys": list(dict.fromkeys(
            asset
            for progress in content_progress.values()
            for asset in progress.get("asset_keys", [])
        )),
        "next_content_group": _next_group(spec, completed) if route and not history_unknown else None,
        "allowed_content_groups": list(spec.get("groups", {})) if not history_unknown else [],
        "price_reference_ready": bool(not history_unknown and price_gate and price_gate in completed),
    }


def journey_context(journey: ConversationJourney) -> dict:
    return journey_context_from_values(
        journey.route_variant,
        journey.stage,
        journey.slots,
        journey.sent_groups,
    )


def playbook_prompt(route_variant: str = "", slots: dict | None = None) -> dict:
    """Use the catalog for selection only; supplied slots require a bound snapshot."""
    if slots is not None:
        spec = route_snapshot_from_values(route_variant, slots)
        routes = {route_variant: spec} if spec is not None else {}
    else:
        routes = {route_variant: ROUTES[route_variant]} if route_variant in ROUTES else (
            {} if route_variant else ROUTES
        )
    return {
        route: {
            "name": spec["name"],
            "selection_title": spec["selection_title"],
            "package_version": spec["package_version"],
            "ai_guidance": spec.get("ai_guidance", ""),
            "required_slots": spec["required_slots"],
            "sequence": spec["sequence"],
            "policies": spec["policies"],
            "groups": {
                key: {
                    "purpose": group["purpose"],
                    "reference_text": group["text"],
                    "asset_keys": group["assets"],
                    "evidence_refs": group["evidence"],
                }
                for key, group in spec["groups"].items()
            },
        }
        for route, spec in deepcopy(dict(routes)).items()
    }


def reception_policy_prompt() -> dict:
    """Versioned policy derived from direct raw-conversation analysis."""
    return JOURNEY_POLICY


def prepare_route_reply_values(
    decision,
    *,
    current_route: str = "",
    preserve_current_route: bool = False,
    stage: str = "route_selection",
    slots: dict | None = None,
    sent_groups: list[str] | None = None,
    source_message_id: int | str | None = None,
) -> tuple[object, dict]:
    """Persist the validated decision and code-owned journey transition."""
    previous_route = current_route or ""
    bound_entry = getattr(decision, "bound_route_snapshot", None)
    bound_spec = None
    if bound_entry is not None:
        bound_spec = route_snapshot_from_values(
            decision.route_variant, {ROUTE_SNAPSHOTS_KEY: {decision.route_variant: bound_entry}},
        )
        if bound_spec is None:
            raise ValueError("decision_route_snapshot_invalid")
    route = decision.route_variant if (
        decision.route_variant in ROUTES
        or bound_spec is not None
        or route_snapshot_from_values(decision.route_variant, slots) is not None
        or decision.route_variant == previous_route
    ) else ""
    if preserve_current_route and not route:
        route = previous_route
    previous_slots = dict(slots or {})
    progress_groups = list(sent_groups or [])

    if route and route != previous_route:
        progress_groups = []
    decision.route_variant = route

    merged_slots = merge_profile(previous_slots, decision, source_message_id=source_message_id)
    if getattr(decision, 'v2_events', None):
        from app.reception_v2.events import merge_events
        events = [{**event, 'source_message_id': source_message_id or event.get('source_message_id')}
                  for event in decision.v2_events]
        decision.v2_events = events
        merged_slots = merge_events(merged_slots, events)
    stored_snapshots = merged_slots.get(ROUTE_SNAPSHOTS_KEY)
    snapshots = deepcopy(stored_snapshots) if isinstance(stored_snapshots, dict) else {}
    if bound_spec is not None and route in snapshots:
        existing = route_snapshot_from_values(route, previous_slots)
        if existing is None or _snapshot_digest(existing) != _snapshot_digest(bound_spec):
            raise ValueError("decision_route_snapshot_mismatch")
    # Preserve unknown old routes even after switching away and back.
    if previous_route and previous_route not in snapshots:
        snapshots[previous_route] = {"schema_version": 1, "history_unknown": True}
    if stored_snapshots is not None and not isinstance(stored_snapshots, dict) and route:
        snapshots[route] = {"schema_version": 1, "history_unknown": True}
    merged_slots[ROUTE_SNAPSHOTS_KEY] = snapshots
    # Only a newly selected route without prior receipts can bind today's catalog.
    stored_progress = merged_slots.get(CONTENT_PROGRESS_KEY)
    prior_progress = stored_progress.get(route) if isinstance(stored_progress, dict) else stored_progress
    if (route and route != previous_route and route not in snapshots
            and not prior_progress and (bound_spec is not None or route in ROUTES)
            and not (not previous_route and sent_groups)):
        snapshots[route] = deepcopy(bound_entry) if bound_spec is not None else make_route_snapshot(route, ROUTES[route])
        merged_slots[ROUTE_SNAPSHOTS_KEY] = snapshots
    progress_stage = normalize_journey_stage(
        getattr(decision, "journey_stage", "") or stage or "route_selection"
    )
    if not route:
        progress_stage = "route_selection"
    progress = {
        "knowledge_version": journey_context_from_values(route, progress_stage, merged_slots, progress_groups)["knowledge_version"],
        "route_variant": route,
        "stage": progress_stage,
        "slots": merged_slots,
        "sent_content_groups": progress_groups,
    }
    return decision, progress


def prepare_route_reply(
    db: Session,
    state: ConversationState,
    decision,
    trigger_message_id: int | None = None,
):
    """Bind a validated decision to reviewed route content and durable progress."""
    journey = journey_for(db, state)
    previous_route = journey.route_variant
    decision, progress = prepare_route_reply_values(
        decision,
        current_route=journey.route_variant,
        stage=journey.stage,
        slots=journey.slots,
        sent_groups=journey.sent_groups,
        source_message_id=trigger_message_id,
    )
    journey.route_variant = progress["route_variant"]
    journey.stage = progress["stage"]
    journey.slots = progress["slots"]
    journey.sent_groups = progress["sent_content_groups"]
    journey.knowledge_version_key = progress["knowledge_version"]
    journey.last_trigger_message_id = trigger_message_id
    if previous_route != journey.route_variant:
        journey.last_group_key = None
        journey.version += 1
    journey.updated_at = utcnow()
    return decision, journey


def mark_group_delivered(journey: ConversationJourney, group_key: str | None) -> None:
    """Legacy topic marker; not evidence of full text or asset delivery."""
    if not group_key:
        return
    journey.slots, journey.sent_groups, _ = update_content_progress_values(
        journey.route_variant, journey.slots, journey.sent_groups, group_key, complete=True,
    )
    journey.last_group_key = group_key
    journey.version += 1
    journey.updated_at = utcnow()


def record_group_delivery(
    journey: ConversationJourney,
    group_key: str | None,
    *,
    text_delivered: bool = False,
    asset_keys: list[str] | None = None,
    delivered_text: str | None = None,
    topic_covered: bool = False,
) -> bool:
    """Record only items confirmed by the channel and return group completion."""
    if not group_key:
        return False
    journey.slots, journey.sent_groups, completed = update_content_progress_values(
        journey.route_variant,
        journey.slots,
        journey.sent_groups,
        group_key,
        text_delivered=text_delivered,
        asset_keys=asset_keys,
        delivered_text=delivered_text,
        topic_covered=topic_covered,
    )
    journey.last_group_key = group_key
    journey.version += 1
    journey.updated_at = utcnow()
    return completed


def deferred_initial_follow_up(
    decision, sent_groups: list[str] | None, slots: dict | None = None,
) -> dict | None:
    """Defer behind snapshot groups when slots are supplied, pausing unknown history.

    Omitted slots retain the catalog-only compatibility path. Journey callers
    must supply slots, including an empty dict for unbound historical journeys.
    """
    def value(key: str):
        return decision.get(key) if isinstance(decision, dict) else getattr(decision, key, "")

    route_variant = str(value("route_variant") or "")
    route = (route_snapshot_from_values(route_variant, slots) if slots is not None
             else ROUTES.get(route_variant))
    if slots is not None:
        context = journey_context_from_values(route_variant, slots=slots, sent_groups=sent_groups)
        if context["automatic_delivery_paused"]:
            return None
        sent_groups = context["completed_content_groups"]
    question = str(value("follow_up_question") or "").strip()
    if not route or value("action") != "reply":
        return None
    if set(value("safety_flags") or []) & {
        "skip_silence_enrollment", "deferred_follow_up_prohibited",
    }:
        return None
    sent = set(sent_groups or [])
    pending_initial = [
        key for key in route.get("sequence", [])
        if route.get("groups", {}).get(key, {}).get("initial_delivery") is True
        and key not in sent
    ]
    if not pending_initial:
        return None
    if not question:
        # A direct or verbatim answer may intentionally have no appended
        # question. The completed introductory sequence can still ask one
        # missing field, without editing that answer or repeating known facts.
        missing = set(value("missing_slots") or [])
        field = next((key for key in ("party_size", "departure_window") if key in missing), None)
        if not field:
            return None
        return {"type": "slot", "field": field, "question": slot_follow_up_question(field)}
    return {
        "type": str(value("follow_up_type") or ""),
        "field": str(value("follow_up_field") or ""),
        "question": question,
    }


def deferred_follow_up_group(
    route_variant: str, follow_up: dict | None, slots: dict | None = None,
) -> str | None:
    follow_up = follow_up or {}
    route = (route_snapshot_from_values(route_variant, slots) if slots is not None
             else ROUTES.get(route_variant))
    if slots is not None and route is None:
        return None
    if follow_up.get("type") == "contact":
        key = (route or {}).get("policies", {}).get("contact_request_group", "contact_request")
        return key if slots is None or key in route["groups"] else None
    policies = (route or {}).get("policies", {})
    key = {
        "party_size": policies.get("party_question_group"),
        "departure_window": policies.get("departure_question_group"),
    }.get(follow_up.get("field"))
    return key if slots is None or key in (route or {}).get("groups", {}) else None


def append_deferred_initial_follow_up(
    nodes: list[dict], route_variant: str, deferred_follow_up: dict | None,
    slots: dict | None = None,
) -> list[dict]:
    """Insert a final immediate question before the first silence-driven node."""
    question = str((deferred_follow_up or {}).get("question") or "").strip()
    if not question or not any(node.get("initial_delivery") is True for node in nodes):
        return nodes
    route = (route_snapshot_from_values(route_variant, slots) if slots is not None
             else ROUTES.get(route_variant))
    if slots is not None and journey_context_from_values(
        route_variant, slots=slots,
    )["automatic_delivery_paused"]:
        return nodes
    result = [dict(node) for node in nodes]
    insertion = next(
        (index for index, node in enumerate(result) if node.get("initial_delivery") is not True),
        len(result),
    )
    interval_seconds = int(
        (route or {}).get("initial_delivery_interval_seconds", 2)
    )
    question_group = deferred_follow_up_group(route_variant, deferred_follow_up, slots)
    result.insert(insertion, {
        "key": "initial_delivery_follow_up",
        # Both delivery engines record this group only after delivery.
        "content_group_key": question_group,
        "schedule_type": "relative",
        "basis": "previous_node",
        "delay_minutes": 0,
        "delay_seconds": interval_seconds,
        "delivery_interval_seconds": 0,
        "initial_delivery": True,
        "deferred_follow_up": dict(deferred_follow_up or {}),
        "messages": [{
            "key": "follow_up",
            "content_type": "text",
            "content": question,
        }],
    })
    return result


def resolve_journey_payload(payload: dict, sent_groups: list[str]) -> dict | None:
    """Resolve a timer to the first reviewed content group not yet delivered."""
    value = dict(payload or {})
    candidates = value.get("content_group_candidates") or []
    if not candidates:
        return value
    sent = set(sent_groups or [])
    selected = next(
        (candidate for candidate in candidates if candidate.get("content_group_key") not in sent),
        None,
    )
    if not selected:
        return None
    value.update(selected)
    value.pop("content_group_candidates", None)
    value["selected_dynamically"] = True
    return value
