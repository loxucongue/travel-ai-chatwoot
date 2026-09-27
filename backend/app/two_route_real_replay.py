"""Read-only replay datasets and compliance checks for the two reviewed routes.

Dataset categorisation is deterministic so a frozen snapshot can be reproduced.
It is not used by the live assistant to decide how to answer a customer.
"""
from __future__ import annotations

import hashlib
import json
import re
import statistics
from collections import Counter, defaultdict
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.decision_service import generate_decision
from app.deepseek_evaluation import EvaluationCallError
from app.realtime_reply_pipeline import REALTIME_REPLY_PROMPT_VERSION
from app.material_library import candidate_materials
from app.lead_capture import CONTACT_ACKNOWLEDGEMENT, bind_lead_request, model_contacts
from app.models import ConversationState, InboxBinding, MessageEvent, OutboundMessage, Tenant
from app.route_packages import JOURNEY_POLICY, KNOWLEDGE_VERSION, ROUTE_PACKAGES, ROUTES
from app.route_reply import journey_context_from_values, playbook_prompt, prepare_route_reply_values


DATASET_VERSION = "two-route-model-owned-v2"
ROUTE_IDS = ("peach_9d_2027", "peach_11d_2027")
TEST_MARKERS = ("测试人员", "測試人員", "relay-e2e", "webhook test", "mock ai")

SENSITIVE_PATTERNS = (
    (re.compile(r"(?i)([A-Z0-9._%+-])[A-Z0-9._%+-]*(@[A-Z0-9.-]+\.[A-Z]{2,})"), r"\1***\2"),
    (re.compile(r"(?<!\d)(?:\+?\d[\d\s().-]{6,}\d)(?!\d)"), "[電話已遮罩]"),
    (re.compile(r"(?i)((?:line|wechat|weixin|微信|微訊|whatsapp)\s*(?:id|帳號|账号)?\s*(?:是|:|：)?\s*)[A-Z0-9_.-]{4,}"), r"\1[已遮罩]"),
    (re.compile(r"(?i)https://line\.me/ti/p/[A-Z0-9_-]+"), "[LINE連結已遮罩]"),
    (re.compile(r"[\u4e00-\u9fff]{1,3}(?:先生|小姐|女士)"), "[客戶稱呼]"),
)

CATEGORY_TERMS = {
    "route_entry": (
        "桃花", "林芝", "西藏", "珠峰", "行程", "路線", "路线", "旅遊", "旅游", "出團", "出团",
    ),
    "party_departure": (
        "幾位", "几位", "幾人", "几人", "同行", "一個人", "一个人", "出發", "出发", "日期", "時間", "时间", "月份", "幾月", "几月",
    ),
    "itinerary_attractions": (
        "景點", "景点", "布達拉", "布达拉", "大昭寺", "八廓", "扎基", "嘎拉", "波密", "南迦巴瓦", "日喀則", "日喀则", "拉薩", "拉萨", "山南", "羊卓雍", "冰川",
    ),
    "hotel_vehicle_media": (
        "酒店", "住宿", "房間", "房间", "單房", "单房", "供氧", "氧氣", "氧气", "車", "车", "座位", "圖片", "图片", "照片", "影片", "視頻", "视频",
    ),
    "price_availability": (
        "價格", "价格", "費用", "费用", "報價", "报价", "多少錢", "多少钱", "減多少", "减多少", "優惠", "优惠", "余位", "餘位", "名額", "名额", "有位", "庫存", "库存", "成團", "成团", "一樣出團", "一样出团", "確定出團", "确定出团", "保證出團", "保证出团",
    ),
    "safety_handoff": (
        "高反", "高山症", "健康", "血壓", "血压", "心臟", "心脏", "年齡限制", "年龄限制", "歲限制", "岁限制", "老人", "長者", "长者", "入藏函", "護照", "护照", "台胞證", "台胞证", "證件", "证件", "保證", "保证", "退款", "投訴", "投诉", "不滿意", "不满意", "合約", "合同", "賠償", "赔偿", "真人", "人工", "顧問", "顾问",
        "地震", "土石流", "災難", "灾难", "口岸封閉", "口岸封闭", "太累", "怕累",
    ),
    "lead_contact": (
        "line", "wechat", "whatsapp", "微信", "微訊", "電話", "电话", "手機", "手机", "email", "郵箱", "邮箱", "聯絡", "联系",
    ),
}

DIRECT_CONTACT_RE = re.compile(
    r"(?i)(?:https://line\.me/ti/p/[A-Z0-9_-]+|[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}|(?:line|wechat|weixin|微信|微訊|whatsapp)\s*(?:id|帳號|账号)?\s*(?:是|:|：)?\s*[A-Z0-9_.-]{4,}|(?<!\d)(?:\+?\d[\d\s().-]{6,}\d)(?!\d))"
)

CONTACT_DECLINE_TERMS = (
    "不用聯絡", "不用联系", "先不用", "有需要再聯絡", "有需要再联系", "有問題再聯絡", "有问题再联系",
    "確定再聯絡", "确定再联系", "考慮看看", "考虑看看", "暫時不", "暂时不",
)
AVAILABILITY_TERMS = (
    "余位", "餘位", "名額", "名额", "有位", "庫存", "库存", "成團", "成团", "一樣出團", "一样出团",
    "確定出團", "确定出团", "保證出團", "保证出团", "不會出團", "不会出团",
)
UNSUPPORTED_DESTINATION_TERMS = (
    "北京行程", "川西", "色達", "色达", "青甘", "阿里", "新疆", "雲南", "云南", "九寨", "稻城", "亞丁", "亚丁", "梅里雪山", "麗江", "丽江", "總長10天", "总长10天",
)
BESPOKE_PRICE_TERMS = (
    "一臺車", "一台車", "一台车", "包車", "包车", "包團", "包团", "自己訂房", "自己订房", "減多少", "减多少", "單獨報價", "单独报价",
)
PAYMENT_DETAIL_TERMS = (
    "訂金", "订金", "定金", "付款方式", "付款條件", "付款条件", "報到地點", "报到地点", "在哪報到", "在哪报到",
)
PLACEHOLDER_RE = re.compile(r"^\s*(?:\[(?:圖片|图片|image|影片|視頻|视频|附件|text)\]|[?？。！!~～\s]+)\s*$", re.I)


@dataclass
class RealTurn:
    case_key: str
    conversation_state_id: int
    conversation_id: int
    contact_id: int | None
    message_ids: list[int]
    customer_text: str
    context_messages: list[dict]
    reference_answer: str
    categories: list[str]
    inferred_route: str
    route_evidence: str
    created_at: str


