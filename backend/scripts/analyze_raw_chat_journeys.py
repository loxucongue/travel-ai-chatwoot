"""Derive a reception policy by letting DeepSeek inspect raw Chatwoot transcripts.

The transcript payload deliberately preserves message text, timestamps, roles,
attachments and ordering. It does not classify, normalize, redact, truncate, or
keyword-filter the business content. Batching is only a transport constraint.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import httpx
from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal
from app.models import Contact, ConversationState, InboxBinding, MessageEvent


RAW_CHAT_BATCH_VERSION = "raw-chat-batch-analyzer-v3"
RAW_CHAT_SYNTHESIS_VERSION = "raw-chat-policy-synthesizer-v3"
ANALYSIS_VERSION = RAW_CHAT_SYNTHESIS_VERSION
SYSTEM_PROMPT = """你是資深旅遊私域銷售運營分析師。你的輸入是按時間順序排列的Chatwoot原始會話，內容未經主題分類、關鍵詞篩選、話術改寫或人工標註。請直接閱讀原始對話本身，不要使用關鍵詞計數代替語義判斷。

目標是為“AI實時回覆 + 客戶沉默時繼續線路主線 + 長時間沉默後有限喚醒”的統一接待旅程提供證據。必須區分：
1. observed：歷史顧問真實採用的做法；
2. response_observed_patterns：顧問表達後觀察到客戶繼續回覆、明確需求或提供聯繫方式的做法；這隻表示後續回應，不能聲稱因果；
3. no_response_observed_patterns：顧問表達後未觀察到客戶在下一輪繼續回覆的做法；
4. harmful_patterns：刷屏、重複、過早留資、連續多問、無回應仍密集發送等不應複製的做法；
5. recommended：適合自動化生產的保守建議，不能把歷史做法未經判斷直接固化。

分析回覆風格、繁簡體、稱呼、長度、單輪消息條數、圖片與文字組合、追問數量、回答後如何推進、線路切換、多人團與高意向轉人工信號。回覆速度、沉默間隔、連續觸達數量和客戶是否回應只允許讀取代碼提供的computed_metrics，不得自行用時間戳計算。引用具體conversation_id和message_id作為證據。

只輸出JSON對象，不要Markdown。不得複述個人聯繫方式或其他敏感值；證據只使用會話ID、消息ID、時間和不包含聯繫方式的短句。"""

AGGREGATE_PROMPT = """你要彙總多批原始會話分析，生成一份候選線路接待策略建議。批次結論仍然只是證據，不得用多數投票掩蓋風險。優先採用有客戶後續回應、明確需求或留資證據支持的模式，拒絕複製刷屏、重複追問和密集觸達。

