from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
from typing import Callable, Iterable

from sqlalchemy.orm import Session

from app.automation_api import safe_text
from app.config import settings
from app.decision_service import VALIDATOR_VERSION, generate_decision
from app.deepseek_evaluation import EvaluationCallError, EvaluationDecision
from app.realtime_reply_pipeline import REALTIME_REPLY_PROMPT_VERSION
from app.lead_capture import bind_lead_request
from app.material_library import candidate_materials
from app.models import Tenant
from app.route_packages import JOURNEY_POLICY, ROUTE_PACKAGES, ROUTES
from app.route_reply import (bind_new_route_snapshot, group_content_from_values,
                             journey_context_from_values, playbook_prompt,
                             prepare_route_reply_values, update_content_progress_values)
from app.reception_config import effective_reception_policy


DATASET_ROOT = (
    Path(__file__).resolve().parents[2]
    / "data"
    / "evaluation"
    / "chatwoot_customer_journeys"
)
DEFAULT_DATASET_VERSION = "v2026-09-03"
MAX_RUN_CASES = 120


BLOCK_REASON_LABELS = {
    "missing_ai_label": "无 ai 标签",
    "channel_cannot_reply": "can_reply=false",
    "human_or_contact_block": "人工接管或客户已留资",
    "contact_state_unknown": "联系方式状态未知",
    "automatic_window_closed": "Facebook 回复窗口关闭",
    "model_unavailable": "模型失败或输出不合格",
    "send_state_unknown": "发送状态未知，等待人工核对",
}


TOPIC_GROUPS = {
    "price": {"price_reference"},
    "hotel": {"hotel_reference"},
    "itinerary": {"itinerary_overview", "peach_highlights", "landmarks", "zhaji"},
    "vehicle": {"vehicle_reference"},
    "departure": {"departure_reference"},
    "contact": {"contact_request"},
    "rongbuk": {"rongbuk_reference"},
}


def dataset_dir(version: str = DEFAULT_DATASET_VERSION) -> Path:
    return DATASET_ROOT / version


def load_dataset(version: str = DEFAULT_DATASET_VERSION) -> dict:
    root = dataset_dir(version)
    manifest = root / "manifest.json"
    if not manifest.is_file():
        raise FileNotFoundError(f"customer_journey_dataset_missing:{version}")
    data = json.loads(manifest.read_text(encoding="utf-8"))
    cases: list[dict] = []
    for name in ("answer_cases.json", "journey_cases.json", "shadow_cases.json"):
        path = root / name
        if path.is_file():
            items = json.loads(path.read_text(encoding="utf-8")).get("cases", [])
            for item in items:
                item = dict(item)
                item["suite"] = item.get("suite") or name.removesuffix("_cases.json")
                # The original 2026-09-03 expectations predate the mandatory
                # unclassified silence journey. Every accepted reply now
                # enrolls either a route journey or the route-selection
                # journey; safety stops still use no_action/handoff/blocks.
                expected = dict(item.get("expected") or {})
                if expected.get("action") == "reply" and expected.get("will_enroll_sop") is False:
                    expected["will_enroll_sop"] = True
                    item["expected"] = expected
                cases.append(item)
    data["cases"] = cases
    return data


def dataset_summary(version: str = DEFAULT_DATASET_VERSION) -> dict:
    data = load_dataset(version)
    cases = data["cases"]
    by_suite: dict[str, int] = {}
    by_type: dict[str, int] = {}
    by_scenario: dict[str, int] = {}
    for case in cases:
        by_suite[case.get("suite", "unknown")] = by_suite.get(case.get("suite", "unknown"), 0) + 1
        by_type[case.get("customer_type", "未分类")] = by_type.get(case.get("customer_type", "未分类"), 0) + 1
        by_scenario[case.get("scenario", "unknown")] = by_scenario.get(case.get("scenario", "unknown"), 0) + 1
    return {
        **{key: value for key, value in data.items() if key != "cases"},
        "case_count": len(cases),
        "by_suite": by_suite,
        "by_customer_type": by_type,
        "by_scenario": by_scenario,
        "cases": [
            {
                "case_id": case["case_id"],
                "suite": case.get("suite"),
                "scenario": case.get("scenario"),
                "customer_type": case.get("customer_type"),
                "title": case.get("title"),
                "source_conversation_id": case.get("source_conversation_id"),
                "expected_action": (case.get("expected") or {}).get("action"),
            }
            for case in cases
        ],
    }


