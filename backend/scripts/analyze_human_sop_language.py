"""Analyze human silence follow-ups from the frozen Chatwoot mirror.

This is an analytics-only script. It never imports outbound services and never
writes Chatwoot. The output keeps conversation/message ids for auditability but
redacts contact-like values from text snippets.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import defaultdict
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = ROOT / "output" / "analytics" / "prod-chatwoot-analysis-20260903-092510.db"
DEFAULT_OUTPUT = ROOT / "output" / "analytics" / "human-sop-language-analysis-20260903.json"

TACTICS = {
    "generic_checkin": (
        "看了嗎", "看过吗", "看得怎", "行程您看", "行程還喜歡", "有需要協助",
        "有看到嗎", "收到訊息", "收到信息", "已讀未回", "未讀的狀態",
    ),
    "specific_value": (
        "桃花", "珠峰", "帕邦喀", "秀巴", "布達拉宮", "住宿", "酒店", "車",
        "高反", "入藏", "證件", "手續", "團期", "報價", "價格", "花期",
    ),
    "need_question": (
        "幾位", "几位", "人數", "人数", "同行", "幾月", "几月", "月份",
        "什麼時候", "什么时候", "出發時間", "出发时间", "假期安排",
    ),
    "objection_probe": (
        "擔心", "担心", "顧慮", "顾虑", "卡在", "不清楚", "高反有",
        "哪個問題", "哪个问题", "對行程不清楚", "对行程不清楚",
    ),
    "low_pressure": (
        "不用急", "慢慢", "參考", "参考", "方便您後續", "方便您后续",
        "有想法隨時", "有想法随时", "跟家人", "和家人", "討論", "讨论",
    ),
    "contact_request": (
        "LINE", "Line", "line", "微信", "WeChat", "電話", "电话", "WhatsApp",
        "聯絡方式", "联系方式", "加您",
    ),
    "social_proof": (
        "客拍", "團上客人", "团上客人", "出團照", "出团照", "去年桃花",
        "今日份", "今天的",
    ),
    "scarcity": (
        "旺季", "緊張", "紧张", "早報名", "早报名", "名額", "名额", "餘位",
        "余位", "保證出發", "保证出发",
    ),
}

CONTACT_PATTERNS = (
    re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}"),
    re.compile(r"(?<!\d)(?:\+?\d[\d\s().-]{6,}\d)(?!\d)"),
    re.compile(r"(?i)(LINE|微信|WeChat|WhatsApp)\s*(?:ID|帳號|账号)?\s*[:：]?\s*[A-Za-z0-9_.-]{4,}"),
)


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def redact(text: str) -> str:
    value = re.sub(r"\s+", " ", text or "").strip()
    for pattern in CONTACT_PATTERNS:
        value = pattern.sub("[已脱敏联系方式]", value)
    return value


def tag_text(text: str) -> list[str]:
    return [name for name, terms in TACTICS.items() if any(term in text for term in terms)]


def build_bursts(messages: list[dict], burst_gap_minutes: int) -> list[dict]:
    bursts: list[dict] = []
    for message in messages:
        timestamp = parse_time(message["created_at"])
        if (
            not bursts
            or bursts[-1]["direction"] != message["direction"]
            or (timestamp - bursts[-1]["end"]).total_seconds() > burst_gap_minutes * 60
        ):
            bursts.append({
                "direction": message["direction"],
                "start": timestamp,
                "end": timestamp,
                "messages": [message],
            })
        else:
            bursts[-1]["end"] = timestamp
            bursts[-1]["messages"].append(message)
    return bursts


def analyze(db_path: Path, min_silence_minutes: int = 30) -> dict:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    rows = connection.execute(
        """
        SELECT cs.chatwoot_conversation_id AS conversation_id,
               m.chatwoot_message_id AS message_id,
               m.direction, m.attribution, m.content_type, m.content, m.created_at
          FROM message_events m
          JOIN conversation_states cs ON cs.id = m.conversation_state_id
         WHERE m.private = 0
           AND (
             (m.direction = 'incoming' AND m.attribution = 'customer')
             OR
             (m.direction = 'outgoing' AND m.attribution IN ('inferred_human', 'manual'))
           )
         ORDER BY cs.chatwoot_conversation_id, m.created_at, m.id
        """
    ).fetchall()
    grouped: dict[int, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[int(row["conversation_id"])].append(dict(row))

    followups: list[dict] = []
    for conversation_id, messages in grouped.items():
        bursts = build_bursts(messages, burst_gap_minutes=5)
        for index, burst in enumerate(bursts):
            if index == 0 or burst["direction"] != "outgoing":
                continue
            previous = bursts[index - 1]
            if previous["direction"] != "outgoing":
                continue
            silence_minutes = (burst["start"] - previous["end"]).total_seconds() / 60
            if silence_minutes < min_silence_minutes:
                continue

            next_customer = None
            for candidate in bursts[index + 1:]:
                if candidate["direction"] == "incoming":
                    next_customer = candidate
                    break
                if candidate["direction"] == "outgoing":
                    break

            text_parts = [
                item["content"].strip()
                for item in burst["messages"]
                if item["content"].strip() and item["content"].strip() != "[图片]"
            ]
            text = " ".join(text_parts)
            response_text = ""
            response_minutes = None
            if next_customer:
                response_text = " ".join(
                    item["content"].strip()
                    for item in next_customer["messages"]
                    if item["content"].strip() and item["content"].strip() != "[图片]"
                )
                response_minutes = (next_customer["start"] - burst["end"]).total_seconds() / 60

            followups.append({
                "conversation_id": conversation_id,
                "first_message_id": burst["messages"][0]["message_id"],
                "silence_minutes": round(silence_minutes, 1),
                "text": redact(text),
                "image_count": sum(item["content_type"] == "image" for item in burst["messages"]),
                "tactics": tag_text(text),
                "customer_responded_before_next_followup": next_customer is not None,
                "response_minutes": round(response_minutes, 1) if response_minutes is not None else None,
                "response_text": redact(response_text),
            })

    tactic_stats = {}
    for tactic in TACTICS:
        matches = [item for item in followups if tactic in item["tactics"]]
        responded = sum(item["customer_responded_before_next_followup"] for item in matches)
        tactic_stats[tactic] = {
            "followups": len(matches),
            "responded": responded,
            "observed_response_rate": round(responded / len(matches), 4) if matches else 0,
        }

    responded_examples = sorted(
        (item for item in followups if item["customer_responded_before_next_followup"] and item["text"]),
        key=lambda item: (item["response_minutes"] is None, item["response_minutes"] or 10**9),
    )
    generic_failures = [
        item for item in followups
        if "generic_checkin" in item["tactics"] and not item["customer_responded_before_next_followup"]
    ]
    return {
        "generated_at": datetime.now().astimezone().isoformat(),
        "source_db": str(db_path),
        "method": {
            "burst_gap_minutes": 5,
            "minimum_silence_minutes": min_silence_minutes,
            "response_definition": "customer incoming before the next human outbound burst",
            "limitations": [
                "Observed correlations are not causal A/B-test results.",
                "Multiple tactics can appear in one follow-up.",
                "Historical claims are not treated as approved product facts.",
            ],
        },
        "summary": {
            "conversation_count": len(grouped),
            "silence_followup_count": len(followups),
            "silence_followup_conversations": len({item["conversation_id"] for item in followups}),
            "responded_count": sum(item["customer_responded_before_next_followup"] for item in followups),
        },
        "tactic_stats": tactic_stats,
        "effective_examples": responded_examples[:20],
        "generic_nonresponse_examples": generic_failures[:10],
        "design_findings": [
            "Do not use a generic read-check as the default touch; add one customer-relevant value first.",
            "Use one low-friction question tied to the current profile: party, date, concern, or decision state.",
            "When the customer is considering with family, send a forwardable summary and reduce pressure.",
            "Contact requests belong after answering a substantive question and confirming buying signals.",
            "Use photos as proof or context, not as an uncaptioned batch.",
            "Never copy unverified historical guarantees, medical claims, availability, or scarcity claims.",
        ],
    }


def markdown(report: dict) -> str:
    summary = report["summary"]
    lines = [
        "# 真实人工沉默跟进话术分析",
        "",
        f"- 会话：{summary['conversation_count']}",
        f"- 沉默后人工跟进轮次：{summary['silence_followup_count']}",
        f"- 涉及会话：{summary['silence_followup_conversations']}",
        f"- 在下一次人工跟进前获得客户回复：{summary['responded_count']}",
        "",
        "> 回复率只代表历史相关性，不代表因果；同一轮可能同时使用多个策略。",
        "",
        "## 分类型观察",
        "",
        "| 类型 | 跟进轮次 | 获得回复 | 观察回复率 |",
        "|---|---:|---:|---:|",
    ]
    names = {
        "generic_checkin": "纯催问/确认收到",
        "specific_value": "补充具体价值",
        "need_question": "询问人数/日期",
        "objection_probe": "探查顾虑",
        "low_pressure": "低压力表达",
        "contact_request": "索取联系方式",
        "social_proof": "实拍/社会证明",
        "scarcity": "资源紧迫",
    }
    for key, item in report["tactic_stats"].items():
        lines.append(
            f"| {names[key]} | {item['followups']} | {item['responded']} | "
            f"{item['observed_response_rate']:.1%} |"
        )
    lines += [
        "",
        "## 可产品化结论",
        "",
        "1. 每次沉默触达先补一个和客户档案有关的新价值，再问一个低压力问题。",
        "2. 禁止连续使用“看了吗／满意吗／有需要吗”这类没有新增价值的催问。",
        "3. 客户说在与家人讨论时，改发便于转发的短总结，不马上追联系方式。",
        "4. 客户已明确线路、人数、时间并询价/住宿/余位后，先回答，再自然索取一种联系方式。",
        "5. 图片必须有对应说明和触达目的；不能一次倾倒大量图片。",
        "6. 历史话术里的供氧浓度、保证花期、保证余位、绝对赔付等内容不能自动复用。",
        "",
        "## 获得回复的脱敏样本",
        "",
    ]
    for item in report["effective_examples"][:12]:
        lines += [
            f"### 会话 #{item['conversation_id']}（沉默 {item['silence_minutes']:.0f} 分钟后）",
            "",
            f"- 人工：{item['text'][:500]}",
            f"- 客户：{item['response_text'][:300] or '[图片/附件回复]'}",
            f"- 客户响应：{item['response_minutes']:.1f} 分钟",
            "",
        ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--min-silence-minutes", type=int, default=30)
    args = parser.parse_args()
    report = analyze(args.db.resolve(), args.min_silence_minutes)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    args.output.with_suffix(".md").write_text(markdown(report), encoding="utf-8")
    print(json.dumps(report["summary"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
