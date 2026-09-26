"""Paid-model semantic check for the two routes. Never calls Chatwoot."""
from __future__ import annotations

import json
import sys
from dataclasses import asdict
from pathlib import Path

from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import SessionLocal
from app.decision_service import generate_decision
from app.deepseek_evaluation import EvaluationCallError
from app.material_library import candidate_materials
from app.models import Tenant
from app.route_reply import ROUTES, playbook_prompt


def _history(route_text: str) -> list[dict]:
    messages = [
        {"direction": "incoming", "content": route_text},
        {"direction": "outgoing", "content": "可以，我先了解您的人數與時間，再按路線介紹。"},
        {"direction": "incoming", "content": "我們兩位，預計3月底出發。"},
        {"direction": "outgoing", "content": "收到，已記錄兩位及3月底。"},
    ]
    for index in range(1, 9):
        messages.extend([
            {"direction": "incoming", "content": f"前面第{index}個問題只是確認行程內容，不改變兩位及3月底的需求。"},
            {"direction": "outgoing", "content": "了解，仍以您已提供的需求為準。"},
        ])
    return messages


SCENARIOS = [
    {
        "name": "ambiguous_route",
        "customer_text": "想了解明年的林芝桃花行程",
        "route_variant": "",
        "context_messages": [],
        "expect": {"action": "reply", "route_variant": ""},
    },
    {
        "name": "nine_day_intro",
        "customer_text": "我要9日不上珠峰，我們兩位，3月底出發，先看完整行程",
        "route_variant": "",
        "context_messages": [],
        "expect": {"action": "reply", "route_variant": "peach_9d_2027", "content_group_key": "itinerary_overview"},
    },
    {
        "name": "nine_day_long_context_hotel",
        "customer_text": "那住宿和房間條件怎麼樣？有圖片嗎？",
        "route_variant": "peach_9d_2027",
        "context_messages": _history("我想了解2027桃花9日、不上珠峰的路線。"),
        "expect": {"action": "reply", "route_variant": "peach_9d_2027", "content_group_key": "hotel_reference"},
    },
    {
        "name": "nine_day_price",
        "customer_text": "9日行程一個人價格多少？",
        "route_variant": "peach_9d_2027",
        "context_messages": _history("我想了解2027桃花9日、不上珠峰的路線。"),
        "expect": {"action": "reply", "route_variant": "peach_9d_2027", "intent": "price"},
    },
    {
        "name": "eleven_day_rongbuk",
        "customer_text": "11日去珠峰那晚住哪裡？想看住宿圖",
        "route_variant": "peach_11d_2027",
        "context_messages": _history("我確認要2027桃花加珠峰11日。"),
        "expect": {"action": "reply", "route_variant": "peach_11d_2027", "content_group_key": "rongbuk_reference"},
    },
    {
        "name": "health_boundary_reply",
        "customer_text": "家人75歲，可以保證辦到入藏函而且不會高反嗎？",
        "route_variant": "peach_11d_2027",
        "context_messages": _history("我確認要2027桃花加珠峰11日。"),
        "expect": {"action": "reply", "route_variant": "peach_11d_2027"},
    },
]


def run() -> dict:
    with SessionLocal() as db:
        tenant = db.scalar(select(Tenant).order_by(Tenant.id))
        materials = candidate_materials(db, tenant.id)
    results = []
    for item in SCENARIOS:
        route = item["route_variant"]
        context = {
            "module": "reply",
            "customer_text": item["customer_text"],
            "context_messages": item["context_messages"],
            "context_complete": True,
            "route_variant": route,
            "memory": {},
            "journey": {
                "route_variant": route,
                "stage": "introducing" if route else "route_selection",
                "slots": {},
                "sent_content_groups": ["itinerary_overview"] if route else [],
                "next_content_group": "peach_highlights" if route else None,
                "allowed_content_groups": list(ROUTES[route]["groups"]) if route else [],
            },
            "route_playbook": playbook_prompt(),
            "lead_capture": {"status": "not_started", "request_count": 0, "captured_kinds": []},
            "available_materials": materials,
        }
        try:
            decision, logs, digest, trace = generate_decision(context)
        except EvaluationCallError as exc:
            results.append({
                "name": item["name"],
                "passed": False,
                "checks": {"model_call": False},
                "decision": {"action": "handoff", "handoff_reason": "model_unavailable"},
                "approved_reply": None,
                "approved_assets": [],
                "trace": {"request_hash": exc.digest, "model_calls": exc.logs,
                          "context_messages": len(context["context_messages"]),
                          "context_characters": sum(len(x["content"]) for x in context["context_messages"])},
            })
            continue
        value = asdict(decision)
        expected = item["expect"]
        checks = {key: value.get(key) == expected_value for key, expected_value in expected.items()}
        group = value.get("content_group_key")
        final = (ROUTES.get(value.get("route_variant"), {}).get("groups", {}).get(group, {})
                 if value.get("action") == "reply" else {})
        results.append({
            "name": item["name"],
            "passed": all(checks.values()),
            "checks": checks,
            "decision": value,
            "approved_reply": final.get("text") or value.get("reply"),
            "approved_assets": final.get("assets", []),
            "trace": {
                "context_messages": trace["context_messages"],
                "context_characters": trace["context_characters"],
                "model_ms": trace["model_ms"],
                "request_count": trace["request_count"],
                "input_tokens": trace["input_tokens"],
                "output_tokens": trace["output_tokens"],
                "request_hash": digest,
                "model_calls": logs,
            },
        })
    return {
        "outbound_requests": 0,
        "chatwoot_requests": 0,
        "total": len(results),
        "passed": sum(row["passed"] for row in results),
        "results": results,
    }


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    report = run()
    destination = Path(__file__).resolve().parents[2] / "output/deepseek/routes-1-2-verification.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "report": str(destination),
        "total": report["total"],
        "passed": report["passed"],
        "outbound_requests": report["outbound_requests"],
    }, ensure_ascii=False))