輸出JSON，必須包含：analysis_version、source_summary、observed_vs_recommended、style_policy、reply_limits、silence_policy、wakeup_policy、route_switch_policy、handoff_policy、stop_conditions、safety_caps、evidence、uncertainties。所有時間均使用分鐘；reply_limits給出字符上限、單輪消息條數上限、問句上限；silence_policy和wakeup_policy給出明確延遲與最多次數；handoff_policy包含可配置的大團人數閾值但不要憑空宣稱歷史閾值。每條建議說明是raw_evidence、safety_default還是business_config。只輸出候選建議JSON，不得聲稱已經發布或可跳過人工審核、離線評測與演練驗證。"""

BATCH_SCHEMA = {
    "observed_style": [],
    "observed_timing": [],
    "response_observed_patterns": [],
    "no_response_observed_patterns": [],
    "harmful_patterns": [],
    "route_switch_patterns": [],
    "handoff_patterns": [],
    "recommended_limits": {
        "reply_max_characters": None,
        "reply_max_messages_per_turn": None,
        "reply_max_questions": None,
        "silence_followup_delays_minutes": [],
        "max_silence_touches": None,
        "minimum_touch_interval_minutes": None,
        "stop_after_silence_minutes": None,
    },
    "evidence": [],
    "uncertainties": [],
}


def _seconds(value: object) -> float | None:
    if value in (None, ""):
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _conversation_metrics(messages: list[MessageEvent]) -> dict:
    response_delays: list[int] = []
    proactive_gaps: list[int] = []
    consecutive_outgoing = 0
    max_consecutive_outgoing = 0
    outgoing_touch_count = 0
    customer_replied_after_outgoing = False
    previous_direction = ""
    previous_at: float | None = None
    latest_incoming_at: float | None = None
    for row in messages:
        at = _seconds(row.created_at)
        if row.direction == "incoming":
            if previous_direction == "outgoing":
                customer_replied_after_outgoing = True
            latest_incoming_at = at
            consecutive_outgoing = 0
        elif row.direction == "outgoing":
            outgoing_touch_count += 1
            if latest_incoming_at is not None and previous_direction == "incoming" and at is not None:
                response_delays.append(max(0, round(at - latest_incoming_at)))
            if previous_direction == "outgoing" and previous_at is not None and at is not None:
                proactive_gaps.append(max(0, round((at - previous_at) / 60)))
                consecutive_outgoing += 1
            else:
                consecutive_outgoing = 1
            max_consecutive_outgoing = max(max_consecutive_outgoing, consecutive_outgoing)
        previous_direction = row.direction
        previous_at = at
    return {
        "advisor_response_delay_seconds": response_delays,
        "consecutive_outgoing_gaps_minutes": proactive_gaps,
        "outgoing_message_count": outgoing_touch_count,
        "max_consecutive_outgoing_messages": max_consecutive_outgoing,
        "customer_replied_after_an_outgoing_message": customer_replied_after_outgoing,
    }


def _require_shape(value: dict, shape: dict, label: str) -> dict:
    if not isinstance(value, dict):
        raise RuntimeError(f"{label}_not_object")
    missing = [key for key in shape if key not in value]
    if missing:
        raise RuntimeError(f"{label}_missing_fields:{','.join(missing)}")
    for key, expected in shape.items():
        actual = value[key]
        if isinstance(expected, list) and not isinstance(actual, list):
            raise RuntimeError(f"{label}_invalid_list:{key}")
        if isinstance(expected, dict):
            _require_shape(actual, expected, f"{label}.{key}")
    return value


def _raw_conversations() -> list[dict]:
    with SessionLocal() as db:
        states = list(db.scalars(select(ConversationState).order_by(ConversationState.id)).all())
        inboxes = {row.id: row for row in db.scalars(select(InboxBinding)).all()}
        contacts = {row.id: row for row in db.scalars(select(Contact)).all()}
        grouped: dict[int, list[MessageEvent]] = defaultdict(list)
        for row in db.scalars(
            select(MessageEvent)
            .where(
                MessageEvent.private.is_(False),
                MessageEvent.direction.in_(("incoming", "outgoing")),
            )
            .order_by(MessageEvent.conversation_state_id, MessageEvent.created_at, MessageEvent.id)
        ).all():
            grouped[row.conversation_state_id].append(row)

        conversations: list[dict] = []
        for state in states:
            messages = grouped.get(state.id, [])
            if not messages or not any(row.direction == "incoming" for row in messages):
                continue
            inbox = inboxes.get(state.inbox_binding_id)
            contact = contacts.get(state.contact_id) if state.contact_id else None
            conversations.append({
                "conversation_state_id": state.id,
                "chatwoot_conversation_id": state.chatwoot_conversation_id,
                "inbox": {
                    "chatwoot_inbox_id": inbox.chatwoot_inbox_id if inbox else None,
                    "name": inbox.name if inbox else None,
                    "channel_type": inbox.channel_type if inbox else None,
                },
                "contact": {
                    "chatwoot_contact_id": contact.chatwoot_contact_id if contact else None,
                    "name": contact.name if contact else None,
                },
                "conversation_status": state.status,
                "computed_metrics": _conversation_metrics(messages),
                "messages": [{
                    "local_message_id": row.id,
                    "chatwoot_message_id": row.chatwoot_message_id,
                    "created_at": row.created_at,
                    "direction": row.direction,
                    "attribution": row.attribution,
                    "sender_id": row.sender_id,
                    "content_type": row.content_type,
                    "content": row.content,
                    "attachments": row.attachments,
                    "content_attributes": row.content_attributes,
                    "status": row.status,
                } for row in messages],
            })
        return conversations


def _batches(conversations: list[dict], max_characters: int) -> list[list[dict]]:
    batches: list[list[dict]] = []
    current: list[dict] = []
    size = 0
    for conversation in conversations:
        encoded = json.dumps(conversation, ensure_ascii=False, separators=(",", ":"))
        if current and size + len(encoded) > max_characters:
            batches.append(current)
            current, size = [], 0
        current.append(conversation)
        size += len(encoded)
    if current:
        batches.append(current)
    return batches


def _call(messages: list[dict], *, max_tokens: int = 10000) -> dict:
    last_error: Exception | None = None
    for _attempt in range(3):
        response = httpx.post(
            f"{settings.deepseek_base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {settings.deepseek_api_key}"},
            json={
                "model": settings.deepseek_model,
                "messages": messages,
                "response_format": {"type": "json_object"},
                "thinking": {"type": "disabled"},
                "temperature": 0.0,
                "max_tokens": max_tokens,
            },
            timeout=max(90, settings.deepseek_timeout_seconds),
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"].strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        try:
            return json.loads(content)
        except json.JSONDecodeError as exc:
            last_error = exc
    raise RuntimeError("deepseek_analysis_invalid_json") from last_error


def _analyze_batch(batch: list[dict], index: int, total: int) -> dict:
    payload = {
        "analysis_version": RAW_CHAT_BATCH_VERSION,
        "batch": {"index": index, "total": total},
        "required_output_shape": BATCH_SCHEMA,
        "raw_conversations": batch,
    }
    return _require_shape(_call([
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]), BATCH_SCHEMA, "raw_chat_batch")


def _aggregate(batch_results: list[dict], source_summary: dict) -> dict:
    return _call([
        {"role": "system", "content": AGGREGATE_PROMPT},
        {"role": "user", "content": json.dumps({
            "source_summary": source_summary,
            "batch_analyses": batch_results,
        }, ensure_ascii=False)},
    ], max_tokens=7000)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="../output/raw-chat-journey-analysis")
    parser.add_argument("--batch-characters", type=int, default=180000)
    args = parser.parse_args()
    if not settings.deepseek_api_key:
        raise SystemExit("DEEPSEEK_API_KEY is required")

    conversations = _raw_conversations()
    batches = _batches(conversations, args.batch_characters)
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    raw_path = output / "raw-conversations.json"
    raw_text = json.dumps(conversations, ensure_ascii=False, indent=2)
    raw_path.write_text(raw_text, encoding="utf-8")

    results: list[dict] = []
    for index, batch in enumerate(batches, 1):
        batch_path = output / f"batch-{index:03d}.json"
        if batch_path.exists():
            result = json.loads(batch_path.read_text(encoding="utf-8"))
            try:
                results.append(_require_shape(result, BATCH_SCHEMA, "raw_chat_batch"))
                print(f"reused batch {index}/{len(batches)}")
                continue
            except RuntimeError:
                pass
        result = _analyze_batch(batch, index, len(batches))
        results.append(result)
        batch_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"analyzed batch {index}/{len(batches)}")

    source_summary = {
        "conversation_count": len(conversations),
        "message_count": sum(len(row["messages"]) for row in conversations),
        "batch_count": len(batches),
        "raw_sha256": hashlib.sha256(raw_text.encode("utf-8")).hexdigest(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "transformation": "raw public messages preserved; timing/count metrics added deterministically; transport batching only",
    }
    policy = _aggregate(results, source_summary)
    policy["analysis_version"] = ANALYSIS_VERSION
    policy["source_summary"] = source_summary
    (output / "journey-policy.json").write_text(
        json.dumps(policy, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output / "manifest.json").write_text(json.dumps({
        **source_summary,
        "model": settings.deepseek_model,
        "files": [raw_path.name, *[f"batch-{i:03d}.json" for i in range(1, len(batches) + 1)], "journey-policy.json"],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(source_summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
