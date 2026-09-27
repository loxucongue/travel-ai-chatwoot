from __future__ import annotations

from functools import lru_cache

from app.decision_knowledge import FACTS, KNOWLEDGE_KEY
from app.route_packages import ROUTES
from app.reception_v2.skill_registry import SkillRegistry
from app.reception_v2.route_profiles import route_profile, resolve_topic
from app.reception_v2.route_agent_tools import (
    compare_routes,
    get_route_details,
    get_route_material_packet,
    search_routes,
)


TOOL_NAMES = {
    "load_skill", "get_route_catalog", "get_route_capabilities", "get_route_facts",
    "get_route_materials", "get_service_facts", "search_routes", "compare_routes",
    "get_route_details", "get_route_material_packet",
}

TOPIC_FACTS = (
    (("集合", "接機", "接机", "arrival", "meeting"), ("service.peach_arrival",)),
    (("成都", "入藏函", "permit"), ("service.peach_permit",)),
    (("青藏", "青鐵", "青铁", "鐵路", "铁路", "火車", "火车", "rail"), ("service.peach_rail",)),
    (("歲", "岁", "年齡", "年龄", "長輩", "长辈", "健康證明", "健康证明", "age"), ("service.peach_age", "service.safety")),
    (("價格", "价格", "多少錢", "多少钱", "報價", "报价", "price", "費用", "费用"), (".price", ".scope", "multi_person", "discount", "group_offer", "service.availability")),
    (("包含", "含什麼", "含什么", "不含", "自費", "自费", "include", "exclude"), (".scope", ".price")),
    (("出發", "出发", "團期", "团期", "日期", "departure"), (".departure", "service.availability")),
    (("住宿", "飯店", "酒店", "hotel"), ("hotel_reference", ".scope")),
    (("車", "车", "交通", "接送", "vehicle", "transport"), ("vehicle_reference", ".scope")),
    (("氧氣", "氧气", "供氧", "高反", "高原反應", "高原反应", "oxygen", "altitude"), ("medical_support", "medical_logistics", "altitude_response", "service.safety")),
    (("行程", "景點", "景点", "路線", "路线", "itinerary"), (".overview", ".days", "landmarks")),
)


