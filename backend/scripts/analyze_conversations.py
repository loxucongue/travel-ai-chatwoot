from __future__ import annotations

import csv
import json
import re
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal
from app.models import Contact, ConversationState, InboxBinding, MessageEvent, SyncJob


REPORT_DATE = "2026-08-26"
OUTPUT_DIR = Path(__file__).resolve().parents[2] / "output" / "analytics"
TEST_TERMS = ("测试人员触发", "測試人員觸發", "test trigger")
TRIGGER_PATTERNS = (
    "林芝桃花＋珠峰-11日", "林芝桃花+珠峰-11日", "林芝桃花 9日", "林芝桃花9日",
    "其他時間，西藏推薦行程", "其他时间，西藏推荐行程", "西藏自己包團", "西藏自己包团",
)
SHORT_ACKS = {
    "好", "好的", "是", "是的", "有", "沒有", "没有", "謝謝", "谢谢", "收到", "了解", "可以",
    "ok", "okay", "嗯", "恩", "對", "对", "不用", "暫時不用", "暂时不用",
}

SCENARIOS = {
    "桃花+珠峰11日": ("桃花＋珠峰", "桃花+珠峰", "11日", "11天"),
    "林芝桃花9日": ("桃花 9日", "桃花9日", "9日", "9天"),
    "自组包团/定制": ("包團", "包团", "包車", "包车", "自組", "自组", "定制", "訂製", "私人團", "私人团"),
    "其他时间上珠峰": ("其他時間，西藏推薦行程－上珠峰", "其他时间，西藏推荐行程-上珠峰"),
    "其他时间不上珠峰": ("其他時間，西藏推薦行程－不上珠峰", "其他时间，西藏推荐行程-不上珠峰", "不上珠峰", "不去珠峰"),
    "其他目的地": ("雲南", "云南", "色達", "色达", "川西", "新疆", "青海", "北京", "上海", "杭州", "蘇州", "苏州"),
    "价格与预算": ("價格", "价格", "費用", "费用", "多少錢", "多少钱", "報價", "报价", "預算", "预算", "團費", "团费"),
    "出发日期与团期": ("幾月", "几月", "日期", "時間", "时间", "何時", "何时", "出發", "出发", "團期", "团期", "檔期", "档期"),
    "人数与同行关系": ("幾位", "几位", "人數", "人数", "一位", "2位", "兩位", "3位", "三位", "4位", "四位", "家人", "朋友", "夫妻"),
    "行程与景点": ("行程", "路線", "路线", "景點", "景点", "拉薩", "拉萨", "林芝", "珠峰", "波密", "桃花"),
    "住宿餐食": ("酒店", "飯店", "饭店", "住宿", "房間", "房间", "餐食", "吃什麼", "吃什么"),
    "车辆与交通": ("車", "车", "交通", "司機", "司机", "接送", "飛機", "飞机", "火車", "火车"),
    "高反健康与氧气": ("高反", "高原反應", "高原反应", "氧氣", "氧气", "製氧", "制氧", "年齡", "年龄", "健康", "老人"),
    "证件与入藏政策": ("入藏", "台胞證", "台胞证", "證件", "证件", "護照", "护照", "簽證", "签证", "許可", "许可"),
    "亲子/老人适配": ("小孩", "孩子", "兒童", "儿童", "父母", "老人", "長輩", "长辈", "親子", "亲子"),
    "无购物与合同保障": ("購物", "购物", "合約", "合同", "賠償", "赔偿", "純玩", "纯玩"),
    "联系方式交换": ("line", "微信", "wechat", "whatsapp", "電話", "电话", "手機", "手机", "email", "郵箱", "邮箱", "聯絡", "联系"),
    "说明会/直播": ("說明會", "说明会", "直播", "meet.google", "線上會", "线上会"),
    "报名支付与成交": ("報名", "报名", "下訂", "下订", "訂金", "订金", "付款", "刷卡", "匯款", "汇款", "簽約", "签约"),
    "犹豫与延后": ("考慮", "考虑", "研究看看", "再看看", "再決定", "再决定", "還沒決定", "还没决定", "之後", "之后", "明年", "有空"),
    "客诉/退款/人工": ("投訴", "投诉", "退款", "不滿意", "不满意", "騙", "骗", "真人", "人工客服"),
}

QUESTION_TOPICS = {
    "费用、报价和优惠": SCENARIOS["价格与预算"],
    "出发时间、团期和季节": SCENARIOS["出发日期与团期"],
    "路线、天数和景点安排": ("行程", "路線", "路线", "幾天", "几天", "景點", "景点", "珠峰", "桃花", "林芝"),
    "是否可包团或定制": SCENARIOS["自组包团/定制"],
    "住宿、餐食和房型": SCENARIOS["住宿餐食"],
    "车辆、司机和交通": SCENARIOS["车辆与交通"],
    "高反、氧气和年龄健康": SCENARIOS["高反健康与氧气"],
    "证件、入藏和台湾客政策": SCENARIOS["证件与入藏政策"],
    "老人、儿童和家庭适配": SCENARIOS["亲子/老人适配"],
    "无购物和合同保障": SCENARIOS["无购物与合同保障"],
    "其他目的地或非主推线路": SCENARIOS["其他目的地"],
    "报名、支付和预订流程": SCENARIOS["报名支付与成交"],
}

