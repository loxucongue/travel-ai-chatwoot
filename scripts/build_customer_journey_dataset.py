from __future__ import annotations

import csv
import json
import re
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ANALYSIS_JSON = ROOT / "output" / "analytics" / "chatwoot-customer-scenario-analysis-20260903-092757.json"
DETAIL_CSV = ROOT / "output" / "analytics" / "chatwoot-customer-scenario-detail-20260903-092757.csv"
OUT = ROOT / "data" / "evaluation" / "chatwoot_customer_journeys" / "v2026-09-03"
CORE_ANSWER_TARGET = 100


def mask(text: str) -> str:
    value = str(text or "")
    value = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[email]", value)
    value = re.sub(r"(?<!\d)(?:\+?\d[\d\s().-]?){8,16}\d(?!\d)", "[phone]", value)
    value = re.sub(
        r"(?i)((?:line|wechat|weixin|微信|whatsapp)\s*(?:id|账号|帳號|是|:|：)?\s*)[A-Za-z][A-Za-z0-9_.@+-]{3,}",
        r"\1[contact_id]",
        value,
    )
    return value[:1000]


def split_labels(value: str) -> list[str]:
    return [item.strip() for item in str(value or "").split("|") if item.strip()]


def read_details() -> list[dict]:
    with DETAIL_CSV.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def find_source(rows: list[dict], *, customer_type: str = "", scenario: str = "", topic: str = "") -> int | None:
    for row in rows:
        if customer_type and row.get("customer_type") != customer_type:
            continue
        if scenario and scenario not in row.get("scenarios", ""):
            continue
        if topic and topic not in row.get("question_topics", ""):
            continue
        return int(row["conversation_id"])
    return None


def case(
    case_id: str,
    title: str,
    suite: str,
    scenario: str,
    customer_type: str,
    messages: list[dict],
    expected: dict,
    *,
    source_conversation_id: int | None = None,
    initial_route_variant: str = "",
    initial_memory: dict | None = None,
    initial_sent_groups: list[str] | None = None,
    lead_capture: dict | None = None,
    controls: dict | None = None,
    mask_messages: bool = False,
) -> dict:
    return {
        "case_id": case_id,
        "title": title,
        "suite": suite,
        "scenario": scenario,
        "customer_type": customer_type,
        "source_conversation_id": source_conversation_id,
        "messages": [
            {
                "role": m.get("role", "customer"),
                "content": mask(m["content"]) if mask_messages else m["content"],
                **({"content_type": m["content_type"]} if m.get("content_type") else {}),
            }
            for m in messages
        ],
        "initial_route_variant": initial_route_variant,
        "initial_memory": initial_memory or {},
        "initial_sent_groups": initial_sent_groups or [],
        "lead_capture": lead_capture or {"status": "not_started", "request_count": 0, "captured_kinds": []},
        "controls": controls or {"ai_label": True, "can_reply": True, "human": False},
        "expected": expected,
    }