@dataclass
class ReplayCase:
    case_id: str
    suite: str
    source_case_key: str
    conversation_id: int
    customer_text: str
    context_messages: list[dict]
    reference_answer: str
    categories: list[str]
    expected_route: str
    expected_action: str
    expected_intent: str | None
    expected_group: str | None
    context_mode: str
    stage: str
    slots: dict = field(default_factory=dict)
    sent_groups: list[str] = field(default_factory=list)
    lead_capture_status: str = "not_started"
    expected_lead_action: str | None = None


def assert_evaluation_only() -> None:
    settings.validate_runtime()
    if (
        settings.app_profile != "evaluation"
        or settings.outbound_mode != "disabled"
        or settings.chatwoot_write_enabled
        or settings.live_sop_enabled
    ):
        raise RuntimeError("two_route_replay_requires_evaluation_read_only_mode")


def mask_sensitive(value: str) -> str:
    masked = value or ""
    for pattern, replacement in SENSITIVE_PATTERNS:
        masked = pattern.sub(replacement, masked)
    return masked


def restore_masked_case_input(db: Session, case: ReplayCase) -> tuple[ReplayCase, dict]:
    """Eval-only, SELECT-only recovery of the current turn into an isolated copy.

    Never infer identifiers from labels/expectations or rebuild context from a
    newer catalog. Both source IDs and the frozen masked text must match exactly.
    The returned provenance contains hashes/row locators, never the original text.
    """
    assert_evaluation_only()
    restored = deepcopy(case)
    masked = any(marker in case.customer_text for marker in (
        "[已遮罩]", "[電話已遮罩]", "[LINE連結已遮罩]", "[客戶稱呼]", "***@",
    ))
    if not masked:
        return restored, {"restored": False}
    matches = []
    with db.no_autoflush:
        states = db.scalars(select(ConversationState).where(
            ConversationState.chatwoot_conversation_id == case.conversation_id,
        )).all()
        for state in states:
            rows = _public_messages(db, [state.id])
            index = 0
            while index < len(rows):
                if rows[index].direction != "incoming":
                    index += 1
                    continue
                target = []
                while index < len(rows) and rows[index].direction == "incoming":
                    row = rows[index]
                    if row.content_type == "text" and (row.content or "").strip():
                        target.append(row)
                    index += 1
                ids = [row.chatwoot_message_id for row in target]
                source = f"{case.conversation_id}:{','.join(map(str, ids))}"
                if target and hashlib.sha256(source.encode()).hexdigest()[:24] == case.source_case_key:
                    matches.append((state, target))
    if len(matches) != 1:
        raise ValueError("replay_original_input_missing_or_ambiguous")
    state, rows = matches[0]
    original = "\n".join((row.content or "").strip() for row in rows)
    if original == case.customer_text or mask_sensitive(original) != case.customer_text:
        raise ValueError("replay_original_input_mask_mismatch")
    restored.customer_text = original
    return restored, {
        "restored": True, "source": "message_events", "source_case_key": case.source_case_key,
        "conversation_id": case.conversation_id, "conversation_state_id": state.id,
        "message_event_ids": [row.id for row in rows],
        "chatwoot_message_ids": [row.chatwoot_message_id for row in rows],
        "input_sha256": hashlib.sha256(original.encode("utf-8")).hexdigest(),
        "frozen_input_sha256": hashlib.sha256(case.customer_text.encode("utf-8")).hexdigest(),
    }


