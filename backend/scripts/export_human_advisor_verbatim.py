"""Build auditable human-advisor verbatim exports from a frozen JSONL snapshot."""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import html
import json
import re
import sqlite3
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INPUT = ROOT / "output" / "analytics" / "human-advisor-verbatim-20260904.jsonl.gz"
DEFAULT_OUTPUT = ROOT / "output" / "analytics"
MEDIA_PLACEHOLDER = re.compile(r"^\[(?:图片|image|視頻|视频|video|文件|file|音频|audio)\]$", re.I)


PATTERNS = {
    "greeting": re.compile(r"您好|你好|哈嘍|哈喽|Hi\b|Hello\b|早安|午安|下午好|晚上好", re.I),
    "self_intro": re.compile(r"我是|我們是|我们是|國旅環球|国旅环球|China\s*2?Go", re.I),
    "question": re.compile(r"[?？]|請問|请问|方便.*(?:嗎|吗)|想了解您|不知道您"),
    "contact_request": re.compile(
        r"LINE|微信|WhatsApp|電話|电话|Email|郵箱|邮箱|聯繫方式|联系方式|換個私信|换个私信|加您", re.I
    ),
    "send_material": re.compile(r"行程圖|行程图|完整.{0,8}(?:文檔|文档|文件|行程)|電子檔|电子档|PDF|我先.{0,8}(?:發|发|傳|传)|分享"),
    "route_value": re.compile(r"桃花|珠峰|林芝|拉薩|拉萨|布達拉宮|布达拉宫|景點|景点|行程"),
    "hotel": re.compile(r"住宿|飯店|酒店|希爾頓|希尔顿|旅館|旅馆|供氧"),
    "vehicle": re.compile(r"車|车|座椅|製氧機|制氧机|氧氣罐|氧气罐"),
    "price": re.compile(r"價格|价格|報價|报价|費用|费用|¥|RMB|人民幣|人民币"),
    "low_pressure": re.compile(r"您先看|先參考|先参考|慢慢看|慢慢討論|慢慢讨论|不著急|不着急|隨時|随时|我都在|沒問題|没问题"),
    "generic_checkin": re.compile(r"看了嗎|看了吗|看的怎麼樣|看的怎么样|瞭解的怎麼樣|了解的怎么样|還有.{0,8}計劃嗎|还有.{0,8}计划吗|有需要嗎|有需要吗"),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--stamp", default="20260904")
    parser.add_argument("--source-message-count", type=int, default=7509)
    parser.add_argument("--outbound-count", type=int, default=45)
    return parser.parse_args()


def load_rows(path: Path) -> list[dict]:
    if path.suffix.lower() in {".db", ".sqlite", ".sqlite3"}:
        with sqlite3.connect(path) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute("""
                SELECT
                    c.chatwoot_conversation_id AS conversation_id,
                    m.chatwoot_message_id AS message_id,
                    m.direction,
                    m.private,
                    m.content_type,
                    m.content,
                    m.status,
                    m.sender_id,
                    m.attribution,
                    m.attachments,
                    m.content_attributes,
                    m.created_at
                FROM message_events AS m
                JOIN conversation_states AS c ON c.id = m.conversation_state_id
                WHERE m.private = 0
                  AND m.direction = 'outgoing'
                  AND m.attribution IN ('manual', 'inferred_human', 'human')
                ORDER BY m.created_at, m.id
            """)]
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def text_rows(rows: list[dict]) -> list[dict]:
    return [
        row for row in rows
        if row.get("content_type") in {"text", "input_select"}
        and str(row.get("content") or "").strip()
        and not MEDIA_PLACEHOLDER.fullmatch(str(row.get("content") or "").strip())
    ]


def is_media_record(row: dict) -> bool:
    content = str(row.get("content") or "").strip()
    attachments = row.get("attachments")
    if isinstance(attachments, str):
        try:
            attachments = json.loads(attachments)
        except json.JSONDecodeError:
            pass
    return (
        row.get("content_type") in {"image", "video", "audio", "file"}
        or bool(MEDIA_PLACEHOLDER.fullmatch(content))
        or bool(attachments)
    )


