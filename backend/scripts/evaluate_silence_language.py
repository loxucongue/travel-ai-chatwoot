"""Run outbound-disabled DeepSeek checks for human-style silence follow-ups."""
from __future__ import annotations

import argparse
import json
from copy import deepcopy
from datetime import datetime
from pathlib import Path

from app.decision_service import generate_decision
from app.reception_config import default_reception_configuration
from app.route_packages import JOURNEY_POLICY, ROUTES
from app.route_reply import playbook_prompt


ROOT = Path(__file__).resolve().parents[2]
FORBIDDEN_GENERIC = ("行程看了嗎", "行程還滿意嗎", "有需要嗎", "看到您一直沒有回覆")
FORBIDDEN_INTERNAL = ("AI", "模型", "提示词", "提示詞", "事实ID", "事實ID", "校验器", "校驗器")
UNSUPPORTED_DERIVATIONS = (
    "休息会舒适很多", "休息會舒適很多", "休息更有保障", "休息更有保障",
    "适合初次进藏", "適合初次進藏", "更安全", "手续不复杂", "手續不複雜",
    "手续很简单", "手續很簡單", "不用太担心", "不用太擔心", "舒服很多",
)
ASSET_NARRATIVES = {
    "itinerary_overview": {
        "what_it_shows": "完整行程長圖",
        "feature_points": ["每日行程順序", "主要停留地點"],
        "customer_value": "讓客戶掌握整段行程怎麼走",
        "recommended_caption": "完整行程圖放在這裡，每天的順序和停留點都標清楚了。",
    },
    "peach_highlights": {
        "what_it_shows": "桃花與沿線景點照片",
        "feature_points": ["桃花景觀", "沿線人文景點"],
        "customer_value": "讓客戶比較自然景觀與人文安排",
        "recommended_caption": "這組是桃花和沿線景點，畫面會比文字更容易看出差異。",
    },
    "hotel_reference": {
        "what_it_shows": "飯店客房與房內設備照片",
        "feature_points": ["實際客房空間", "床鋪與房內供氧設備"],
        "customer_value": "讓客戶確認住宿環境和設備配置",
        "recommended_caption": "這組是飯店客房和房內設備照片，實際空間和床鋪都能先看清楚。",
    },
    "vehicle_reference": {
        "what_it_shows": "行程用車與車內設備照片",
        "feature_points": ["航空座椅", "車內空間與供氧設備"],
        "customer_value": "讓客戶看清楚多日行程使用的車型與設備",
        "recommended_caption": "行程用車照片放在這裡，座椅和車內設備都能直接看到。",
    },
    "rongbuk_reference": {
        "what_it_shows": "珠峰段絨布旅館房間照片",
        "feature_points": ["房間實況", "獨立衛浴與供氧設備"],
        "customer_value": "讓客戶確認珠峰段的住宿安排",
        "recommended_caption": "這張是珠峰段入住的絨布旅館，房間實況可以先看一下。",
    },
}


def policy() -> dict:
    config = default_reception_configuration()
    value = deepcopy(JOURNEY_POLICY)
    value["operator_configuration"] = {
        "business_goal": config["reply"]["goal"],
        "tone_guidance": config["reply"].get("tone_guidance", ""),
        "custom_guidance": config["reply"]["custom_guidance"],
        "profile_fields": config["profile_fields"],
        "lead_capture": config["lead_capture"],
        "business_rules": config["business_rules"],
        "stage_journey": config["stage_journey"],
    }
    return value


def route_assets(route_variant: str) -> list[dict]:
    result: list[dict] = []
    for group_key, group in ROUTES.get(route_variant, {}).get("groups", {}).items():
        narrative = ASSET_NARRATIVES.get(group_key)
        for key in group.get("assets", []):
            item = {"key": key, "route_variant": route_variant, "topic": group_key}
            if narrative:
                item.update(narrative)
                item["avoid_claims"] = ["保證不會高反", "一定更安全", "一定更舒適"]
            result.append(item)
    return result