def selected_cases(version: str, case_ids: list[str] | None, suites: list[str] | None) -> list[dict]:
    cases = load_dataset(version)["cases"]
    if suites:
        allowed = set(suites)
        cases = [case for case in cases if case.get("suite") in allowed]
    if case_ids:
        ids = set(case_ids)
        cases = [case for case in cases if case.get("case_id") in ids]
    return cases


def _customer_message(case: dict) -> dict:
    for message in reversed(case.get("messages", [])):
        if message.get("role") == "customer":
            return message
    raise ValueError("customer_message_required")


def _history(case: dict) -> list[dict]:
    messages = list(case.get("messages", []))
    rows = messages[:-1] if messages and messages[-1].get("role") == "customer" else messages
    return [
        {
            "direction": "incoming" if item.get("role") == "customer" else "outgoing",
            "content": item.get("content", ""),
            "content_type": item.get("content_type", "text"),
            "private": False,
        }
        for item in rows
    ]


def _attachments(case: dict, customer: dict) -> list[dict]:
    content = str(customer.get("content") or "")
    if (
        customer.get("content_type", "text") != "text"
        or case.get("scenario") in {"attachment_question", "pure_attachment_handoff"}
        or content.strip() in {"[图片]", "[圖片]", "[image]"}
    ):
        return [{
            "file_type": customer.get("content_type", "image"),
            "extension": "jpg" if customer.get("content_type", "image") == "image" else "bin",
            "content_type": customer.get("content_type", "image"),
        }]
    return []


def _control_block(case: dict) -> str | None:
    controls = case.get("controls") or {}
    if controls.get("ai_label") is False:
        return "missing_ai_label"
    if controls.get("can_reply") is False:
        return "channel_cannot_reply"
    if controls.get("human") is True:
        return "human_or_contact_block"
    if controls.get("send_state_unknown") is True:
        return "send_state_unknown"
    if controls.get("contact_state_unknown") is True:
        return "contact_state_unknown"
    return None


def _low_intent_customer_message(case: dict) -> bool:
    text = str(_customer_message(case).get("content") or "")
    explicit_opt_out = any(
        term in text
        for term in ("不要打扰", "不要打擾", "勿需打扰", "勿需打擾", "不要联系", "不要聯絡", "停止", "封锁", "封鎖", "黑名单", "黑名單", "退订", "退訂")
    )
    return (
        not explicit_opt_out
        and any(
            term in text
            for term in ("按错", "按錯", "误按", "誤按", "先看看", "参考一下", "參考一下", "列入參考", "列入参考", "暂时没需求", "暫時沒需求", "目前沒有規劃", "目前没有规划", "尚未决定", "尚未決定", "還沒決定", "还没决定", "确定参加再", "確定參加再", "以后再说", "以後再說", "需要再联系", "需要再聯絡", "已找别家", "已找別家")
        )
    )


def _sop_preview(
    decision: EvaluationDecision,
    initial_sent: Iterable[str],
    case: dict | None = None,
    reception_policy: dict | None = None,
) -> dict:
    if decision.action != "reply" or decision.route_variant not in {"", *ROUTE_PACKAGES}:
        return {
            "will_enroll": False,
            "timeline_minutes": [],
            "next_content_group": None,
            "next_behavior": "not_enrolled",
        }
    policy = (reception_policy or JOURNEY_POLICY)["silence_journey"]
    timeline = [
        int(policy["mainline_after_minutes"]),
        *[int(item) for item in policy["wakeup_after_minutes"]],
    ]
    sent = set(initial_sent)
    sent.update(decision.covered_content_groups or [])
    if decision.content_group_key:
        sent.add(decision.content_group_key)
    next_group = next((
        key for key in ROUTES.get(decision.route_variant, {}).get("sequence", []) if key not in sent
    ), None)
    return {
        "will_enroll": True,
        "timeline_minutes": timeline,
        "next_node": "silence_mainline",
        "next_content_group": next_group,
        "next_behavior": "stage_driven_mandatory_touch",
    }


