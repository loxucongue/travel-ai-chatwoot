"""Export public customer/advisor transcripts from Chatwoot."""

from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from app.chatwoot import ReadOnlyChatwootClient
from app.db import SessionLocal
from app.history_sync import fetch_message_history
from app.models import ChatwootConnection
from app.security import decrypt_secret

PAGE_SIZE = 20


def connection_config() -> tuple[str, int, str]:
    with SessionLocal() as db:
        connection = db.scalar(select(ChatwootConnection))
        if not connection or not connection.encrypted_api_token:
            raise RuntimeError("Chatwoot connection is not configured")
        return connection.base_url, int(connection.account_id), decrypt_secret(connection.encrypted_api_token)


def conversation_payload(response: object) -> tuple[list[dict], dict]:
    if isinstance(response, dict) and isinstance(response.get("data"), dict):
        response = response["data"]
    if not isinstance(response, dict):
        return [], {}
    payload = response.get("payload")
    items = [item for item in payload if isinstance(item, dict)] if isinstance(payload, list) else []
    meta = response.get("meta") if isinstance(response.get("meta"), dict) else {}
    return items, meta


def fetch_all_conversations(config: tuple[str, int, str]) -> list[dict]:
    client = ReadOnlyChatwootClient(*config)
    conversations: dict[int, dict] = {}
    try:
        for page in range(1, 1001):
            batch, meta = conversation_payload(client.list_conversations(page=page))
            if not batch:
                break
            for item in batch:
                if item.get("id") is not None:
                    conversations[int(item["id"])] = item
            all_count = meta.get("all_count")
            if isinstance(all_count, int) and len(conversations) >= all_count:
                break
            if len(batch) < PAGE_SIZE:
                break
    finally:
        client.close()
    return [conversations[key] for key in sorted(conversations)]


def epoch(value: object) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return 0.0


def sender_info(item: dict) -> tuple[str | None, str | None]:
    sender = item.get("sender") if isinstance(item.get("sender"), dict) else {}
    sender_type = str(item.get("sender_type") or sender.get("type") or "") or None
    sender_name = sender.get("name") or sender.get("available_name")
    return sender_type, str(sender_name) if sender_name else None


def public_message(item: dict) -> dict | None:
    if not item.get("id") or bool(item.get("private")):
        return None
    if (item.get("content_attributes") or {}).get("deleted"):
        return None
    message_type = item.get("message_type")
    if message_type in (0, "0", "incoming"):
        role = "customer"
    elif message_type in (1, "1", "outgoing"):
        role = "consultant"
    else:
        return None
    sender_type, sender_name = sender_info(item)
    advisor_kind = None
    if role == "consultant":
        kind = str(sender_type or "").lower()
        advisor_kind = "agent_account" if kind in {"user", "agent"} else "ai_or_bot" if kind in {"agentbot", "agent_bot", "bot"} else "unknown"
    return {
        "message_id": int(item["id"]),
        "created_at": item.get("created_at"),
        "role": role,
        "content": str(item.get("content") or ""),
        "content_type": str(item.get("content_type") or "text"),
        "has_attachments": bool(item.get("attachments")),
        "sender_type": sender_type,
        "sender_name": sender_name,
        "advisor_kind": advisor_kind,
    }


def turns(messages: list[dict]) -> list[dict]:
    result: list[dict] = []
    current: dict | None = None
    for message in messages:
        if message["role"] == "customer":
            if current is None or current["consultant_replies"]:
                current = {"customer_messages": [], "consultant_replies": []}
                result.append(current)
            current["customer_messages"].append(message)
        else:
            if current is None:
                current = {"customer_messages": [], "consultant_replies": []}
                result.append(current)
            current["consultant_replies"].append(message)
    return result