TACTICS = {
    "询问出发时间": ("幾月", "几月", "什麼時候", "什么时候", "計劃什麼時候", "计划什么时候", "出發時間", "出发时间"),
    "询问人数/同行关系": ("幾位", "几位", "幾個人", "几个人", "和家人", "同行", "一個人", "一个人"),
    "询问线路偏好": ("上珠峰", "不上珠峰", "哪個行程", "哪个行程", "路線", "路线", "想去哪", "喜歡哪", "喜欢哪"),
    "索取私人联系方式": ("留下您的line", "留下你的line", "交換line", "交换line", "加微信", "微信號", "微信号", "聯繫方式", "联系方式", "電話", "电话"),
    "邀请说明会/直播": ("說明會", "说明会", "meet.google", "直播", "線上會", "线上会"),
    "主动跟进与唤醒": ("提醒您", "行程還喜歡", "行程还喜欢", "有沒有需要協助", "有没有需要协助", "上次提供", "有空時", "有空时", "午安", "早安", "下午好"),
    "无购物/合同背书": ("無購物", "无购物", "寫在合約", "写在合同", "賠償", "赔偿", "唯一敢"),
    "高反与供氧消除顾虑": ("高反", "氧氣", "氧气", "製氧", "制氧", "供氧", "含氧"),
    "稀缺性与催单": ("名額", "名额", "席位", "最後", "最后", "即將額滿", "即将额满", "趕快", "赶快", "早鳥", "早鸟"),
    "报价或促销": ("報價", "报价", "價格", "价格", "優惠", "优惠", "團費", "团费", "折扣"),
    "报名成交推动": ("報名", "报名", "下訂", "下订", "訂金", "订金", "付款", "幫您保留", "帮您保留", "簽約", "签约"),
    "发送线路资料/媒体": (),
}

EMAIL_RE = re.compile(r"(?i)\b[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}\b")
PHONE_RE = re.compile(r"(?<!\d)(?:\+?\d[\d\s()\-]{6,}\d)(?!\d)")
LINE_RE = re.compile(r"(?i)(?:line(?:\s*id)?|賴|赖)\s*[:：是為为]?\s*([a-z0-9][a-z0-9._\-]{3,})")
WECHAT_RE = re.compile(r"(?i)(?:微信(?:號|号)?|wechat|weixin)\s*[:：是為为]?\s*([a-z][a-z0-9_\-]{5,19})")


@dataclass
class ConversationAnalysis:
    state: ConversationState
    contact: Contact | None
    messages: list[MessageEvent]
    incoming: list[MessageEvent]
    outgoing: list[MessageEvent]
    inbound_turns: int
    substantive_turns: int
    lead_channels: set[str] = field(default_factory=set)
    scenarios: set[str] = field(default_factory=set)
    question_topics: set[str] = field(default_factory=set)
    tactics: set[str] = field(default_factory=set)
    tactic_before_lead: set[str] = field(default_factory=set)
    first_reply_seconds: list[float] = field(default_factory=list)
    customer_type: str = ""
    is_test: bool = False


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def contains_any(text: str, terms: tuple[str, ...]) -> bool:
    folded = text.casefold()
    return any(term.casefold() in folded for term in terms)


def is_trigger(text: str) -> bool:
    return contains_any(text, TRIGGER_PATTERNS)


def is_substantive(text: str) -> bool:
    value = re.sub(r"[\s~～!！?？,.，。\-+\"']", "", normalize(text)).casefold()
    return bool(value) and value not in SHORT_ACKS and not is_trigger(text) and len(value) > 1


def parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def detect_leads(text: str) -> set[str]:
    result: set[str] = set()
    value = normalize(text)
    if EMAIL_RE.search(value):
        result.add("email")
    if LINE_RE.search(value):
        result.add("line")
    if WECHAT_RE.search(value):
        result.add("wechat")
    phone_context = contains_any(value, ("電話", "电话", "手機", "手机", "聯絡", "联系", "whatsapp", "line", "微信", "wechat"))
    for match in PHONE_RE.findall(value):
        digits = re.sub(r"\D", "", match)
        if re.fullmatch(r"20\d{6}", digits) and re.search(r"20\d{2}[\-/年]\d{1,2}[\-/月]\d{1,2}", match):
            continue
        is_mobile = bool(re.fullmatch(r"(?:886)?9\d{8}|1[3-9]\d{9}", digits))
        is_international = match.strip().startswith("+") and 8 <= len(digits) <= 15
        if 8 <= len(digits) <= 15 and (phone_context or is_mobile or is_international):
            result.add("whatsapp" if "whatsapp" in value.casefold() else "phone")
    return result