def _topic_answered(topic: str, decision: EvaluationDecision) -> bool:
    if topic in TOPIC_GROUPS:
        groups = set(decision.covered_content_groups or [])
        if decision.content_group_key:
            groups.add(decision.content_group_key)
        if groups & TOPIC_GROUPS[topic]:
            return True
    text = decision.reply or ""
    fallback_terms = {
        "price": ["价格", "價", "費用", "费用", "报价", "報價", "预算", "預算", "人民幣", "人民币"],
        "hotel": ["住宿", "酒店", "飯店", "房", "單房差", "单房差"],
        "itinerary": ["行程", "天", "路線", "路线", "线路", "桃花", "珠峰"],
        "vehicle": ["車", "车", "司机", "司機", "交通", "座"],
        "departure": ["出发", "出發", "团期", "團期", "日期", "3月", "三月"],
        "contact": ["LINE", "微信", "電話", "电话", "Email", "邮箱", "郵箱"],
        "rongbuk": ["絨布", "绒布", "珠峰"],
    }
    return any(term in text for term in fallback_terms.get(topic, []))


def _recommends_supported_routes(decision: EvaluationDecision) -> bool:
    text = decision.reply or ""
    options = set(decision.reply_options or [])
    return (
        ("桃花9日" in text or "9日" in text or "桃花9日" in options)
        and ("桃花+珠峰11日" in text or "桃花加珠峰11日" in text or "11日" in text or "桃花+珠峰11日" in options)
    )


def _recommends_supported_product(decision: EvaluationDecision) -> bool:
    text = decision.reply or ""
    options = set(decision.reply_options or [])
    if decision.route_variant in ROUTE_PACKAGES:
        return True
    normalized_text = text.replace("＋", "+").replace(" ", "")
    normalized_options = {
        item.replace("＋", "+").replace(" ", "") for item in options
    }
    for spec in ROUTES.values():
        titles = {
            str(spec.get("selection_title") or "").replace("＋", "+").replace(" ", ""),
            str(spec.get("name") or "").replace("＋", "+").replace(" ", ""),
        }
        if any(title and (title in normalized_text or title in normalized_options) for title in titles):
            return True
        day_token = "11日" if "11日" in "".join(titles) else "9日" if "9日" in "".join(titles) else ""
        if day_token and day_token in normalized_text and any(term in normalized_text for term in ("桃花", "珠峰", "行程", "路線", "路线")):
            return True
    return False