def local_time(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
    except (TypeError, ValueError):
        return value


def pct(part: int, whole: int) -> str:
    return f"{part / whole * 100:.1f}%" if whole else "0.0%"


def percentile(values: list[int], fraction: float) -> int:
    if not values:
        return 0
    return sorted(values)[round((len(values) - 1) * fraction)]


def has_emoji(value: str) -> bool:
    return bool(re.search(r"[\U0001F000-\U0001FAFF\u2600-\u27BF]|[◍⌯☺❤♡]", value))


def write_json(path: Path, rows: list[dict], source: dict) -> None:
    path.write_text(
        json.dumps({"source": source, "messages": rows}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def write_csv(path: Path, rows: list[dict]) -> None:
    fields = [
        "conversation_id", "message_id", "created_at", "local_created_at",
        "content_type", "attribution", "sender_id", "content",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({
                "conversation_id": row.get("conversation_id"),
                "message_id": row.get("message_id"),
                "created_at": row.get("created_at"),
                "local_created_at": local_time(str(row.get("created_at") or "")),
                "content_type": row.get("content_type"),
                "attribution": row.get("attribution"),
                "sender_id": row.get("sender_id"),
                "content": row.get("content"),
            })


def write_verbatim_markdown(path: Path, rows: list[dict], source: dict) -> None:
    grouped: dict[int, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[int(row["conversation_id"])].append(row)

    lines = [
        "# 真实人工顾问公开消息逐字稿",
        "",
        "> 内部资料。正文保持线上原始文字，不改写、不纠错、不合并、不去重。",
        "> `inferred_human` 表示历史消息没有匹配到本系统 AI/SOP 出站记录，因此按人工发送归因；它不是顾问姓名。",
        "",
        f"- 导出时间：{source['exported_at']}",
        f"- 来源总消息数：{source['source_message_count']}",
        f"- 人工归因的公开出站记录：{source['advisor_message_count']}",
        f"- 覆盖会话：{source['conversation_count']}",
        f"- 来源文件 SHA-256：`{source['sha256']}`",
        "",
    ]
    for conversation_id in sorted(grouped):
        lines.extend([f"## 会话 #{conversation_id}", ""])
        for row in grouped[conversation_id]:
            content = str(row.get("content") or "")
            lines.append(
                f"### {local_time(str(row.get('created_at') or ''))} · 消息 {row.get('message_id')} · {row.get('content_type')} · {row.get('attribution')}"
            )
            lines.append("")
            if content:
                lines.append(f"<pre>{html.escape(content)}</pre>")
            else:
                lines.append("*该记录没有文字；媒体和附件字段保留在 JSON 原始清单中。*")
            lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def representative_examples(texts: list[dict], pattern: re.Pattern[str], limit: int = 8) -> list[dict]:
    seen: set[str] = set()
    result: list[dict] = []
    for row in texts:
        content = str(row["content"]).strip()
        if not pattern.search(content) or content in seen:
            continue
        seen.add(content)
        result.append(row)
        if len(result) == limit:
            break
    return result


def quote_block(row: dict) -> list[str]:
    content = str(row["content"]).strip().replace("\r\n", "\n")
    return [
        f"- 会话 #{row['conversation_id']} / 消息 {row['message_id']}：",
        *[f"  > {line}" for line in content.split("\n")],
    ]


def write_summary(path: Path, rows: list[dict], source: dict) -> None:
    texts = text_rows(rows)
    lengths = [len(str(row["content"]).strip()) for row in texts]
    unique_texts = {str(row["content"]).strip() for row in texts}
    frequencies = Counter(str(row["content"]).strip() for row in texts)
    categories = {name: sum(bool(pattern.search(str(row["content"]))) for row in texts) for name, pattern in PATTERNS.items()}
    emoji_count = sum(has_emoji(str(row["content"])) for row in texts)
    tilde_count = sum("～" in str(row["content"]) or "~" in str(row["content"]) for row in texts)
    exclamation_count = sum("!" in str(row["content"]) or "！" in str(row["content"]) for row in texts)
    multi_question = sum(len(re.findall(r"[?？]", str(row["content"]))) >= 2 for row in texts)

    grouped: dict[int, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[int(row["conversation_id"])].append(row)
    first_three_media = sum(
        any(is_media_record(row) for row in messages[:3])
        for messages in grouped.values()
    )
    first_five_media = sum(
        any(is_media_record(row) for row in messages[:5])
        for messages in grouped.values()
    )
    first_ten_media = sum(
        any(is_media_record(row) for row in messages[:10])
        for messages in grouped.values()
    )
    opening_texts = []
    for messages in grouped.values():
        first = next((row for row in messages if row in texts), None)
        if first:
            opening_texts.append(str(first["content"]).strip())
    opening_frequency = Counter(opening_texts)

    lines = [
        "# 真实人工顾问表达风格总结",
        "",
        "## 数据口径",
        "",
        f"- 线上总消息：{source['source_message_count']} 条。",
        f"- 人工归因的公开出站：{len(rows)} 条，覆盖 {source['conversation_count']} 个会话。",
        f"- 有实际文字的消息：{len(texts)} 条；精确去重后 {len(unique_texts)} 条。",
        f"- 图片/视频/附件记录或占位符：{sum(is_media_record(row) for row in rows)} 条。",
        f"- 时间范围：{local_time(str(rows[0]['created_at']))} 至 {local_time(str(rows[-1]['created_at']))}。",
        "- 仅包含公开消息；已排除客户消息、AI、SOP、系统事件和私密备注。",
        f"- 归因限制：{sum(row.get('attribution') == 'inferred_human' for row in rows)} 条为 `inferred_human`，只表示没有匹配到本平台 AI/SOP 出站记录；历史数据缺少 sender_id，无法证明每条都是顾问现场手输，完全重复文案很可能包含复制粘贴或外部自动化。",
        "",
        "## 可量化特征",
        "",
        f"- 单条文字中位数 {int(statistics.median(lengths))} 字，75 分位 {percentile(lengths, .75)} 字，90 分位 {percentile(lengths, .9)} 字，最长 {max(lengths)} 字。",
        f"- {categories['question']} 条带问句或询问表达，占 {pct(categories['question'], len(texts))}；其中 {multi_question} 条含两个及以上问号。",
        f"- {categories['greeting']} 条带问候，{categories['self_intro']} 条带身份/品牌介绍。",
        f"- {categories['contact_request']} 条提到联系方式，{categories['send_material']} 条提到发资料或分享内容。",
        f"- {emoji_count} 条含 Emoji/颜文字，{tilde_count} 条使用波浪号，{exclamation_count} 条使用感叹号。",
        f"- {first_three_media}/{len(grouped)} 个会话的前三条顾问记录内出现素材；前五条为 {first_five_media}/{len(grouped)}，前十条为 {first_ten_media}/{len(grouped)}。",
        "",
        "## 风格结论",
        "",
        "1. **明显是聊天式销售，不是客服公文。** 常用“您好～”“哈嘍”“我先發您看看”“我都在”等口语，句子短，常拆成多条连续发送。",
        "2. **先快速展示产品，再推进需求或联系方式。** 常见顺序是身份介绍、行程图、桃花/珠峰亮点、酒店、车辆、纯玩承诺，然后询问人数/日期或索取 LINE、微信。",
        "3. **图片和文字成对出现。** 顾问会先说明“我先把行程圖發您”，随后发图，再用一两句解释图中卖点；不是只丢一张无说明图片。",
        "4. **强调具体、可感知的卖点。** 高频内容是小团、低海拔入藏、供氧酒店、车辆座椅、珠峰住宿、无购物、档期和价格，而不是抽象说“品质好”。",
        "5. **留资理由以继续服务为主。** 常用“发完整行程文件/报价”“方便后续联系”“安排顾问详细介绍”作为索取联系方式的理由，并不总是等人数和日期齐全。",
        "6. **沉默后有两种风格。** 较好的做法是补一个新景点、客拍或具体信息后低压力收尾；较差的做法是重复“看得怎么样”“还有计划吗”。",
        "7. **语言不完全统一。** 主体是繁体中文，但夹杂简体、台湾/大陆用词、英文品牌和不同顾问口头禅；这让语气有人味，也造成品牌口径不稳定。",
        "8. **历史原话不能原样全部进入 AI。** 部分消息存在一次倾倒过多内容、多问题连问、重复自我介绍，以及对氧气浓度、花期、余位、赔付等强承诺。应学习节奏和表达方式，事实仍以当前线路资料为准。",
        "",
        "## 代表性原话",
        "",
        "以下均为逐字原话，不是总结改写。完整内容见逐字稿。",
        "",
    ]
    selected_patterns = [
        PATTERNS["self_intro"], PATTERNS["send_material"], PATTERNS["route_value"],
        PATTERNS["hotel"], PATTERNS["contact_request"], PATTERNS["low_pressure"],
    ]
    selected: list[dict] = []
    seen_ids: set[int] = set()
    for pattern in selected_patterns:
        for row in representative_examples(texts, pattern, limit=3):
            message_id = int(row["message_id"])
            if message_id not in seen_ids:
                selected.append(row); seen_ids.add(message_id)
            if len(selected) >= 14:
                break
        if len(selected) >= 14:
            break
    for row in selected:
        lines.extend(quote_block(row)); lines.append("")

    lines.extend(["## 高频完全相同原话", ""])
    for content, count in frequencies.most_common(30):
        if count < 2:
            break
        lines.append(f"- {count} 次：`{content.replace(chr(10), ' / ')}`")

    lines.extend([
        "", "## 每个会话在本快照中的首条顾问原话", "",
        "> 快照可能从既有会话中段开始，因此这里不能等同于客户真正的首次开场。", "",
    ])
    for content, count in opening_frequency.most_common(20):
        lines.append(f"- {count} 个会话：`{content.replace(chr(10), ' / ')}`")

    lines.extend([
        "",
        "## 对 AI 话术的直接启示",
        "",
        "- 开场不要声明自己是 AI，也不要先讲系统流程。",
        "- 客户未选线路时，先用一句话讲清主要差异，紧接着给线路图或关键素材，再让客户轻松选择。",
        "- 前三轮优先完成“线路图、核心亮点、住宿/车辆”中的高价值展示；每张图都配一句解释。",
        "- 客户说时间不确定时，不再重复逼问日期；可用“发完整行程文件、之后随时联系”为理由自然索取一种联系方式。",
        "- 转人工时先完成标签和接管动作，再告知正在安排专项顾问，同时补一项当前线路价值，避免客户空等。",
        "- 沉默跟进优先发新内容或低压力提醒，不使用无新增价值的连续催问。",
    ])
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    args = parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    rows = load_rows(args.input)
    if not rows:
        raise RuntimeError("advisor export is empty")
    invalid = [
        row for row in rows
        if row.get("private") not in (0, False)
        or row.get("direction") != "outgoing"
        or row.get("attribution") not in {"manual", "inferred_human", "human"}
    ]
    if invalid:
        raise RuntimeError(f"invalid advisor rows: {len(invalid)}")

    source = {
        "exported_at": datetime.now(timezone.utc).astimezone().isoformat(),
        "source_message_count": args.source_message_count,
        "advisor_message_count": len(rows),
        "conversation_count": len({row["conversation_id"] for row in rows}),
        "outbound_count": args.outbound_count,
        "selection": "public outgoing messages attributed as manual, inferred_human, or human",
        "sha256": hashlib.sha256(args.input.read_bytes()).hexdigest(),
    }
    base = f"human-advisor-verbatim-{args.stamp}"
    write_json(args.output / f"{base}.json", rows, source)
    write_csv(args.output / f"{base}.csv", rows)
    write_verbatim_markdown(args.output / f"{base}.md", rows, source)
    write_summary(args.output / f"human-advisor-style-summary-{args.stamp}.md", rows, source)
    print(json.dumps(source, ensure_ascii=False))


if __name__ == "__main__":
    main()
