"""Evaluate customer-visible generation against a Taiwan advisor voice rubric.

The script calls only the isolated reply and silence wording nodes. It never
creates conversations, outbound rows, Chatwoot tasks, or live messages.
"""
from __future__ import annotations

import argparse
import json
import re
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path

from app.advisor_voice import taiwan_copy_violation
from app.reception_config import default_reception_configuration
from app.reply_generation import call_reply_generator
from app.reply_planning import FollowUp, ReplyPlan
from app.route_packages import JOURNEY_POLICY, ROUTES
from app.silence_generation import call_silence_generator
from app.silence_planning import SilencePlan


ROOT = Path(__file__).resolve().parents[2]
INTERNAL_WORDS = (
    "AI", "機器人", "机器人", "模型", "提示詞", "提示词", "事實ID", "事实ID",
    "校驗器", "校验器", "系統判斷", "系统判断", "當前可接待", "当前可接待",
)
STIFF_PHRASES = (
    "補充一個重點", "补充一个重点", "資料顯示", "资料显示", "頁面展示", "页面展示",
    "僅供參考", "仅供参考", "方便您了解", "方便您比較", "一目了然", "匹配方案",
    "出行方案", "進一步為您", "进一步为您", "這邊為您", "这边为您",
    "很多客人", "大多數客人", "大家通常",
)
EMPTY_VALUE_PHRASES = (
    "品質很好", "质量很好", "很有品質", "非常優質", "非常优质", "高品質體驗",
)
SOFT_MARKER = re.compile(r"[～~]|(?:😊|☺️|❤️|✨|😄|😆|喔|唷|呀|啦|呢)(?=[。！!，,]|$)")
SIMPLIFIED_RESIDUE = re.compile(r"(?:帐篷|酒店|线路|联系|出发|费用|这边|我们|给您|发您|可以看到)")
UNSUPPORTED_BENEFIT = re.compile(r"(?:好好休息|休息有保障|更安心|更放心|更安全|不會高反|不会高反)")
FORMULA_FRAGMENTS = ("我先把", "可以呀", "沒關係", "可以直接看到", "方便您")


def _policy() -> dict:
    config = default_reception_configuration()
    policy = deepcopy(JOURNEY_POLICY)
    policy["route_switch"]["allowed_routes"] = list(ROUTES)
    policy["operator_configuration"] = {
        "business_goal": config["reply"]["goal"],
        "tone_guidance": config["reply"].get("tone_guidance", ""),
        "custom_guidance": "",
        "profile_fields": config["profile_fields"],
        "lead_capture": config["lead_capture"],
        "business_rules": config["business_rules"],
        "stage_journey": config["stage_journey"],
    }
    return policy


def _asset(route: str, group: str) -> dict | None:
    keys = ROUTES[route]["groups"][group].get("assets") or []
    if not keys:
        return None
    narratives = {
        "itinerary_overview": (
            "完整行程長圖", ["每日行程順序", "主要停留地點"],
            "讓客戶先掌握整段行程怎麼走", "我把完整行程圖放上來，您可以先順著每天的安排看。",
        ),
        "peach_highlights": (
            "桃花與沿線景點照片", ["桃花景觀", "沿線人文景點"],
            "讓客戶直接比較自然景觀與人文安排", "這組是桃花和沿線景點，畫面會比文字更容易看出差異。",
        ),
        "hotel_reference": (
            "飯店客房與房內設備照片", ["實際客房空間", "床鋪與房內供氧設備"],
            "讓客戶先確認住宿環境和設備配置", "這組是飯店客房和房內設備照片，實際空間和床鋪都能先看清楚。",
        ),
        "vehicle_reference": (
            "行程用車與車內設備照片", ["航空座椅", "充電孔與車內供氧設備"],
            "讓客戶看清楚長途移動使用的車型與設備", "我把行程用車的照片放上來，座椅和車內設備都看得到。",
        ),
        "rongbuk_reference": (
            "珠峰段絨布旅館房間照片", ["房間實況", "獨立衛浴與供氧設備"],
            "讓客戶先確認珠峰段的住宿安排", "這張是珠峰段入住的絨布旅館，房間實況可以先看一下。",
        ),
    }
    what, points, value, caption = narratives.get(group, narratives["itinerary_overview"])
    return {
        "key": keys[0], "name": what, "topic": group, "routes": [route],
        "what_it_shows": what, "feature_points": points, "customer_value": value,
        "recommended_caption": caption,
        "avoid_claims": ["保證不會高反", "一定更安全", "一定更舒適"],
    }