def _check_result(case: dict, decision: EvaluationDecision | None, blocked_reason: str | None, sop_preview: dict) -> list[dict]:
    expected = case.get("expected") or {}
    checks: list[dict] = []

    def add(name: str, passed: bool, actual=None, expected_value=None) -> None:
        checks.append({
            "name": name,
            "passed": bool(passed),
            "actual": safe_text(actual),
            "expected": safe_text(expected_value),
        })

    if expected.get("blocked_reason"):
        add("blocked_reason", blocked_reason == expected["blocked_reason"], blocked_reason, expected["blocked_reason"])
        return checks
    if decision is None:
        add("decision_created", False, None, "model decision")
        return checks
    if expected.get("action"):
        add("action", decision.action == expected["action"], decision.action, expected["action"])
    if "route_variant" in expected:
        add("route_variant", decision.route_variant == expected["route_variant"], decision.route_variant, expected["route_variant"])
    if "lead_action" in expected:
        add("lead_action", decision.lead_action == expected["lead_action"], decision.lead_action, expected["lead_action"])
    if expected.get("journey_stage"):
        add("journey_stage", decision.journey_stage == expected["journey_stage"], decision.journey_stage, expected["journey_stage"])
    if expected.get("touch_goal"):
        allowed_goals = expected["touch_goal"] if isinstance(expected["touch_goal"], list) else [expected["touch_goal"]]
        add("touch_goal", decision.touch_goal in allowed_goals, decision.touch_goal, allowed_goals)
    for key, value in (expected.get("profile_updates") or {}).items():
        actual = (decision.profile_updates.get(key) or {}).get("value")
        add(f"profile:{key}", str(actual) == str(value), actual, value)
    if expected.get("handoff_reason"):
        add("handoff_reason", decision.handoff_reason == expected["handoff_reason"], decision.handoff_reason, expected["handoff_reason"])
    for key, value in (expected.get("slots") or {}).items():
        add(f"slot:{key}", str(decision.slots.get(key, "")) == str(value), decision.slots.get(key), value)
    for topic in expected.get("must_answer_topics", []):
        add(f"topic:{topic}", _topic_answered(topic, decision), decision.covered_content_groups or decision.reply, topic)
    if expected.get("recommend_supported_routes"):
        add("recommend_supported_routes", _recommends_supported_routes(decision), {
            "reply": decision.reply,
            "reply_options": decision.reply_options,
        }, "9日与11日支持线路推荐")
    if expected.get("recommend_supported_product"):
        add("recommend_supported_product", _recommends_supported_product(decision), {
            "reply": decision.reply,
            "reply_options": decision.reply_options,
        }, "至少一个支持线路推荐")
    if "will_enroll_sop" in expected:
        add("will_enroll_sop", sop_preview["will_enroll"] == expected["will_enroll_sop"], sop_preview["will_enroll"], expected["will_enroll_sop"])
    if expected.get("next_sop_behavior"):
        add("next_sop_behavior", sop_preview["next_behavior"] == expected["next_sop_behavior"], sop_preview["next_behavior"], expected["next_sop_behavior"])
    return checks


def _evaluation_journey_slots(route: str, memory: dict, sent: list[str], materials: list[dict]) -> dict:
    """Initialize a synthetic, read-only fixture, not migrate customer history.

    Dataset sent groups are explicit scenario assumptions under the evaluated
    catalog. They are not evidence that historical customers received its assets.
    """
    if not route:
        return dict(memory)
    slots = bind_new_route_snapshot(route, memory, available_materials=materials)
    for group_key in sent:
        group = group_content_from_values(route, slots, group_key)
        if group is None:
            raise ValueError("evaluation_fixture_group_unknown")
        slots, _, _ = update_content_progress_values(
            route, slots, [], group_key,
            delivered_text=group.get("text"), asset_keys=group.get("assets", []),
        )
    slots["_evaluation_fixture"] = {"synthetic": True, "assumed_sent_groups": list(sent)}
    return slots


