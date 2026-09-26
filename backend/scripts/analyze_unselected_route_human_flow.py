"""Analyze how human advisors handle customers who have not chosen a route.

This script reads a frozen production database, sends public human/customer
transcripts to DeepSeek for semantic analysis, and never imports outbound code.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import httpx

from app.config import settings


UNSELECTED_BATCH_VERSION = "unselected-route-batch-analyzer-v3"
UNSELECTED_SYNTHESIS_VERSION = "unselected-route-flow-synthesizer-v3"
SYSTEM_PROMPT = """你是旅遊私域銷售質檢負責人。輸入是線上 Chatwoot 的真實公開對話，按時間順序保留客戶與真人顧問原文。
本任務只研究兩個問題：
1. 客戶尚未明確選擇具體線路時，真人顧問如何繼續跟進、介紹差異、詢問需求，以及哪些表達獲得客戶後續回覆；
2. 真人顧問在開場、線路區別、行程亮點、住宿、車輛、價格、證件手續、高原適應、團期、留資和低壓力收尾等階段實際如何表達。

必須按語義判斷客戶在顧問發言當時是否已經明確選擇線路，不能用關鍵詞計數代替理解。不要把客戶僅僅提到某個景點視為已經選線。不要把 AI、系統、機器人消息當作真人樣本。
保留可審計證據：conversation_id、message_ids、時間和不含聯繫方式的短原文。不得輸出客戶姓名、電話、郵箱、LINE、微信或其他個人資料。
區分 observed、response_observed（該輪後客戶在下一次顧問發言前回復）、no_response_observed（下一次顧問發言前未回覆）。後兩者只描述觀察結果，不代表因果。回覆間隔、連續消息數量和是否回覆只允許讀取代碼提供的 computed_metrics，不得自行計算。網址資料是產品事實來源；歷史人工對話只用於學習表達與推進方式。
只輸出 JSON。"""

SUMMARY_PROMPT_SUFFIX = """你的輸入是多個“未選線路真人話術分批分析結果”，不是原始聊天。只彙總批次中已有的證據，不重新解釋不存在的原始消息。刪除重複證據，不用多數投票掩蓋未回應案例。推薦話術必須像真人對話，避免說明書腔；每次只推進一件事。為避免輸出截斷：每個stage_language最多保留3條，response_observed_patterns和no_response_observed_patterns各最多8條，recommended_unselected_flow最多6步，evidence最多20條；advisor_expression只保留最有代表性的短句，不重複粘貼整段聊天。輸出僅是候選分析，必須經過人工審核、離線評測和演練後才能進入生產配置。"""

OUTPUT_SHAPE = {
    "unselected_route_followups": [],
    "stage_language": {
        "opening": [], "route_comparison": [], "needs_discovery": [],
        "itinerary_highlights": [], "accommodation": [], "vehicle": [],
        "price": [], "documents": [], "altitude": [], "departure": [],
        "contact_request": [], "low_pressure_close": [],
    },
    "response_observed_patterns": [],
    "no_response_observed_patterns": [],
    "recommended_unselected_flow": [],
    "evidence": [],
}


def _timestamp(value: object) -> float | None:
    if value in (None, ""):
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def conversation_metrics(messages: list[dict]) -> dict:
    advisor_delays: list[int] = []
    advisor_gaps: list[int] = []
    response_observed = 0
    no_response_observed = 0
    latest_customer_at: float | None = None
    pending_advisor_turn = False
    previous_advisor_at: float | None = None
    for item in messages:
        at = _timestamp(item.get("created_at"))
        if item["role"] == "customer":
            if pending_advisor_turn:
                response_observed += 1
                pending_advisor_turn = False
            latest_customer_at = at
            previous_advisor_at = None
            continue
        if latest_customer_at is not None and previous_advisor_at is None and at is not None:
            advisor_delays.append(max(0, round(at - latest_customer_at)))
        if previous_advisor_at is not None and at is not None:
            advisor_gaps.append(max(0, round((at - previous_advisor_at) / 60)))
        if pending_advisor_turn:
            no_response_observed += 1
        pending_advisor_turn = True
        previous_advisor_at = at
    if pending_advisor_turn:
        no_response_observed += 1
    return {
        "advisor_response_delay_seconds": advisor_delays,
        "consecutive_advisor_gaps_minutes": advisor_gaps,
        "response_observed_turns": response_observed,
        "no_response_observed_turns": no_response_observed,
    }


def require_shape(value: dict, shape: dict, label: str) -> dict:
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
            require_shape(actual, expected, f"{label}.{key}")
    return value


def load_conversations(db_path: Path) -> list[dict]:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    rows = connection.execute(
        """
        SELECT cs.chatwoot_conversation_id AS conversation_id,
               m.chatwoot_message_id AS message_id, m.direction, m.attribution,
               m.content_type, m.content, m.created_at
          FROM message_events m
          JOIN conversation_states cs ON cs.id = m.conversation_state_id
         WHERE m.private = 0
           AND ((m.direction = 'incoming' AND m.attribution = 'customer')
             OR (m.direction = 'outgoing' AND m.attribution IN ('manual', 'inferred_human')))
         ORDER BY cs.chatwoot_conversation_id, m.created_at, m.id
        """
    ).fetchall()
    grouped: dict[int, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[int(row["conversation_id"])].append({
            "message_id": row["message_id"],
            "created_at": row["created_at"],
            "role": "customer" if row["direction"] == "incoming" else "human_advisor",
            "content_type": row["content_type"],
            "content": row["content"],
        })
    return [
        {"conversation_id": key, "computed_metrics": conversation_metrics(value), "messages": value}
        for key, value in grouped.items()
        if any(item["role"] == "customer" for item in value)
        and any(item["role"] == "human_advisor" for item in value)
    ]


def batches(conversations: list[dict], limit: int = 80_000) -> list[list[dict]]:
    result: list[list[dict]] = []
    current: list[dict] = []
    size = 0
    for conversation in conversations:
        encoded = json.dumps(conversation, ensure_ascii=False)
        if current and size + len(encoded) > limit:
            result.append(current)
            current, size = [], 0
        current.append(conversation)
        size += len(encoded)
    if current:
        result.append(current)
    return result


def call(messages: list[dict], max_tokens: int) -> dict:
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
            timeout=max(120, settings.deepseek_timeout_seconds),
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"].strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        try:
            return json.loads(content)
        except json.JSONDecodeError as exc:
            last_error = exc
    raise RuntimeError("deepseek_unselected_analysis_invalid_json") from last_error


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not settings.deepseek_api_key:
        raise SystemExit("DEEPSEEK_API_KEY is required")

    conversations = load_conversations(args.db.resolve())
    grouped = batches(conversations)
    args.output.mkdir(parents=True, exist_ok=True)
    results = []
    for index, batch in enumerate(grouped, 1):
        path = args.output / f"batch-{index:03d}.json"
        if path.exists():
            cached = json.loads(path.read_text(encoding="utf-8"))
            try:
                results.append(require_shape(cached, OUTPUT_SHAPE, "unselected_route_batch"))
                continue
            except RuntimeError:
                pass
        result = require_shape(call([
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps({
                "batch": {"index": index, "total": len(grouped)},
                "analysis_version": UNSELECTED_BATCH_VERSION,
                "required_shape": OUTPUT_SHAPE,
                "conversations": batch,
            }, ensure_ascii=False)},
        ], 9000), OUTPUT_SHAPE, "unselected_route_batch")
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        results.append(result)
        print(f"analyzed {index}/{len(grouped)}", flush=True)

    summary = require_shape(call([
        {"role": "system", "content": SUMMARY_PROMPT_SUFFIX},
        {"role": "user", "content": json.dumps({
            "required_shape": OUTPUT_SHAPE,
            "batch_results": results,
        }, ensure_ascii=False)},
    ], 8000), OUTPUT_SHAPE, "unselected_route_summary")
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "analysis_version": UNSELECTED_SYNTHESIS_VERSION,
        "source": {
            "database": str(args.db.resolve()),
            "conversation_count": len(conversations),
            "batch_count": len(grouped),
            "transformation": "public customer and human-advisor messages only; original order and wording preserved",
        },
        "analysis": summary,
    }
    (args.output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report["source"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
