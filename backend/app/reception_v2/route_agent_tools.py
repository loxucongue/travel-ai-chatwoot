"""High-level, deterministic route capabilities for the reception agent.

The model decides which capability is useful. This module only reads the
published route catalog and returns evidence-backed data; it does not choose a
sales stage, send material, or perform a handoff.
"""
from __future__ import annotations

import re
from typing import Any

from app.route_packages import ROUTES


_TOPIC_GROUPS = {
    "itinerary": ("itinerary_overview", "route_scope", "landmarks"),
    "hotel": ("hotel_reference", "accommodation_summary"),
    "vehicle": ("vehicle_reference",),
    "transport": ("vehicle_reference",),
    "oxygen": ("altitude_health", "hotel_reference", "vehicle_reference"),
    "price": ("price_reference", "price_deferral"),
    "departure": ("departure_reference",),
    "highlights": ("peach_highlights", "landmarks", "zhaji"),
    "scope": ("route_scope",),
}


def _days(route_id: str) -> int | None:
    match = re.search(r"(?:^|_)(\d+)d(?:_|$)", route_id)
    return int(match.group(1)) if match else None


def _route_text(route_id: str, route: dict) -> str:
    groups = route.get("groups", {})
    pieces = [route_id, route.get("name", ""), route.get("selection_title", ""),
              *route.get("match_keywords", [])]
    pieces.extend(str(group.get("text") or "") for group in groups.values())
    pieces.extend(str(fact.get("id") or "") for fact in route.get("knowledge_facts", []))
    return " ".join(pieces).casefold()


def _price(route: dict) -> int | None:
    for fact in route.get("knowledge_facts", []):
        if ".price" not in str(fact.get("id") or ""):
            continue
        values = [int(value.replace(",", "")) for value in re.findall(r"(?<!\d)\d[\d,]{3,}(?!\d)", str(fact.get("text") or ""))]
        if values:
            return values[0]
    return None


def _evidence_for_groups(route: dict, group_keys: list[str]) -> list[str]:
    refs: list[str] = []
    for key in group_keys:
        refs.extend(str(ref) for ref in route.get("groups", {}).get(key, {}).get("evidence", []))
    return list(dict.fromkeys(refs))


def _route_summary(route_id: str, route: dict) -> dict:
    days = _days(route_id)
    return {
        "route_variant": route_id,
        "name": route.get("name", ""),
        "selection_title": route.get("selection_title", ""),
        "branch": route.get("branch", ""),
        "days": days,
        "includes_everest": "11d" in route_id,
        "required_slots": list(route.get("required_slots", [])),
        "price_per_person": _price(route),
    }


def search_routes(query: str = "", filters: dict | None = None) -> dict:
    """Return a ranked shortlist without inventing route facts."""
    filters = filters if isinstance(filters, dict) else {}
    query_text = str(query or "").strip().casefold()
    scored: list[tuple[int, str, dict, list[str]]] = []
    for route_id, route in ROUTES.items():
        summary = _route_summary(route_id, route)
        days = summary["days"]
        if filters.get("days") is not None and days != int(filters["days"]):
            continue
        if filters.get("min_days") is not None and (days is None or days < int(filters["min_days"])):
            continue
        if filters.get("max_days") is not None and (days is None or days > int(filters["max_days"])):
            continue
        if filters.get("includes_everest") is not None and summary["includes_everest"] != bool(filters["includes_everest"]):
            continue
        if filters.get("max_price") is not None and summary["price_per_person"] is not None and summary["price_per_person"] > int(filters["max_price"]):
            continue

        searchable = _route_text(route_id, route)
        reasons: list[str] = []
        score = 0
        if query_text:
            if query_text in searchable:
                score += 5
                reasons.append("query_match")
            for token in re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]", query_text):
                if len(token) > 1 and token in searchable:
                    score += 1
            # A natural-language query often contains constraints that are not
            # literal catalog keywords (for example, "不要太累"). Keep those
            # candidates and let the returned route facts support the agent's
            # comparison instead of returning an empty shortlist.
        if filters.get("days") is not None:
            reasons.append("days")
            score += 3
        if filters.get("includes_everest") is not None:
            reasons.append("everest_preference")
            score += 3
        if filters.get("max_price") is not None:
            reasons.append("budget")
            score += 2
        if not reasons:
            reasons.append("catalog")
        scored.append((score, route_id, summary, reasons))

    scored.sort(key=lambda item: (-item[0], item[1]))
    return {
        "query": query,
        "filters": filters,
        "routes": [
            {**summary, "score": score, "match_reasons": reasons,
             "evidence_refs": _evidence_for_groups(route := ROUTES[route_id], ["route_scope", "itinerary_overview"]) }
            for score, route_id, summary, reasons in scored[:6]
        ],
    }