def run_dataset_cases(
    db: Session,
    cases: list[dict],
    *,
    model_call: Callable[[dict], tuple[EvaluationDecision, list[dict], str]] | None = None,
) -> dict:
    tenant = db.query(Tenant).order_by(Tenant.id).first()
    materials = candidate_materials(db, tenant.id if tenant else None)
    reception_policy = effective_reception_policy(db)
    results = []
    for case in cases:
        blocked = _control_block(case)
        decision = None
        trace = {}
        attempts = 0
        error_code = None
        customer = _customer_message(case)
        initial_memory = dict(case.get("initial_memory") or {})
        initial_sent = list(case.get("initial_sent_groups") or [])
        route = str(case.get("initial_route_variant") or "")
        initial_slots = initial_memory
        if not blocked:
            try:
                initial_slots = _evaluation_journey_slots(route, initial_memory, initial_sent, materials)
            except ValueError as exc:
                blocked, error_code = "model_unavailable", str(exc)[:120]
                trace = {"outbound": False}
        if not blocked:
            context = {
                "module": case.get("module", "reply"),
                "customer_text": customer.get("content", ""),
                "context_messages": _history(case),
                "context_complete": True,
                "route_variant": route,
                "memory": initial_memory,
                "journey": journey_context_from_values(
                    route,
                    case.get("initial_stage", "route_selection"),
                    initial_slots,
                    initial_sent,
                ),
                "route_playbook": playbook_prompt(),
                "current_attachments": _attachments(case, customer),
                "lead_capture": case.get("lead_capture") or {
                    "status": "not_started",
                    "request_count": 0,
                    "captured_kinds": [],
                },
                "available_materials": materials,
                "reception_policy": reception_policy,
            }
            try:
                if model_call is None:
                    decision, logs, _digest, trace = generate_decision(context)
                else:
                    decision, logs, _digest, trace = generate_decision(context, model_call=model_call)
                decision, progress = prepare_route_reply_values(
                    decision,
                    current_route=route,
                    stage=context["journey"].get("stage", "route_selection"),
                    slots=initial_slots,
                    sent_groups=initial_sent,
                )
                decision, _ = bind_lead_request(
                    decision,
                    route_variant=progress.get("route_variant", ""),
                    journey_stage=progress.get("stage", "route_selection"),
                    capture_status=(case.get("lead_capture") or {}).get("status", "not_started"),
                )
                attempts = len(logs)
            except EvaluationCallError as exc:
                blocked = "model_unavailable"
                error_code = exc.code
                attempts = len(exc.logs)
                trace = {"outbound": False, "request_hash": exc.digest}
            except Exception as exc:  # noqa: BLE001 - evaluation must report the failure, not hide it.
                blocked = "model_unavailable"
                error_code = str(exc)[:120]
                trace = {"outbound": False}
        sop = _sop_preview(decision, initial_sent, case, reception_policy) if decision else {
            "will_enroll": False,
            "timeline_minutes": [],
            "next_node": None,
            "next_content_group": None,
            "next_behavior": "blocked",
        }
        checks = _check_result(case, decision, blocked, sop)
        passed = bool(checks) and all(item["passed"] for item in checks)
        results.append({
            "case_id": case["case_id"],
            "suite": case.get("suite"),
            "scenario": case.get("scenario"),
            "customer_type": case.get("customer_type"),
            "title": case.get("title"),
            "source_conversation_id": case.get("source_conversation_id"),
            "passed": passed,
            "checks": checks,
            "action": decision.action if decision else "no_action",
            "reply": safe_text(decision.reply if decision else None),
            "route_variant": decision.route_variant if decision else "",
            "slots": safe_text(decision.slots if decision else {}),
            "missing_slots": decision.missing_slots if decision else [],
            "lead_action": decision.lead_action if decision else "none",
            "journey_stage": decision.journey_stage if decision else "route_selection",
            "touch_goal": decision.touch_goal if decision else "",
            "touch_reason": safe_text(decision.touch_reason if decision else ""),
            "profile_updates": safe_text(decision.profile_updates if decision else {}),
            "handoff_reason": safe_text(decision.handoff_reason if decision else None),
            "covered_content_groups": decision.covered_content_groups if decision else [],
            "content_group_key": decision.content_group_key if decision else "",
            "reply_options": decision.reply_options if decision else [],
            "sop_preview": sop,
            "blocked_reason": blocked,
            "blocked_label": BLOCK_REASON_LABELS.get(blocked or "", blocked),
            "error_code": error_code,
            "attempts": attempts,
            "trace": safe_text(trace),
            "outbound": False,
        })
    return {
        "outbound": False,
        "prompt_version": REALTIME_REPLY_PROMPT_VERSION,
        "validator_version": VALIDATOR_VERSION,
        "validator_version": VALIDATOR_VERSION,
        "summary": {
            "total": len(results),
            "passed": sum(1 for item in results if item["passed"]),
            "failed": sum(1 for item in results if not item["passed"]),
        },
        "results": results,
    }


def decision_to_dict(decision: EvaluationDecision) -> dict:
    return asdict(decision)