@lru_cache(maxsize=1)
def tool_specs() -> list[dict]:
    return [
        {"type":"function","function":{"name":"get_service_facts","description":"Read one published service-knowledge module from the server-provided index. Resolve pronouns using the conversation, then select its module_key. Never fetch websites or infer unpublished service terms.","parameters":{"type":"object","properties":{"module_key":{"type":"string"}},"required":["module_key"],"additionalProperties":False}}},
        {"type": "function", "function": {"name": "load_skill", "description": "Load the instructions for one relevant reception skill. Use only a name listed in the system skill index.", "parameters": {"type": "object", "properties": {"name": {"type": "string", "enum": [item["name"] for item in SkillRegistry().index()]}}, "required": ["name"], "additionalProperties": False}}},
        {"type": "function", "function": {"name": "get_route_catalog", "description": "List the supported route products and their stable ids.", "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}},
        {"type": "function", "function": {"name": "search_routes", "description": "Find a shortlist of published routes from the customer's stated constraints. Use all stated constraints; this returns route candidates and evidence refs, not a final reply.", "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "filters": {"type": "object", "properties": {"days": {"type": "integer"}, "min_days": {"type": "integer"}, "max_days": {"type": "integer"}, "includes_everest": {"type": "boolean"}, "max_price": {"type": "integer"}}, "additionalProperties": False}}, "required": ["query"], "additionalProperties": False}}},
        {"type": "function", "function": {"name": "compare_routes", "description": "Compare two or more published routes on the customer's requested dimensions. Values are backed by route package evidence.", "parameters": {"type": "object", "properties": {"route_ids": {"type": "array", "items": {"type": "string"}}, "criteria": {"type": "array", "items": {"type": "string"}}}, "required": ["route_ids"], "additionalProperties": False}}},
        {"type": "function", "function": {"name": "get_route_details", "description": "Read the approved details for one selected route and the requested topics.", "parameters": {"type": "object", "properties": {"route_variant": {"type": "string"}, "topics": {"type": "array", "items": {"type": "string"}}}, "required": ["route_variant", "topics"], "additionalProperties": False}}},
        {"type": "function", "function": {"name": "get_route_capabilities", "description": "Read the selected route's reviewed topics, human-check boundaries and follow-up candidates.", "parameters": {"type": "object", "properties": {"route_variant": {"type": "string"}}, "required": ["route_variant"], "additionalProperties": False}}},
        {"type": "function", "function": {"name": "get_route_facts", "description": "Read approved facts for a route and customer topic. Use before stating prices, itinerary, accommodation, transport, oxygen, dates or inclusions.", "parameters": {"type": "object", "properties": {"route_variant": {"type": "string"}, "topic": {"type": "string"}}, "required": ["route_variant", "topic"], "additionalProperties": False}}},
        {"type": "function", "function": {"name": "get_route_material_packet", "description": "List approved materials for a selected route and topics without sending them.", "parameters": {"type": "object", "properties": {"route_variant": {"type": "string"}, "topics": {"type": "array", "items": {"type": "string"}}}, "required": ["route_variant", "topics"], "additionalProperties": False}}},
        {"type": "function", "function": {"name": "get_route_materials", "description": "List approved material ids relevant to a route and topic. This does not send anything.", "parameters": {"type": "object", "properties": {"route_variant": {"type": "string"}, "topic": {"type": "string"}}, "required": ["route_variant", "topic"], "additionalProperties": False}}},
    ]


def execute_tool(name: str, arguments: dict, registry: SkillRegistry, *, context: dict | None = None) -> dict:
    if name == "load_skill":
        return registry.load(str(arguments.get("name") or ""))
    if name == "get_route_catalog":
        return {"routes": [{"route_variant": key, "name": item["name"], "selection_title": item["selection_title"], "branch": item["branch"]} for key, item in ROUTES.items()]}
    if name == "search_routes":
        return search_routes(str(arguments.get("query") or ""), arguments.get("filters"))
    if name == "compare_routes":
        return compare_routes(arguments.get("route_ids") or [], arguments.get("criteria"))
    if name == "get_route_details":
        return get_route_details(str(arguments.get("route_variant") or ""), arguments.get("topics") or [])
    if name == "get_route_material_packet":
        return get_route_material_packet(str(arguments.get("route_variant") or ""), arguments.get("topics") or [])
    if name == 'get_service_facts':
        from app.web_knowledge import context_fact_map
        supplied = context or {}
        pool = context_fact_map({'global_knowledge_facts': supplied.get('global_knowledge_candidates')
                                 or supplied.get('global_knowledge_facts', [])})
        key = str(arguments.get('module_key') or '')
        facts = [fact for fact in pool.values() if key and fact.get('module_key') == key]
        if not facts:
            raise ValueError('v2_service_module_unavailable')
        return {'module_key':key, 'facts':facts}
    route_id = str(arguments.get("route_variant") or "")
    if route_id not in ROUTES:
        raise ValueError("v2_route_not_found")
    topic = str(arguments.get("topic") or "").casefold()
    route = ROUTES[route_id]
    if name == "get_route_capabilities":
        profile = route_profile(route_id)
        return {
            "route_variant": route_id,
            "skill": profile["skill"],
            "topics": sorted(profile["topics"]),
            "human_check": list(profile["human_check"]),
            "followup_candidates": list(profile["followup_candidates"]),
            "candidate_topics": profile.get("candidate_topics", {}),
        }
    if name == "get_route_facts":
        candidates = [fact for fact in FACTS if not fact.get("branches") or route["branch"] in fact.get("branches", [])]
        tokens = {value for value in re_split(topic) if len(value) >= 2}
        structured_topic = resolve_topic(route_id, topic)
        preferred = {
            fact_id for aliases, ids in TOPIC_FACTS if any(alias in topic for alias in aliases)
            for fact_id in ids
        }
        topic_ids = {
            "arrival": ("service.peach_arrival",),
            "permit": ("service.peach_permit",),
            "rail": ("service.peach_rail",),
            "age": ("service.peach_age", "service.safety"),
            "price": (".price", ".scope", "multi_person", "discount", "group_offer", "service.availability"),
            "departure": (".departure", "service.availability"),
            "hotel": ("hotel_reference", ".scope"),
            "transport": ("vehicle_reference", ".scope"),
            "oxygen": ("medical_support", "medical_logistics", "altitude_response", "service.safety"),
            "itinerary": (".overview", ".days", "landmarks"),
        }
        preferred.update(topic_ids.get(structured_topic, ()))
        ranked = sorted(candidates, key=lambda fact: (
            sum(marker in fact["id"] for marker in preferred),
            sum(token in (fact["id"] + fact["text"]).casefold() for token in tokens),
            fact["id"],
        ), reverse=True)
        # Do not pad a targeted answer with unrelated facts that invite digression.
        matched = [fact for fact in ranked if any(marker in fact["id"] for marker in preferred)]
        selected = matched[:12] if matched else (ranked[:12] if tokens else ranked[:8])
        return {"route_variant": route_id, "knowledge_version": KNOWLEDGE_KEY, "facts": [{"id": item["id"], "text": item["text"], "source": item.get("source", ""), "branches": item.get("branches", [])} for item in selected]}
    if name == "get_route_materials":
        groups = route.get("groups", {})
        tokens = {value for value in re_split(topic) if len(value) >= 2}
        result = []
        ordered = sorted(groups.items(), key=lambda pair: sum(token in
            (pair[0] + ' ' + str(pair[1].get('purpose') or '') + ' ' + str(pair[1].get('text') or '')).casefold()
            for token in tokens), reverse=True)
        for group_key, group in ordered:
            if topic in {"itinerary", "行程", "行程總覽", "行程总览"} and group_key != "itinerary_overview":
                continue
            for key in group.get("assets", []):
                if key not in {item["key"] for item in result}:
                    result.append({"key": key, "content_group_key": group_key, "purpose": group.get("purpose", "")})
        return {"route_variant": route_id, "materials": result}
    raise ValueError("v2_tool_not_allowed")


def re_split(value: str) -> list[str]:
    import re
    return [item for item in re.split(r"[^\w\u4e00-\u9fff]+", value) if item]
