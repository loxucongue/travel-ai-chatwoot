"""Read-only normalization of the durable journey state for one V2 turn."""
from __future__ import annotations

from app.reception_v2.route_profiles import resolve_topic, route_profile
from app.route_reply import route_snapshot_from_values
from app.route_packages import ROUTES


def build_journey_memory(context: dict) -> dict:
    journey = context.get("journey") or {}
    route = str(context.get("route_variant") or journey.get("route_variant") or "")
    slots = context.get("memory") or journey.get("slots") or {}
    progress = journey.get("content_progress") or {}
    sent = set(journey.get("sent_content_groups") or [])
    topic = resolve_topic(route, context.get("customer_text", "")) if route else "general"
    if route and topic == "general":
        # A silence event is often triggered by a polite acknowledgement. Use
        # the latest substantive customer turn as the goal, not the acknowledgement.
        for message in reversed(context.get("context_messages") or []):
            if str(message.get("role") or message.get("direction") or "").casefold() in {"customer", "user", "incoming"}:
                candidate = resolve_topic(route, message.get("content", ""))
                if candidate != "general":
                    topic = candidate
                    break
    v2_state = (journey.get('slots') or slots).get('_v2_state') or {}
    unresolved = [q for q in v2_state.get('questions', []) if q.get('status') == 'pending']
    candidates = []
    delivered = set()
    if route:
        profile = route_profile(route)
        # Group keys and asset keys are not fact ids. Resolve confirmed text
        # delivery through the bound catalog before comparing candidate facts.
        spec = route_snapshot_from_values(route, journey.get("slots") or slots) or ROUTES.get(route, {})
        delivered_groups = set(journey.get("completed_content_groups") or []) | set(journey.get("topic_covered_groups") or [])
        delivered_groups.update(key for key, value in progress.items()
                                if not value.get("history_unknown")
                                and (value.get("text_delivered") or value.get("topic_covered")))
        delivered = {fact for group in delivered_groups
                     for fact in spec.get("groups", {}).get(group, {}).get("evidence", [])}
        delivered.update(v2_state.get('provided_fact_ids', {}).get(route, []))
        candidate_topics = profile.get("candidate_topics", {})
        candidates = [
            item for item in profile["followup_candidates"]
            if item not in delivered
        ]
        candidates.sort(key=lambda item: topic not in candidate_topics.get(item, ()))
        if not str(context.get('customer_text') or '').strip() and not context.get('context_messages') and not v2_state.get('events'):
            candidates = []
        # Do not let the first proactive photo overtake the requested itinerary.
        requested_itinerary = any(e.get('type') == 'material_requested'
            and e.get('material_kind') in {'itinerary', 'full_introduction'}
            and e.get('route_variant', route) == route for e in v2_state.get('events', []))
        if topic == "itinerary" or requested_itinerary:
            itinerary_progress = progress.get("itinerary_overview", {})
            required = set(spec.get("groups", {}).get("itinerary_overview", {}).get("assets", []))
            received = set(itinerary_progress.get("asset_keys", []))
            if not required or not required <= received or itinerary_progress.get("history_unknown"):
                candidates = []
    return {
        "route_variant": route,
        "stage": journey.get("stage") or "route_selection",
        "customer_facts": {
            key: value for key, value in slots.items()
            if not str(key).startswith("_")
        },
        "profile_provenance": journey.get("profile_provenance") or slots.get("_profile_meta", {}),
        "sent_content_groups": sorted(sent),
        "unresolved_topics": unresolved,
        "last_customer_topic": topic,
        "followup_candidates": candidates,
        "delivered_fact_ids": sorted(delivered),
        "last_group_key": journey.get("last_group_key"),
        "lead_capture": context.get("lead_capture") or {},
        'contact_preferences': v2_state,
    }