def compare_routes(route_ids: list[str], criteria: list[str] | None = None) -> dict:
    ids = list(dict.fromkeys(str(route_id) for route_id in (route_ids or [])))
    if not ids:
        ids = list(ROUTES)
    if len(ids) > 4:
        raise ValueError("v2_route_comparison_too_many_routes")
    unknown = [route_id for route_id in ids if route_id not in ROUTES]
    if unknown:
        raise ValueError("v2_route_not_found:" + ",".join(unknown))
    dimensions = list(dict.fromkeys(str(item) for item in (criteria or ["duration", "pace", "hotel", "price", "highlights"])))
    rows = []
    for route_id in ids:
        route = ROUTES[route_id]
        values: dict[str, dict[str, Any]] = {}
        for criterion in dimensions:
            key = {
                "duration": None, "days": None, "pace": "itinerary_overview",
                "hotel": "hotel_reference", "price": "price_reference",
                "highlights": "peach_highlights", "itinerary": "itinerary_overview",
                "departure": "departure_reference", "vehicle": "vehicle_reference",
                "oxygen": "altitude_health",
            }.get(criterion, criterion)
            if key is None:
                values[criterion] = {"value": f"{_days(route_id)}日", "evidence_refs": []}
                continue
            group = route.get("groups", {}).get(key)
            if group:
                values[criterion] = {
                    "value": group.get("text", ""),
                    "evidence_refs": list(group.get("evidence", [])),
                    "material_keys": list(group.get("assets", [])),
                }
            else:
                values[criterion] = {"value": "", "evidence_refs": []}
        rows.append({**_route_summary(route_id, route), "dimensions": values})
    return {"route_ids": ids, "criteria": dimensions, "routes": rows}


def get_route_details(route_id: str, topics: list[str] | None = None) -> dict:
    if route_id not in ROUTES:
        raise ValueError("v2_route_not_found")
    route = ROUTES[route_id]
    requested = list(dict.fromkeys(str(topic).casefold() for topic in (topics or ["itinerary", "hotel", "vehicle", "price", "departure"])))
    details = []
    for topic in requested:
        group_keys = _TOPIC_GROUPS.get(topic, (topic,))
        groups = [route.get("groups", {}).get(key) for key in group_keys]
        groups = [group for group in groups if group]
        if not groups:
            continue
        details.append({
            "topic": topic,
            "text": "\n".join(str(group.get("text") or "") for group in groups if group.get("text")),
            "purpose": "\n".join(str(group.get("purpose") or "") for group in groups if group.get("purpose")),
            "evidence_refs": list(dict.fromkeys(ref for group in groups for ref in group.get("evidence", []))),
            "material_keys": list(dict.fromkeys(asset for group in groups for asset in group.get("assets", []))),
        })
    return {**_route_summary(route_id, route), "topics": requested, "details": details}


def get_route_material_packet(route_id: str, topics: list[str] | None = None) -> dict:
    details = get_route_details(route_id, topics)
    return {
        "route_variant": route_id,
        "topics": details["topics"],
        "materials": [
            {"key": key, "topic": detail["topic"], "evidence_refs": detail["evidence_refs"]}
            for detail in details["details"] for key in detail["material_keys"]
        ],
    }