def fixed_answer_cases(rows: list[dict]) -> list[dict]:
    src = lambda **kw: find_source(rows, **kw)
    return [
        case(
            "answer_route_9d_intro",
            "9 日普通咨询应回复并进入线路",
            "answer",
            "route_intro",
            "初步有效咨询客户",
            [{"content": "我想了解桃花9日行程"}],
            {"action": "reply", "route_variant": "peach_9d_2027", "must_answer_topics": ["itinerary"], "lead_action": "none", "will_enroll_sop": True},
            source_conversation_id=src(scenario="明确咨询桃花9日"),
        ),
        case(
            "answer_route_11d_intro",
            "11 日普通咨询应回复并进入线路",
            "answer",
            "route_intro",
            "初步有效咨询客户",
            [{"content": "我想了解林芝桃花加珠峰11日"}],
            {"action": "reply", "route_variant": "peach_11d_2027", "must_answer_topics": ["itinerary"], "lead_action": "none", "will_enroll_sop": True},
            source_conversation_id=src(scenario="明确咨询桃花+珠峰11日"),
        ),
        case(
            "answer_price_hotel_high_intent_9d",
            "9 日价格住宿高意向应先回答再留资",
            "answer",
            "price_hotel",
            "价格住宿高意向客户",
            [{"content": "我们2位，明年3月底出发，住宿和价格怎样？"}],
            {
                "action": "reply",
                "route_variant": "peach_9d_2027",
                "slots": {"party_size": "2位", "departure_window": "明年3月底"},
                "must_answer_topics": ["price", "hotel", "contact"],
                "lead_action": "ask",
                "will_enroll_sop": True,
                "next_sop_behavior": "stage_driven_mandatory_touch",
            },
            source_conversation_id=src(customer_type="价格住宿高意向客户", topic="价格报价"),
            initial_route_variant="peach_9d_2027",
        ),
        case(
            "answer_price_hotel_high_intent_11d",
            "11 日价格住宿高意向应先回答再留资",
            "answer",
            "price_hotel",
            "价格住宿高意向客户",
            [{"content": "我们4位，2027年3月下旬想走桃花加珠峰，费用和住宿怎么安排？"}],
            {
                "action": "reply",
                "route_variant": "peach_11d_2027",
                "slots": {"party_size": "4位", "departure_window": "2027年3月下旬"},
                "must_answer_topics": ["price", "hotel", "contact"],
                "lead_action": "ask",
                "will_enroll_sop": True,
            },
            source_conversation_id=src(customer_type="价格住宿高意向客户", scenario="明确咨询桃花+珠峰11日"),
            initial_route_variant="peach_11d_2027",
        ),
        case(
            "answer_route_switch_9_to_11",
            "客户从 9 日切换到 11 日",
            "answer",
            "route_switch",
            "线路对比/切换客户",
            [{"role": "assistant", "content": "9日路线不走珠峰。"}, {"content": "那11日加珠峰的区别是什么？"}],
            {"action": "reply", "route_variant": "peach_11d_2027", "must_answer_topics": ["itinerary"], "will_enroll_sop": True},
            source_conversation_id=src(customer_type="线路对比/切换客户"),
            initial_route_variant="peach_9d_2027",
            initial_sent_groups=["itinerary_overview"],
        ),
        case(
            "answer_route_switch_11_to_9",
            "客户从 11 日切换到 9 日",
            "answer",
            "route_switch",
            "线路对比/切换客户",
            [{"role": "assistant", "content": "11日会去珠峰和绒布寺。"}, {"content": "如果不去珠峰，9日怎么走？"}],
            {"action": "reply", "route_variant": "peach_9d_2027", "must_answer_topics": ["itinerary"], "will_enroll_sop": True},
            source_conversation_id=src(customer_type="线路对比/切换客户"),
            initial_route_variant="peach_11d_2027",
            initial_sent_groups=["itinerary_overview", "rongbuk_reference"],
        ),
        case(
            "answer_large_group_12",
            "12 人及以上应转人工定制",
            "answer",
            "large_group",
            "大团/包团高价值客户",
            [{"content": "我们12个人想包团，明年3月去桃花节"}],
            {"action": "handoff", "handoff_reason": "large_group_custom_quote", "lead_action": "none", "will_enroll_sop": False},
            source_conversation_id=src(customer_type="大团/包团高价值客户"),
        ),
        case(
            "answer_eight_people_no_handoff",
            "8 人继续正常接待",
            "answer",
            "medium_group",
            "初步有效咨询客户",
            [{"content": "我们8个人想了解桃花9日行程"}],
            {"action": "reply", "route_variant": "peach_9d_2027", "slots": {"party_size": "8个人"}, "will_enroll_sop": True},
            source_conversation_id=src(scenario="8-11人多人但非大团"),
        ),
        case(
            "answer_ten_people_no_handoff",
            "10 人继续正常接待",
            "answer",
            "medium_group",
            "初步有效咨询客户",
            [{"content": "10位可以参加桃花加珠峰11日吗？"}],
            {"action": "reply", "route_variant": "peach_11d_2027", "slots": {"party_size": "10位"}, "will_enroll_sop": True},
            source_conversation_id=src(scenario="8-11人多人但非大团"),
        ),
        case(
            "answer_eleven_people_no_handoff",
            "11 人不误判大团",
            "answer",
            "medium_group",
            "初步有效咨询客户",
            [{"content": "我们11个人想了解桃花9日行程"}],
            {"action": "reply", "route_variant": "peach_9d_2027", "slots": {"party_size": "11个人"}, "lead_action": "none", "will_enroll_sop": True},
            source_conversation_id=src(scenario="人数明确"),
        ),
        case(
            "answer_other_destination",
            "资料外线路不硬套桃花产品",
            "answer",
            "other_destination",
            "资料外线路客户",
            [{"content": "你们有云南行程吗？"}],
            {"action": "reply", "route_variant": "", "lead_action": "none", "will_enroll_sop": False},
            source_conversation_id=src(customer_type="资料外线路客户", scenario="资料外目的地"),
        ),
        case(
            "answer_contact_channel_only",
            "只问能不能 LINE 不算已留资",
            "answer",
            "contact_channel",
            "已留资/待顾问承接",
            [{"content": "可以用LINE联系吗？"}],
            {"action": "reply", "lead_action": "ask", "must_answer_topics": ["contact"], "will_enroll_sop": True},
            source_conversation_id=src(topic="联系方式"),
            initial_route_variant="peach_9d_2027",
        ),
        case(
            "answer_contact_value_line",
            "提供 LINE ID 后转人工",
            "answer",
            "contact_value",
            "已留资/待顾问承接",
            [{"content": "我的LINE是 abc123"}],
            {"action": "handoff", "lead_action": "captured", "handoff_reason": "lead_captured", "will_enroll_sop": False},
            source_conversation_id=src(customer_type="已留资/待顾问承接"),
            initial_route_variant="peach_9d_2027",
        ),
        case(
            "answer_contact_value_wechat",
            "提供微信号后转人工",
            "answer",
            "contact_value",
            "已留资/待顾问承接",
            [{"content": "我的微信是 wx_travel_2027"}],
            {"action": "handoff", "lead_action": "captured", "handoff_reason": "lead_captured", "will_enroll_sop": False},
            source_conversation_id=src(customer_type="已留资/待顾问承接"),
            initial_route_variant="peach_9d_2027",
        ),
        case(
            "answer_contact_value_phone",
            "提供电话后转人工",
            "answer",
            "contact_value",
            "已留资/待顾问承接",
            [{"content": "电话可以打 0912345678"}],
            {"action": "handoff", "lead_action": "captured", "handoff_reason": "lead_captured", "will_enroll_sop": False},
            source_conversation_id=src(customer_type="已留资/待顾问承接"),
            initial_route_variant="peach_9d_2027",
        ),
        case(
            "answer_contact_value_email",
            "提供 Email 后转人工",
            "answer",
            "contact_value",
            "已留资/待顾问承接",
            [{"content": "可以寄到 test@example.com"}],
            {"action": "handoff", "lead_action": "captured", "handoff_reason": "lead_captured", "will_enroll_sop": False},
            source_conversation_id=src(customer_type="已留资/待顾问承接"),
            initial_route_variant="peach_9d_2027",
        ),
        case(
            "answer_attachment_requires_vision",
            "依赖图片内容的问题转人工",
            "answer",
            "attachment_question",
            "风险顾虑/政策确认客户",
            [{"content": "图片里的这个行程多少钱？", "content_type": "image"}],
            {"action": "handoff", "handoff_reason": "attachment_requires_vision", "lead_action": "none", "will_enroll_sop": False},
            source_conversation_id=src(scenario="只发图片/附件相关"),
        ),
        case(
            "answer_certificate_policy",
            "证件/入藏政策问题应回答边界",
            "answer",
            "certificate_policy",
            "风险顾虑/政策确认客户",
            [{"content": "台湾人去西藏需要什么证件？会不会影响入藏？"}],
            {"action": "reply", "lead_action": "none"},
            source_conversation_id=src(topic="证件政策"),
        ),
        case(
            "answer_vehicle_question",
            "车辆交通问题应回答",
            "answer",
            "vehicle",
            "深度规划客户",
            [{"content": "这条路线是什么车？司机会一起跟吗？"}],
            {"action": "reply", "must_answer_topics": ["vehicle"], "lead_action": "none"},
            source_conversation_id=src(topic="车辆交通"),
            initial_route_variant="peach_9d_2027",
        ),
        case(
            "answer_departure_question",
            "团期/出发时间问题应回答",
            "answer",
            "departure",
            "深度规划客户",
            [{"content": "明年3月底有团期吗？什么时候出发？"}],
            {"action": "reply", "route_variant": "peach_9d_2027", "slots": {"departure_window": "明年3月底"}, "must_answer_topics": ["departure"]},
            source_conversation_id=src(topic="出发时间/团期"),
            initial_route_variant="peach_9d_2027",
        ),
        case(
            "answer_availability_question",
            "余位/成团问题不直接甩人工",
            "answer",
            "availability",
            "价格住宿高意向客户",
            [{"content": "明年3月底还有位置吗？可以保证出发吗？"}],
            {"action": "reply", "route_variant": "peach_9d_2027", "must_answer_topics": ["departure"], "will_enroll_sop": True},
            source_conversation_id=src(topic="成团/余位/保证出发"),
            initial_route_variant="peach_9d_2027",
        ),
        case(
            "answer_health_altitude",
            "高反健康问题继续回答安全边界",
            "answer",
            "health",
            "风险顾虑/政策确认客户",
            [{"content": "我爸75岁，担心高反和氧气，这样适合去吗？"}],
            {"action": "reply", "lead_action": "none"},
            source_conversation_id=src(topic="高反健康"),
            initial_route_variant="peach_9d_2027",
        ),
        case(
            "answer_payment_contract",
            "报名付款/合同争议边界",
            "answer",
            "payment_contract",
            "风险顾虑/政策确认客户",
            [{"content": "报名要怎么付款？合同保障是什么？"}],
            {"action": "reply", "lead_action": "ask", "must_answer_topics": ["contact"]},
            source_conversation_id=src(topic="报名付款"),
            initial_route_variant="peach_9d_2027",
        ),
        case(
            "answer_complaint_handoff",
            "投诉争议应转人工",
            "answer",
            "complaint",
            "风险顾虑/政策确认客户",
            [{"content": "我要投诉，你们之前报价和现在说的不一样"}],
            {"action": "handoff", "handoff_reason": "complaint", "will_enroll_sop": False},
            source_conversation_id=src(customer_type="风险顾虑/政策确认客户"),
        ),
        case(
            "answer_refund_handoff",
            "退款问题应转人工",
            "answer",
            "refund",
            "风险顾虑/政策确认客户",
            [{"content": "如果我报名后取消，退款怎么处理？"}],
            {"action": "handoff", "handoff_reason": "refund", "will_enroll_sop": False},
            source_conversation_id=src(customer_type="风险顾虑/政策确认客户"),
        ),
        case(
            "answer_no_ai_label_blocked",
            "无 ai 标签不回复",
            "answer",
            "blocked_missing_ai",
            "不回复原因",
            [{"content": "我想了解桃花9日"}],
            {"blocked_reason": "missing_ai_label"},
            controls={"ai_label": False, "can_reply": True, "human": False},
        ),
        case(
            "answer_cannot_reply_blocked",
            "can_reply=false 不回复",
            "answer",
            "blocked_can_reply",
            "不回复原因",
            [{"content": "我想了解桃花9日"}],
            {"blocked_reason": "channel_cannot_reply"},
            controls={"ai_label": True, "can_reply": False, "human": False},
        ),
        case(
            "answer_human_blocked",
            "人工接管后不回复",
            "answer",
            "blocked_human",
            "不回复原因",
            [{"content": "我想了解桃花9日"}],
            {"blocked_reason": "human_or_contact_block"},
            controls={"ai_label": True, "can_reply": True, "human": True},
        ),
        case(
            "answer_send_unknown_blocked",
            "发送状态未知时等待人工核对",
            "answer",
            "blocked_send_unknown",
            "不回复原因",
            [{"content": "刚刚那条有没有发成功？"}],
            {"blocked_reason": "send_state_unknown"},
            controls={"ai_label": True, "can_reply": True, "human": False, "send_state_unknown": True},
        ),
    ]