def cases() -> list[dict]:
    return [
        {
            "case_id": "route_choice_touch_1",
            "touch_index": 1,
            "customer_text": "你好，我想了解西藏桃花行程",
            "route_variant": "",
            "stage": "route_selection",
            "memory": {"destination": "西藏桃花"},
            "sent": [],
            "lead": "not_started",
            "history": [
                {"direction": "incoming", "content": "你好，我想了解西藏桃花行程"},
                {"direction": "outgoing", "content": "目前有桃花9日和桃花加珠峰11日两条路线。"},
            ],
        },
        {
            "case_id": "need_discovery_touch_2",
            "touch_index": 2,
            "customer_text": "我想先看桃花9日",
            "route_variant": "peach_9d_2027",
            "stage": "needs_discovery",
            "memory": {"destination": "桃花9日"},
            "sent": ["itinerary_overview", "party_question"],
            "lead": "not_started",
            "history": [
                {"direction": "incoming", "content": "我想先看桃花9日"},
                {"direction": "outgoing", "content": "9日路线不上珠峰，我先给您看行程总览。请问预计几位同行？"},
            ],
        },
        {
            "case_id": "first_tibet_objection_touch_3",
            "touch_index": 3,
            "customer_text": "我们2位，明年3月底，第一次去西藏，手续不太清楚",
            "route_variant": "peach_9d_2027",
            "stage": "objection_handling",
            "memory": {
                "destination": "桃花9日", "party_size": "2位", "departure_window": "明年3月底",
                "first_time_tibet": True, "permit_awareness": "不太清楚",
            },
            "sent": ["itinerary_overview", "entry_question", "departure_question"],
            "lead": "not_started",
            "history": [
                {"direction": "incoming", "content": "我们2位，明年3月底，第一次去西藏，手续不太清楚"},
                {"direction": "outgoing", "content": "第一次进藏可以先把入藏手续和高原适应分开了解，我先说明可确认的手续范围。"},
            ],
        },
        {
            "case_id": "family_considering_touch_4",
            "touch_index": 4,
            "customer_text": "目前还在跟家人讨论，谢谢你",
            "route_variant": "peach_9d_2027",
            "stage": "considering",
            "memory": {
                "destination": "桃花9日", "party_size": "2位", "departure_window": "明年3月底",
                "first_time_tibet": True,
            },
            "sent": ["itinerary_overview", "peach_highlights", "entry_question", "departure_question"],
            "lead": "not_started",
            "history": [
                {"direction": "incoming", "content": "第一次去西藏，手续不太清楚"},
                {"direction": "outgoing", "content": "第一次进藏可以先把手续和高原适应分开了解。"},
                {"direction": "incoming", "content": "目前还在跟家人讨论，谢谢你"},
                {"direction": "outgoing", "content": "好的，您先和家人讨论，有想确认的细节再告诉我。"},
            ],
        },
        {
            "case_id": "high_intent_contact_touch_5",
            "touch_index": 5,
            "customer_text": "我们2位，明年3月底出发，住宿和价格怎样？",
            "route_variant": "peach_11d_2027",
            "stage": "contact_ready",
            "memory": {"destination": "桃花加珠峰11日", "party_size": "2位", "departure_window": "明年3月底"},
            "sent": ["itinerary_overview", "hotel_reference", "price_reference", "entry_question", "departure_question"],
            "lead": "not_started",
            "history": [
                {"direction": "incoming", "content": "我们2位，明年3月底出发，住宿和价格怎样？"},
                {"direction": "outgoing", "content": "11日参考价和住宿范围我已经给您说明，最终安排需要按团期核对。"},
            ],
        },
        {
            "case_id": "contact_reminder_touch_6",
            "touch_index": 6,
            "customer_text": "好的，我先看看",
            "route_variant": "peach_11d_2027",
            "stage": "contact_requested",
            "memory": {"destination": "桃花加珠峰11日", "party_size": "2位", "departure_window": "明年3月底"},
            "sent": [
                "itinerary_overview", "hotel_reference", "price_reference", "entry_question",
                "departure_question", "contact_request",
            ],
            "lead": "asked",
            "history": [
                {"direction": "incoming", "content": "好的，我先看看"},
                {"direction": "outgoing", "content": "您先参考；方便留一个LINE ID，让顾问按日期核对团期吗？"},
            ],
        },
    ]