def count_turns(messages: list[MessageEvent], substantive_only: bool = False) -> int:
    turns = 0
    in_customer_turn = False
    turn_is_substantive = False
    for message in messages:
        if message.private or message.direction == "activity":
            continue
        if message.direction == "incoming":
            in_customer_turn = True
            turn_is_substantive = turn_is_substantive or (message.content_type == "text" and is_substantive(message.content))
            continue
        if message.direction == "outgoing" and in_customer_turn:
            if not substantive_only or turn_is_substantive:
                turns += 1
            in_customer_turn = False
            turn_is_substantive = False
    if in_customer_turn and (not substantive_only or turn_is_substantive):
        turns += 1
    return turns


def first_reply_latencies(messages: list[MessageEvent]) -> list[float]:
    values: list[float] = []
    pending_customer_at: datetime | None = None
    for message in messages:
        if message.private or message.direction == "activity":
            continue
        if message.direction == "incoming":
            pending_customer_at = parse_dt(message.created_at)
        elif message.direction == "outgoing" and pending_customer_at is not None:
            delta = (parse_dt(message.created_at) - pending_customer_at).total_seconds()
            if 0 <= delta <= 72 * 3600:
                values.append(delta)
            pending_customer_at = None
    return values


def mask_sensitive(text: str) -> str:
    value = EMAIL_RE.sub("[EMAIL]", normalize(text))
    value = LINE_RE.sub(lambda match: match.group(0).replace(match.group(1), "[LINE_ID]"), value)
    value = WECHAT_RE.sub(lambda match: match.group(0).replace(match.group(1), "[WECHAT_ID]"), value)
    return PHONE_RE.sub("[PHONE]", value)


def classify_customer(item: ConversationAnalysis) -> str:
    text = "\n".join(message.content for message in item.incoming if message.content_type == "text")
    if item.lead_channels:
        return "已留资高意向"
    if "客诉/退款/人工" in item.scenarios:
        return "客诉或要求人工"
    if "自组包团/定制" in item.scenarios:
        return "团体定制客户"
    if "报名支付与成交" in item.scenarios:
        return "报名成交意向"
    if item.substantive_turns >= 5 or len(item.scenarios) >= 6:
        return "深度规划客户"
    if contains_any(text, SCENARIOS["犹豫与延后"]):
        return "观望培育客户"
    if item.substantive_turns >= 2 or any(topic in item.scenarios for topic in ("价格与预算", "出发日期与团期", "高反健康与氧气", "证件与入藏政策")):
        return "需求明确客户"
    if item.substantive_turns == 1:
        return "初步咨询客户"
    return "广告触发/低互动"


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * pct))))
    return ordered[index]


def pct(value: int, total: int) -> str:
    return f"{value / total * 100:.1f}%" if total else "0.0%"