def sampled_answer_case(row: dict, index: int) -> dict:
    scenarios = split_labels(row.get("scenarios", ""))
    topics = split_labels(row.get("question_topics", ""))
    sample = " / ".join(part.strip() for part in row.get("sample_incoming", "").split("/")[:4] if part.strip())
    first_part = next((part.strip() for part in row.get("sample_incoming", "").split("/") if part.strip()), "")
    expected: dict = {"action": "reply"}
    scenario = "real_sample"
    initial_route = ""
    unsupported_terms = [
        "云南", "雲南", "川西", "稻城", "亞丁", "亚丁", "青甘", "甘青",
        "陝西", "陕西", "歷史博物館", "历史博物馆", "臨潼", "临潼", "包車", "包车",
        "Free travel", "成都", "八天行",
    ]
    opt_out_terms = ["勿需打擾", "勿需打扰", "不要打擾", "不要打扰", "不要聯絡", "不要联系", "退訂", "退订", "封鎖", "封锁", "黑名單", "黑名单"]
    low_intent_terms = ["按錯", "按错", "誤按", "误按", "沒需求", "没需求", "先不用", "都了解", "謝謝", "谢谢", "尚未決定", "尚未决定", "還沒決定", "还没决定", "需要再聯絡", "需要再联系", "找別的旅遊", "找别的旅游"]
    custom_group_terms = ["自己包團", "自己包团", "自組包團", "自组包团", "包團方案", "包团方案", "包車", "包车"]
    platform_risk_terms = ["Facebook 專頁", "Meta", "社群準則", "申訴", "審核資訊", "帳戶", "账户", "舉報", "举报", "驗證您的訊息", "验证您的信息"]
    has_unsupported = any(term in sample for term in unsupported_terms) or "资料外目的地" in scenarios
    has_unsupported_time = "其他時間" in sample or "其他时间" in sample
    has_supported_11d = any(term in sample for term in ("桃花＋珠峰", "桃花+珠峰", "加珠峰", "珠峰-11日", "珠峰11日", "11日"))
    has_supported_9d = any(term in sample for term in ("桃花9日", "9日路线", "9日路線", "九日"))
    has_price_question = any(term in sample for term in ("价格", "價格", "费用", "費用", "报价", "報價", "多少", "多少钱", "多少錢", "預算", "预算", "包含哪些內容", "包含哪些内容"))
    has_hotel_question = any(term in sample for term in ("住宿", "酒店", "飯店", "房差", "房型"))
    has_departure_question = any(term in sample for term in ("团期", "團期", "日期", "出發", "出发", "發團", "发团", "拼團日期", "拼团日期"))
    has_ambiguous_route_label = (
        any(term in sample for term in ("行程1", "行程 1", "行程2", "行程 2", "这个行程", "這個行程"))
        or first_part in {"1", "2", "行程1", "行程2"}
    )
    has_contact_value = (
        "[LINE_ID]" in sample
        or "WeChat ID" in sample
        or "微信是" in sample
        or "LINE是" in sample
        or bool(re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", sample))
        or bool(re.search(r"(?<!\d)0?\d[\d\s-]{7,}\d(?!\d)", sample))
    )
    pure_attachment = sample.strip() in {"[图片]", "[圖片]", "[image]"}
    has_opt_out = any(term in sample for term in opt_out_terms)
    has_low_intent = any(term in sample for term in low_intent_terms)
    if "12人及以上大团" in scenarios:
        scenario = "large_group"
        expected = {"action": "handoff", "handoff_reason": "large_group_custom_quote", "will_enroll_sop": False}
    elif pure_attachment:
        scenario = "pure_attachment_handoff"
        expected = {"action": "handoff", "handoff_reason": "attachment_requires_vision", "will_enroll_sop": False}
    elif any(term in sample for term in platform_risk_terms):
        scenario = "platform_risk_no_action"
        expected = {"action": "no_action", "will_enroll_sop": False}
    elif has_contact_value:
        scenario = "contact_real_sample"
        expected = {"action": "handoff", "lead_action": "captured", "handoff_reason": "lead_captured", "will_enroll_sop": False}
    elif has_opt_out:
        scenario = "explicit_opt_out_real_sample"
        expected = {"action": "no_action", "will_enroll_sop": False}
    elif has_low_intent:
        scenario = "low_intent_recommendation"
        expected.update({"lead_action": "none", "will_enroll_sop": False})
        if "林芝桃花" in sample or "桃花" in sample:
            expected.update({"recommend_supported_product": True})
        else:
            expected.update({"recommend_supported_routes": True})
    elif has_unsupported_time:
        scenario = "other_time_recommendation"
        expected.update({"route_variant": "", "lead_action": "none", "will_enroll_sop": False, "recommend_supported_product": True})
    elif has_ambiguous_route_label and not (has_supported_9d or has_supported_11d):
        scenario = "ambiguous_route_selection"
        expected.update({"route_variant": "", "lead_action": "none", "will_enroll_sop": False, "recommend_supported_routes": True})
    elif any(term in sample for term in custom_group_terms):
        scenario = "custom_group_recommendation"
        expected.update({"route_variant": "", "lead_action": "none", "will_enroll_sop": False, "recommend_supported_routes": True})
    elif has_supported_11d and "按錯" not in sample and "按错" not in sample:
        scenario = "route_11d_real_sample"
        expected.update({"route_variant": "peach_11d_2027", "will_enroll_sop": True})
        topics_expected = []
        if has_price_question:
            topics_expected.append("price")
        if has_hotel_question:
            topics_expected.append("hotel")
        if has_departure_question:
            topics_expected.append("departure")
        if topics_expected:
            expected["must_answer_topics"] = topics_expected
        initial_route = "peach_11d_2027"
    elif has_supported_9d and "按錯" not in sample and "按错" not in sample:
        scenario = "route_9d_real_sample"
        expected.update({"route_variant": "peach_9d_2027", "will_enroll_sop": True})
        topics_expected = []
        if has_price_question:
            topics_expected.append("price")
        if has_hotel_question:
            topics_expected.append("hotel")
        if has_departure_question:
            topics_expected.append("departure")
        if topics_expected:
            expected["must_answer_topics"] = topics_expected
        initial_route = "peach_9d_2027"
    elif has_unsupported:
        scenario = "other_destination"
        expected.update({"route_variant": "", "lead_action": "none", "will_enroll_sop": False, "recommend_supported_routes": True})
    elif "明确咨询桃花+珠峰11日" in scenarios and "按錯" not in sample and "按错" not in sample:
        scenario = "route_11d_real_sample"
        expected.update({"route_variant": "peach_11d_2027", "will_enroll_sop": True})
        initial_route = "peach_11d_2027"
    elif "明确咨询桃花9日" in scenarios and "按錯" not in sample and "按错" not in sample:
        scenario = "route_9d_real_sample"
        expected.update({"route_variant": "peach_9d_2027", "will_enroll_sop": True})
        initial_route = "peach_9d_2027"
    elif "预算/价格敏感" in scenarios or "价格报价" in topics:
        scenario = "price_real_sample"
        expected.update({"must_answer_topics": ["price"]})
    elif "问住宿/房型/单房差" in scenarios:
        scenario = "hotel_real_sample"
        expected.update({"must_answer_topics": ["hotel"]})
    elif "问车辆/司机/交通" in scenarios:
        scenario = "vehicle_real_sample"
        expected.update({"must_answer_topics": ["vehicle"]})
    elif "问证件/入藏/台湾客" in scenarios:
        scenario = "certificate_real_sample"
    elif "只发图片/附件相关" in scenarios:
        scenario = "attachment_real_sample"
    elif "问联系方式/能否加LINE" in scenarios:
        scenario = "contact_real_sample"
    return case(
        f"answer_real_sample_{int(row['conversation_id']):04d}",
        f"真实聊天抽样 #{row['conversation_id']}：{row.get('customer_type') or '未分类'}",
        "answer",
        scenario,
        row.get("customer_type") or "未分类",
        [{"content": sample or "我想了解行程"}],
        expected,
        source_conversation_id=int(row["conversation_id"]),
        initial_route_variant=initial_route,
        mask_messages=True,
    )


def build_answer_cases(rows: list[dict]) -> list[dict]:
    cases = fixed_answer_cases(rows)
    seen = {item["source_conversation_id"] for item in cases if item.get("source_conversation_id") is not None}
    sorted_rows = sorted(
        rows,
        key=lambda row: (
            row.get("customer_type") or "",
            -int(row.get("substantive_turns") or 0),
            int(row.get("conversation_id") or 0),
        ),
    )
    for row in sorted_rows:
        if len(cases) >= CORE_ANSWER_TARGET:
            break
        conv_id = int(row["conversation_id"])
        if conv_id in seen:
            continue
        if not row.get("sample_incoming"):
            continue
        cases.append(sampled_answer_case(row, len(cases) + 1))
        seen.add(conv_id)
    return cases


def build_journey_cases(rows: list[dict]) -> list[dict]:
    src = lambda **kw: find_source(rows, **kw)
    return [
        case(
            "journey_new_9d_silence_mainline",
            "新客户问 9 日后沉默，应进入 1/3/5/10/30/60 分钟主线",
            "journey",
            "silence_mainline",
            "初步有效咨询客户",
            [{"content": "我想了解桃花9日行程"}],
            {"action": "reply", "route_variant": "peach_9d_2027", "will_enroll_sop": True, "next_sop_behavior": "stage_driven_mandatory_touch"},
            source_conversation_id=src(scenario="明确咨询桃花9日"),
        ),
        case(
            "journey_skip_known_party_date",
            "已知人数日期后沉默，不重复问",
            "journey",
            "silence_skip_known_slots",
            "价格住宿高意向客户",
            [{"content": "我们2位，明年3月底出发，住宿和价格怎样？"}],
            {
                "action": "reply",
                "route_variant": "peach_9d_2027",
                "slots": {"party_size": "2位", "departure_window": "明年3月底"},
                "lead_action": "ask",
                "will_enroll_sop": True,
                "next_sop_behavior": "stage_driven_mandatory_touch",
            },
            initial_route_variant="peach_9d_2027",
            source_conversation_id=src(customer_type="价格住宿高意向客户"),
        ),
        case(
            "journey_customer_reply_cancels_old_sop",
            "客户回复后旧 SOP 取消，AI 继续承接",
            "journey",
            "customer_reply_cancels_sop",
            "深度规划客户",
            [{"role": "assistant", "content": "9日行程参考如下。"}, {"content": "我们2位，明年3月底出发，住宿和价格怎样？"}],
            {
                "action": "reply",
                "route_variant": "peach_9d_2027",
                "slots": {"party_size": "2位", "departure_window": "明年3月底"},
                "must_answer_topics": ["price", "hotel"],
                "lead_action": "ask",
                "will_enroll_sop": True,
            },
            initial_route_variant="peach_9d_2027",
            initial_sent_groups=["itinerary_overview"],
            source_conversation_id=src(customer_type="深度规划客户"),
        ),
        case(
            "journey_content_exhausted_no_duplicate",
            "内容全部提供后沉默，不重复轰炸",
            "journey",
            "content_exhausted",
            "深度规划客户",
            [{"content": "还有什么要注意的吗？"}],
            {"action": "reply", "route_variant": "peach_9d_2027", "will_enroll_sop": True, "next_sop_behavior": "stage_driven_mandatory_touch"},
            initial_route_variant="peach_9d_2027",
            initial_sent_groups=["itinerary_overview", "peach_highlights", "hotel_reference", "vehicle_reference", "accommodation_summary", "landmarks", "zhaji"],
            source_conversation_id=src(customer_type="深度规划客户"),
        ),
        case(
            "journey_11d_rongbuk_followup",
            "11 日沉默主线应覆盖绒布寺/珠峰住宿",
            "journey",
            "silence_rongbuk",
            "初步有效咨询客户",
            [{"content": "我想了解桃花加珠峰11日"}],
            {"action": "reply", "route_variant": "peach_11d_2027", "will_enroll_sop": True},
            source_conversation_id=src(scenario="明确咨询桃花+珠峰11日"),
        ),
        case(
            "journey_other_destination_guided_sop",
            "资料外线路不绑定产品，但进入支持线路导流旅程",
            "journey",
            "other_destination_no_sop",
            "资料外线路客户",
            [{"content": "你们有云南行程吗？"}],
            {"action": "reply", "route_variant": "", "lead_action": "none", "will_enroll_sop": True,
             "next_sop_behavior": "stage_driven_mandatory_touch"},
            source_conversation_id=src(customer_type="资料外线路客户"),
        ),
        case(
            "journey_contact_captured_stops",
            "留资后转人工并停止 SOP",
            "journey",
            "contact_captured_stops",
            "已留资/待顾问承接",
            [{"content": "我的微信是 wx_travel_2027"}],
            {"action": "handoff", "lead_action": "captured", "handoff_reason": "lead_captured", "will_enroll_sop": False},
            initial_route_variant="peach_9d_2027",
            source_conversation_id=src(customer_type="已留资/待顾问承接"),
        ),
        case(
            "journey_large_group_stops",
            "12 人大团转人工且不进入 SOP",
            "journey",
            "large_group_stops",
            "大团/包团高价值客户",
            [{"content": "我们12人想包团，想明年3月走桃花加珠峰"}],
            {"action": "handoff", "handoff_reason": "large_group_custom_quote", "will_enroll_sop": False},
            source_conversation_id=src(customer_type="大团/包团高价值客户"),
        ),
    ]


def build_shadow_cases(rows: list[dict]) -> list[dict]:
    cases = []
    for row in rows:
        sample = " / ".join(part.strip() for part in row.get("sample_incoming", "").split("/")[:3] if part.strip())
        if not sample:
            continue
        expected_route = ""
        scenarios = row.get("scenarios", "")
        if "明确咨询桃花+珠峰11日" in scenarios:
            expected_route = "peach_11d_2027"
        elif "明确咨询桃花9日" in scenarios:
            expected_route = "peach_9d_2027"
        expected = {"action": "reply", "route_variant": expected_route} if expected_route else {"action": "reply"}
        cases.append(
            case(
                f"shadow_real_conversation_{int(row['conversation_id']):04d}",
                f"真实会话影子回放 #{row['conversation_id']}",
                "shadow",
                "real_conversation_shadow",
                row.get("customer_type") or "未分类",
                [{"content": sample}],
                expected,
                source_conversation_id=int(row["conversation_id"]),
                mask_messages=True,
            )
        )
    return cases


def main() -> None:
    if not ANALYSIS_JSON.is_file() or not DETAIL_CSV.is_file():
        raise SystemExit("missing analytics files; run chatwoot scenario analysis first")
    analysis = json.loads(ANALYSIS_JSON.read_text(encoding="utf-8"))
    rows = read_details()
    OUT.mkdir(parents=True, exist_ok=True)
    answer_cases = build_answer_cases(rows)
    journey_cases = build_journey_cases(rows)
    shadow_cases = build_shadow_cases(rows)
    manifest = {
        "dataset_version": "v2026-09-03",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source": {
            "analysis_json": str(ANALYSIS_JSON.relative_to(ROOT)).replace("\\", "/"),
            "detail_csv": str(DETAIL_CSV.relative_to(ROOT)).replace("\\", "/"),
            "source_generated_at": analysis.get("generated_at"),
            "source_db_path": analysis.get("db_path"),
        },
        "statistics": {
            "counts": analysis.get("counts", {}),
            "customer_types": analysis.get("customer_types", {}),
            "scenarios": analysis.get("scenarios", {}),
            "question_topics": analysis.get("question_topics", {}),
            "contact_channels": analysis.get("contact_channels", {}),
            "non_reply_reasons": analysis.get("non_reply_reasons", {}),
        },
        "contact_timing_policy": [
            "先回答客户当前问题，再在购买意向明确时索取一种联系方式。",
            "支持线路、人数、出发时间，且客户询问价格、余位、报名或需要顾问核对时，可以自然索取 LINE、微信、电话或 Email。",
            "只问能不能用 LINE/微信联系不算已留资；必须请客户直接发送 ID、号码或邮箱。",
            "客户提供有效联系方式后，确认收到并转人工。",
            "资料外线路、投诉退款、合同争议、纯附件依赖视觉内容时，不进入常规留资 SOP。",
        ],
        "sop_silence_minutes": [1, 3, 5, 10, 30, 60],
        "safety": {"outbound": False, "chatwoot_write": False, "pii": "masked"},
    }
    (OUT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (OUT / "answer_cases.json").write_text(json.dumps({"cases": answer_cases}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (OUT / "journey_cases.json").write_text(json.dumps({"cases": journey_cases}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (OUT / "shadow_cases.json").write_text(json.dumps({"cases": shadow_cases}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "dataset": str(OUT),
                "answer_cases": len(answer_cases),
                "journey_cases": len(journey_cases),
                "shadow_cases": len(shadow_cases),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