def _plan(
    route: str,
    group: str,
    goal: str,
    *,
    follow_up: FollowUp | None = None,
) -> ReplyPlan:
    spec = ROUTES[route]
    asset = _asset(route, group)
    return ReplyPlan(
        action="reply", intent=group, route_variant=route, branch=spec["branch"],
        next_stage="value_building", reply_goal=goal, follow_up=follow_up,
        allowed_fact_ids=list(spec["groups"][group].get("evidence") or []),
        allowed_content_group_keys=[group],
        allowed_asset_ids=[asset["key"]] if asset else [], reply_options=[],
        slots={}, slot_evidence={}, missing_slots=[], handoff_reason=None,
        lead_action="ask" if follow_up and follow_up.type == "contact" else "none",
        contact_values={}, route_evidence="", confidence=1.0, safety_flags=[],
    )


def _cases() -> list[dict]:
    return [
        {"id": "reply_9d_entry", "mode": "reply", "route": "peach_9d_2027", "group": "itinerary_overview", "message": "想先看看9天不去珠峰的行程", "goal": "直接介绍9日走法，并说明行程图用途"},
        {"id": "reply_11d_entry", "mode": "reply", "route": "peach_11d_2027", "group": "itinerary_overview", "message": "我想去珠峰，11天怎么走？", "goal": "回答11日整体走法，并说明行程图用途"},
        {"id": "reply_hotel", "mode": "reply", "route": "peach_9d_2027", "group": "hotel_reference", "message": "住宿是什么样的？", "goal": "回答住宿安排，并自然介绍客房照片"},
        {"id": "reply_vehicle", "mode": "reply", "route": "peach_11d_2027", "group": "vehicle_reference", "message": "你们用什么车？车上有氧气吗？", "goal": "直接回答车辆和设备，并说明车辆照片用途"},
        {"id": "reply_rongbuk", "mode": "reply", "route": "peach_11d_2027", "group": "rongbuk_reference", "message": "珠峰那晚住帐篷吗？", "goal": "回答珠峰段住宿，并自然介绍房间照片"},
        {"id": "reply_price", "mode": "reply", "route": "peach_9d_2027", "group": "price_reference", "message": "9天费用是多少？", "goal": "直接回答已发布费用，不说系统或参考口径"},
        {"id": "reply_undecided", "mode": "reply", "route": "peach_9d_2027", "group": "peach_highlights", "message": "时间还没决定，先了解看看", "goal": "接住尚未决定的状态，分享一项桃花价值，不催日期"},
        {"id": "reply_family", "mode": "reply", "route": "peach_11d_2027", "group": "hotel_reference", "message": "我先跟家人讨论一下", "goal": "给一段方便转发给家人的住宿信息，不催促"},
        {"id": "reply_thanks", "mode": "reply", "route": "peach_9d_2027", "group": "peach_highlights", "message": "谢谢，我先看看", "goal": "简短回应并补一个值得看的亮点，不重新问候"},
        {"id": "reply_contact", "mode": "reply", "route": "peach_11d_2027", "group": "hotel_reference", "message": "住宿看起来不错，详细资料怎么拿？", "goal": "回答资料取得方式并补充住宿内容", "follow_up": FollowUp("contact", "line", "您可以把 LINE 留給我嗎？我把完整行程整理好傳給您。")},
        {"id": "silence_itinerary", "mode": "silence", "route": "peach_9d_2027", "group": "itinerary_overview", "message": "想先看看行程", "goal": "补充方便快速阅读的行程结构"},
        {"id": "silence_highlight", "mode": "silence", "route": "peach_9d_2027", "group": "peach_highlights", "message": "好的", "goal": "补充一个尚未介绍的桃花或沿线亮点"},
        {"id": "silence_hotel", "mode": "silence", "route": "peach_11d_2027", "group": "hotel_reference", "message": "我先看一下", "goal": "补充住宿照片和一个具体画面重点"},
        {"id": "silence_vehicle", "mode": "silence", "route": "peach_11d_2027", "group": "vehicle_reference", "message": "晚点再研究", "goal": "补充车辆照片与实际设备，不催回复"},
        {"id": "silence_family", "mode": "silence", "route": "peach_9d_2027", "group": "itinerary_overview", "message": "我在跟家人讨论", "goal": "整理成方便转发给家人的短摘要"},
        {"id": "silence_rongbuk", "mode": "silence", "route": "peach_11d_2027", "group": "rongbuk_reference", "message": "珠峰住宿我先看看", "goal": "补充珠峰段住宿实况，不延伸舒适或健康保证"},
        {"id": "silence_contact", "mode": "silence", "route": "peach_11d_2027", "group": "contact_transition", "message": "资料我先看看", "goal": "主线内容完成后低压力提醒联络方式", "follow_up": FollowUp("contact", "line", "如果您想收完整行程，可以把 LINE 留給我，我整理好再傳給您。")},
    ]