def run_case(item: dict) -> dict:
    route_variant = item["route_variant"]
    decision, calls, _digest, trace = generate_decision({
        "module": "silence_touch",
        "customer_text": item["customer_text"],
        "context_messages": item["history"],
        "context_complete": True,
        "route_variant": route_variant,
        "memory": item["memory"],
        "journey": {
            "route_variant": route_variant,
            "stage": item["stage"],
            "slots": item["memory"],
            "sent_content_groups": item["sent"],
            "next_content_group": "",
        },
        "route_playbook": playbook_prompt(),
        "reception_policy": policy(),
        "lead_capture": {"status": item["lead"], "request_count": int(item["lead"] == "asked")},
        "available_materials": route_assets(route_variant),
        "touch_index": item["touch_index"],
        "evaluation_at": datetime.now().astimezone().isoformat(),
    })
    generic = [phrase for phrase in FORBIDDEN_GENERIC if phrase in decision.reply]
    internal = [phrase for phrase in FORBIDDEN_INTERNAL if phrase in decision.reply]
    unsupported_derivations = [phrase for phrase in UNSUPPORTED_DERIVATIONS if phrase in decision.reply]
    asks_known_departure = bool(item["memory"].get("departure_window")) and any(
        phrase in decision.reply for phrase in ("幾月出發", "几月出发", "什麼時候出發", "什么时候出发", "計劃幾月", "计划几月")
    )
    repeats_unanswered_party_question = (
        item["case_id"] == "need_discovery_touch_2"
        and any(phrase in decision.reply for phrase in ("幾位同行", "几位同行", "多少位", "多少人"))
    )
    invents_first_tibet = (
        "first_time_tibet" not in item["memory"]
        and any(phrase in decision.reply for phrase in ("第一次進藏", "第一次进藏", "第一次去西藏"))
    )
    checks = {
        "must_send": decision.action in {"reply", "handoff"} and bool(decision.reply.strip()),
        "touch_goal_present": bool(decision.touch_goal),
        "not_generic_checkin": not generic,
        "no_internal_system_wording": not internal,
        "no_unsupported_benefit_derivation": not unsupported_derivations,
        "not_exact_repeat": decision.reply.strip() != item["history"][-1]["content"].strip(),
        "does_not_reask_known_departure": not asks_known_departure,
        "does_not_repeat_unanswered_party_question": not repeats_unanswered_party_question,
        "does_not_invent_first_tibet": not invents_first_tibet,
        "considering_not_premature_contact": item["case_id"] != "family_considering_touch_4" or decision.lead_action == "none",
        "contact_reminder_not_recaptured": item["case_id"] != "contact_reminder_touch_6" or decision.lead_action != "captured",
    }
    return {
        "case_id": item["case_id"],
        "passed": all(checks.values()),
        "checks": checks,
        "action": decision.action,
        "touch_goal": decision.touch_goal,
        "touch_reason": decision.touch_reason,
        "journey_stage": decision.journey_stage,
        "reply": decision.reply,
        "lead_action": decision.lead_action,
        "content_group_key": decision.content_group_key,
        "covered_content_groups": decision.covered_content_groups,
        "material_keys": decision.material_keys,
        "model_attempts": len(calls),
        "trace": trace,
        "outbound": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "output" / "evaluation" / "silence-language-split-v1.json",
    )
    parser.add_argument("--case-id")
    args = parser.parse_args()
    selected = [item for item in cases() if not args.case_id or item["case_id"] == args.case_id]
    if not selected:
        raise SystemExit(f"unknown_case_id:{args.case_id}")
    results = [run_case(item) for item in selected]
    report = {
        "prompt_version": results[0]["trace"]["prompt_version"] if results else "",
        "outbound": False,
        "summary": {"total": len(results), "passed": sum(item["passed"] for item in results)},
        "results": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["summary"], ensure_ascii=False))
    for result in results:
        print(f"[{result['case_id']}] {result['touch_goal']}: {result['reply']}")
    return 0 if report["summary"]["passed"] == report["summary"]["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
