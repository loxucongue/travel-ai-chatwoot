from app.deepseek_evaluation import EvaluationDecision
from app.reception_v2.route_agent_tools import (
    compare_routes,
    get_route_details,
    get_route_material_packet,
    search_routes,
)
from app.reception_v2.tools import execute_tool, tool_specs
from app.reception_v2.skill_registry import SkillRegistry
from app.reception_v2.runtime import _validated_decision
import json


def test_search_routes_returns_published_shortlist_and_filters():
    result = search_routes("兩個人，不想太累", {"includes_everest": False})
    assert [item["route_variant"] for item in result["routes"]] == ["peach_9d_2027"]
    assert result["routes"][0]["price_per_person"] == 9980
    assert "route.9.overview" in result["routes"][0]["evidence_refs"]


def test_compare_routes_is_structured_and_evidence_backed():
    result = compare_routes(["peach_9d_2027", "peach_11d_2027"], ["duration", "price", "hotel"])
    assert result["routes"][0]["dimensions"]["duration"]["value"] == "9日"
    assert result["routes"][1]["dimensions"]["duration"]["value"] == "11日"
    assert "route.9.price" in result["routes"][0]["dimensions"]["price"]["evidence_refs"]
    assert "route.shared.hotel_reference" in result["routes"][1]["dimensions"]["hotel"]["evidence_refs"]


def test_details_and_material_packet_keep_ordered_route_assets():
    details = get_route_details("peach_9d_2027", ["itinerary"])
    assert details["details"][0]["material_keys"][0] == "routes12-9d-itinerary"
    packet = get_route_material_packet("peach_9d_2027", ["itinerary"])
    assert packet["materials"][0]["key"] == "routes12-9d-itinerary"


def test_high_level_tools_are_exposed_to_agent():
    names = {item["function"]["name"] for item in tool_specs()}
    assert {"search_routes", "compare_routes", "get_route_details", "get_route_material_packet"} <= names
    result = execute_tool("search_routes", {"query": "桃花", "filters": {}}, SkillRegistry())
    assert result["routes"]


def test_presentations_are_part_of_decision_contract():
    decision = EvaluationDecision.parse({
        "action": "reply", "branch": "peach_9d", "intent": "other",
        "reply": "我先幫您比較兩條路線。", "route_variant": "",
        "evidence_refs": ["route.9.overview", "route.11.overview"],
        "presentations": [{
            "type": "route_comparison",
            "route_ids": ["peach_9d_2027", "peach_11d_2027"],
            "criteria": ["duration", "price"],
        }],
    })
    assert decision.presentations[0]["type"] == "route_comparison"


def test_runtime_keeps_only_grounded_presentation_refs():
    message = {
        "content": json.dumps({
            "action": "reply", "branch": "unclassified", "intent": "other",
            "reply": "我先幫您比較兩條路線。", "route_variant": "",
            "evidence_refs": ["route.9.overview", "route.11.overview"],
            "v2_events": [],
            "presentations": [{
                "type": "route_comparison",
                "route_ids": ["peach_9d_2027", "peach_11d_2027"],
                "criteria": ["duration"],
                "recommendation": "peach_9d_2027",
                "evidence_refs": ["route.9.overview", "unknown.fact"],
                "material_keys": ["unknown-image"],
            }],
        }, ensure_ascii=False),
    }
    decision = _validated_decision(
        message,
        {"route.9.overview", "route.11.overview"},
        {"routes12-9d-itinerary"},
        {"customer_text": "9日和11日怎麼選"},
    )
    presentation = decision.presentations[0]
    assert presentation["evidence_refs"] == ["route.9.overview", "route.11.overview"]
    assert presentation["material_keys"] == []
    assert [item["route_variant"] for item in presentation["routes"]] == ["peach_9d_2027", "peach_11d_2027"]
    assert presentation["routes"][0]["dimensions"]["duration"]["value"].startswith("9")