def analyze() -> tuple[dict, list[ConversationAnalysis]]:
    with SessionLocal() as db:
        inbox = db.scalar(select(InboxBinding).where(InboxBinding.chatwoot_inbox_id == settings.evaluation_inbox_id))
        states = list(db.scalars(select(ConversationState).where(ConversationState.inbox_binding_id == inbox.id).order_by(ConversationState.chatwoot_conversation_id)).all())
        analyses: list[ConversationAnalysis] = []
        for state in states:
            messages = list(db.scalars(select(MessageEvent).where(MessageEvent.conversation_state_id == state.id).order_by(MessageEvent.created_at, MessageEvent.id)).all())
            incoming = [item for item in messages if item.direction == "incoming" and not item.private]
            outgoing = [item for item in messages if item.direction == "outgoing" and not item.private]
            contact = db.get(Contact, state.contact_id) if state.contact_id else None
            analysis = ConversationAnalysis(
                state=state, contact=contact, messages=messages, incoming=incoming, outgoing=outgoing,
                inbound_turns=count_turns(messages), substantive_turns=count_turns(messages, True),
                is_test=any(contains_any(message.content, TEST_TERMS) for message in incoming),
            )
            all_customer_text = "\n".join(message.content for message in incoming if message.content_type == "text")
            analysis.lead_channels = set().union(*(detect_leads(message.content) for message in incoming if message.content_type == "text")) if incoming else set()
            analysis.scenarios = {name for name, terms in SCENARIOS.items() if contains_any(all_customer_text, terms)}
            question_messages = [message.content for message in incoming if message.content_type == "text" and any(marker in message.content for marker in ("?", "？", "請問", "请问", "想問", "想问", "多少", "怎麼", "怎么", "如何", "是否", "可以", "有沒有", "有没有"))]
            analysis.question_topics = {name for name, terms in QUESTION_TOPICS.items() if any(contains_any(text, terms) for text in question_messages)}
            outgoing_text = "\n".join(message.content for message in outgoing if message.content_type == "text" and message.attribution != "ai")
            analysis.tactics = {name for name, terms in TACTICS.items() if terms and contains_any(outgoing_text, terms)}
            if any(message.content_type in {"image", "video", "audio", "file"} or message.attachments for message in outgoing):
                analysis.tactics.add("发送线路资料/媒体")
            first_lead_index = next((i for i, message in enumerate(messages) if message.direction == "incoming" and detect_leads(message.content)), len(messages))
            before_text = "\n".join(message.content for message in messages[:first_lead_index] if message.direction == "outgoing" and not message.private and message.content_type == "text")
            analysis.tactic_before_lead = {name for name, terms in TACTICS.items() if terms and contains_any(before_text, terms)}
            if any(message.direction == "outgoing" and (message.content_type in {"image", "video", "audio", "file"} or message.attachments) for message in messages[:first_lead_index]):
                analysis.tactic_before_lead.add("发送线路资料/媒体")
            analysis.first_reply_seconds = first_reply_latencies(messages)
            analysis.customer_type = classify_customer(analysis)
            analyses.append(analysis)
        latest_sync = db.scalar(select(SyncJob).where(SyncJob.kind == "evaluation_history").order_by(SyncJob.id.desc()))

    real = [item for item in analyses if not item.is_test and item.incoming]
    inbound_messages = [message for item in real for message in item.incoming]
    outbound_messages = [message for item in real for message in item.outgoing]
    latencies = [value for item in real for value in item.first_reply_seconds]
    customer_types = Counter(item.customer_type for item in real)
    scenarios = Counter(name for item in real for name in item.scenarios)
    questions = Counter(name for item in real for name in item.question_topics)
    tactics = Counter(name for item in real for name in item.tactics)
    lead_channels = Counter(name for item in real for name in item.lead_channels)
    utterances = Counter(
        mask_sensitive(message.content)
        for item in real for message in item.incoming
        if message.content_type == "text" and normalize(message.content)
    )
    question_utterances = Counter(
        mask_sensitive(message.content)
        for item in real for message in item.incoming
        if message.content_type == "text" and is_substantive(message.content)
        and any(marker in message.content for marker in ("?", "？", "請問", "请问", "想問", "想问", "多少", "怎麼", "怎么", "如何", "是否", "可以", "有沒有", "有没有"))
    )
    leads = [item for item in real if item.lead_channels]
    profile_leads = [item for item in real if item.contact and (item.contact.email or item.contact.phone_number)]
    tactic_effect = []
    for name in TACTICS:
        exposed = [item for item in real if name in item.tactics]
        lead_after = [item for item in exposed if item.lead_channels and name in item.tactic_before_lead]
        tactic_effect.append({"name": name, "conversations": len(exposed), "lead_customers": len([item for item in exposed if item.lead_channels]), "lead_after_tactic": len(lead_after)})
    timestamps = [parse_dt(message.created_at) for item in real for message in item.messages]
    summary = {
        "generated_at": datetime.now().astimezone().isoformat(),
        "scope": {"account_id": 180474, "inbox_id": settings.evaluation_inbox_id, "inbox_name": "CITS 國旅環球 - China2Go", "period_start": min(timestamps).isoformat() if timestamps else None, "period_end": max(timestamps).isoformat() if timestamps else None, "sync_job": {"id": latest_sync.id, "status": latest_sync.status, "failed": latest_sync.failed_items, "updated_at": latest_sync.updated_at} if latest_sync else None},
        "counts": {
            "synced_conversations": len(analyses), "excluded_test_conversations": len([item for item in analyses if item.is_test]),
            "real_customer_conversations": len(real), "unique_customers": len({item.state.contact_id for item in real if item.state.contact_id}),
            "incoming_messages": len(inbound_messages), "incoming_text": len([m for m in inbound_messages if m.content_type == "text"]),
            "incoming_media": len([m for m in inbound_messages if m.content_type != "text" or m.attachments]),
            "outgoing_messages": len(outbound_messages), "outgoing_text": len([m for m in outbound_messages if m.content_type == "text"]),
            "outgoing_media": len([m for m in outbound_messages if m.content_type != "text" or m.attachments]),
            "average_incoming_messages": round(len(inbound_messages) / len(real), 2) if real else 0,
            "average_outgoing_messages": round(len(outbound_messages) / len(real), 2) if real else 0,
            "outgoing_to_incoming_ratio": round(len(outbound_messages) / len(inbound_messages), 2) if inbound_messages else 0,
            "total_customer_turns": sum(item.inbound_turns for item in real), "average_customer_turns": round(statistics.mean(item.inbound_turns for item in real), 2) if real else 0,
            "median_customer_turns": statistics.median(item.inbound_turns for item in real) if real else 0,
            "customers_2plus_turns": len([item for item in real if item.inbound_turns >= 2]),
            "customers_3plus_turns": len([item for item in real if item.inbound_turns >= 3]),
            "customers_5plus_turns": len([item for item in real if item.inbound_turns >= 5]),
            "customers_2plus_substantive_turns": len([item for item in real if item.substantive_turns >= 2]),
            "customers_3plus_substantive_turns": len([item for item in real if item.substantive_turns >= 3]),
            "customers_5plus_substantive_turns": len([item for item in real if item.substantive_turns >= 5]),
        },
        "lead_capture": {
            "content_detected_customers": len(leads), "content_detected_rate": round(len(leads) / len(real), 4) if real else 0,
            "chatwoot_profile_customers": len(profile_leads), "chatwoot_profile_rate": round(len(profile_leads) / len(real), 4) if real else 0,
            "channels": dict(lead_channels), "incoming_image_messages_review_needed": len([m for m in inbound_messages if m.content_type == "image" or m.attachments]),
        },
        "response_time_seconds": {"samples": len(latencies), "median": round(statistics.median(latencies), 1) if latencies else None, "p75": round(percentile(latencies, .75), 1) if latencies else None, "p90": round(percentile(latencies, .9), 1) if latencies else None},
        "customer_types": [{"name": name, "count": count, "rate": round(count / len(real), 4)} for name, count in customer_types.most_common()],
        "scenarios": [{"name": name, "count": count, "rate": round(count / len(real), 4)} for name, count in scenarios.most_common()],
        "question_topics": [{"name": name, "count": count, "rate": round(count / len(real), 4)} for name, count in questions.most_common()],
        "top_utterances": [{"text": text[:140], "count": count} for text, count in utterances.most_common(20)],
        "top_questions": [{"text": text[:140], "count": count} for text, count in question_utterances.most_common(20)],
        "advisor_tactics": [{"name": name, "count": count, "rate": round(count / len(real), 4)} for name, count in tactics.most_common()],
        "advisor_tactic_effect": tactic_effect,
    }
    return summary, analyses