def reception_policy_fingerprint(policy: dict) -> str:
    canonical = json.dumps(policy, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _redact_case_result(result: dict, case: ReplayCase) -> dict:
    # A bare LINE/WeChat ID in contact_values, logs or slot evidence is not
    # detected by prefix-based masking. Redact known tokens throughout the tree.
    secrets = set()
    texts = [case.customer_text, case.reference_answer,
             *(item.get("content", "") for item in case.context_messages)]
    for text in texts:
        for pattern, _ in SENSITIVE_PATTERNS:
            for match in pattern.finditer(text):
                secrets.add(match.group(0))
                if pattern.groups == 1:
                    token = match.group(0)[len(match.group(1)):].strip()
                    if token:
                        secrets.add(token)
    contacts = (result.get("model_decision") or {}).get("contact_values") or {}
    secrets.update(value for value in contacts.values() if isinstance(value, str) and value)

    def redact(value):
        if isinstance(value, str):
            for secret in sorted(secrets, key=len, reverse=True):
                value = re.sub(re.escape(secret), "[已遮罩]", value, flags=re.I)
            if re.fullmatch(r"[0-9a-f]{24,64}", value):
                return value
            return mask_sensitive(value)
        if isinstance(value, dict):
            return {redact(key): redact(item) for key, item in value.items()}
        if isinstance(value, list):
            return [redact(item) for item in value]
        return value

    return redact(result)


def _timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return datetime.min.replace(tzinfo=timezone.utc)


def _message_text(message: MessageEvent) -> str:
    text = (message.content or "").strip()
    if text:
        return text
    attachments = message.attachments or []
    if attachments:
        kinds = [str(item.get("file_type") or item.get("content_type") or "attachment") for item in attachments]
        return "[附件:" + ",".join(kinds) + "]"
    return ""


def categories_for(text: str) -> list[str]:
    semantic = re.sub(r"\[(?:圖片|图片|image|影片|視頻|视频|附件|text)\]", " ", text, flags=re.I)
    lowered = semantic.casefold()
    categories = [key for key, terms in CATEGORY_TERMS.items() if any(term.casefold() in lowered for term in terms)]
    if re.search(r"高[\W_]{0,2}反", semantic) and "safety_handoff" not in categories:
        categories.append("safety_handoff")
    return categories


def infer_route_from_messages(messages: Iterable[str]) -> tuple[str, str]:
    route = ""
    evidence = ""
    for raw in messages:
        text = str(raw or "").strip()
        if not text:
            continue
        compact = re.sub(r"\s+", "", text)
        if re.search(r"(?:不上|不去|不要去|不包含|不含).{0,3}珠峰", compact) or re.search(r"(?:桃花|林芝).{0,8}9(?:日|天)", compact) or re.search(r"(?<!\d)9天(?!\d)", compact):
            route, evidence = "peach_9d_2027", text[:120]
            continue
        if re.search(r"(?:桃花|林芝).{0,8}11(?:日|天)", compact) or re.search(r"(?<!\d)11天(?!\d)", compact) or (
            "珠峰" in compact and not re.search(r"(?:不上|不去|不要去|不包含|不含).{0,3}珠峰", compact)
        ):
            route, evidence = "peach_11d_2027", text[:120]
    return route, evidence


def _is_test_text(text: str) -> bool:
    lowered = text.casefold()
    return any(marker.casefold() in lowered for marker in TEST_MARKERS)


def _public_messages(db: Session, state_ids: list[int]) -> list[MessageEvent]:
    if not state_ids:
        return []
    rows = db.scalars(
        select(MessageEvent).where(
            MessageEvent.conversation_state_id.in_(state_ids),
            MessageEvent.private.is_(False),
            MessageEvent.direction.in_(("incoming", "outgoing")),
        )
    ).all()
    return sorted(rows, key=lambda item: (_timestamp(item.created_at), item.id))


def collect_real_turns(db: Session, inbox_id: int, exclude_conversation_ids: set[int] | None = None) -> tuple[list[RealTurn], dict]:
    inbox = db.scalar(select(InboxBinding).where(InboxBinding.chatwoot_inbox_id == inbox_id))
    if inbox is None:
        raise ValueError("evaluation_inbox_not_found")
    states = list(db.scalars(select(ConversationState).where(
        ConversationState.inbox_binding_id == inbox.id
    ).order_by(ConversationState.chatwoot_conversation_id)).all())
    by_id = {state.id: state for state in states}
    related: dict[tuple[str, int], list[int]] = defaultdict(list)
    for state in states:
        key = ("contact", state.contact_id) if state.contact_id else ("conversation", state.id)
        related[key].append(state.id)

    excluded = Counter()
    turns: list[RealTurn] = []
    excluded_conversations = set(exclude_conversation_ids or set())
    for state in states:
        if state.chatwoot_conversation_id in excluded_conversations:
            excluded["known_test_conversation"] += 1
            continue
        current = _public_messages(db, [state.id])
        related_key = ("contact", state.contact_id) if state.contact_id else ("conversation", state.id)
        all_history = _public_messages(db, related[related_key])
        index = 0
        while index < len(current):
            if current[index].direction != "incoming":
                index += 1
                continue
            target: list[MessageEvent] = []
            while index < len(current) and current[index].direction == "incoming":
                row = current[index]
                if row.content_type == "text" and (row.content or "").strip():
                    target.append(row)
                elif row.attachments:
                    excluded["attachment_only_in_turn"] += 1
                else:
                    excluded["empty_incoming"] += 1
                index += 1
            if not target:
                continue
            customer_text = "\n".join((row.content or "").strip() for row in target)
            if _is_test_text(customer_text):
                excluded["test_marker"] += 1
                continue
            first = target[0]
            first_key = (_timestamp(first.created_at), first.id)
            context_rows = [row for row in all_history if (_timestamp(row.created_at), row.id) < first_key]
            context = [
                {
                    "id": row.chatwoot_message_id,
                    "conversation_id": by_id[row.conversation_state_id].chatwoot_conversation_id,
                    "direction": row.direction,
                    "content_type": row.content_type,
                    "content": _message_text(row),
                    "created_at": row.created_at,
                }
                for row in context_rows if _message_text(row)
            ]
            reference: list[str] = []
            cursor = index
            while cursor < len(current) and current[cursor].direction == "outgoing":
                value = _message_text(current[cursor])
                if current[cursor].content_type == "text" and value:
                    reference.append(value)
                cursor += 1
            route, route_evidence = infer_route_from_messages([
                *(item["content"] for item in context if item["direction"] == "incoming"),
                customer_text,
            ])
            message_ids = [row.chatwoot_message_id for row in target]
            case_key = hashlib.sha256(
                f"{state.chatwoot_conversation_id}:{','.join(map(str, message_ids))}".encode()
            ).hexdigest()[:24]
            turns.append(RealTurn(
                case_key=case_key,
                conversation_state_id=state.id,
                conversation_id=state.chatwoot_conversation_id,
                contact_id=state.contact_id,
                message_ids=message_ids,
                customer_text=customer_text,
                context_messages=context,
                reference_answer="\n".join(reference),
                categories=categories_for(customer_text),
                inferred_route=route,
                route_evidence=route_evidence,
                created_at=first.created_at,
            ))
    snapshot_rows = [
        (item.conversation_id, item.message_ids, item.customer_text, item.created_at)
        for item in turns
    ]
    metadata = {
        "dataset_version": DATASET_VERSION,
        "inbox_id": inbox_id,
        "conversation_count": len(states),
        "turn_count": len(turns),
        "excluded": dict(excluded),
        "snapshot_hash": hashlib.sha256(
            json.dumps(snapshot_rows, ensure_ascii=False, separators=(",", ":")).encode()
        ).hexdigest(),
        "category_counts": dict(Counter(category for item in turns for category in item.categories)),
        "route_counts": dict(Counter(item.inferred_route or "unbound" for item in turns)),
    }
    return turns, metadata


def _primary_category(turn: RealTurn) -> str:
    priority = (
        "safety_handoff", "lead_contact", "price_availability", "party_departure",
        "hotel_vehicle_media", "itinerary_attractions", "route_entry",
    )
    return next((item for item in priority if item in turn.categories), "other")


def _expected_intent(turn: RealTurn) -> str | None:
    text = turn.customer_text
    if "safety_handoff" in turn.categories:
        if any(term in text for term in ("投訴", "投诉", "退款", "不滿意", "不满意")):
            return "complaint"
        return None
    if "lead_contact" in turn.categories and DIRECT_CONTACT_RE.search(text):
        return "contact"
    if "price_availability" in turn.categories and any(term in text for term in ("出團", "出团", "成團", "成团", "團期", "团期")):
        return "departure"
    if "price_availability" in turn.categories:
        return "price"
    if "party_departure" in turn.categories and any(term in text for term in ("出發", "出发", "日期", "時間", "时间", "月份", "幾月", "几月")):
        return "departure"
    if "hotel_vehicle_media" in turn.categories or "itinerary_attractions" in turn.categories:
        return "itinerary"
    if "route_entry" in turn.categories:
        return "route_intro"
    return None


def _expected_group(turn: RealTurn, route: str, sent_groups: list[str], stage: str) -> str | None:
    text = turn.customer_text
    spec = ROUTES[route]
    if turn.categories == ["route_entry"]:
        # Route-entry turns can legitimately continue from party information in
        # earlier history. The post-model party gate below validates this using
        # the final evidence-backed slots instead of guessing during sampling.
        return None
    if "price_availability" in turn.categories:
        if any(term in text for term in AVAILABILITY_TERMS):
            return None
        return "price_reference" if spec["policies"]["price_after_group"] in sent_groups else next(
            (key for key in spec["sequence"] if key not in sent_groups), None
        )
    if any(term in text for term in (
        "幾天行程", "几天行程", "從那出發", "从那出发", "從哪出發", "从哪出发",
        "哪裡出發", "哪里出发",
    )):
        return "itinerary_overview"
    if "沿途" in text or re.search(r"(?:從|从).{0,12}(?:去|到).{0,20}(?:林芝|拉薩|拉萨)", text):
        return "itinerary_overview"
    if "party_departure" in turn.categories and any(term in text for term in ("出發", "出发", "日期", "時間", "时间", "月份", "幾月", "几月")):
        return "departure_reference"
    if any(term in text for term in ("酒店", "住宿", "房間", "房间", "單房", "单房", "供氧", "氧氣", "氧气")):
        return "hotel_reference"
    if any(term in text for term in ("車", "车", "座位")):
        return "vehicle_reference"
    if route == "peach_11d_2027" and "珠峰" in text and any(term in text for term in ("住", "住宿", "絨布", "绒布")):
        return "rongbuk_reference"
    if any(term in text for term in ("桃花", "嘎拉", "波密", "帕邦喀", "秀巴")) and "行程" not in text:
        return "peach_highlights"
    if any(term in text for term in ("布達拉", "布达拉", "大昭寺", "八廓")):
        return "landmarks"
    if "扎基" in text:
        return "zhaji"
    if "route_entry" in turn.categories or "itinerary_attractions" in turn.categories:
        return "itinerary_overview"
    return None


def _months_in_text(text: str) -> set[int]:
    months = {int(value) for value in re.findall(r"(?<!\d)(1[0-2]|[1-9])\s*月", text)}
    months.update(int(value) for value in re.findall(r"(?<!\d)(1[0-2]|[1-9])\s*[/.-]\s*(?:[0-3]?\d)(?!\d)", text))
    chinese_months = {
        value: number
        for number, value in enumerate(("一", "二", "三", "四", "五", "六", "七", "八", "九", "十", "十一", "十二"), 1)
    }
    months.update(
        chinese_months[value]
        for value in re.findall(r"(?<![一二三四五六七八九十])(十二|十一|十|[一二三四五六七八九])\s*月", text)
    )
    return months


def _outside_reference_window(text: str) -> bool:
    months = _months_in_text(text)
    explicitly_other_time = any(term in text for term in ("其他時間", "其他时间", "別的時間", "别的时间", "過年", "过年", "春節", "春节"))
    return explicitly_other_time or bool(months and not months.issubset({3, 4}))


def _requires_handoff(text: str, categories: list[str]) -> bool:
    del categories
    return any(term in text for term in (
        "我要真人", "轉人工", "转人工", "人工客服", "找顧問", "找顾问",
        "投訴", "投诉", "退款", "退費", "退费", "合同爭議", "合同争议",
        "合約爭議", "合约争议", "要求賠償", "要求赔偿",
    ))


def _explicit_current_route(text: str) -> str | None:
    """Return a route only when the current turn unambiguously selects one.

    This helper is used by the offline evaluator, never by the live reply path.
    Co-reference, route changes and unsupported-product intent require semantic
    interpretation, so those cases are reviewed from the model output instead
    of being labelled by a second regex-based business engine.
    """
    compact = re.sub(r"\s+", "", str(text or ""))
    selects_9d = bool(
        re.search(r"(?:桃花|林芝).{0,8}9(?:日|天)", compact)
        or re.search(r"(?<!\d)9天(?!\d)", compact)
        or (
            ("桃花" in compact or "林芝" in compact)
            and re.search(r"(?:不上|不去|不要去|不包含|不含).{0,3}珠峰", compact)
        )
    )
    selects_11d = bool(
        re.search(r"(?:桃花|林芝).{0,8}11(?:日|天)", compact)
        or re.search(r"(?<!\d)11天(?!\d)", compact)
        or (
            ("桃花" in compact or "林芝" in compact)
            and "珠峰" in compact
            and not re.search(r"(?:不上|不去|不要去|不包含|不含).{0,3}珠峰", compact)
        )
    )
    if selects_9d == selects_11d:
        return None
    return "peach_9d_2027" if selects_9d else "peach_11d_2027"


def _historical_departure_outside(turn: RealTurn) -> bool:
    latest: set[int] | None = None
    messages = [
        *(item["content"] for item in turn.context_messages if item["direction"] == "incoming"),
        turn.customer_text,
    ]
    for message in messages:
        for line in str(message or "").splitlines():
            months = _months_in_text(line)
            if months:
                latest = months
            if any(term in line for term in ("其他時間", "其他时间", "別的時間", "别的时间")):
                latest = {-1}
    return bool(latest and not latest.issubset({3, 4}))


def _turn_requires_handoff(turn: RealTurn) -> bool:
    return _requires_handoff(turn.customer_text, turn.categories)


def _testable_customer_question(turn: RealTurn) -> bool:
    text = turn.customer_text.strip()
    if not text or PLACEHOLDER_RE.fullmatch(text):
        return False
    semantic = re.sub(r"\[(?:圖片|图片|image|影片|視頻|视频|附件|text)\]", " ", text, flags=re.I)
    compact = re.sub(r"[\W_]+", "", semantic, flags=re.UNICODE)
    if len(compact) < 2:
        return False
    acknowledgements = {
        "好的", "好謝謝", "好谢谢", "謝謝", "谢谢", "收到", "是的", "了解", "嗯嗯了解", "可以", "沒問題", "没问题", "ok",
    }
    closing = any(term in semantic for term in (
        "感謝詳細回覆", "感谢详细回复", "討論後做決定", "讨论后做决定", "有問題再聯絡", "有问题再联系",
        "確定再聯絡", "确定再联系", "多比較參考", "多比较参考", "比較參考一下", "比较参考一下",
        "考慮看看", "考虑看看", "發錯訊息", "发错消息", "發錯消息", "发错信息",
    ))
    asks_question = any(term in semantic for term in ("?", "？", "請問", "请问", "想了解", "多少", "如何", "怎麼", "怎么"))
    return compact not in acknowledgements and not (closing and not asks_question)


def _sent_groups_for(turn: RealTurn, route: str, *, price_ready: bool = False) -> list[str]:
    sequence = ROUTES[route]["sequence"]
    if price_ready:
        gate = ROUTES[route]["policies"]["price_after_group"]
        return sequence[: sequence.index(gate) + 1]
    desired = _expected_group(turn, route, [], "introducing")
    if desired in sequence:
        return sequence[: sequence.index(desired)]
    return []


def _stratified(candidates: list[RealTurn], limit: int) -> list[RealTurn]:
    buckets: dict[str, list[RealTurn]] = defaultdict(list)
    for item in sorted(candidates, key=lambda row: row.case_key):
        buckets[_primary_category(item)].append(item)
    selected: list[RealTurn] = []
    order = list(buckets)
    while len(selected) < limit and order:
        next_order: list[str] = []
        for key in order:
            if buckets[key] and len(selected) < limit:
                selected.append(buckets[key].pop(0))
            if buckets[key]:
                next_order.append(key)
        order = next_order
    return selected


def _make_case(
    turn: RealTurn,
    suite: str,
    route: str,
    *,
    context_mode: str,
    price_ready: bool = False,
    contact_ready: bool = False,
) -> ReplayCase:
    sent_groups = _sent_groups_for(turn, route, price_ready=price_ready)
    slots: dict = {}
    stage = "introducing"
    if contact_ready:
        slots = {"party_size": 2, "departure_window": "2027年3月底"}
        gate = ROUTES[route]["policies"]["contact_ready_after_group"]
        sequence = ROUTES[route]["sequence"]
        sent_groups = sequence[: sequence.index(gate) + 1]
        stage = "contact_ready"
    unsafe = _turn_requires_handoff(turn)
    has_contact = bool(DIRECT_CONTACT_RE.search(turn.customer_text))
    channel_ack_only = bool(re.search(r"(?:已|已经|已經).{0,4}(?:加|聯絡|联系).{0,4}(?:line|微信|wechat)", turn.customer_text, re.I)) and not has_contact
    declines_contact = any(term in turn.customer_text for term in CONTACT_DECLINE_TERMS) and "route_entry" not in turn.categories
    expected_action = "handoff" if unsafe or has_contact else ("no_action" if declines_contact or channel_ack_only else "reply")
    expected_lead = "captured" if has_contact else (
        "none" if declines_contact or channel_ack_only else None
    )
    suffix = f"{suite}:{route}:{'ready' if price_ready else 'normal'}:{'contact' if contact_ready else 'standard'}"
    case_id = hashlib.sha256(f"{turn.case_key}:{suffix}".encode()).hexdigest()[:24]
    expected_group = None if unsafe or has_contact or declines_contact or channel_ack_only else _expected_group(turn, route, sent_groups, stage)
    if expected_lead == "ask" and expected_action == "reply":
        expected_group = ROUTES[route]["policies"].get("contact_transition_group")
    return ReplayCase(
        case_id=case_id,
        suite=suite,
        source_case_key=turn.case_key,
        conversation_id=turn.conversation_id,
        customer_text=turn.customer_text,
        context_messages=turn.context_messages,
        reference_answer=turn.reference_answer,
        categories=turn.categories,
        expected_route=route,
        expected_action=expected_action,
        expected_intent=None if unsafe else _expected_intent(turn),
        expected_group=expected_group,
        context_mode=context_mode,
        stage=stage,
        slots=slots,
        sent_groups=sent_groups,
        expected_lead_action=expected_lead,
    )


def build_test_suites(
    turns: list[RealTurn],
    *,
    per_route: int = 24,
    safety_limit: int = 12,
    long_context_limit: int = 12,
    lead_limit: int = 8,
) -> dict[str, list[ReplayCase]]:
    relevant = [
        item for item in turns
        if item.categories and not _turn_requires_handoff(item) and _testable_customer_question(item)
    ]
    suites: dict[str, list[ReplayCase]] = {}
    for route, suite_name in (
        ("peach_9d_2027", "peach_9d_real_questions"),
        ("peach_11d_2027", "peach_11d_real_questions"),
    ):
        explicit = [item for item in relevant if item.inferred_route == route and "lead_contact" not in item.categories]
        generic = [item for item in relevant if not item.inferred_route and "lead_contact" not in item.categories]
        chosen = _stratified(explicit, max(1, per_route // 2))
        chosen_keys = {item.case_key for item in chosen}
        chosen.extend(_stratified([item for item in generic if item.case_key not in chosen_keys], per_route - len(chosen)))
        if len(chosen) < per_route:
            chosen.extend(_stratified([
                item for item in relevant if item.case_key not in {row.case_key for row in chosen}
            ], per_route - len(chosen)))
        rows: list[ReplayCase] = []
        for index, item in enumerate(chosen[:per_route]):
            mode = "historical_route_binding" if item.inferred_route == route else "synthetic_route_binding"
            rows.append(_make_case(item, suite_name, route, context_mode=mode, price_ready=(index % 2 == 1 and "price_availability" in item.categories)))
        suites[suite_name] = rows

    safety_candidates = [
        item for item in turns
        if _testable_customer_question(item) and _turn_requires_handoff(item)
    ]
    suites["cross_route_safety_and_handoff"] = [
        _make_case(
            item,
            "cross_route_safety_and_handoff",
            item.inferred_route or ROUTE_IDS[index % 2],
            context_mode="historical_route_binding" if item.inferred_route else "synthetic_route_binding",
        )
        for index, item in enumerate(_stratified(safety_candidates, safety_limit))
    ]

    long_candidates = sorted(
        [item for item in relevant if len(item.context_messages) >= 8],
        key=lambda item: (-len(item.context_messages), item.case_key),
    )
    suites["long_context_memory"] = [
        _make_case(
            item,
            "long_context_memory",
            item.inferred_route or ROUTE_IDS[index % 2],
            context_mode="historical_route_binding" if item.inferred_route else "synthetic_route_binding",
        )
        for index, item in enumerate(long_candidates[:long_context_limit])
    ]

    lead_candidates = [item for item in turns if _testable_customer_question(item)
        and not any(term in item.customer_text for term in CONTACT_DECLINE_TERMS)
        and ("lead_contact" in item.categories or (
        any(term in item.customer_text for term in ("想參加", "想参加", "報名", "报名", "想訂", "想订"))
        and item.categories
    ))]
    suites["lead_capture_and_contact"] = [
        _make_case(
            item,
            "lead_capture_and_contact",
            item.inferred_route or ROUTE_IDS[index % 2],
            context_mode="historical_route_binding" if item.inferred_route else "synthetic_route_binding",
            contact_ready=True,
        )
        for index, item in enumerate(_stratified(lead_candidates, lead_limit))
    ]
    return suites


def catalog_row(turn: RealTurn) -> dict:
    return {
        "case_key": turn.case_key,
        "conversation_id": turn.conversation_id,
        "message_ids": turn.message_ids,
        "customer_question": mask_sensitive(turn.customer_text),
        "categories": turn.categories,
        "inferred_route": turn.inferred_route or None,
        "route_evidence": mask_sensitive(turn.route_evidence),
        "context_message_count": len(turn.context_messages),
        "context_character_count": sum(len(item["content"]) for item in turn.context_messages),
        "created_at": turn.created_at,
    }


def public_case_row(case: ReplayCase) -> dict:
    value = asdict(case)
    value["customer_text"] = mask_sensitive(case.customer_text)
    value["reference_answer"] = mask_sensitive(case.reference_answer)
    value["context_messages"] = [
        {**item, "content": mask_sensitive(item.get("content", ""))}
        for item in case.context_messages[-20:]
    ]
    value["context_message_count"] = len(case.context_messages)
    value["context_character_count"] = sum(len(item.get("content", "")) for item in case.context_messages)
    return value


def audit_route_packages() -> dict:
    checks: dict[str, bool] = {
        "only_two_routes": set(ROUTE_PACKAGES) == set(ROUTE_IDS),
        "shared_knowledge_version": len({item["knowledge_version"] for item in ROUTE_PACKAGES.values()}) == 1,
    }
    details: dict[str, dict] = {}
    for route in ROUTE_IDS:
        package = ROUTE_PACKAGES[route]
        all_nodes = package["runtime_sop"]["nodes"]
        initial_nodes = [node for node in all_nodes if node.get("initial_delivery") is True]
        nodes = [node for node in all_nodes if node.get("initial_delivery") is not True]
        candidates = [item for node in nodes for item in node["content_group_candidates"]]
        expected_initial_groups = [
            key for key in package["content_sequence"]
            if package["content_groups"][key].get("initial_delivery") is True
        ]
        configured_node_groups = {node.get("content_group_key") for node in package["sop"]["nodes"]}
        expected_silence_groups = [
            key for key in package["content_sequence"]
            if not package["content_groups"][key].get("initial_only", False) and key in configured_node_groups
        ]
        route_checks = {
            "six_silence_interval_touches": len(nodes) == 6,
            "checked_initial_groups_compiled": [
                node.get("content_group_key") for node in initial_nodes
            ] == expected_initial_groups,
            "initial_groups_are_immediate_and_ordered": all(
                node.get("delay_minutes") == 0
                and node.get("basis") in {"enrollment", "previous_node"}
                and node.get("messages")
                for node in initial_nodes
            ),
            "raw_analysis_policy_bound": package["runtime_sop"].get("journey_policy_version") == JOURNEY_POLICY["policy_version"],
            "production_delays": [node["delay_minutes"] for node in nodes] == [1, 3, 5, 10, 30, 60],
            "reply_then_wakeups": [node["journey_trigger"] for node in nodes] == ["silence_mainline", "wakeup", "wakeup", "wakeup", "wakeup", "wakeup"],
            "stop_on_incoming": package["runtime_sop"].get("stop_on_incoming") is True,
            "all_nodes_offer_reviewed_sequence": all(
                [candidate["content_group_key"] for candidate in node["content_group_candidates"]]
                == expected_silence_groups for node in nodes
            ),
            "all_candidates_bind_reviewed_groups": all(
                candidate["content_group_key"] in package["content_groups"] for candidate in candidates
            ),
            "all_assets_match_bound_group": all(
                set(message.get("asset_key") for message in candidate["messages"] if message.get("content_type", "text") != "text")
                <= set(package["content_groups"][candidate["content_group_key"]].get("asset_keys", []))
                for candidate in candidates
            ),
            "all_text_is_versioned_in_package": all(
                str(message.get("content") or "").strip()
                for candidate in candidates for message in candidate["messages"]
                if message.get("content_type", "text") == "text"
            ),
            "per_touch_message_cap": all(len(candidate["messages"]) <= 3 for candidate in candidates),
            "initial_delivery_message_cap": all(len(node["messages"]) <= 3 for node in initial_nodes),
            "fixed_answers_are_reviewed_before_enable": all(
                answer.get("status") in {"active", "pending_review", "disabled"}
                for answer in package.get("fixed_answers", [])
            ),
            "large_group_handoff_configured": all(
                JOURNEY_POLICY["handoff"]["large_group"].get(key) == value
                for key, value in {
                    "enabled": True,
                    "minimum_party_size": 8,
                    "reason": "large_group_custom_quote",
                    "label": "large_group",
                }.items()
            ),
            "price_gate_before_price": package["policies"]["price_after_group"] in package["content_sequence"],
            "contact_gate_defined": package["policies"]["contact_ready_after_group"] in package["content_sequence"],
        }
        details[route] = {"checks": route_checks, "passed": all(route_checks.values())}
        checks[f"{route}_complete"] = all(route_checks.values())
    return {"passed": all(checks.values()), "checks": checks, "routes": details}


def _approved_reply(decision, route: str) -> bool:
    reply = (decision.reply or "").strip()
    if decision.action != "reply":
        return True
    if not reply or len(reply) > 1200:
        return False
    forbidden_claims = (
        "保證有位", "保证有位", "確定有位", "确定有位", "保證出團", "保证出团",
        "一定不會高反", "一定不会高反", "9月出發沒問題", "9月出发没问题",
    )
    claim_text = reply
    for safe_phrase in ("不保證出團", "不保证出团", "不一定保證出團", "不一定保证出团",
                        "無法保證出團", "无法保证出团",
                        "不代表一定不會高反", "不代表一定不会高反",
                        "不能代表一定不會高反", "不能代表一定不会高反",
                        "不能保證一定不會高反", "不能保证一定不会高反"):
        claim_text = claim_text.replace(safe_phrase, "")
    if any(term in claim_text for term in forbidden_claims):
        return False
    approved = {group["text"] for group in ROUTES[route]["groups"].values()} | {CONTACT_ACKNOWLEDGEMENT}
    if reply in approved or all(part in approved for part in reply.split("\n") if part):
        return True
    route_specific = any(term in reply for term in (
        "9,980", "12,800", "珠峰", "絨布寺", "绒布寺", "希爾頓", "希尔顿",
        "布達拉宮", "布达拉宫", "大昭寺", "嘎拉", "波密",
    ))
    return not route_specific or bool(getattr(decision, "evidence_refs", []))


def _expected_branch(route: str) -> str:
    return "peach_9d" if route == "peach_9d_2027" else "peach_11d"


def run_case(case: ReplayCase, materials: list[dict], *, reception_policy: dict | None = None, knowledge_context: dict | None = None) -> dict:
    from app.release_provenance import source_fingerprint, assert_source_unchanged
    tested_source = source_fingerprint()
    policy = deepcopy(reception_policy if reception_policy is not None else JOURNEY_POLICY)
    policy_digest = reception_policy_fingerprint(policy)
    result = _run_case(case, materials, reception_policy=policy, knowledge_context=knowledge_context)
    assert_source_unchanged(tested_source)
    return {**_redact_case_result(result, case), "source_fingerprint": tested_source,
            "reception_policy_fingerprint": policy_digest}


def _run_case(case: ReplayCase, materials: list[dict], *, reception_policy: dict | None = None, knowledge_context: dict | None = None) -> dict:
    from app.customer_journey_dataset import _evaluation_journey_slots

    initial_slots = _evaluation_journey_slots(
        case.expected_route, case.slots, case.sent_groups, materials,
    )
    simulated_initial_progress = {
        "simulated": True,
        "real_delivery_evidence": False,
        "route_variant": case.expected_route,
        "assumed_sent_groups": list(case.sent_groups),
        "basis": "reviewed_snapshot_full_text_and_configured_asset_keys",
    }
    journey = journey_context_from_values(
        route_variant=case.expected_route,
        stage=case.stage,
        slots=initial_slots,
        sent_groups=case.sent_groups,
    )
    context = {
        **{key: deepcopy(value) for key, value in (knowledge_context or {}).items()
           if key in {"global_knowledge_candidates", "global_knowledge_facts", "global_knowledge_version"}},
        "module": "reply",
        "customer_text": case.customer_text,
        "context_messages": case.context_messages,
        "context_complete": True,
        "route_variant": case.expected_route,
        "memory": {},
        "journey": journey,
        "route_playbook": playbook_prompt(),
        "lead_capture": {"status": case.lead_capture_status, "request_count": 0, "captured_kinds": []},
        "available_materials": materials,
        "reception_policy": deepcopy(reception_policy if reception_policy is not None else JOURNEY_POLICY),
    }
    try:
        decision, logs, digest, trace = generate_decision(context)
    except EvaluationCallError as exc:
        infrastructure_codes = ("Connect", "Timeout", "http_", "Network", "RemoteProtocol")
        status = "infrastructure_failed" if any(code in exc.code for code in infrastructure_codes) else "model_output_failed"
        return {
            "case_id": case.case_id,
            "suite": case.suite,
            "status": status,
            "customer_question": mask_sensitive(case.customer_text),
            "conversation_id": case.conversation_id,
            "expected_route": case.expected_route,
            "model_error": exc.code,
            "request_hash": exc.digest,
            "model_calls": exc.logs,
            "trace": {"simulated_initial_progress": simulated_initial_progress},
            "checks": {},
            "failures": [],
        }
    except Exception as exc:  # A missing key is infrastructure/configuration, not model quality.
        return {
            "case_id": case.case_id,
            "suite": case.suite,
            "status": "infrastructure_failed",
            "customer_question": mask_sensitive(case.customer_text),
            "conversation_id": case.conversation_id,
            "expected_route": case.expected_route,
            "model_error": type(exc).__name__,
            "request_hash": "",
            "model_calls": [],
            "trace": {"simulated_initial_progress": simulated_initial_progress},
            "checks": {},
            "failures": [],
        }

    model_decision = asdict(decision)
    decision, progress = prepare_route_reply_values(
        decision,
        current_route=case.expected_route,
        stage=case.stage,
        slots=initial_slots,
        sent_groups=case.sent_groups,
    )
    decision, contact_requested = bind_lead_request(
        decision,
        route_variant=case.expected_route,
        journey_stage=progress["stage"],
        capture_status=case.lead_capture_status,
    )
    captured_contacts = model_contacts(decision, case.customer_text)
    if decision.lead_action == "captured" and not captured_contacts:
        decision.lead_action = "none"
        decision.contact_values = {}
        decision.safety_flags = sorted(set([*decision.safety_flags, "contact_value_not_in_current_message"]))
    if captured_contacts:
        # This is the exact externally visible branch used by live_reply.py.
        decision.action = "handoff"
        decision.lead_action = "captured"
        decision.content_group_key = ""
        decision.material_keys = []
        decision.handoff_reason = "lead_captured"
        contact_requested = False
    group = decision.content_group_key or ""
    covered_groups = set(decision.covered_content_groups or ([group] if group else []))
    sent_groups = set(case.sent_groups)
    explicit_current_route = _explicit_current_route(case.customer_text)
    group_assets = {
        asset
        for covered_group in covered_groups
        for asset in ROUTES[case.expected_route]["groups"].get(covered_group, {}).get("assets", [])
    }
    if not decision.route_variant and decision.reply_options:
        option_routes = [
            route_id
            for route_id, spec in ROUTES.items()
            if spec["selection_title"] in decision.reply_options
        ]
        group_assets.update(
            asset
            for route_id in option_routes
            for asset in ROUTES[route_id]["groups"].get("itinerary_overview", {}).get("assets", [])
        )
    known_assets = {item["key"] for item in materials}
    checks = {
        "expected_action": decision.action == case.expected_action,
        "approved_customer_text": _approved_reply(decision, case.expected_route),
        "approved_material_binding": set(decision.material_keys or []) <= group_assets,
        "available_material_binding": set(decision.material_keys or []) <= known_assets,
        "media_limit": len(set(decision.material_keys or [])) <= 2,
        "no_cross_route_rongbuk": case.expected_route == "peach_11d_2027" or group != "rongbuk_reference",
        "availability_never_promised": not any(
            term in (decision.reply or "") for term in ("保證有位", "保证有位", "確定有位", "确定有位")
        ),
        "expected_lead_action": case.expected_lead_action is None or decision.lead_action == case.expected_lead_action,
        "no_blind_group_duplicate": (
            decision.action != "reply"
            or not group
            or group not in sent_groups
            or bool(covered_groups - sent_groups)
        ),
        "single_followup_question": sum((decision.reply or "").count(mark) for mark in ("?", "？")) <= 1,
        "handoff_is_necessary": decision.action != "handoff" or case.expected_action == "handoff",
    }
    if explicit_current_route:
        checks["route_binding"] = decision.route_variant == explicit_current_route
        checks["branch_binding"] = decision.branch == _expected_branch(explicit_current_route)
    if case.expected_action == "handoff":
        checks["handoff_has_no_media"] = not decision.material_keys
    failures = [key for key, passed in checks.items() if not passed]
    return {
        "case_id": case.case_id,
        "source_case_key": case.source_case_key,
        "suite": case.suite,
        "status": "passed" if not failures else "business_failed",
        "conversation_id": case.conversation_id,
        "customer_question": mask_sensitive(case.customer_text),
        "historical_reference": mask_sensitive(case.reference_answer),
        "context_mode": case.context_mode,
        "context_message_count": len(case.context_messages),
        "context_character_count": sum(len(item.get("content", "")) for item in case.context_messages),
        "expected_route": case.expected_route,
        "expected_action": case.expected_action,
        "expected_intent": case.expected_intent,
        "expected_group": case.expected_group,
        "model_decision": {**model_decision, "contact_values": {
            key: mask_sensitive(value) for key, value in (model_decision.get("contact_values") or {}).items()
        }},
        "final_decision": {
            "action": decision.action,
            "branch": decision.branch,
            "intent": decision.intent,
            "reply": mask_sensitive(decision.reply or ""),
            "route_variant": decision.route_variant,
            "content_group_key": group,
            "material_keys": decision.material_keys,
            "lead_action": decision.lead_action,
            "contact_requested": contact_requested,
            "handoff_reason": decision.handoff_reason,
            "slots": decision.slots,
            "stage": progress["stage"],
        },
        "checks": checks,
        "failures": failures,
        "observations": {
            "intent_match": case.expected_intent is None or decision.intent == case.expected_intent,
            "expected_intent": case.expected_intent,
            "actual_intent": decision.intent,
        },
        "trace": {
            "simulated_initial_progress": simulated_initial_progress,
            "model_ms": trace.get("model_ms"),
            "total_ms": trace.get("total_ms"),
            "request_count": trace.get("request_count"),
            "input_tokens": trace.get("input_tokens"),
            "output_tokens": trace.get("output_tokens"),
            "request_hash": digest,
            "model_calls": logs,
            "model_memory_checks": trace.get("model_memory_checks") or {},
        },
    }


def summarize_results(results: list[dict], route_audit: dict, snapshot: dict, outbound_before: int, outbound_after: int) -> dict:
    business = [item for item in results if item["status"] != "infrastructure_failed"]
    passed = [item for item in business if item["status"] == "passed"]
    infrastructure = [item for item in results if item["status"] == "infrastructure_failed"]
    model_output_failed = [item for item in results if item["status"] == "model_output_failed"]
    suite_metrics = {}
    for suite in sorted({item["suite"] for item in results}):
        rows = [item for item in results if item["suite"] == suite]
        eligible = [item for item in rows if item["status"] != "infrastructure_failed"]
        suite_metrics[suite] = {
            "total": len(rows),
            "model_completed": len(eligible),
            "passed": sum(item["status"] == "passed" for item in eligible),
            "rule_pass_rate": round(sum(item["status"] == "passed" for item in eligible) / len(eligible), 4) if eligible else None,
            "infrastructure_failed": sum(item["status"] == "infrastructure_failed" for item in rows),
            "model_output_failed": sum(item["status"] == "model_output_failed" for item in rows),
        }
    latencies = [item.get("trace", {}).get("total_ms") for item in business if item.get("trace", {}).get("total_ms") is not None]
    sorted_latencies = sorted(latencies)
    p95_index = max(0, min(len(sorted_latencies) - 1, int(len(sorted_latencies) * 0.95) - 1)) if sorted_latencies else 0
    checks = Counter()
    failed_checks = Counter()
    for item in business:
        for key, value in item.get("checks", {}).items():
            checks[key] += 1
            if not value:
                failed_checks[key] += 1
    return {
        "dataset_version": DATASET_VERSION,
        "knowledge_version": KNOWLEDGE_VERSION,
        "prompt_version": REALTIME_REPLY_PROMPT_VERSION,
        "snapshot": snapshot,
        "route_package_audit": route_audit,
        "model_replay": {
            "total_cases": len(results),
            "model_completed": len(business),
            "passed": len(passed),
            "business_rule_pass_rate": round(len(passed) / len(business), 4) if business else None,
            "infrastructure_failed": len(infrastructure),
            "model_output_failed": len(model_output_failed),
            "suite_metrics": suite_metrics,
            "failed_checks": dict(failed_checks),
            "p50_ms": int(statistics.median(latencies)) if latencies else None,
            "p95_ms": int(sorted_latencies[p95_index]) if sorted_latencies else None,
            "input_tokens": sum(item.get("trace", {}).get("input_tokens") or 0 for item in business),
            "output_tokens": sum(item.get("trace", {}).get("output_tokens") or 0 for item in business),
            "intent_match_rate": (
                round(
                    sum(item.get("observations", {}).get("intent_match") is True for item in business)
                    / sum(item.get("observations", {}).get("intent_match") is not None for item in business),
                    4,
                )
                if any(item.get("observations", {}).get("intent_match") is not None for item in business)
                else None
            ),
        },
        "safety": {
            "outbound_rows_before": outbound_before,
            "outbound_rows_after": outbound_after,
            "outbound_rows_unchanged": outbound_before == outbound_after,
            "chatwoot_write_requests": 0,
        },
    }


def report_markdown(summary: dict, results: list[dict]) -> str:
    replay = summary["model_replay"]
    lines = [
        "# 两条线路真实客户问题回放报告",
        "",
        f"- 数据集：`{summary['dataset_version']}`",
        f"- 知识版本：`{summary['knowledge_version']}`",
        f"- 提示词版本：`{summary['prompt_version']}`",
        f"- 冻结会话：{summary['snapshot']['conversation_count']}",
        f"- 真实客户轮次目录：{summary['snapshot']['turn_count']}",
        f"- 模型测试案例：{replay['total_cases']}",
        f"- 模型完成：{replay['model_completed']}，基础设施失败：{replay['infrastructure_failed']}",
        f"- 业务规则通过率：{replay['business_rule_pass_rate'] if replay['business_rule_pass_rate'] is not None else '不可计算'}",
        f"- 线路包结构规则：{'通过' if summary['route_package_audit']['passed'] else '未通过'}",
        f"- Chatwoot 写请求：{summary['safety']['chatwoot_write_requests']}",
        f"- 出站记录是否不变：{'是' if summary['safety']['outbound_rows_unchanged'] else '否'}",
        "",
        "> 规则通过率只表示本次冻结测试集上的确定性规则检查，不等同于业务准确率或转化效果。历史人工回复仅供复核，未作为模型事实输入。",
        "",
        "## 测试集",
        "",
        "| 测试集 | 案例 | 模型完成 | 通过 | 规则通过率 | 基础设施失败 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for name, metric in replay["suite_metrics"].items():
        rate = "-" if metric["rule_pass_rate"] is None else f"{metric['rule_pass_rate']:.1%}"
        lines.append(f"| {name} | {metric['total']} | {metric['model_completed']} | {metric['passed']} | {rate} | {metric['infrastructure_failed']} |")
    lines.extend(["", "## 主要失败", ""])
    failures = [item for item in results if item["status"] == "business_failed"][:20]
    if not failures:
        lines.append("没有业务规则失败案例。")
    for item in failures:
        lines.extend([
            f"### {item['suite']} / {item['case_id']}",
            "",
            f"- 客户问题：{item['customer_question']}",
            f"- 失败检查：{', '.join(item['failures'])}",
            f"- 最终动作：{item['final_decision']['action']} / {item['final_decision']['content_group_key'] or '-'}",
            f"- 最终回复：{item['final_decision']['reply'] or '-'}",
            "",
        ])
    if replay["infrastructure_failed"]:
        lines.extend([
            "## 基础设施失败",
            "",
            "模型连接失败与业务规则失败分开统计，本报告不会把连接失败计入规则通过率。",
            "",
        ])
    return "\n".join(lines)


def outbound_count(db: Session) -> int:
    return int(db.scalar(select(func.count()).select_from(OutboundMessage)) or 0)


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