def fetch_conversation(config: tuple[str, int, str], summary: dict) -> dict:
    conversation_id = int(summary["id"])
    client = ReadOnlyChatwootClient(*config)
    try:
        raw_messages, _ = fetch_message_history(client, conversation_id)
    finally:
        client.close()
    messages = [message for raw in raw_messages if (message := public_message(raw))]
    messages.sort(key=lambda item: (epoch(item["created_at"]), item["message_id"]))
    return {
        "conversation_id": conversation_id,
        "status": summary.get("status"),
        "inbox_id": summary.get("inbox_id"),
        "channel": summary.get("channel"),
        "labels": summary.get("labels") or [],
        "message_count": len(messages),
        "customer_message_count": sum(item["role"] == "customer" for item in messages),
        "consultant_message_count": sum(item["role"] == "consultant" for item in messages),
        "messages": messages,
        "turns": turns(messages),
    }


def render_markdown(report: dict) -> str:
    lines = [
        "# Chatwoot 客户与顾问原话",
        "",
        f"- 抓取时间（UTC）：{report['fetched_at_utc']}",
        f"- 会话数：{report['conversation_count']}（列表接口发现 {report['listed_conversation_count']}）",
        f"- 公开客户消息：{report['customer_message_count']}",
        f"- 公开顾问消息：{report['consultant_message_count']}",
        "- 口径：仅保留 private=false 且 message_type 为 incoming/outgoing 的消息；活动事件、私有备注、删除消息不纳入原话。",
        "",
    ]
    for conversation in report["conversations"]:
        lines.extend([
            f"## 会话 #{conversation['conversation_id']}",
            "",
            f"状态：{conversation.get('status') or 'unknown'}；Inbox：{conversation.get('inbox_id') or 'unknown'}；渠道：{conversation.get('channel') or 'unknown'}",
            "",
        ])
        if not conversation["messages"]:
            lines.extend(["（没有公开客户或顾问文字消息）", ""])
            continue
        for message in conversation["messages"]:
            role = "客户" if message["role"] == "customer" else "顾问"
            kind = ""
            if message["role"] == "consultant":
                kind = {"agent_account": "（坐席账号，人工/自动化未核实）", "ai_or_bot": "（机器人账号）", "unknown": "（发送者未知）"}.get(message["advisor_kind"], "（发送者未知）")
            sender = f"，发送者：{message['sender_name']}" if message.get("sender_name") else ""
            content = message["content"].replace("\r\n", "\n").replace("\r", "\n")
            if not content and message["has_attachments"]:
                content = "[仅附件，无文字内容]"
            lines.extend([
                f"### {role}{kind}{sender} | {message.get('created_at') or ''} | message_id={message['message_id']}",
                "",
                content,
                "",
            ])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", help="Output directory")
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    if args.workers < 1 or args.workers > 8:
        raise SystemExit("--workers must be between 1 and 8")
    config = connection_config()
    listed = fetch_all_conversations(config)
    output = Path(args.output).resolve() if args.output else Path(__file__).resolve().parents[2] / "output" / f"chatwoot-transcripts-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    output.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []
    errors: list[dict] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(fetch_conversation, config, item): int(item["id"]) for item in listed}
        for index, future in enumerate(as_completed(futures), 1):
            conversation_id = futures[future]
            try:
                results.append(future.result())
            except Exception as exc:
                errors.append({"conversation_id": conversation_id, "error": type(exc).__name__})
            if index == 1 or index % 10 == 0 or index == len(futures):
                print(f"fetched {index}/{len(futures)} conversations", flush=True)
    results.sort(key=lambda item: item["conversation_id"])
    report = {
        "export_version": "chatwoot-public-transcripts-v1",
        "fetched_at_utc": datetime.now(timezone.utc).isoformat(),
        "account_id": config[1],
        "conversation_count": len(results),
        "listed_conversation_count": len(listed),
        "failed_conversation_count": len(errors),
        "failed_conversations": sorted(errors, key=lambda item: item["conversation_id"]),
        "customer_message_count": sum(item["customer_message_count"] for item in results),
        "consultant_message_count": sum(item["consultant_message_count"] for item in results),
        "conversations": results,
    }
    (output / "transcripts.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "transcripts.md").write_text(render_markdown(report), encoding="utf-8")
    print(json.dumps({"output": str(output), "conversation_count": len(results), "failed": errors}, ensure_ascii=False))
    return 0 if not errors else 2


if __name__ == "__main__":
    sys.exit(main())