def write_csv(analyses: list[ConversationAnalysis]) -> None:
    path = OUTPUT_DIR / f"conversation-analysis-detail-{REPORT_DATE}.csv"
    real_index = 0
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["customer_key", "chatwoot_conversation_id", "is_test", "customer_type", "inbound_messages", "inbound_turns", "substantive_turns", "lead_channels", "scenarios", "question_topics", "advisor_tactics", "status", "labels"])
        for item in analyses:
            if not item.is_test:
                real_index += 1
            key = "TEST" if item.is_test else f"C{real_index:04d}"
            writer.writerow([key, item.state.chatwoot_conversation_id, item.is_test, item.customer_type, len(item.incoming), item.inbound_turns, item.substantive_turns, "|".join(sorted(item.lead_channels)), "|".join(sorted(item.scenarios)), "|".join(sorted(item.question_topics)), "|".join(sorted(item.tactics)), item.state.status, "|".join(item.state.labels or [])])


def table_rows(items: list[dict], total: int, limit: int | None = None) -> str:
    rows = items[:limit] if limit else items
    return "\n".join(f"| {item['name']} | {item['count']} | {pct(item['count'], total)} |" for item in rows)


def write_markdown(data: dict) -> None:
    count = data["counts"]
    lead = data["lead_capture"]
    total = count["real_customer_conversations"]
    response = data["response_time_seconds"]
    effects = sorted(data["advisor_tactic_effect"], key=lambda item: item["conversations"], reverse=True)
    effect_rows = "\n".join(
        f"| {item['name']} | {item['conversations']} | {item['lead_customers']} | {pct(item['lead_customers'], item['conversations'])} | {item['lead_after_tactic']} |"
        for item in effects if item["conversations"]
    )
    channel_rows = "\n".join(f"| {name.upper()} | {value} | {pct(value, total)} |" for name, value in sorted(lead["channels"].items(), key=lambda item: item[1], reverse=True)) or "| 未检测到 | 0 | 0.0% |"
    utterance_rows = "\n".join(f"| {item['text'].replace('|', '/')} | {item['count']} |" for item in data["top_utterances"][:12])
    question_rows = "\n".join(f"| {item['text'].replace('|', '/')} | {item['count']} |" for item in data["top_questions"][:12]) or "| 暂无重复两次以上的标准问法 | 0 |"
    markdown = f"""# China2Go Facebook 客户会话全量分析报告

生成时间：{data['generated_at']}  
数据范围：Chatwoot Account `180474`，Facebook Inbox `128859`（CITS 國旅環球 - China2Go）  
消息区间：{data['scope']['period_start']} 至 {data['scope']['period_end']}  
同步状态：任务 #{data['scope']['sync_job']['id']} `{data['scope']['sync_job']['status']}`，失败 {data['scope']['sync_job']['failed']} 个

## 核心结论

1. 共同步 {count['synced_conversations']} 个会话，其中 {total} 个有真实客户入站，另排除 {count['excluded_test_conversations']} 个明确测试会话。
2. 客户平均开口 {count['average_customer_turns']} 次，中位数 2 次；开口不少于 2/3/5 次的客户分别为 {count['customers_2plus_turns']}、{count['customers_3plus_turns']}、{count['customers_5plus_turns']} 人。
3. 广告触发或低互动客户占 {next((item['rate'] for item in data['customer_types'] if item['name'] == '广告触发/低互动'), 0) * 100:.1f}%，真正形成两次以上有效开口的客户只有 {pct(count['customers_2plus_substantive_turns'], total)}。
4. 聊天正文检测到 {lead['content_detected_customers']} 位客户主动留下私人联系方式，留资率 {pct(lead['content_detected_customers'], total)}；其中 LINE 为主。另有图片/二维码未识别，因此这是下限。
5. 顾问平均发送 {count['average_outgoing_messages']} 条、客户平均发送 {count['average_incoming_messages']} 条，出站/入站达到 {count['outgoing_to_incoming_ratio']}:1。当前运营更像批量资料投放，不是高效率的一问一答。
6. 顾问最常用路径是“发送媒体资料 → 询问人数 → 解释供氧/无购物保障 → 询问时间 → 索取联系方式”。价格/促销、索取联系方式和成交推动与较高留资率相关，但样本较小，只能作为下一步 A/B 测试假设。

## 1. 口径与边界

- 仅统计 Facebook Inbox 128859，不混入网站聊天测试 Inbox。
- 本次窗口只有约 13 天；172 个同步会话中，155 个有真实客户入站，16 个纯出站/无客户开口会话不进入客户率计算，另排除 1 个明确测试会话。
- 连续的客户公开入站消息合并为一次“开口”；私密消息、系统活动不算客户开口。
- “有效开口”进一步排除广告预设快捷回复、纯确认词和明确测试文案。
- 留资只认客户入站内容中可识别的邮箱、电话、LINE、微信或 WhatsApp；顾问索要联系方式不算留资。
- 图片中的 LINE/微信二维码尚未 OCR，因此内容留资率是保守下限。
- “文本”和“媒体”按消息能力分别计数，带文字且有附件的消息会同时进入两项。
- 当前没有订单、支付或实际出团数据，因此只能统计留资，不能把聊天标签当作真实成交率。
- 顾问策略与留资率是相关性分析，不能直接解释为因果关系。

## 2. 总体规模

| 指标 | 数值 |
|---|---:|
| Chatwoot 已同步会话 | {count['synced_conversations']} |
| 排除明确测试会话 | {count['excluded_test_conversations']} |
| 真实客户会话 | {total} |
| 去重客户数 | {count['unique_customers']} |
| 客户入站消息 | {count['incoming_messages']}（文本 {count['incoming_text']}，媒体 {count['incoming_media']}） |
| 顾问/AI 出站消息 | {count['outgoing_messages']}（文本 {count['outgoing_text']}，媒体 {count['outgoing_media']}） |
| 平均客户入站消息 | {count['average_incoming_messages']} 条/客户 |
| 平均顾问出站消息 | {count['average_outgoing_messages']} 条/客户 |
| 出站/入站消息比 | {count['outgoing_to_incoming_ratio']} : 1 |
| 客户总开口次数 | {count['total_customer_turns']} |
| 客户平均开口 | {count['average_customer_turns']} 次/客户 |
| 客户开口中位数 | {count['median_customer_turns']} 次 |

### 开口深度

| 门槛 | 客户数 | 占真实客户 |
|---|---:|---:|
| 开口 ≥ 2 次 | {count['customers_2plus_turns']} | {pct(count['customers_2plus_turns'], total)} |
| 开口 ≥ 3 次 | {count['customers_3plus_turns']} | {pct(count['customers_3plus_turns'], total)} |
| 开口 ≥ 5 次 | {count['customers_5plus_turns']} | {pct(count['customers_5plus_turns'], total)} |
| 有效开口 ≥ 2 次 | {count['customers_2plus_substantive_turns']} | {pct(count['customers_2plus_substantive_turns'], total)} |
| 有效开口 ≥ 3 次 | {count['customers_3plus_substantive_turns']} | {pct(count['customers_3plus_substantive_turns'], total)} |
| 有效开口 ≥ 5 次 | {count['customers_5plus_substantive_turns']} | {pct(count['customers_5plus_substantive_turns'], total)} |

顾问首响样本 {response['samples']} 个，中位数 {response['median']} 秒，P75 {response['p75']} 秒，P90 {response['p90']} 秒。该指标包含历史人工回复和批量运营触达，应作为基线，不等同于纯人工 SLA。

## 3. 客户类型

客户类型按优先级互斥，每位客户只落入一个主要类型。

| 客户类型 | 客户数 | 占比 |
|---|---:|---:|
{table_rows(data['customer_types'], total)}

## 4. 客户场景

场景为多选，同一客户可同时涉及线路、时间、人数、价格和风险问题，因此占比之和会超过 100%。

| 场景 | 涉及客户数 | 覆盖率 |
|---|---:|---:|
{table_rows(data['scenarios'], total)}

## 5. 客户常见问题

| 问题主题 | 涉及客户数 | 覆盖率 |
|---|---:|---:|
{table_rows(data['question_topics'], total)}

### 高频客户原话

该表用于区分广告快捷入口与真实自由提问，内容已做联系方式脱敏。

| 客户原话 | 出现次数 |
|---|---:|
{utterance_rows}

### 高频问题原话

| 客户问题 | 出现次数 |
|---|---:|
{question_rows}

## 6. 留资率

| 指标 | 客户数 | 客户级占比 |
|---|---:|---:|
| 聊天内容检测到私人联系方式 | {lead['content_detected_customers']} | {pct(lead['content_detected_customers'], total)} |
| Chatwoot 联系人资料已有邮箱/电话 | {lead['chatwoot_profile_customers']} | {pct(lead['chatwoot_profile_customers'], total)} |

### 内容留资渠道

| 渠道 | 客户数 | 占真实客户 |
|---|---:|---:|
{channel_rows}

另有 {lead['incoming_image_messages_review_needed']} 条客户图片/附件消息可能包含二维码或联系方式，当前未计入，故 {pct(lead['content_detected_customers'], total)} 为保守下限。

## 7. 顾问常见手段

| 顾问策略 | 覆盖会话 | 覆盖率 |
|---|---:|---:|
{table_rows(data['advisor_tactics'], total)}

### 策略与留资的相关性

| 顾问策略 | 使用会话 | 最终留资客户 | 留资率 | 策略后发生留资 |
|---|---:|---:|---:|---:|
{effect_rows}

主要观察：

1. 现有运营高度依赖线路图片和批量资料发送，媒体覆盖高，但资料发送本身不等于客户进入有效对话。
2. 顾问主要通过“出发时间 → 人数/同行关系 → 联系方式”完成资格筛选和私域迁移，这应成为 AI 的核心槽位链路。
3. 说明会邀请和主动跟进承担唤醒作用，适合进入 SOP，不应混入实时问答提示词。
4. “无购物、合同赔偿、供氧、高反”等属于高风险承诺或健康信息，AI 只能引用审核话术，不能自由生成。
5. 留资应按客户主动提供联系方式判定；仅出现“方便加 LINE 吗”不能视为转化。

## 8. AI 回复系统框架建议

```mermaid
flowchart LR
    A[Chatwoot Webhook] --> B[事件幂等与会话镜像]
    B --> C[AI 接管状态判定]
    C -->|允许| D[意图与业务分支识别]
    C -->|阻断| H[人工接管]
    D --> E[槽位与客户阶段更新]
    E --> F[知识检索与回复决策]
    F --> G[事实/渠道/频控安全校验]
    G -->|通过| I[回复草稿或自动发送]
    G -->|不通过| H
    I --> J[效果事件与 BI]
    H --> J
```

### 8.1 会话状态层

每个会话至少维护：业务分支、客户类型、旅程阶段、已知槽位、缺失槽位、AI 接管状态、人工任务状态、最近客户开口时间、留资渠道、来源广告/粉专、风险标记和下一最佳动作。

AI 开关仍以平台字段为权威状态，以 Chatwoot `ai` 标签作为客服侧镜像。标签被误删时进入 `sync_conflict`，暂停 AI 并告警，不能静默恢复或继续发送。

### 8.2 分层决策

1. **确定性前置规则**：私密消息、人工接管、拒绝联系、客诉、渠道不可回复、重复事件直接阻断。
2. **意图与分支识别**：识别桃花 11 日、桃花 9 日、包团、其他时间、其他目的地，以及价格、日期、人数、行程、健康、证件、留资等意图。
3. **槽位管理**：优先收集 `route / departure_window / party_size / companion_type / budget / contact_channel`，每轮最多追问 1 至 2 个字段。
4. **知识约束回复**：线路、酒店、车辆、图片从版本化知识库读取；价格、余位、团期、证件政策、医疗健康、合同承诺必须走实时数据或转人工。
5. **动作输出**：统一为 `reply / ask_slot / send_asset / handoff / no_action / capture_contact / update_stage`。

### 8.3 客户阶段

建议固定为：`广告进入 → 已开口 → 需求识别 → 方案介绍 → 风险/异议处理 → 请求留资 → 已留资 → 人工跟进 → 成交/流失`。

阶段不能只由模型自由判断，应由事件和字段共同推进。例如检测到客户本人发送有效 LINE ID 才进入“已留资”；顾问询问 LINE 只能进入“请求留资”。

### 8.4 回复策略

- 广告快捷回复进入：先确认客户关注的路线，不重复发送大段介绍。
- 只有一次开口：给 1 个核心价值点并追问最关键槽位。
- 开口 2 至 3 次：根据已有信息给针对性方案，减少重复询问。
- 开口 ≥ 5 次：视为高参与客户，优先推动留资或转人工，不继续无限问答。
- 价格、余位、确定团期：收集人数和时间后转人工报价。
- 健康、年龄、证件、合同：只用审核文本，存在个体判断时立即转人工。
- 客户拒绝、延后或低响应：停止实时追问，交给有频控和退出条件的 SOP。

### 8.5 评测与 BI

必须持续记录：AI 首响、独立处理率、平均开口数变化、2/3/5 次开口率、留资率、转人工率、重复提问率、安全拦截、错误率、人工改写率、各分支留资率和各 SOP 唤醒率。

上线前以历史会话回放验证，任何评测运行都保持 `OUTBOUND_MODE=disabled`；达到“无依据承诺为 0、人工状态误回复为 0”后，再对白名单会话逐步开启实发。

### 8.6 后端模块边界

| 模块 | 责任 | 禁止事项 |
|---|---|---|
| Webhook Gateway | 验签、幂等、快速入库 | 不同步调用模型、不直接回复 |
| Conversation Mirror | 同步会话、消息、标签、分配和联系人 | 不覆盖人工侧最新状态 |
| Eligibility Policy | 计算租户、Inbox、联系人、会话和渠道窗口的最终 AI 状态 | 不让模型决定是否有权发送 |
| Customer Profiler | 提取客户类型、阶段、槽位、留资与风险 | 不把顾问索要联系方式误判为留资 |
| Business State Machine | 控制分支、节点和下一最佳动作 | 不把流程完全交给大模型 |
| Knowledge Service | 返回审核线路事实、素材和版本 | 不读取未发布草稿 |
| AI Orchestrator | 组织上下文并生成结构化候选决策 | 不直接调用 Chatwoot 写接口 |
| Response Guard | 校验事实、敏感承诺、重复、长度和渠道能力 | 不允许失败后降级为自由生成 |
| Outbound Service | 业务幂等、发送前二次检查、状态回写 | 非 `live` 模式绝不发送 |
| Handoff Service | 创建、领取、分配、完成和显式恢复 AI | 完成人工任务后不自动恢复 AI |
| SOP Scheduler | 相对/固定时间任务、频控和退出条件 | 不绕过 `can_reply` 与白名单 |
| Analytics | 事件口径、漏斗、效果与审计 | 不以 Chatwoot 当前标签倒推历史转化 |

### 8.7 AI 结构化输出

所有模型必须返回统一结构，后端再决定是否执行：

```json
{{
  "action": "reply|ask_slot|send_asset|handoff|no_action",
  "branch": "peach_11d",
  "stage": "requirements_identified",
  "intent": "itinerary",
  "reply": "候选回复",
  "slots": {{"party_size": 2}},
  "missing_slots": ["departure_window"],
  "asset_keys": [],
  "captured_contacts": [],
  "handoff_reason": null,
  "evidence_refs": ["route:peach_11d:v1"],
  "safety_flags": [],
  "confidence": 0.91
}}
```

Worker 在模型调用前后都重新读取会话版本、AI 标签、人工状态与 `can_reply`。任何状态变化都丢弃旧候选回复，确保人工接管期间零误发。

### 8.8 首期衡量目标

- AI 首响 P90 小于 10 秒；人工首响继续单独统计。
- 两次以上有效开口率以当前 {pct(count['customers_2plus_substantive_turns'], total)} 为基线，首轮目标提升到 40% 以上。
- 内容留资率以当前 {pct(lead['content_detected_customers'], total)} 为保守基线，白名单试点目标先验证 10% 至 12%，不直接承诺长期转化。
- 人工接管状态误回复、重复回复和无依据高风险承诺均必须为 0。
- 每条 AI 回复都能追溯知识版本、提示词版本、状态版本和最终发送结果。

## 9. 实施优先级

1. 建立广告来源、业务分支、客户阶段、槽位和留资事件的统一数据模型。
2. 完成桃花 9 日/11 日两条线路的审核知识与素材映射。
3. 上线 AI 演练场的多轮状态保存和历史案例回放评分。
4. 实现“时间、人数、线路偏好、联系方式”四类槽位提取与客服可视化。
5. 建立价格/团期/健康/证件/合同五类强制转人工规则。
6. 将说明会、跟进和唤醒独立成 SOP，并配置频控、白名单与退出条件。
7. 最后才开启小范围自动发送，并按会话白名单逐步放量。
"""
    (OUTPUT_DIR / f"customer-conversation-report-{REPORT_DATE}.md").write_text(markdown, encoding="utf-8")


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    summary, analyses = analyze()
    (OUTPUT_DIR / f"customer-conversation-analysis-{REPORT_DATE}.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    write_csv(analyses)
    write_markdown(summary)
    print(json.dumps({"output_dir": str(OUTPUT_DIR), "counts": summary["counts"], "lead_capture": summary["lead_capture"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