def _context(item: dict, plan: ReplyPlan) -> dict:
    material = _asset(item["route"], item["group"])
    history = [
        {"direction": "incoming", "content": item["message"]},
        {"direction": "outgoing", "content": "前面已經先說明過整體方向。"},
    ]
    return {
        "module": "silence_touch" if item["mode"] == "silence" else "reply",
        "customer_text": item["message"], "context_messages": history,
        "route_variant": item["route"], "memory": {},
        "journey": {"stage": "value_building", "sent_content_groups": [], "customer_profile": {}},
        "lead_capture": {"status": "not_started"},
        "available_materials": [material] if material else [],
        "reception_policy": _policy(), "touch_index": 2,
    }


def _checks(body: str) -> dict[str, bool]:
    return {
        "has_body": bool(body.strip()),
        "within_limit": len(body) <= 200,
        "no_question_in_body": "？" not in body and "?" not in body,
        "no_internal_terms": not any(term in body for term in INTERNAL_WORDS),
        "no_mainland_service_term": taiwan_copy_violation(body) is None,
        "no_stiff_phrase": not any(term in body for term in STIFF_PHRASES),
        "no_empty_value_claim": not any(term in body for term in EMPTY_VALUE_PHRASES),
        "no_simplified_residue": SIMPLIFIED_RESIDUE.search(body) is None,
        "no_unsupported_benefit": UNSUPPORTED_BENEFIT.search(body) is None,
        "soft_marker_limit": len(SOFT_MARKER.findall(body)) <= 1,
        "sentence_count": 1 <= len([part for part in re.split(r"[。！？!?]+", body) if part.strip()]) <= 3,
    }


def run_case(item: dict) -> dict:
    plan = _plan(item["route"], item["group"], item["goal"], follow_up=item.get("follow_up"))
    context = _context(item, plan)
    try:
        if item["mode"] == "silence":
            generated, logs, digest = call_silence_generator(
                context,
                SilencePlan(reply_plan=plan, touch_goal=item["goal"], touch_reason="customer_silent"),
            )
        else:
            generated, logs, digest = call_reply_generator(context, plan)
    except Exception as exc:
        return {
            "case_id": item["id"], "mode": item["mode"], "passed": False,
            "checks": {"generation_succeeded": False}, "body": "", "reply": "",
            "asset_ids": [], "model_attempts": len(getattr(exc, "logs", []) or []),
            "digest": str(getattr(exc, "digest", "") or ""), "error": str(exc),
        }
    checks = _checks(generated.body)
    if generated.follow_up is not None and not generated.body:
        checks["has_body"] = bool(generated.reply)
        checks["sentence_count"] = True
    if item["id"] in {"reply_undecided", "reply_family", "reply_thanks", "silence_family"}:
        checks["no_reask_date_or_party"] = not any(
            term in generated.reply for term in ("幾位", "几位", "什麼時候", "什么时候", "幾月", "几月")
        )
    if item["mode"] == "silence":
        checks["no_new_greeting"] = not generated.body.startswith(("您好", "哈囉", "哈嘍", "嗨"))
        checks["not_generic_checkin"] = not any(
            term in generated.body for term in ("看了嗎", "看得怎麼樣", "滿意嗎", "怎麼沒回覆", "有需要嗎")
        )
    return {
        "case_id": item["id"], "mode": item["mode"], "passed": all(checks.values()),
        "checks": checks, "body": generated.body, "reply": generated.reply,
        "asset_ids": generated.asset_ids, "model_attempts": len(logs), "digest": digest,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--label", default="candidate")
    args = parser.parse_args()
    results = [run_case(item) for item in _cases()]
    asset_results = [row for row in results if row["asset_ids"]]
    opening_counts: dict[str, int] = {}
    for row in results:
        opening = re.sub(r"\s+", "", row["reply"])[:8]
        opening_counts[opening] = opening_counts.get(opening, 0) + 1
    aggregate = {
        "no_dominant_opening": max(opening_counts.values(), default=0) <= 3,
        "asset_copy_not_dominated_by_wo_xian_ba": sum(
            "我先把" in row["reply"] for row in asset_results
        ) <= max(3, len(asset_results) // 3),
        "no_dominant_soft_leadin": max(
            sum(row["reply"].startswith(fragment) for row in results)
            for fragment in ("可以呀", "沒關係")
        ) <= 3,
        "no_overused_formula_fragment": max(
            sum(fragment in row["reply"] for row in results)
            for fragment in FORMULA_FRAGMENTS
        ) <= 5,
        "no_exact_duplicate_body": len({row["reply"] for row in results}) == len(results),
    }
    report = {
        "label": args.label,
        "outbound": False,
        "summary": {
            "total": len(results), "passed": sum(row["passed"] for row in results),
            "aggregate_passed": all(aggregate.values()),
        },
        "aggregate_checks": aggregate,
        "opening_counts": opening_counts,
        "results": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["summary"], ensure_ascii=False))
    for row in results:
        print(f"[{row['case_id']}] {row['body']}")
    return 0 if report["summary"]["passed"] == len(results) and all(aggregate.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
